"""What Prelumo learns from the recorder: the consumption, heat pump and EV models (at every start
and once a day), yesterday's PV bias and metered cost, and the BMS charge taper."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from .const import (
    CONF_EV_PLUGGED,
    CONF_EV_POWER,
    CONF_HP_POWER,
    CONF_WEATHER,
    OPT_CAPACITY,
    OPT_HISTORY_WEEKS,
    OPT_HP_BASE_TEMP,
)
from .core.accuracy import day_error
from .core.forecast.ev import EvPattern
from .core.forecast.heatpump import HeatPumpModel
from .core.forecast.load import LoadProfile, base_load_samples
from .core.shadow import energy_cost
from .core.taper import charge_events, merge_events, taper_from_events
from .ha_io import inputs as io

if TYPE_CHECKING:
    from .coordinator import PrelumoCoordinator

_LOGGER = logging.getLogger(__name__)


async def learn_taper(coord: PrelumoCoordinator) -> None:
    """Measure the BMS charge taper from the recent climbs to 100 % (raw SoC history)."""
    end = dt_util.now()
    try:
        points = await io.soc_history(coord.hass, coord.inv.soc, end - timedelta(days=10), end)
    except Exception as err:
        _LOGGER.debug("SoC history lookup failed: %s", err)
        return
    soc = io.state_float(coord.hass, coord.inv.soc)
    if soc is not None and (not points or points[-1][0] < end):
        points.append((end, soc))  # the recorder may not have committed the latest change yet
    merged = merge_events(coord.taper_events, charge_events(points))
    if merged != coord.taper_events:
        coord.taper_events = merged
        _LOGGER.info("Charge taper learned from %d charges: %s", len(merged),
                     taper_from_events(merged, float(coord.opt[OPT_CAPACITY])))
        coord.save()


async def learn_models(coord: PrelumoCoordinator, now: datetime) -> None:
    o = coord.opt
    weeks = int(o[OPT_HISTORY_WEEKS])
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(weeks=weeks)
    c = coord.conf
    house = await io.hourly_kwh(coord.hass, [coord.inv.house], start, end)
    hp = await io.hourly_kwh(coord.hass, [c.get(CONF_HP_POWER)], start, end)
    ev = await io.hourly_kwh(coord.hass, [c.get(CONF_EV_POWER)], start, end)
    coord.load_profile = LoadProfile(base_load_samples(house, hp, ev))
    temps = await io.hourly_mean_temp(coord.hass, c.get(CONF_WEATHER), start, end)
    coord.hp_model = HeatPumpModel(float(o[OPT_HP_BASE_TEMP]))
    await coord.dhw.learn(now)
    if hp and temps:  # hot water runs are forecast apart (dhw.py): the model learns heating only
        coord.hp_model.fit(coord.dhw.heating_only(hp, now), temps)
    sessions = await io.ev_sessions(coord.hass, c.get(CONF_EV_PLUGGED), ev, start, end)
    if sessions:
        coord.ev_pattern = EvPattern.learn(sessions, weeks)
    _LOGGER.debug("Learned: load %s h, HP coef %.3f, EV %s days",
                  coord.load_profile.sample_hours, coord.hp_model.coef, len(coord.ev_pattern.by_weekday))


async def _judge_forecast(
    coord: PrelumoCoordinator, y0: datetime, y1: datetime, actual_pv: dict[datetime, float]
) -> None:
    """Yesterday's forecast (the first plan of that day) against the meters, into the ledger."""
    snap = coord.day_forecast
    if snap.get("date") != y0.date().isoformat():
        return
    house = await io.hourly_kwh(coord.hass, [coord.inv.house], y0, y1)
    ev = await io.hourly_kwh(coord.hass, [coord.conf.get(CONF_EV_POWER)], y0, y1)
    actual = {"load": {h: max(0.0, v - ev.get(h, 0.0)) for h, v in house.items()}, "pv": actual_pv}
    for name, metered in actual.items():
        fc = {dt_util.as_local(dt): v for k, v in (snap.get(name) or {}).items()
              if (dt := dt_util.parse_datetime(k)) is not None}
        err = day_error(fc, metered)
        if err is not None:
            coord.ledger.record_energy(y0.date(), **{f"{name}_{k}": v for k, v in err.items()})


async def learn_daily(coord: PrelumoCoordinator, now: datetime) -> None:
    """Models: at every start (they live in memory only) and once per day after that.

    Once per day also: PV bias from yesterday + metered actual cost for the shadow ledger.
    """
    today = now.date().isoformat()
    if not coord.models_ready or coord.last_learn != today:
        await learn_models(coord, now)
        coord.models_ready = True
    if coord.last_learn == today:
        return
    y0 = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    y1 = y0 + timedelta(days=1)
    actual_pv = await io.hourly_kwh(coord.hass, coord.inv.pv, y0, y1)
    fc = coord.yesterday_pv_forecast
    if fc and actual_pv:
        coord.pv_bias.learn((h, fc[h], actual_pv.get(h, 0.0)) for h in fc if y0 <= h < y1)
    await _judge_forecast(coord, y0, y1, actual_pv)
    imp = await io.hourly_energy_change(coord.hass, coord.inv.grid_import_total, y0, y1)
    exp = await io.hourly_energy_change(coord.hass, coord.inv.grid_export_total, y0, y1)
    if imp:
        tariff = coord.tariff()
        buy = {h: tariff.price(h) for h in imp}
        coord.ledger.record_actual(y0.date(), energy_cost(imp, exp, buy, coord.sell_seen))
    house = await io.hourly_energy_change(coord.hass, coord.inv.house_total, y0, y1)
    pv = await io.hourly_energy_change(coord.hass, coord.inv.pv_total, y0, y1)
    dis = await io.hourly_energy_change(coord.hass, coord.inv.battery_discharge_total, y0, y1)
    if house or imp or dis:
        coord.ledger.record_energy(
            y0.date(), house=sum(house.values()), pv=sum(pv.values()),
            grid_import=sum(imp.values()), grid_export=sum(exp.values()),
            discharged=sum(dis.values()),
        )
    coord.ledger.trim()
    coord.last_learn = today
