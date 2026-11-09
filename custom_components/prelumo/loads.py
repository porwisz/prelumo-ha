"""Devices Prelumo may run at the cheapest time (option "flexible loads"): planned like the hot
water boosts, started and stopped by turning their entity on and off, announced by events.

Switch off (the default): the best start of each load is only reported. Switch on: the battery
plan counts the load at the chosen hour; at that hour the entity (when one is given) is turned on
and the event ``prelumo_load_start`` is fired, after the run it is turned off and
``prelumo_load_end`` is fired - an automation can do anything else a device needs.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import OPT_FLEX_LOADS, OPTION_DEFAULTS
from .core.flex import FlexLoad, LoadError, Placement, TimedLoad, parse_loads, window_starts

if TYPE_CHECKING:
    from .coordinator import PrelumoCoordinator

_LOGGER = logging.getLogger(__name__)

EVENT_START = "prelumo_load_start"
EVENT_END = "prelumo_load_end"


class LoadManager:
    def __init__(self, coord: PrelumoCoordinator) -> None:
        self.coord = coord
        self.done: dict[str, str] = {}  # load key -> day (ISO) whose window has had its run
        self.choice: dict[str, str] = {}  # load key -> start (ISO hour) the last plan chose
        self.running: dict[str, dict[str, str]] = {}  # load key -> {start, end, day}
        self.plan: list[dict[str, Any]] = []
        self.last_action: str | None = None
        self._windows: dict[str, date] = {}

    def items(self) -> list[TimedLoad]:
        text = self.coord.opt.get(OPT_FLEX_LOADS, OPTION_DEFAULTS[OPT_FLEX_LOADS])
        try:
            return parse_loads(text)
        except LoadError as err:
            _LOGGER.warning("Flexible loads not readable (%s): %s", text, err)
            return []

    def controlled(self) -> bool:
        return bool(self.coord.load_control) and bool(self.items())

    def _day_ok(self, item: TimedLoad):
        return self.coord.tariff().peak_day if item.peak_only else (lambda d: True)

    # --- planning ----------------------------------------------------------
    def loads(self, now: datetime, hours: list[datetime]) -> list[FlexLoad]:
        self._windows = {}
        out = []
        for item in self.items():
            done = date.fromisoformat(self.done[item.key]) if item.key in self.done else None
            run = self.running.get(item.key)
            if run is not None:  # started: the rest of the run stays where it is
                end = datetime.fromisoformat(run["end"])
                left = max(0.0, (end - now).total_seconds() / 3600) * item.power_kw
                self._windows[item.key] = date.fromisoformat(run["day"])
                out.append(FlexLoad(item.key, min(item.kwh, left), item.power_kw, (), None, 0))
                continue
            starts, day = window_starts(hours, now, item.window, item.duration.total_seconds() / 3600,
                                        self._day_ok(item), done)
            if not starts or day is None:
                continue
            chosen = self.choice.get(item.key)
            previous = next((i for i in starts if hours[i].isoformat() == chosen), None)
            self._windows[item.key] = day
            out.append(FlexLoad(item.key, item.kwh, item.power_kw, tuple(starts), None, None, previous))
        return out

    def after_plan(self, placements: list[Placement], hours: list[datetime]) -> None:
        by_key = {x.key: x for x in self.items()}
        self.plan = []
        for p in placements:
            item = by_key.get(p.key)
            if item is None or p.start is None:
                continue
            running = p.key in self.running
            if not running:
                self.choice[p.key] = hours[p.start].isoformat()
            self.plan.append({
                "name": item.name, "window": f"{item.start:02d}:00-{item.end:02d}:00",
                "day": self._windows[p.key].isoformat() if p.key in self._windows else None,
                "kwh": item.kwh, "power_kw": item.power_kw, "entity": item.entity,
                "minutes": round(item.duration.total_seconds() / 60),
                "best_start": self.running[p.key]["start"] if running else hours[p.start].isoformat(),
                "running": running,
                "extra_cost_by_start": {f"{hours[i]:%H:%M}": c for i, c in sorted(p.cost_by_start.items())},
            })

    def next_start(self) -> datetime | None:
        starts = [datetime.fromisoformat(p["best_start"]) for p in self.plan if not p["running"]]
        return min(starts) if starts else None

    # --- 30 s loop -----------------------------------------------------------
    def _window_day(self, item: TimedLoad, now: datetime) -> date:
        crossed = item.start >= item.end and now.hour < item.end
        return (now - timedelta(days=1)).date() if crossed else now.date()

    async def control(self, now: datetime) -> None:
        """Start each load at the hour the plan chose and stop it after its run."""
        if not self.controlled():
            self.running = {}
            return
        for item in self.items():
            run = self.running.get(item.key)
            if run is not None:
                if now >= datetime.fromisoformat(run["end"]) and await self._switch(item, False):
                    del self.running[item.key]
                    self.done[item.key] = run["day"]
                    self.coord.save()
                continue
            day = self._window_day(item, now)
            if not item.covers(now.hour) or self.done.get(item.key) == day.isoformat() or not self._day_ok(item)(day):
                continue
            entry = next((p for p in self.plan if p["name"] == item.name and p["day"] == day.isoformat()), None)
            if entry is not None:
                due = now >= datetime.fromisoformat(entry["best_start"])
            else:  # no plan for this window: the latest start that still ends inside it
                due = not item.covers((now + item.duration + timedelta(hours=1)).hour)
            if due and await self._switch(item, True):
                self.running[item.key] = {"start": now.isoformat(), "end": (now + item.duration).isoformat(),
                                          "day": day.isoformat()}
                self.coord.save()

    async def _switch(self, item: TimedLoad, on: bool) -> bool:
        hass = self.coord.hass
        what = f"{item.name}: {'start' if on else 'end'}"
        if item.entity:
            try:
                await hass.services.async_call(
                    "homeassistant", "turn_on" if on else "turn_off", {"entity_id": item.entity}, blocking=True
                )
            except HomeAssistantError as err:
                _LOGGER.warning("Flexible load %s failed: %s", what, err)
                self.last_action = f"{what} - failed: {err}"
                return False
        hass.bus.async_fire(EVENT_START if on else EVENT_END,
                            {"name": item.name, "entity_id": item.entity, "kwh": item.kwh})
        _LOGGER.info("Flexible load %s", what)
        self.last_action = f"{dt_util.now():%Y-%m-%d %H:%M} {what}"
        return True

    # --- storage / state -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {"done": self.done, "choice": self.choice, "running": self.running,
                "last_action": self.last_action}

    def load(self, data: dict[str, Any] | None) -> None:
        data = data or {}
        self.done = dict(data.get("done") or {})
        self.choice = dict(data.get("choice") or {})
        self.running = dict(data.get("running") or {})
        self.last_action = data.get("last_action")

    def attrs(self) -> dict[str, Any]:
        return {"control": self.controlled(), "loads": self.plan, "done": self.done,
                "running": sorted(k.split(":", 1)[1] for k in self.running), "last_action": self.last_action}
