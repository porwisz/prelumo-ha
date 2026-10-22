"""Prelumo coordinator: reads inverter state every 30 s, re-plans every hour."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
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
    CONF_ALERT_ENTITY, CONF_EV_CALENDAR, CONF_EV_LIMIT, CONF_EV_MANUAL_READY, CONF_EV_MANUAL_SOC, CONF_EV_PLUGGED,
    CONF_EV_POWER, CONF_EV_SOC, CONF_HP_POWER, CONF_INVERTER_PREFIX, CONF_MODE_EXPORT_FIRST,
    CONF_MODE_SELECT, CONF_MODE_ZERO_EXPORT, CONF_OUTAGE_CALENDAR, CONF_OUTAGE_ENTITY,
    CONF_OUTAGE_THRESHOLD,
    CONF_RCE_TODAY, CONF_RCE_TOMORROW, CONF_SOLCAST_TODAY, CONF_SOLCAST_TOMORROW, CONF_WEATHER,
    CONF_WRITE_ACTION, DEFAULTS, DOMAIN, ISSUE_STALE_DATA, ISSUE_WRITE_FAILED, OPT_AFTERNOON_SUMMER,
    OPT_AFTERNOON_WINTER, OPT_CAPACITY, OPT_EFFICIENCY, OPT_EV_CAPACITY, OPT_EV_CHARGER_KW,
    OPT_EV_DEFAULT_TARGET, OPT_FALLBACK_PLAN, OPT_GRID_EXPORT, OPT_GRID_IMPORT, OPT_HISTORY_WEEKS,
    OPT_HORIZON, OPT_HP_BASE_TEMP, OPT_MAX_CHARGE, OPT_MAX_DISCHARGE, OPT_MAX_SOC, OPT_MAX_WRITES,
    OPT_MIN_SOC, OPT_MORNING_HOURS, OPT_PRICE_AFTERNOON, OPT_PRICE_MORNING, OPT_PRICE_OFF,
    OPT_ALERT_LEAD, OPT_ALERT_SOC, OPT_ARBITRAGE_MARGIN, OPT_BATTERY_FIRST, OPT_BLOCK_EXPORT_PRICE, OPT_FORCE_SELL_PRICE, OPT_RESERVE_MARGIN, OPT_FULL_CHARGE_DAYS, OPT_MAX_CHARGE_PRICE, OPT_MAX_CHARGE_SOC, OPT_MIN_GAIN, OPT_MIN_SELL_SOC, OPT_PV_TRUST, OPT_SELL_MIN_POWER, OPT_SOC_STEP, OPT_SOC_TOLERANCE, OPT_STALE_HOURS, OPT_WEAR, OPTION_DEFAULTS, PLAN_MINUTE, SLOT_WINDOW_H,
    STORAGE_VERSION, UPDATE_INTERVAL,
)
from .core.balance import FULL_SOC, FullChargeTracker
from .core.diff import RuleCheck, WriteGuard, WritePolicy
from .core.forecast.ev import CarState, EvForecast, EvPattern, forecast_ev
from .core.forecast.heatpump import HeatPumpModel
from .core.forecast.load import LoadProfile, base_load_samples
from .core.forecast.pv import PvBias
from .core.optimizer import BatteryParams, Strategy
from .core.plan import SLOT_COUNT, DayPlan, PlanError, Slot, SlotMode, same_behaviour
from .core.planner import PlannerInputs, PlannerResult, horizon_hours, parse_fallback_plan, run_planner
from .core.shadow import ShadowLedger, energy_cost
from .core.stock import MAX_TICK_KWH, BatteryStock, account_tick
from .core.taper import charge_events, merge_events, taper_from_events
from .core.tariff import G13Tariff
from .ha_io import inputs as io
from .ha_io.inverter import InverterWriter

_LOGGER = logging.getLogger(__name__)

MAX_DRAFT_POWER = 10000  # W, default slot power when nothing can be read
WRITE_SETTLE = timedelta(minutes=5)  # max time the work mode follows the written plan
SELL_SEEN_KEEP = timedelta(days=3)  # how long hourly sell prices are kept


@dataclass
class PrelumoData:
    current_plan: DayPlan | None = None
    active_index: int | None = None
    next_change: datetime | None = None
    sell_now: bool = False
    outage: bool = False
    soc: float | None = None
    export_blocked: bool = False  # sell price at or below the block threshold right now
    desired_mode: str | None = None  # work mode Prelumo wants (also computed in shadow mode)
    mode_action: str | None = None  # what happened: set / unchanged / shadow / auto_off / outage


@dataclass(frozen=True)
class AlertCharge:
    """Charge to be full before a coming power cut."""

    soc: int
    by_hours: int | None  # deadline in hours from now; None = as soon as possible
    reason: str  # weather_alert | planned_outage


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
        # plan just written: the inverter is read back slot by slot, so for a while the read-back
        # mixes old and new slots (15 Oct: new start times with an old sell flag -> Export First
        # for 20 s and 4 kW to the grid). Work mode follows the written plan until they match.
        self._written: DayPlan | None = None
        self._written_at: datetime | None = None
        self.writer = InverterWriter(
            hass, c[CONF_WRITE_ACTION], c[CONF_MODE_SELECT], c[CONF_MODE_EXPORT_FIRST], c[CONF_MODE_ZERO_EXPORT]
        )
        self.store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.plan_state = PlanState()
        # manual schedule draft (Edycja tab): follows the inverter until edited
        self._draft: list[Slot] | None = None
        self.taper_events: list[dict] = []  # measured climbs to 100 % (minutes per SoC band)
        # runtime controls (restored by entities)
        self.strategy = Strategy.SELF_CONSUMPTION
        self.arbitrage = False
        self.grid_energy: bool | None = None  # battery holds energy bought from the grid
        self.alert_on = False  # weather warning now active
        self.planned_outage: datetime | None = None  # start of the next announced outage
        self.ev_power_seen: float | None = None  # kW, last measured EV charging power
        self.shadow = True
        self.auto_mode = True
        # learned models
        self.pv_bias = PvBias()
        self.ev_pattern = EvPattern()
        self.ledger = ShadowLedger()
        self.stock = BatteryStock()  # what the energy now in the battery cost
        self._counters: dict[str, float] = {}  # last inverter energy counters (kWh)
        self.load_profile = LoadProfile()
        self.hp_model = HeatPumpModel()
        self.last_learn: str | None = None
        self.full = FullChargeTracker(int(self.opt[OPT_FULL_CHARGE_DAYS]))
        self.guard = WriteGuard(self._policy())
        self._plan_lock = asyncio.Lock()
        self._yesterday_pv_forecast: dict[datetime, float] = {}
        self._today_pv_forecast: dict[datetime, float] = {}
        # hourly RCE, kept ~3 days: for yesterday's actual cost and for the live export check
        # right after a restart (persisted, so that check is not blind until the first re-plan)
        self._sell_seen: dict[datetime, float] = {}
        self._last_recover: datetime | None = None
        self._models_ready = False  # load profile / heat pump / EV models are not persisted
        self._unsub: list = []

    # --- options -------------------------------------------------------
    @property
    def opt(self) -> dict[str, Any]:
        return {**OPTION_DEFAULTS, **self.config_entry.options}

    def _policy(self) -> WritePolicy:
        o = self.opt
        return WritePolicy(
            soc_tolerance=int(o[OPT_SOC_TOLERANCE]), max_writes_per_day=int(o[OPT_MAX_WRITES]),
            min_gain=float(o[OPT_MIN_GAIN]), window_h=SLOT_WINDOW_H,
        )

    def full_charge_due(self) -> bool:
        return self.full.due(dt_util.now())

    def full_charge_planned(self) -> bool:
        """Plan the 100 % charge when it falls due within the slot window - so the cheapest
        hours around the due time are used, not only the ones after it."""
        return self.full.due(dt_util.now() + timedelta(hours=SLOT_WINDOW_H))

    def alert_charge(self) -> AlertCharge | None:
        """Charge to be full for a coming power cut: weather warning or a planned outage.

        Different from the periodic BMS balancing: here keeping the lights on beats the price,
        so the price cap and battery-first are lifted and the deadline is the event, not the
        slot window.
        """
        soc = int(self.opt[OPT_ALERT_SOC])
        if soc <= 0:
            return None
        if self.alert_on:
            return AlertCharge(soc, None, "weather_alert")
        if self.planned_outage is not None:
            lead = timedelta(hours=float(self.opt[OPT_ALERT_LEAD]))
            by = max(0, int((self.planned_outage - lead - dt_util.now()).total_seconds() // 3600))
            return AlertCharge(soc, by, "planned_outage")
        return None

    def battery_params(self) -> BatteryParams:
        o = self.opt
        alert = self.alert_charge()
        due = self.full_charge_planned() or alert is not None
        # everyday limit (last % charge slowly); full charge only when the periodic one is due
        max_soc = int(o[OPT_MAX_SOC]) if due else min(int(o[OPT_MAX_SOC]), int(o[OPT_MAX_CHARGE_SOC]))
        if alert is not None:
            max_soc = min(int(o[OPT_MAX_SOC]), alert.soc)
        return BatteryParams(
            capacity_kwh=float(o[OPT_CAPACITY]), min_soc=int(o[OPT_MIN_SOC]), max_soc=max_soc,
            require_full=due,
            battery_first_above=None if alert else (
                self.max_grid_charge_price() if o[OPT_BATTERY_FIRST] else None),
            reserve_margin=float(o[OPT_RESERVE_MARGIN]) / 100,
            arbitrage_min_margin=float(o.get(OPT_ARBITRAGE_MARGIN, 0.5)),
            max_charge_kw=float(o[OPT_MAX_CHARGE]), max_discharge_kw=float(o[OPT_MAX_DISCHARGE]),
            roundtrip_efficiency=float(o[OPT_EFFICIENCY]), wear_cost=float(o[OPT_WEAR]),
            grid_import_kw=float(o[OPT_GRID_IMPORT]), grid_export_kw=self.grid_export_kw(),
            soc_step=int(o[OPT_SOC_STEP]),
            max_grid_charge_price=None if alert else self.max_grid_charge_price(),
            min_sell_soc=int(o[OPT_MIN_SELL_SOC]),
            charge_taper=self.charge_taper(),
            sell_min_power_kw=float(o.get(OPT_SELL_MIN_POWER, 6.0)),
            pv_trust=float(o.get(OPT_PV_TRUST, 50)) / 100,
            force_sell_above=self.force_sell_price(),
        )

    def force_sell_price(self) -> float | None:
        """Sell price from which the battery sells past the house reserve (None = off)."""
        v = float(self.opt.get(OPT_FORCE_SELL_PRICE, 0.0))
        return v if v > 0 else None

    def block_export_price(self) -> float:
        return float(self.opt.get(OPT_BLOCK_EXPORT_PRICE, 0.0))

    def grid_export_kw(self) -> float:
        """Option value, capped by the inverter's own max sell power setting when available."""
        v = float(self.opt[OPT_GRID_EXPORT])
        st = self.hass.states.get(self.inv.max_sell_power)
        try:
            inv_kw = float(st.state) / 1000 if st is not None else 0.0
        except ValueError:
            inv_kw = 0.0
        return min(v, inv_kw) if inv_kw > 0 else v

    def max_grid_charge_price(self) -> float:
        """Option value, or the lowest G13 zone price when set to 0."""
        o = self.opt
        v = float(o[OPT_MAX_CHARGE_PRICE])
        if v > 0:
            return v
        return min(float(o[OPT_PRICE_MORNING]), float(o[OPT_PRICE_AFTERNOON]), float(o[OPT_PRICE_OFF]))

    def tariff(self) -> G13Tariff:
        o = self.opt
        return G13Tariff(
            float(o[OPT_PRICE_MORNING]), float(o[OPT_PRICE_AFTERNOON]), float(o[OPT_PRICE_OFF]),
            morning_peak=_range(o[OPT_MORNING_HOURS]),
            afternoon_peak_summer=_range(o[OPT_AFTERNOON_SUMMER]),
            afternoon_peak_winter=_range(o[OPT_AFTERNOON_WINTER]),
        )

    async def _learn_taper(self) -> None:
        """Measure the BMS charge taper from the recent climbs to 100 % (raw SoC history)."""
        end = dt_util.now()
        try:
            points = await io.soc_history(self.hass, self.inv.soc, end - timedelta(days=10), end)
        except Exception as err:  # noqa: BLE001 - recorder may not be ready
            _LOGGER.debug("SoC history lookup failed: %s", err)
            return
        merged = merge_events(self.taper_events, charge_events(points))
        if merged != self.taper_events:
            self.taper_events = merged
            _LOGGER.info("Charge taper learned from %d charges: %s", len(merged),
                         taper_from_events(merged, float(self.opt[OPT_CAPACITY])))
            self._save()

    def charge_taper(self) -> tuple[tuple[int, float], ...]:
        return taper_from_events(self.taper_events, float(self.opt[OPT_CAPACITY]))

    def fallback_plan(self) -> DayPlan:
        power = int(min(12000, float(self.opt[OPT_MAX_DISCHARGE]) * 1000))
        return parse_fallback_plan(self.opt[OPT_FALLBACK_PLAN], power)

    # --- lifecycle -------------------------------------------------------
    async def async_setup(self) -> None:
        stored = await self.store.async_load() or {}
        self.pv_bias = PvBias.from_dict(stored.get("pv_bias"))
        self.ev_pattern = EvPattern.from_dict(stored.get("ev_pattern"))
        self.ledger = ShadowLedger.from_dict(stored.get("shadow"))
        self.stock = BatteryStock.from_dict(stored.get("stock"))
        self._sell_seen = self._load_sell_seen(stored.get("sell_seen"))
        self.last_learn = stored.get("last_learn")
        self.grid_energy = stored.get("grid_energy")
        self.ev_power_seen = stored.get("ev_power_seen")
        self.taper_events = stored.get("taper_events") or []
        await self._learn_taper()
        if self.grid_energy is None:  # first run: look it up in the history
            try:
                self.grid_energy = await io.grid_charged_since_low(
                    self.hass, self.inv, float(self.opt[OPT_MIN_SOC]) + 1, dt_util.now()
                )
            except Exception as err:  # noqa: BLE001 - recorder may not be ready
                _LOGGER.debug("Grid-energy history lookup failed: %s", err)
                self.grid_energy = False
        self.guard.history = [dt for t in stored.get("writes", []) if (dt := dt_util.parse_datetime(t))]
        if stored.get("last_full"):
            self.full.last_full = dt_util.parse_datetime(stored["last_full"])
        if self.full.last_full is None:  # first run: look it up in the SoC history
            end = dt_util.now()
            try:
                self.full.last_full = await io.last_hour_at_or_above(
                    self.hass, self.inv.soc, FULL_SOC, end - timedelta(days=60), end
                )
            except Exception as err:  # noqa: BLE001 - recorder may not be ready
                _LOGGER.debug("Full-charge history lookup failed: %s", err)
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
                "stock": self.stock.to_dict(),
                "sell_seen": {h.isoformat(): v for h, v in self._sell_seen.items()},
                "last_learn": self.last_learn,
                "writes": [t.isoformat() for t in self.guard.history],
                "last_full": self.full.last_full.isoformat() if self.full.last_full else None,
                "grid_energy": self.grid_energy,
                "ev_power_seen": self.ev_power_seen,
                "taper_events": self.taper_events,
            },
            5,
        )

    def _load_sell_seen(self, stored: dict | None) -> dict[datetime, float]:
        """Restore the recent hourly sell prices, dropping anything older than the window."""
        now = dt_util.now()
        out: dict[datetime, float] = {}
        for key, value in (stored or {}).items():
            dt = dt_util.parse_datetime(key)
            if dt is None or now - dt_util.as_local(dt) >= SELL_SEEN_KEEP:
                continue
            try:
                out[dt_util.as_local(dt)] = float(value)
            except (TypeError, ValueError):
                continue
        return out

    # --- 30 s loop: state + work-mode control ------------------------------
    async def _async_update_data(self) -> PrelumoData:
        now = dt_util.now()
        plan = io.read_current_plan(self.hass, self.inv)
        outage = io.is_outage(self.hass, self.conf.get(CONF_OUTAGE_ENTITY), float(self.conf[CONF_OUTAGE_THRESHOLD]))
        data = PrelumoData(current_plan=plan, outage=outage, soc=io.state_float(self.hass, self.inv.soc))
        was_due = self.full_charge_planned()
        if self.full.observe(data.soc, now):
            self._save()
            # the climb to 100 % just finished: measure how long the last % took this time
            self.config_entry.async_create_background_task(
                self.hass, self._learn_taper(), f"{DOMAIN}_learn_taper"
            )
            if was_due:  # balancing done -> back to the everyday limit at once
                self.config_entry.async_create_background_task(
                    self.hass, self.async_replan(), f"{DOMAIN}_after_full_charge"
                )
        if io.alert_active(self.hass, self.conf.get(CONF_ALERT_ENTITY)) != self.alert_on:
            # a warning appeared or ended: plan at once, do not wait for the hour
            self.config_entry.async_create_background_task(
                self.hass, self.async_replan(), f"{DOMAIN}_alert_changed"
            )
        self._maybe_recover(now)
        self._track_grid_energy(plan, now, data.soc)
        self._track_stock(now, data.soc)
        if plan is not None:
            data.active_index = plan.active_index(now)
            data.next_change, _ = plan.next_change(now)
            data.export_blocked = self._export_blocked(now)
            data.sell_now = plan.slots[data.active_index].mode.value == "sell" and not data.export_blocked
            data.desired_mode = self.writer.desired_mode(
                self._mode_plan(plan, now), now, may_export=not data.export_blocked
            )
            if outage:
                data.mode_action = "outage"
            elif not self.auto_mode:
                data.mode_action = "auto_off"
            elif self.shadow:  # dry run: never touch the inverter
                data.mode_action = "shadow"
            else:
                try:
                    changed = await self.writer.ensure_mode(data.desired_mode)
                    data.mode_action = "set" if changed else "unchanged"
                except HomeAssistantError as err:
                    data.mode_action = "error"
                    _LOGGER.warning("Work mode change failed: %s", err)
        return data

    def _mode_plan(self, read: DayPlan, now: datetime) -> DayPlan:
        """The plan the work mode follows: the written one until the read-back matches it."""
        w = self._written
        if w is None or self._written_at is None:
            return read
        if same_behaviour(read, w) or now - self._written_at > WRITE_SETTLE:
            self._written = self._written_at = None
            return read
        return w

    def _wrote(self, plan: DayPlan, now: datetime) -> None:
        self._written, self._written_at = plan, now

    @callback
    def _track_grid_energy(self, plan: DayPlan | None, now: datetime, soc: float | None) -> None:
        """Battery holds grid energy: set while charging in a grid-charge slot, cleared at min SoC."""
        new = self.grid_energy
        if soc is not None and soc <= float(self.opt[OPT_MIN_SOC]) + 1:
            new = False
        elif plan is not None and plan.slots[plan.active_index(now)].mode.value == "grid_charge":
            power = io.state_float(self.hass, self.inv.battery_power)
            if power is not None and power < -300:
                new = True
        if new != self.grid_energy:
            self.grid_energy = new
            self._save()

    @callback
    def _export_blocked(self, now: datetime) -> bool:
        """Never Export First while this hour's sell price is at or below the threshold.

        The plan already refuses to sell without a positive price, but it can be up to
        ``stale_hours`` old (or the price can be revised), so the work mode is checked against
        the live price every 30 s as well. With no price for this hour, exporting is blocked.
        """
        price = self._sell_seen.get(now.replace(minute=0, second=0, microsecond=0))
        return price is None or price <= self.block_export_price()

    # --- average cost of the energy in the battery -------------------------
    @property
    def eta(self) -> float:
        """One-way efficiency (charge or discharge)."""
        return float(self.opt[OPT_EFFICIENCY]) ** 0.5

    def battery_discharged_total(self) -> float | None:
        """Lifetime kWh taken out of the battery, as the inverter counts it."""
        return io.state_float(self.hass, self.inv.battery_discharge_total)

    def battery_cycles_total(self) -> float | None:
        """Full cycles since the inverter's discharge counter started."""
        dis = self.battery_discharged_total()
        cap = float(self.opt[OPT_CAPACITY])
        return round(dis / cap, 2) if dis is not None and cap > 0 else None

    def usable_kwh(self, soc: float) -> float:
        """Battery-side kWh above min SoC at ``soc`` % — the energy the stock accounts for."""
        o = self.opt
        return max(0.0, soc - float(o[OPT_MIN_SOC])) / 100 * float(o[OPT_CAPACITY])

    @callback
    def _track_stock(self, now: datetime, soc: float | None) -> None:
        """Book this tick's charging and discharging into ``self.stock``.

        Uses the inverter's monotonic energy counters, not the power reading: a missed tick
        then costs nothing, because the next delta covers the gap. A gap too large to price
        (restart, unavailable sensors) is skipped and the SoC anchor at the next re-plan
        repairs the amount.
        """
        if soc is not None and soc <= float(self.opt[OPT_MIN_SOC]) + 1:
            if self.stock.kwh > 0 or self.stock.estimated:
                self.stock.reset(now)
                self._save()
            return
        names = {
            "charge": self.inv.battery_charge_total,
            "discharge": self.inv.battery_discharge_total,
            "import": self.inv.grid_import_total,
            "house": self.inv.house_total,
            "pv": self.inv.pv_total,
        }
        vals = {k: io.state_float(self.hass, e) for k, e in names.items()}
        if any(v is None for v in vals.values()):
            return
        prev, self._counters = self._counters, {k: float(v) for k, v in vals.items()}
        if not prev or set(prev) != set(vals):
            return  # first tick: only a baseline, nothing to book yet
        d = {k: self._counters[k] - prev[k] for k in vals}
        if any(v < 0 for v in d.values()) or d["charge"] > MAX_TICK_KWH or d["discharge"] > MAX_TICK_KWH:
            self.stock.estimated = True  # counter reset or a gap: amount repaired by the anchor
            return
        if d["charge"] <= 0 and d["discharge"] <= 0:
            return
        eta = self.eta
        tick = account_tick(d["charge"], d["discharge"], d["import"], d["house"], d["pv"], eta)
        self.stock.discharge(tick.discharged)
        price = self.tariff().price(now) / eta  # PLN per battery-side kWh (charging loss in)
        self.stock.charge(tick.from_grid, price, from_grid=True)
        self.stock.charge(tick.charged - tick.from_grid, 0.0)  # PV is free
        # not saved here: the 30 s loop would write the store all the time. The stock is
        # persisted at every re-plan, and after a crash the SoC anchor repairs the amount.

    @callback
    def _maybe_recover(self, now: datetime) -> None:
        """No plan yet, or on the backup plan: plan as soon as the inputs are available.

        Does not rely on Home Assistant's "started" event alone - one slow integration can
        hold startup for a long time.
        """
        ps = self.plan_state
        no_plan = ps.computed_at is None
        if not (ps.fallback_active or no_plan) or self._plan_lock.locked():
            return
        if self._last_recover and now - self._last_recover < timedelta(minutes=5):
            return
        if (no_plan or "battery_soc" in ps.stale_sources) and io.state_float(self.hass, self.inv.soc) is None:
            return
        self._last_recover = now
        self.config_entry.async_create_background_task(self.hass, self.async_replan(), f"{DOMAIN}_recover")

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
        """Models: at every start (they live in memory only) and once per day after that.

        Once per day also: PV bias from yesterday + metered actual cost for the shadow ledger.
        """
        today = now.date().isoformat()
        if not self._models_ready or self.last_learn != today:
            await self._learn(now)
            self._models_ready = True
        if self.last_learn == today:
            return
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
        house = await io.hourly_energy_change(self.hass, self.inv.house_total, y0, y1)
        pv = await io.hourly_energy_change(self.hass, self.inv.pv_total, y0, y1)
        dis = await io.hourly_energy_change(self.hass, self.inv.battery_discharge_total, y0, y1)
        if house or imp or dis:
            self.ledger.record_energy(
                y0.date(), house=sum(house.values()), pv=sum(pv.values()),
                grid_import=sum(imp.values()), grid_export=sum(exp.values()),
                discharged=sum(dis.values()),
            )
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
        else:
            # the SoC is the truth about how much is in the battery; the counters only say
            # where it came from. Seed price on the very first run: the cheapest tariff zone.
            self.stock.rescale(self.usable_kwh(soc), seed_price=self.max_grid_charge_price() / self.eta)

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
        self._sell_seen = {h: v for h, v in self._sell_seen.items() if now - h < SELL_SEEN_KEEP}
        if not any(h in sell_h for h in hours[:12]):
            stale.append("sell_prices")

        self.alert_on = io.alert_active(self.hass, c.get(CONF_ALERT_ENTITY))
        self.planned_outage = await self._next_planned_outage(now, hours[-1])

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

        alert = self.alert_charge()
        inp = PlannerInputs(now, soc, pv, base, hp, ev, sell_h, self.tariff(), horizon, SLOT_WINDOW_H,
                            bool(self.grid_energy), full_by_h=alert.by_hours if alert else None)
        params = self.battery_params()
        result = await self.hass.async_add_executor_job(
            run_planner, inp, params, self.strategy, self.arbitrage, current
        )
        ps.result, ps.proposed, ps.ev = result, result.plan, ev
        ps.pv, ps.load, ps.heat_pump = [pv[h] for h in hours], base, hp
        self.ledger.record_plan(now.date(), result.slot_plan_cost, result.baseline_cost)
        await self._maybe_write(current, result.plan, now, force_write, result.gain_vs_current, result.rules)

    async def _next_planned_outage(self, now: datetime, until: datetime) -> datetime | None:
        """Start of the next announced outage in the horizon (any event in the outage calendar)."""
        events = await io.read_calendar(self.hass, self.conf.get(CONF_OUTAGE_CALENDAR), now, until)
        starts = [dt for dt, _ in events if dt >= now]
        return min(starts) if starts else None

    def _ev_charger_kw(self) -> float:
        """Real charging power: the live reading while charging (remembered), else the last one
        seen, else the option. The car often charges below the charger rating."""
        live = io.state_float(self.hass, self.conf.get(CONF_EV_POWER))
        if live is not None:
            unit = (self.hass.states.get(self.conf[CONF_EV_POWER]).attributes.get("unit_of_measurement") or "kW")
            kw = live / 1000 if unit == "W" else live
            if kw > 0.5:
                if self.ev_power_seen is None or abs(kw - self.ev_power_seen) >= 0.1:
                    self.ev_power_seen = round(kw, 2)
                    self._save()
                return kw
        if self.ev_power_seen:
            return self.ev_power_seen
        return float(self.opt[OPT_EV_CHARGER_KW])

    async def _ev_forecast(self, now: datetime, hours: list[datetime], base: list[float]) -> EvForecast:
        c, o = self.conf, self.opt
        limit = io.state_float(self.hass, c.get(CONF_EV_LIMIT))
        plugged_st = self.hass.states.get(c[CONF_EV_PLUGGED]) if c.get(CONF_EV_PLUGGED) else None
        car = CarState(
            soc=io.state_float(self.hass, c.get(CONF_EV_SOC)),
            limit=limit if limit is not None else float(o[OPT_EV_DEFAULT_TARGET]),
            plugged=bool(plugged_st and plugged_st.state == "on"),
            capacity_kwh=float(o[OPT_EV_CAPACITY]),
            charger_kw=self._ev_charger_kw(),
        )
        manual = io.read_manual_target(self.hass, c.get(CONF_EV_MANUAL_SOC), c.get(CONF_EV_MANUAL_READY), now)
        cal = await io.read_calendar(self.hass, c.get(CONF_EV_CALENDAR), now, hours[-1] + timedelta(hours=1))
        return forecast_ev(
            hours, now, car, self.ev_pattern, manual, cal,
            grid_import_kw=float(o[OPT_GRID_IMPORT]), base_load=base,
            default_target_soc=float(o[OPT_EV_DEFAULT_TARGET]),
        )

    async def _maybe_write(
        self, current: DayPlan | None, new: DayPlan, now: datetime, force: bool, gain: float | None = None,
        rules: RuleCheck | None = None,
    ) -> None:
        ps = self.plan_state
        self.guard.policy = self._policy()
        if self.shadow and not force:
            ps.write_decision = ["shadow_mode"]
            return
        if not self.auto_mode and not force:
            ps.write_decision = ["auto_mode_off"]
            return
        ok, reasons = self.guard.decide(current, new, now, gain, rules)
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
        self._wrote(new, now)
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
    # --- manual schedule draft -------------------------------------------
    def draft_slots(self) -> list[Slot]:
        """The draft being edited, or the schedule now on the inverter."""
        if self._draft is not None:
            return self._draft
        cur = io.read_current_plan(self.hass, self.inv)
        if cur is not None:
            return list(cur.slots)
        return [Slot(h * 400, MAX_DRAFT_POWER, 20) for h in range(SLOT_COUNT)]

    @property
    def draft_dirty(self) -> bool:
        return self._draft is not None

    @callback
    def draft_set(self, index: int, **changes: Any) -> None:
        slots = self.draft_slots()
        mode = changes.pop("mode", None)
        slot = replace(slots[index], **changes)
        if mode is not None:
            slot = slot.with_mode(SlotMode(mode))
        slots = list(slots)
        slots[index] = slot
        self._draft = slots
        self.async_update_listeners()

    @callback
    def draft_reset(self) -> None:
        """Back to the schedule on the inverter."""
        self._draft = None
        self.async_update_listeners()

    async def async_write_draft(self) -> None:
        try:
            plan = DayPlan(tuple(self.draft_slots()))
        except PlanError as err:
            raise HomeAssistantError(f"Harmonogram niepoprawny: {err}") from err
        await self.async_apply(plan)
        self.draft_reset()

    async def async_apply(self, plan: DayPlan) -> None:
        current = io.read_current_plan(self.hass, self.inv)
        merged = plan.merge_preserved_flags(current)
        await self.writer.write_plan(merged)
        now = dt_util.now()
        self.guard.record(now)
        self._wrote(merged, now)
        self.plan_state.last_write = now
        self._save()
        await self.async_request_refresh()
