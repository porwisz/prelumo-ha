"""Write side: schedule (one ESPHome action) and work mode select. Never cyclic."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..core.plan import DayPlan, SlotMode

_LOGGER = logging.getLogger(__name__)


class InverterWriter:
    def __init__(
        self, hass: HomeAssistant, write_action: str, mode_select: str,
        export_first: str, zero_export: str,
    ) -> None:
        self.hass = hass
        self.domain, self.service = write_action.split(".", 1)
        self.mode_select = mode_select
        self.export_first = export_first
        self.zero_export = zero_export

    async def write_plan(self, plan: DayPlan) -> None:
        if not self.hass.services.has_service(self.domain, self.service):
            raise HomeAssistantError(f"Action {self.domain}.{self.service} not available")
        _LOGGER.info("Writing schedule: %s", plan.to_service_data())
        await self.hass.services.async_call(self.domain, self.service, plan.to_service_data(), blocking=True)

    def desired_mode(self, plan: DayPlan, now) -> str:
        return self.export_first if plan.active_slot(now).mode is SlotMode.SELL else self.zero_export

    def current_mode(self) -> str | None:
        st = self.hass.states.get(self.mode_select)
        return st.state if st else None

    async def ensure_mode(self, option: str) -> bool:
        """Select ``option`` only if it differs (register 142 lives in flash)."""
        cur = self.current_mode()
        if cur is None or cur == option or cur in ("unavailable", "unknown"):
            return False
        _LOGGER.info("Work mode %s -> %s", cur, option)
        await self.hass.services.async_call(
            "select", "select_option", {"entity_id": self.mode_select, "option": option}, blocking=True
        )
        return True
