"""Write side: schedule and clock (ESPHome actions) and work mode select. Never cyclic."""

from __future__ import annotations

import logging
from datetime import datetime

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
        # the clock action sits next to the schedule one on the same ESPHome device
        self.clock_service = self.service.removesuffix("zapisz_harmonogram") + "ustaw_zegar"

    async def write_plan(self, plan: DayPlan) -> None:
        if not self.hass.services.has_service(self.domain, self.service):
            raise HomeAssistantError(f"Action {self.domain}.{self.service} not available")
        _LOGGER.info("Writing schedule: %s", plan.to_service_data())
        await self.hass.services.async_call(self.domain, self.service, plan.to_service_data(), blocking=True)

    def can_set_clock(self) -> bool:
        return self.hass.services.has_service(self.domain, self.clock_service)

    async def set_clock(self, t: datetime) -> None:
        """Set the inverter clock to wall-clock ``t`` (local time, no zone, like its read-out)."""
        _LOGGER.info("Setting inverter clock to %s", t.strftime("%Y-%m-%d %H:%M:%S"))
        await self.hass.services.async_call(self.domain, self.clock_service, {
            "rok": t.year, "miesiac": t.month, "dzien": t.day,
            "godzina": t.hour, "minuta": t.minute, "sekunda": t.second,
        }, blocking=True)

    def desired_mode(self, plan: DayPlan, now, may_export: bool = True) -> str:
        """Export First only in a sell slot, and only while exporting is allowed right now."""
        sell = plan.active_slot(now).mode is SlotMode.SELL
        return self.export_first if sell and may_export else self.zero_export

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
