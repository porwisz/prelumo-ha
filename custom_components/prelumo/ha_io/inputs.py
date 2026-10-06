"""Read Home Assistant states / recorder history and convert them to core data."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from ..core.forecast.ev import Session
from ..core.forecast.pv import solcast_to_hourly
from ..core.plan import SLOT_COUNT, DayPlan, PlanError, parse_flags, parse_hhmm
from ..core.tariff import parse_rce_records

_LOGGER = logging.getLogger(__name__)
BAD = (STATE_UNAVAILABLE, STATE_UNKNOWN, "", None)


def parse_local(text: str) -> datetime | None:
    dt = dt_util.parse_datetime(text)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_local(dt)


def state_float(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    if st is None or st.state in BAD:
        return None
    try:
        return float(st.state)
    except ValueError:
        return None


def state_age(hass: HomeAssistant, entity_id: str | None) -> timedelta | None:
    st = hass.states.get(entity_id) if entity_id else None
    if st is None or st.state in BAD:
        return None
    return dt_util.utcnow() - st.last_updated


def _kw_factor(st: State | None) -> float:
    unit = (st.attributes.get("unit_of_measurement") if st else None) or "W"
    return {"W": 0.001, "kW": 1.0, "MW": 1000.0}.get(unit, 0.001)


# --- inverter ------------------------------------------------------------
class InverterEntities:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def s(self, name: str) -> str:
        return f"sensor.{self.prefix}_{name}"

    @property
    def soc(self) -> str:
        return self.s("bateria_soc")

    @property
    def house(self) -> str:
        return self.s("dom_pobor")

    @property
    def pv(self) -> list[str]:
        return [self.s("pv1_moc"), self.s("pv2_moc")]

    @property
    def grid_import_total(self) -> str:
        return self.s("suma_siec_pobor")

    @property
    def grid_export_total(self) -> str:
        return self.s("suma_siec_oddanie")

    def slot(self, n: int, field: str) -> str:
        return self.s(f"harmonogram_{n}_{field}")

    def all_slot_entities(self) -> list[str]:
        return [self.slot(n, f) for n in range(1, SLOT_COUNT + 1) for f in ("start", "moc", "soc", "flagi")]


def read_current_plan(hass: HomeAssistant, inv: InverterEntities) -> DayPlan | None:
    start, power, soc, flags = [], [], [], []
    try:
        for n in range(1, SLOT_COUNT + 1):
            vals = [hass.states.get(inv.slot(n, f)) for f in ("start", "moc", "soc", "flagi")]
            if any(v is None or v.state in BAD for v in vals):
                return None
            start.append(parse_hhmm(vals[0].state))
            power.append(int(float(vals[1].state)))
            soc.append(int(float(vals[2].state)))
            flags.append(parse_flags(vals[3].state))
        return DayPlan.from_lists(start, power, soc, flags)
    except (PlanError, ValueError) as err:
        _LOGGER.debug("Inverter plan not readable: %s", err)
        return None


# --- prices & forecasts ----------------------------------------------------
def read_rce(hass: HomeAssistant, entity_ids: list[str | None]) -> dict[datetime, float]:
    out: dict[datetime, float] = {}
    for eid in entity_ids:
        st = hass.states.get(eid) if eid else None
        if st is None:
            continue
        unit = str(st.attributes.get("unit_of_measurement", "")).lower()
        per_mwh = True if "mwh" in unit else (False if "kwh" in unit else None)
        for key in ("prices", "raw_today", "raw_tomorrow", "forecast", "data"):
            recs = st.attributes.get(key)
            if isinstance(recs, list):
                out.update(parse_rce_records(recs, parse_local, per_mwh))
                break
    return out


def read_solcast(hass: HomeAssistant, entity_ids: list[str | None], key: str = "pv_estimate") -> dict[datetime, float]:
    out: dict[datetime, float] = {}
    for eid in entity_ids:
        st = hass.states.get(eid) if eid else None
        if st is None:
            continue
        recs = st.attributes.get("detailedHourly") or st.attributes.get("detailedForecast") or []
        out.update(solcast_to_hourly(recs, parse_local, key))
    return out


async def read_weather_temps(hass: HomeAssistant, entity_id: str | None) -> dict[datetime, float]:
    if not entity_id:
        return {}
    try:
        resp = await hass.services.async_call(
            "weather", "get_forecasts", {"entity_id": entity_id, "type": "hourly"},
            blocking=True, return_response=True,
        )
    except Exception as err:  # noqa: BLE001 - weather is optional
        _LOGGER.debug("Weather forecast unavailable: %s", err)
        return {}
    out = {}
    for item in (resp or {}).get(entity_id, {}).get("forecast", []):
        dt = parse_local(str(item.get("datetime")))
        if dt is not None and item.get("temperature") is not None:
            out[dt.replace(minute=0, second=0, microsecond=0)] = float(item["temperature"])
    return out


async def read_calendar(
    hass: HomeAssistant, entity_id: str | None, start: datetime, end: datetime
) -> list[tuple[datetime, float | None]]:
    """Events = departures. A number with % in the summary is the target SoC ('Kraków 90%')."""
    if not entity_id:
        return []
    try:
        resp = await hass.services.async_call(
            "calendar", "get_events",
            {"entity_id": entity_id, "start_date_time": start.isoformat(), "end_date_time": end.isoformat()},
            blocking=True, return_response=True,
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Calendar unavailable: %s", err)
        return []
    out = []
    for ev in (resp or {}).get(entity_id, {}).get("events", []):
        dt = parse_local(str(ev.get("start")))
        if dt is None:
            continue
        target = None
        for tok in str(ev.get("summary", "")).replace("%", " % ").split():
            if tok.isdigit() and 20 <= int(tok) <= 100:
                target = float(tok)
        out.append((dt, target))
    return out


def read_manual_target(
    hass: HomeAssistant, soc_entity: str | None, ready_entity: str | None, now: datetime
) -> tuple[float, datetime] | None:
    soc = state_float(hass, soc_entity)
    st = hass.states.get(ready_entity) if ready_entity else None
    if soc is None or soc <= 0 or st is None or st.state in BAD:
        return None
    a = st.attributes
    if a.get("has_date") and a.get("has_time"):
        ready = parse_local(st.state)
    elif a.get("has_time"):
        h, m = (int(x) for x in st.state.split(":")[:2])
        ready = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if ready <= now:
            ready += timedelta(days=1)
    else:
        return None
    return (soc, ready) if ready and ready > now else None


# --- history (recorder) ----------------------------------------------------
async def hourly_kwh(
    hass: HomeAssistant, entity_ids: list[str], start: datetime, end: datetime
) -> dict[datetime, float]:
    """Sum over entities of the hourly mean power (kW) == kWh per hour, from long-term stats."""
    ids = [e for e in entity_ids if e and hass.states.get(e)]
    if not ids:
        return {}
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, set(ids), "hour", None, {"mean"}
    )
    out: dict[datetime, float] = {}
    for eid, rows in stats.items():
        f = _kw_factor(hass.states.get(eid))
        for r in rows:
            if r.get("mean") is None:
                continue
            ts = r["start"]
            dt = dt_util.as_local(dt_util.utc_from_timestamp(ts) if isinstance(ts, (int, float)) else ts)
            out[dt] = out.get(dt, 0.0) + max(0.0, float(r["mean"])) * f
    return out


async def hourly_energy_change(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime
) -> dict[datetime, float]:
    """Hourly change of a total_increasing kWh counter."""
    if not hass.states.get(entity_id):
        return {}
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {entity_id}, "hour", None, {"change"}
    )
    out = {}
    for r in stats.get(entity_id, []):
        ts = r["start"]
        dt = dt_util.as_local(dt_util.utc_from_timestamp(ts) if isinstance(ts, (int, float)) else ts)
        out[dt] = max(0.0, float(r.get("change") or 0.0))
    return out


async def hourly_mean_temp(
    hass: HomeAssistant, weather_entity: str | None, start: datetime, end: datetime
) -> dict[datetime, float]:
    """Historical outdoor temperature from the weather entity's 'temperature' attribute."""
    if not weather_entity:
        return {}

    def _load() -> dict[datetime, float]:
        states = history.state_changes_during_period(
            hass, start, end, weather_entity, no_attributes=False, include_start_time_state=True
        ).get(weather_entity, [])
        buckets: dict[datetime, list[float]] = {}
        for s in states:
            t = s.attributes.get("temperature")
            if t is None:
                continue
            h = dt_util.as_local(s.last_updated).replace(minute=0, second=0, microsecond=0)
            buckets.setdefault(h, []).append(float(t))
        return {h: sum(v) / len(v) for h, v in buckets.items()}

    return await get_instance(hass).async_add_executor_job(_load)


async def ev_sessions(
    hass: HomeAssistant, plugged_entity: str | None, ev_energy: dict[datetime, float],
    start: datetime, end: datetime,
) -> list[Session]:
    if not plugged_entity:
        return []

    def _load() -> list[State]:
        return history.state_changes_during_period(
            hass, start, end, plugged_entity, no_attributes=True, include_start_time_state=True
        ).get(plugged_entity, [])

    states = await get_instance(hass).async_add_executor_job(_load)
    sessions, plug_in = [], None
    for s in states:
        on = s.state == STATE_ON
        ts = dt_util.as_local(s.last_changed)
        if on and plug_in is None:
            plug_in = ts
        elif not on and plug_in is not None and s.state not in BAD:
            energy = sum(v for h, v in ev_energy.items() if plug_in - timedelta(hours=1) < h < ts)
            sessions.append(Session(plug_in, ts, energy))
            plug_in = None
    return sessions


def is_outage(hass: HomeAssistant, entity_id: str | None, threshold: float) -> bool:
    st = hass.states.get(entity_id) if entity_id else None
    if st is None:
        return False
    if entity_id.startswith("binary_sensor."):
        return st.state == STATE_ON  # binary sensor "on" = outage
    if st.state in BAD:
        return False
    try:
        return float(st.state) < threshold
    except ValueError:
        return False


def attr(hass: HomeAssistant, entity_id: str, key: str) -> Any:
    st = hass.states.get(entity_id)
    return st.attributes.get(key) if st else None
