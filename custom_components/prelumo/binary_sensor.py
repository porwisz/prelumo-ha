"""Prelumo binary sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PrelumoConfigEntry
from .entity import PrelumoEntity as _Base


class PrelumoEntity(_Base):
    _platform = "binary_sensor"


async def async_setup_entry(
    hass: HomeAssistant, entry: PrelumoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    async_add_entities([
        SellNow(c, "sell_now"), GridOutage(c, "grid_outage"), FallbackActive(c, "fallback_active"),
        FullChargeDue(c, "full_charge_due"), EmergencyCharge(c, "alert_charge"),
    ])


class SellNow(PrelumoEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.sell_now if self.coordinator.data else None


class GridOutage(PrelumoEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.outage if self.coordinator.data else None


class FallbackActive(PrelumoEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool:
        return self.coordinator.plan_state.fallback_active

    @property
    def extra_state_attributes(self) -> dict:
        return {"stale_sources": self.coordinator.plan_state.stale_sources}


class FullChargeDue(PrelumoEntity, BinarySensorEntity):
    """On while the periodic 100 % charge is due (Prelumo then plans it)."""

    @property
    def is_on(self) -> bool:
        return self.coordinator.full_charge_due()


class EmergencyCharge(PrelumoEntity, BinarySensorEntity):
    """On while Prelumo charges for a coming power cut (weather warning or announced outage)."""

    @property
    def is_on(self) -> bool:
        return self.coordinator.alert_charge() is not None

    @property
    def extra_state_attributes(self) -> dict:
        a = self.coordinator.alert_charge()
        return {
            "reason": a.reason if a else None,
            "target_soc": a.soc if a else None,
            "hours_left": a.by_hours if a else None,
            "weather_alert": self.coordinator.alert_on,
            "planned_outage": (
                self.coordinator.planned_outage.isoformat() if self.coordinator.planned_outage else None
            ),
        }
