"""Prelumo sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PrelumoConfigEntry
from .coordinator import PrelumoCoordinator
from .entity import PrelumoEntity


@dataclass(frozen=True, kw_only=True)
class PrelumoSensorDescription(SensorEntityDescription):
    value: Callable[[PrelumoCoordinator], Any]
    attrs: Callable[[PrelumoCoordinator], dict[str, Any] | None] = lambda c: None


def _active_slot(c: PrelumoCoordinator) -> str | None:
    d = c.data
    if d is None or d.current_plan is None or d.active_index is None:
        return None
    return d.current_plan.slots[d.active_index].mode.value


def _plan_attrs(c: PrelumoCoordinator) -> dict[str, Any] | None:
    d = c.data
    if d is None or d.current_plan is None:
        return None
    return {"slot": (d.active_index or 0) + 1, "slots": d.current_plan.as_list(),
            "soc": d.current_plan.slots[d.active_index or 0].soc,
            "desired_mode": d.desired_mode, "mode_action": d.mode_action}


def _proposed(c: PrelumoCoordinator) -> str | None:
    ps = c.plan_state
    if ps.fallback_active:
        return "fallback"
    if ps.error:
        return "error"
    return "ready" if ps.proposed else None


def _proposed_attrs(c: PrelumoCoordinator) -> dict[str, Any]:
    ps = c.plan_state
    r = ps.result
    return {
        "computed_at": ps.computed_at.isoformat() if ps.computed_at else None,
        "strategy": c.strategy.value,
        "grid_arbitrage": c.arbitrage,
        "max_grid_charge_price": round(c.max_grid_charge_price(), 3),
        "min_sell_soc": int(c.opt["min_sell_soc"]),
        "shadow_mode": c.shadow,
        "slots": ps.proposed.as_list() if ps.proposed else None,
        "hourly": [h.as_dict() for h in r.optimum.hours] if r else None,
        "expected_cost": round(r.slot_plan_cost, 2) if r else None,
        "baseline_cost": round(r.baseline_cost, 2) if r else None,
        "current_plan_cost": round(r.current_plan_cost, 2) if r and r.current_plan_cost is not None else None,
        "gain_vs_current": round(r.gain_vs_current, 2) if r and r.gain_vs_current is not None else None,
        "notes": r.notes if r else None,
        "write_decision": ps.write_decision,
        "last_write": ps.last_write.isoformat() if ps.last_write else None,
        "stale_sources": ps.stale_sources,
        "error": ps.error,
    }


def _series(c: PrelumoCoordinator, values: list[float]) -> dict[str, Any]:
    return {"hourly": [{"start": h.isoformat(), "kwh": round(v, 3)}
                       for h, v in zip(c.plan_state.hours, values)]}


def _sum24(values: list[float]) -> float | None:
    return round(sum(values[:24]), 2) if values else None


SENSORS: tuple[PrelumoSensorDescription, ...] = (
    PrelumoSensorDescription(
        key="active_slot", device_class=SensorDeviceClass.ENUM,
        options=["none", "grid_charge", "sell"], value=_active_slot, attrs=_plan_attrs,
    ),
    PrelumoSensorDescription(
        key="next_change", device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda c: c.data.next_change if c.data else None,
    ),
    PrelumoSensorDescription(
        key="proposed_plan", device_class=SensorDeviceClass.ENUM,
        options=["ready", "fallback", "error"], value=_proposed, attrs=_proposed_attrs,
    ),
    PrelumoSensorDescription(
        key="expected_savings", native_unit_of_measurement="PLN", suggested_display_precision=2,
        value=lambda c: round(c.plan_state.result.expected_savings, 2) if c.plan_state.result else None,
    ),
    PrelumoSensorDescription(
        key="shadow_savings", native_unit_of_measurement="PLN", suggested_display_precision=2,
        state_class=SensorStateClass.TOTAL,
        value=lambda c: c.ledger.total_expected_savings(),
        attrs=lambda c: {"days": c.ledger.days},
    ),
    PrelumoSensorDescription(
        key="ev_energy_needed", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY, suggested_display_precision=1,
        value=lambda c: round(c.plan_state.ev.total_kwh, 2) if c.plan_state.ev else None,
        attrs=lambda c: {
            "needs": [{"kwh": round(n.energy_kwh, 1), "ready_by": n.ready_by.isoformat() if n.ready_by else None,
                       "source": n.source, "probability": n.probability} for n in c.plan_state.ev.needs],
            "battery_allowed_hours": sum(c.plan_state.ev.battery_allowed),
            "pattern": c.ev_pattern.to_dict(),
        } if c.plan_state.ev else None,
    ),
    PrelumoSensorDescription(
        key="load_forecast", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY, suggested_display_precision=1,
        value=lambda c: _sum24([a + b for a, b in zip(c.plan_state.load, c.plan_state.heat_pump)]),
        attrs=lambda c: {**_series(c, c.plan_state.load),
                         "heat_pump": [round(v, 3) for v in c.plan_state.heat_pump],
                         "heat_pump_model": c.hp_model.to_dict()},
    ),
    PrelumoSensorDescription(
        key="pv_forecast_corrected", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY, suggested_display_precision=1,
        value=lambda c: _sum24(c.plan_state.pv),
        attrs=lambda c: {**_series(c, c.plan_state.pv), "bias": c.pv_bias.factors},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: PrelumoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(PrelumoSensor(entry.runtime_data, d) for d in SENSORS)


class PrelumoSensor(PrelumoEntity, SensorEntity):
    _platform = "sensor"
    entity_description: PrelumoSensorDescription

    def __init__(self, coordinator: PrelumoCoordinator, description: PrelumoSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.entity_description.attrs(self.coordinator)
