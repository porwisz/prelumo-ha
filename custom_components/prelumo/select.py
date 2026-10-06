"""Strategy select (restored across restarts)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import PrelumoConfigEntry
from .const import STRATEGY_OPTIONS
from .core.optimizer import Strategy
from .entity import PrelumoEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: PrelumoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([StrategySelect(entry.runtime_data, "strategy")])


class StrategySelect(PrelumoEntity, SelectEntity, RestoreEntity):
    _platform = "select"
    _attr_options = STRATEGY_OPTIONS

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in STRATEGY_OPTIONS:
            self.coordinator.strategy = Strategy(last.state)

    @property
    def current_option(self) -> str:
        return self.coordinator.strategy.value

    async def async_select_option(self, option: str) -> None:
        self.coordinator.strategy = Strategy(option)
        self.async_write_ha_state()
        self.hass.async_create_task(self.coordinator.async_replan())
