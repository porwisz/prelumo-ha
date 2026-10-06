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
    async_add_entities([SellNow(c, "sell_now"), GridOutage(c, "grid_outage"), FallbackActive(c, "fallback_active")])


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
