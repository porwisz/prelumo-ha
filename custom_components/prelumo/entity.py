"""Base entity for Prelumo."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, TAGLINE
from .coordinator import PrelumoCoordinator


class PrelumoEntity(CoordinatorEntity[PrelumoCoordinator]):
    _attr_has_entity_name = True
    _platform: str  # set by each platform class; gives stable, language-independent entity ids

    def __init__(self, coordinator: PrelumoCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}"
        self.entity_id = f"{self._platform}.{DOMAIN}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name="Prelumo",
            manufacturer="Prelumo",
            model=TAGLINE,
            entry_type=DeviceEntryType.SERVICE,
        )
