"""Prelumo coordinator: reads inverter state every 30 s, re-plans every hour."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_EV_CALENDAR, CONF_EV_LIMIT, CONF_EV_MANUAL_READY, CONF_EV_MANUAL_SOC, CONF_EV_PLUGGED,
    CONF_EV_POWER, CONF_EV_SOC, CONF_HP_POWER, CONF_INVERTER_PREFIX, CONF_MODE_EXPORT_FIRST,
    CONF_MODE_SELECT, CONF_MODE_ZERO_EXPORT, CONF_OUTAGE_ENTITY, CONF_OUTAGE_THRESHOLD,
    CONF_RCE_TODAY, CONF_RCE_TOMORROW, CONF_SOLCAST_TODAY, CONF_SOLCAST_TOMORROW, CONF_WEATHER,
    CONF_WRITE_ACTION, DEFAULTS, DOMAIN, ISSUE_STALE_DATA, ISSUE_WRITE_FAILED, OPT_AFTERNOON_SUMMER,
    OPT_AFTERNOON_WINTER, OPT_CAPACITY, OPT_EFFICIENCY, OPT_EV_CAPACITY, OPT_EV_CHARGER_KW,
    OPT_EV_DEFAULT_TARGET, OPT_FALLBACK_PLAN, OPT_GRID_EXPORT, OPT_GRID_IMPORT, OPT_HISTORY_WEEKS,
    OPT_HORIZON, OPT_HP_BASE_TEMP, OPT_MAX_CHARGE, OPT_MAX_DISCHARGE, OPT_MAX_SOC, OPT_MAX_WRITES,
    OPT_MIN_SOC, OPT_MORNING_HOURS, OPT_PRICE_AFTERNOON, OPT_PRICE_MORNING, OPT_PRICE_OFF,
    OPT_SOC_STEP, OPT_SOC_TOLERANCE, OPT_STALE_HOURS, OPT_WEAR, OPTION_DEFAULTS, PLAN_MINUTE,
    STORAGE_VERSION, UPDATE_INTERVAL,
)
from .core.diff import WriteGuard, WritePolicy
from .core.forecast.ev import CarState, EvForecast, EvPattern, forecast_ev
from .core.forecast.heatpump import HeatPumpModel
from .core.forecast.load import LoadProfile, base_load_samples
from .core.forecast.pv import PvBias
from .core.optimizer import BatteryParams, Strategy
from .core.plan import DayPlan
from .core.planner import PlannerInputs, PlannerResult, horizon_hours, parse_fallback_plan, run_planner
from .core.shadow import ShadowLedger, energy_cost
from .core.tariff import G13Tariff
from .ha_io import inputs as io
from .ha_io.inverter import InverterWriter

_LOGGER = logging.getLogger(__name__)


@dataclass
class PrelumoData:
    current_plan: DayPlan | None = None
    active_index: int | None = None
    next_change: datetime | None = None
    sell_now: bool = False
    outage: bool = False
    soc: float | None = None


@dataclass
class PlanState:
    """Result of the last planning run (hourly)."""

    computed_at: datetime | None = None
    result: PlannerResult | None = None
    proposed: DayPlan | None = None
    fallback_active: bool = False
    stale_sources: list[str] = field(default_factory=list)
    write_decision: list[str] = field(default_factory=list)
    last_write: datetime | None = None
    ev: EvForecast | None = None
    pv: list[float] = field(default_factory=list)
    load: list[float] = field(default_factory=list)
    heat_pump: list[float] = field(default_factory=list)
    hours: list[datetime] = field(default_factory=list)
    error: str | None = None


def _range(text: str) -> tuple[int, int]:
    a, b = (int(x) for x in text.split("-"))
    return a, b


class PrelumoCoordinator(DataUpdateCoordinator[PrelumoData]):
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL, config_entry=entry)
        c = {**DEFAULTS, **entry.data}
        self.conf = c
        self.inv = io.InverterEntities(c[CONF_INVERTER_PREFIX])
        self.writer = InverterWriter(
            hass, c[CONF_WRITE_ACTION], c[CONF_MODE_SELECT], c[CONF_MODE_EXPORT_FIRST], c[CONF_MODE_ZERO_EXPORT]
        )
        self.store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.plan_state = PlanState()
        # runtime controls (restored by entities)
        self.strategy = Strategy.SELF_CONSUMPTION
        self.arbitrage = False
        self.shadow = True
        self.auto_mode = True
        # learned models
        self.pv_bias = PvBias()
        self.ev_pattern = EvPattern()
        self.ledger = ShadowLedger()
        self.load_profile = LoadProfile()
        self.hp_model = HeatPumpModel()
        self.last_learn: str | None = None
        self.guard = WriteGuard(self._policy())
        self._plan_lock = asyncio.Lock()
        self._yesterday_pv_forecast: dict[datetime, float] = {}
        self._today_pv_forecast: dict[datetime, float] = {}
        self._sell_seen: dict[datetime, float] = {}  # hourly RCE, kept ~3 days for actual cost
        self._unsub: list = []

    # --- options -------------------------------------------------------
    @property
    def opt(self) -> dict[str, Any]:
        return {**OPTION_DEFAULTS, **self.config_entry.options}

    def _policy(self) -> WritePolicy:
        o = self.opt
        return WritePolicy(soc_tolerance=int(o[OPT_SOC_TOLERANCE]), max_writes_per_day=int(o[OPT_MAX_WRITES]))

    def battery_params(self) -> BatteryParams:
        o = self.opt
        return BatteryParams(
            capacity_kwh=float(o[OPT_CAPACITY]), min_soc=int(o[OPT_MIN_SOC]), max_soc=int(o[OPT_MAX_SOC]),
            max_charge_kw=float(o[OPT_MAX_CHARGE]), max_discharge_kw=float(o[OPT_MAX_DISCHARGE]),
            roundtrip_efficiency=float(o[OPT_EFFICIENCY]), wear_cost=float(o[OPT_WEAR]),
            grid_import_kw=float(o[OPT_GRID_IMPORT]), grid_export_kw=float(o[OPT_GRID_EXPORT]),
            soc_step=int(o[OPT_SOC_STEP]),
        )

    def tariff(self) -> G13Tariff:
        o = self.opt
        return G13Tariff(
            float(o[OPT_PRICE_MORNING]), float(o[OPT_PRICE_AFTERNOON]), float(o[OPT_PRICE_OFF]),
            morning_peak=_range(o[OPT_MORNING_HOURS]),
            afternoon_peak_summer=_range(o[OPT_AFTERNOON_SUMMER]),
            afternoon_peak_winter=_range(o[OPT_AFTERNOON_WINTER]),
        )

    def fallback_plan(self) -> DayPlan:
        power = int(min(12000, float(self.opt[OPT_MAX_DISCHARGE]) * 1000))
        return parse_fallback_plan(self.opt[OPT_FALLBACK_PLAN], power)

    # --- lifecycle -------------------------------------------------------
    async def async_setup(self) -> None:
        stored = await self.store.async_load() or {}
        self.pv_bias = PvBias.from_dict(stored.get("pv_bias"))
        self.ev_pattern = EvPattern.from_dict(stored.get("ev_pattern"))
        self.ledger = ShadowLedger.from_dict(stored.get("shadow"))
        self.last_learn = stored.get("last_learn")
        self.guard.history = [dt for t in stored.get("writes", []) if (dt := dt_util.parse_datetime(t))]
        self._unsub.append(
            async_track_time_change(self.hass, self._hourly, minute=PLAN_MINUTE, second=0)
        )

    async def async_shutdown(self) -> None:
        for u in self._unsub:
            u()
        self._unsub.clear()
        await super().async_shutdown()

    @callback
    def _save(self) -> None:
        self.store.async_delay_save(
            lambda: {
                "pv_bias": self.pv_bias.to_dict(),
                "ev_pattern": self.ev_pattern.to_dict(),
                "shadow": self.ledger.to_dict(),
                "last_learn": self.last_learn,
                "writes": [t.isoformat() for t in self.guard.history],
            },
            5,
        )

    # --- 30 s loop: state + work-mode control ------------------------------
    async def _async_update_data(self) -> PrelumoData:
        now = dt_util.now()
        plan = io.read_current_plan(self.hass, self.inv)
        outage = io.is_outage(self.hass, self.conf.get(CONF_OUTAGE_ENTITY), float(self.conf[CONF_OUTAGE_THRESHOLD]))
        data = PrelumoData(current_plan=plan, outage=outage, soc=io.state_float(self.hass, self.inv.soc))
        if plan is not None:
            data.active_index = plan.active_index(now)
            data.next_change, _ = plan.next_change(now)
            data.sell_now = plan.slots[data.active_index].mode.value == "sell"
            if self.auto_mode and not outage:
                try:
                    await self.writer.ensure_mode(self.writer.desired_mode(plan, now))
                except HomeAssistantError as err:
                    _LOGGER.warning("Work mode change failed: %s", err)
        return data

    async def _hourly(self, _now: datetime) -> None:
        await self.async_replan()

    # --- planning ----------------------------------------------------------
    async def async_replan(self, force_write: bool = False) -> PlanState:
        async with self._plan_lock:
            try:
                await self._replan(force_write)
                self.plan_state.error = None
            except Exception as err:  # noqa: BLE001 - never kill the loop
                _LOGGER.exception("Planning failed")
                self.plan_state.error = str(err)
            self._save()
            self.async_update_listeners()
            return self.plan_state

    async def _learn(self, now: datetime) -> None:
        o = self.opt
        weeks = int(o[OPT_HISTORY_WEEKS])
        end = now.replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(weeks=weeks)
        c = self.conf
        house = await io.hourly_kwh(self.hass, [self.inv.house], start, end)
        hp = await io.hourly_kwh(self.hass, [c.get(CONF_HP_POWER)], start, end)
        ev = await io.hourly_kwh(self.hass, [c.get(CONF_EV_POWER)], start, end)
        self.load_profile = LoadProfile(base_load_samples(house, hp, ev))
        temps = await io.hourly_mean_temp(self.hass, c.get(CONF_WEATHER), start, end)
        self.hp_model = HeatPumpModel(float(o[OPT_HP_BASE_TEMP]))
        if hp and temps:
            self.hp_model.fit(hp, temps)
        sessions = await io.ev_sessions(self.hass, c.get(CONF_EV_PLUGGED), ev, start, end)
        if sessions:
            self.ev_pattern = EvPattern.learn(sessions, weeks)
        _LOGGER.debug("Learned: load %s h, HP coef %.3f, EV %s days",
                      self.load_profile.sample_hours, self.hp_model.coef, len(self.ev_pattern.by_weekday))

    async def _learn_daily(self, now: datetime) -> None:
        """Once per day: PV bias from yesterday + metered actual cost for the shadow ledger."""
        today = now.date().isoformat()
        if self.last_learn == today:
            return
        await self._learn(now)
        y0 = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        y1 = y0 + timedelta(days=1)
        actual_pv = await io.hourly_kwh(self.hass, self.inv.pv, y0, y1)
        fc = self._yesterday_pv_forecast
        if fc and actual_pv:
            self.pv_bias.learn((h, fc[h], actual_pv.get(h, 0.0)) for h in fc if y0 <= h < y1)
        imp = await io.hourly_energy_change(self.hass, self.inv.grid_import_total, y0, y1)
        exp = await io.hourly_energy_change(self.hass, self.inv.grid_export_total, y0, y1)
        if imp:
            tariff = self.tariff()
            buy = {h: tariff.price(h) for h in imp}
            self.ledger.record_actual(y0.date(), energy_cost(imp, exp, buy, self._sell_seen))
        self.ledger.trim()
        self.last_learn = today

    async def _replan(self, force_write: bool) -> None:
        now = dt_util.now()
        c, o = self.conf, self.opt
        if self._today_pv_forecast and next(iter(self._today_pv_forecast)).date() != now.date():
            self._yesterday_pv_forecast = self._today_pv_forecast
        await self._learn_daily(now)

        horizon = int(o[OPT_HORIZON])
        hours = horizon_hours(now, horizon)
        stale: list[str] = []
        soc = io.state_float(self.hass, self.inv.soc)
        if soc is None:
            stale.append("battery_soc")

        pv_raw = io.read_solcast(self.hass, [c.get(CONF_SOLCAST_TODAY), c.get(CONF_SOLCAST_TOMORROW)])
        self._today_pv_forecast = {h: v for h, v in pv_raw.items() if h.date() == now.date()} or self._today_pv_forecast
        if not any(h in pv_raw for h in hours[:12]):
            stale.append("pv_forecast")
        sell = io.read_rce(self.hass, [c.get(CONF_RCE_TODAY), c.get(CONF_RCE_TOMORROW)])
        sell_hourly: dict[datetime, list[float]] = {}
        for ts, p in sell.items():
            sell_hourly.setdefault(ts.replace(minute=0, second=0, microsecond=0), []).append(p)
        sell_h = {h: sum(v) / len(v) for h, v in sell_hourly.items()}
        self._sell_seen.update(sell_h)
        self._sell_seen = {h: v for h, v in self._sell_seen.items() if now - h < timedelta(days=3)}
        if not any(h in sell_h for h in hours[:12]):
            stale.append("sell_prices")

        ps = self.plan_state
        ps.stale_sources = stale
        ps.computed_at = now
        ps.hours = hours
        current = io.read_current_plan(self.hass, self.inv)
        hours_stale = int(o[OPT_STALE_HOURS])

        if stale:
            age_ok = ps.result is not None and ps.proposed is not None and not ps.fallback_active and (
                now - (ps.result.hours[0].start if ps.result.hours else now) < timedelta(hours=hours_stale)
            )
            self._raise_stale(stale)
            if age_ok:
                ps.write_decision = ["stale_inputs_keep_last_plan"]
                return
            ps.fallback_active = True
            ps.proposed = self.fallback_plan().merge_preserved_flags(current)
            await self._maybe_write(current, ps.proposed, now, force_write)
            return
        if ps.fallback_active:
            persistent_notification.async_create(
                self.hass, "Dane źródłowe są znów dostępne — Prelumo wraca do planowania.",
                title="Prelumo", notification_id=f"{DOMAIN}_stale",
            )
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_STALE_DATA)
        ps.fallback_active = False

        pv = dict(zip(hours, self.pv_bias.apply(hours, [pv_raw.get(h, 0.0) for h in hours])))
        base = self.load_profile.predict(hours)
        temps = await io.read_weather_temps(self.hass, c.get(CONF_WEATHER))
        hp = self.hp_model.predict(hours, temps) if self.hp_model.coef or self.hp_model.dhw else [0.0] * horizon
        ev = await self._ev_forecast(now, hours, [b + h for b, h in zip(base, hp)])
        outage = self.data.outage if self.data else False
        if outage:
            ev.battery_allowed = [True] * len(hours)

        inp = PlannerInputs(now, soc, pv, base, hp, ev, sell_h, self.tariff(), horizon)
        params = self.battery_params()
        result = await self.hass.async_add_executor_job(
            run_planner, inp, params, self.strategy, self.arbitrage, current
        )
        ps.result, ps.proposed, ps.ev = result, result.plan, ev
        ps.pv, ps.load, ps.heat_pump = [pv[h] for h in hours], base, hp
        self.ledger.record_plan(now.date(), result.slot_plan_cost, result.baseline_cost)
        await self._maybe_write(current, result.plan, now, force_write)

    async def _ev_forecast(self, now: datetime, hours: list[datetime], base: list[float]) -> EvForecast:
        c, o = self.conf, self.opt
        limit = io.state_float(self.hass, c.get(CONF_EV_LIMIT))
        plugged_st = self.hass.states.get(c[CONF_EV_PLUGGED]) if c.get(CONF_EV_PLUGGED) else None
        car = CarState(
            soc=io.state_float(self.hass, c.get(CONF_EV_SOC)),
            limit=limit if limit is not None else float(o[OPT_EV_DEFAULT_TARGET]),
            plugged=bool(plugged_st and plugged_st.state == "on"),
            capacity_kwh=float(o[OPT_EV_CAPACITY]),
            charger_kw=float(o[OPT_EV_CHARGER_KW]),
        )
        manual = io.read_manual_target(self.hass, c.get(CONF_EV_MANUAL_SOC), c.get(CONF_EV_MANUAL_READY), now)
        cal = await io.read_calendar(self.hass, c.get(CONF_EV_CALENDAR), now, hours[-1] + timedelta(hours=1))
        return forecast_ev(
            hours, now, car, self.ev_pattern, manual, cal,
            grid_import_kw=float(o[OPT_GRID_IMPORT]), base_load=base,
            default_target_soc=float(o[OPT_EV_DEFAULT_TARGET]),
        )

    async def _maybe_write(self, current: DayPlan | None, new: DayPlan, now: datetime, force: bool) -> None:
        ps = self.plan_state
        self.guard.policy = self._policy()
        if self.shadow and not force:
            ps.write_decision = ["shadow_mode"]
            return
        if not self.auto_mode and not force:
            ps.write_decision = ["auto_mode_off"]
            return
        ok, reasons = self.guard.decide(current, new, now)
        if force and reasons != ["no_material_change"]:
            ok = True
        ps.write_decision = reasons
        if not ok:
            return
        try:
            await self.writer.write_plan(new)
        except HomeAssistantError as err:
            ps.write_decision = ["write_failed", str(err)]
            ir.async_create_issue(
                self.hass, DOMAIN, ISSUE_WRITE_FAILED, is_fixable=False,
                severity=ir.IssueSeverity.ERROR, translation_key=ISSUE_WRITE_FAILED,
                translation_placeholders={"error": str(err)},
            )
            return
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_WRITE_FAILED)
        self.guard.record(now)
        ps.last_write = now

    def _raise_stale(self, stale: list[str]) -> None:
        names = ", ".join(stale)
        ir.async_create_issue(
            self.hass, DOMAIN, ISSUE_STALE_DATA, is_fixable=False, severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_STALE_DATA, translation_placeholders={"sources": names},
        )
        persistent_notification.async_create(
            self.hass,
            f"Brak lub nieaktualne dane: {names}. Prelumo trzyma ostatni plan lub przechodzi na plan awaryjny.",
            title="Prelumo", notification_id=f"{DOMAIN}_stale",
        )

    # --- manual actions --------------------------------------------------
    async def async_apply(self, plan: DayPlan) -> None:
        current = io.read_current_plan(self.hass, self.inv)
        merged = plan.merge_preserved_flags(current)
        await self.writer.write_plan(merged)
        now = dt_util.now()
        self.guard.record(now)
        self.plan_state.last_write = now
        self._save()
        await self.async_request_refresh()
