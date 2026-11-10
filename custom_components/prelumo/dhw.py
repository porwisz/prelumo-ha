"""Hot water boosts as flexible loads: learning from the recorder, the loads for the planner,
what the plan says about them, and starting them.

With the hot water switch off Prelumo only watches: the boosts run when the user's own
automations start them (``usual`` hour) and the best hour and its saving are reported. With it on
Prelumo sets the tank's target temperature at the hour the plan chose and the temperature to keep
afterwards when the tank is hot or the window ends."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import CONF_DHW_CLIMATE, CONF_DHW_TEMP, CONF_HP_POWER, OPT_DHW_BOOSTS, OPTION_DEFAULTS
from .core import dhw
from .core.flex import FlexLoad, Placement, window_starts
from .ha_io import inputs as io

if TYPE_CHECKING:
    from .coordinator import PrelumoCoordinator

_LOGGER = logging.getLogger(__name__)

HISTORY = timedelta(days=10)  # raw recorder history
NEAR = timedelta(hours=3)  # a boost this close starts from the temperature the tank has now
DONE_MARGIN = 1.0  # deg C below the target: the boost is done (not planned again that day)
REACHED = 0.3  # deg C below the target: a running boost is ended
KEEP_DONE = timedelta(days=3)


class DhwManager:
    def __init__(self, coord: PrelumoCoordinator) -> None:
        self.coord = coord
        self.events: list[dict] = []  # climbs of the tank temperature (core.dhw.heating_events)
        self.done: dict[str, str] = {}  # boost key -> day (ISO) whose window is already heated
        self.choice: dict[str, str] = {}  # boost key -> start (ISO hour) the last plan chose
        self.usual: dict[str, int] = {}  # boost key -> clock hour it usually starts in
        self.profile: dict[int, float] = {}  # kWh per clock hour of the reheats that are no boost
        self.plan: list[dict[str, Any]] = []  # the last plan, for the sensor
        self.running: dict[str, dict[str, str]] = {}  # boost key -> {start, day} of a boost Prelumo started
        self.last_action: str | None = None
        self._windows: dict[str, date] = {}  # boost key -> day of the window the plan is about

    # --- configuration ---------------------------------------------------
    @property
    def climate(self) -> str | None:
        eid = self.coord.conf.get(CONF_DHW_CLIMATE)
        return eid if eid and self.coord.hass.states.get(eid) is not None else None

    @property
    def configured(self) -> bool:
        return self.climate is not None and bool(self.boosts())

    def boosts(self) -> list[dhw.DhwBoost]:
        text = self.coord.opt.get(OPT_DHW_BOOSTS, OPTION_DEFAULTS[OPT_DHW_BOOSTS])
        try:
            return dhw.parse_boosts(text)
        except dhw.DhwError as err:
            _LOGGER.warning("Hot water boosts not readable (%s): %s", text, err)
            return []

    def temperature(self) -> float | None:
        """Tank temperature: the sensor, else the climate entity's current temperature."""
        hass = self.coord.hass
        v = io.state_float(hass, self.coord.conf.get(CONF_DHW_TEMP))
        if v is None and self.climate:
            try:
                v = float(hass.states.get(self.climate).attributes.get("current_temperature"))
            except (TypeError, ValueError):
                v = None
        return v

    @property
    def runs(self) -> list[dict]:
        """The climbs that were heating runs."""
        return dhw.heated(self.events)

    @property
    def per_degree(self) -> float:
        return dhw.kwh_per_degree(self.events) or dhw.DEFAULT_KWH_PER_DEG

    @property
    def power_kw(self) -> float:
        return dhw.power_kw(self.events) or dhw.DEFAULT_POWER_KW

    def controlled(self) -> bool:
        """Does Prelumo start the boosts itself?"""
        return bool(self.coord.dhw_control) and self.configured

    # --- learning ----------------------------------------------------------
    async def learn(self, now: datetime) -> None:
        """New heating runs from the recorder; the usual hours and the rest-of-day profile."""
        c = self.coord
        temp_e, power_e = c.conf.get(CONF_DHW_TEMP), c.conf.get(CONF_HP_POWER)
        if not self.configured or not temp_e or not power_e:
            return
        start = now - HISTORY
        if self.events:
            start = max(start, datetime.fromisoformat(self.events[-1]["end"]))
        try:
            temp = await io.soc_history(c.hass, temp_e, start, now)
            power = await io.power_history(c.hass, power_e, start, now)
        except Exception as err:  # recorder may not be ready
            _LOGGER.debug("Hot water history lookup failed: %s", err)
            return
        self.events = dhw.merge_events(self.events, dhw.heating_events(temp, power), now)
        boosts = self.boosts()
        if not self.controlled():  # once Prelumo starts them, "usual" stays what it was before
            for b in boosts:
                hour = dhw.usual_start(self.runs, b)
                if hour is not None:
                    self.usual[b.key] = hour
        self.profile = dhw.residual_profile(self.runs, boosts, now)
        _LOGGER.debug("Hot water: %d runs, %.3f kWh/deg, %.1f kW, usual %s",
                      len(self.events), self.per_degree, self.power_kw, self.usual)

    def heating_only(self, hp: dict[datetime, float], now: datetime) -> dict[datetime, float]:
        """The heat pump's hourly energy without the hot water runs (for the heating model).

        With a week of runs on record only that period is used: older hours still have the hot
        water in them."""
        if not self.runs:
            return hp
        runs = dhw.hourly_kwh(self.runs)
        out = {h: max(0.0, v - runs.get(h, 0.0)) for h, v in hp.items()}
        first = datetime.fromisoformat(self.runs[0]["start"])
        if now - first >= timedelta(days=7):
            out = {h: v for h, v in out.items() if h >= first}
        return out

    def rest_of_day(self, hours: list[datetime]) -> list[float]:
        """Forecast (kWh per hour) of the reheats that are not a boost."""
        return [self.profile.get(h.hour, 0.0) for h in hours]

    # --- planning ----------------------------------------------------------
    def loads(self, now: datetime, hours: list[datetime]) -> list[FlexLoad]:
        """One flexible load per boost whose next window is in the horizon."""
        self._windows = {}
        if not self.configured:
            return []
        tariff = self.coord.tariff()
        temp = self.temperature()
        out = []
        for b in self.boosts():
            day_ok = tariff.peak_day if b.peak_only else (lambda d: True)
            done = date.fromisoformat(self.done[b.key]) if b.key in self.done else None
            open_hours, day = window_starts(hours, now, b.window, 0.0, day_ok, done)
            if not open_hours or day is None:
                continue
            near = hours[open_hours[0]] - now <= NEAR
            t0 = temp if near and temp is not None else (dhw.usual_start_temp(self.runs, b) or temp)
            kwh = dhw.boost_kwh(b, t0, self.per_degree)
            starts, _ = window_starts(hours, now, b.window, kwh / self.power_kw, day_ok, done)
            starts = starts or open_hours[:1]
            usual = next((i for i in open_hours if hours[i].hour == self.usual.get(b.key)), starts[-1])
            chosen = self.choice.get(b.key)
            previous = next((i for i in starts if hours[i].isoformat() == chosen), None)
            self._windows[b.key] = day
            out.append(FlexLoad(b.key, kwh, self.power_kw, tuple(starts), usual, self.fixed(b, hours), previous))
        return out

    def fixed(self, boost: dhw.DhwBoost, hours: list[datetime]) -> int | None:
        """Hour index of a boost Prelumo has started and not finished (not moved any more)."""
        run = self.running.get(boost.key)
        if run is None or self.done.get(boost.key) == run["day"]:
            return None
        start = datetime.fromisoformat(run["start"]).replace(minute=0, second=0, microsecond=0)
        return next((i for i, h in enumerate(hours) if h == start), 0)

    def after_plan(self, placements: list[Placement], hours: list[datetime]) -> None:
        by_key = {b.key: b for b in self.boosts()}
        self.plan = []
        for p in placements:
            b = by_key.get(p.key)
            if b is None or p.start is None:
                continue
            self.choice[p.key] = hours[p.start].isoformat()

            def at(i: int | None) -> str | None:
                return hours[i].isoformat() if i is not None and i < len(hours) else None

            self.plan.append({
                "window": f"{b.start:02d}:00-{b.end:02d}:00", "target": b.target, "after": b.after,
                "day": self._windows[p.key].isoformat() if p.key in self._windows else None,
                "kwh": p.kwh, "best_start": at(p.start), "usual_start": at(p.usual),
                "planned_start": at(p.planned), "saving_vs_usual": p.saving_vs_usual,
                "extra_cost_by_start": {f"{hours[i]:%H:%M}": c for i, c in sorted(p.cost_by_start.items())},
            })

    def next_start(self) -> datetime | None:
        starts = [datetime.fromisoformat(p["best_start"]) for p in self.plan if p["best_start"]]
        return min(starts) if starts else None

    # --- 30 s loop -----------------------------------------------------------
    def window_day(self, boost: dhw.DhwBoost, now: datetime) -> date:
        """Day (of its start) of the window ``now`` is in."""
        crossed = boost.start >= boost.end and now.hour < boost.end
        return (now - timedelta(days=1)).date() if crossed else now.date()

    def tick(self, now: datetime) -> bool:
        """Note the windows whose boost is done (the tank is at the target). True = changed."""
        if not self.configured:
            return False
        changed = False
        temp = self.temperature()
        for b in self.boosts():
            if temp is not None and b.covers(now.hour) and temp >= b.target - DONE_MARGIN:
                day = self.window_day(b, now).isoformat()
                if self.done.get(b.key) != day:
                    self.done[b.key] = day
                    changed = True
        return changed

    async def control(self, now: datetime) -> None:
        """Start the boosts at the hour the plan chose and end them (switch on, grid present).

        The target is set once, at the start - a temperature changed by hand meanwhile is left
        alone. Without a plan for today's window the boost starts in the window's last hour.
        """
        if not self.controlled():
            self.running = {}
            return
        temp = self.temperature()
        for b in self.boosts():
            day = self.window_day(b, now).isoformat()
            run = self.running.get(b.key)
            inside = b.covers(now.hour) and (not b.peak_only or self.coord.tariff().peak_day(
                self.window_day(b, now)))
            if run is not None:
                hot = temp is not None and temp >= b.target - REACHED
                if hot or not inside or run["day"] != day:
                    if await self._set(b.after, f"{b.key}: end, keep {b.after:g}"):
                        del self.running[b.key]
                        self.done[b.key] = run["day"]
                        self.coord.save()
                continue
            if not inside or self.done.get(b.key) == day:
                continue
            entry = next((p for p in self.plan if p["window"] == f"{b.start:02d}:00-{b.end:02d}:00"
                          and p["day"] == day), None)
            if entry is not None and entry["best_start"]:
                due = now >= datetime.fromisoformat(entry["best_start"])
            else:  # no plan for this window: do not leave the house without hot water
                due = not b.covers((now + timedelta(hours=1)).hour)
            if due and await self._set(b.target, f"{b.key}: start, heat to {b.target:g}"):
                self.running[b.key] = {"start": now.isoformat(), "day": day}
                self.coord.save()

    async def _set(self, temperature: float, what: str) -> bool:
        try:
            await self.coord.hass.services.async_call(
                "climate", "set_temperature", {"entity_id": self.climate, "temperature": temperature},
                blocking=True,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Hot water %s failed: %s", what, err)
            self.last_action = f"{what} - failed: {err}"
            return False
        _LOGGER.info("Hot water %s", what)
        self.last_action = f"{dt_util.now():%Y-%m-%d %H:%M} {what}"
        return True

    # --- storage / state -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {"events": self.events, "done": self.done, "choice": self.choice, "usual": self.usual,
                "running": self.running, "last_action": self.last_action}

    def load(self, data: dict[str, Any] | None) -> None:
        data = data or {}
        # climbs recorded before the compressor time was measured are read from the history again
        self.events = [e for e in data.get("events") or [] if "on_h" in e]
        self.done = dict(data.get("done") or {})
        self.choice = dict(data.get("choice") or {})
        self.usual = {k: int(v) for k, v in (data.get("usual") or {}).items()}
        self.running = dict(data.get("running") or {})
        self.last_action = data.get("last_action")

    def attrs(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "control": self.controlled(),
            "temperature": self.temperature(),
            "boosts": self.plan,
            "done": self.done,
            "running": self.running,
            "last_action": self.last_action,
            "kwh_per_degree": self.per_degree,
            "heating_power_kw": self.power_kw,
            "learned": dhw.kwh_per_degree(self.events) is not None,
            "runs_seen": len(self.runs),
            "climbs_seen": len(self.events),
            "usual_start_hour": self.usual,
            "other_reheats_kwh_by_hour": self.profile,
            "last_runs": self.runs[-6:],
        }
