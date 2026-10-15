"""Manual schedule draft: load from / write to the inverter."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PrelumoConfigEntry
from .entity import PrelumoEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: PrelumoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    async_add_entities([DraftLoad(c, "draft_load"), DraftWrite(c, "draft_write")])


class DraftLoad(PrelumoEntity, ButtonEntity):
    _platform = "button"

    async def async_press(self) -> None:
        self.coordinator.draft_reset()


class DraftWrite(PrelumoEntity, ButtonEntity):
    _platform = "button"

    async def async_press(self) -> None:
        await self.coordinator.async_write_draft()
