"""Manual schedule draft: slot SoC and power."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PrelumoConfigEntry
from .core.plan import MAX_POWER_W, SLOT_COUNT
from .entity import PrelumoSlotEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: PrelumoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    async_add_entities(
        [SlotSoc(c, n, "soc") for n in range(1, SLOT_COUNT + 1)]
        + [SlotPower(c, n, "power") for n in range(1, SLOT_COUNT + 1)]
    )


class SlotSoc(PrelumoSlotEntity, NumberEntity):
    _platform = "number"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return self.slot.soc

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.draft_set(self._index, soc=int(round(value)))


class SlotPower(PrelumoSlotEntity, NumberEntity):
    """Slot power in kW (the inverter takes W): caps ALL battery output in the slot."""

    _platform = "number"
    _attr_native_min_value = 0
    _attr_native_max_value = MAX_POWER_W / 1000
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return round(self.slot.power / 1000, 1)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.draft_set(self._index, power=int(round(value * 10)) * 100)
