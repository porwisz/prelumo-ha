"""Manual schedule draft: slot start times."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PrelumoConfigEntry
from .core.plan import SLOT_COUNT
from .entity import PrelumoSlotEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: PrelumoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(SlotStart(entry.runtime_data, n, "start") for n in range(1, SLOT_COUNT + 1))


class SlotStart(PrelumoSlotEntity, TimeEntity):
    _platform = "time"

    @property
    def native_value(self) -> time:
        return self.slot.start_time

    async def async_set_value(self, value: time) -> None:
        self.coordinator.draft_set(self._index, start=value.hour * 100 + value.minute)
