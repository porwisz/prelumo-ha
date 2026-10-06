"""Switches: automatic control, shadow mode, grid arbitrage (all restored)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import PrelumoConfigEntry
from .coordinator import PrelumoCoordinator
from .entity import PrelumoEntity

# key -> (coordinator attribute, default, replan on change)
SWITCHES = {
    "auto_mode": ("auto_mode", True, False),
    "shadow_mode": ("shadow", True, True),
    "grid_arbitrage": ("arbitrage", False, True),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: PrelumoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(PrelumoSwitch(entry.runtime_data, k) for k in SWITCHES)


class PrelumoSwitch(PrelumoEntity, SwitchEntity, RestoreEntity):
    _platform = "switch"
    def __init__(self, coordinator: PrelumoCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._field, self._default, self._replan = SWITCHES[key]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        setattr(self.coordinator, self._field, (last.state == STATE_ON) if last else self._default)

    @property
    def is_on(self) -> bool:
        return bool(getattr(self.coordinator, self._field))

    async def _set(self, value: bool) -> None:
        setattr(self.coordinator, self._field, value)
        self.async_write_ha_state()
        if self._replan:
            self.hass.async_create_task(self.coordinator.async_replan())

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
