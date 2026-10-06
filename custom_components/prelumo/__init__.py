"""Prelumo — one step ahead of the sun.

Plans and controls a hybrid inverter's battery schedule via Home Assistant entities and actions.
"""

from __future__ import annotations

from datetime import time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN, PLATFORMS, SERVICE_APPLY_PLAN, SERVICE_READ_PLAN, SERVICE_REPLAN, SERVICE_SET_SLOT,
)
from .coordinator import PrelumoCoordinator
from .core.plan import MAX_POWER_W, DayPlan, PlanError, Slot, SlotMode
from .ha_io.inputs import read_current_plan

type PrelumoConfigEntry = ConfigEntry[PrelumoCoordinator]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SLOT_SCHEMA = vol.Schema({
    vol.Required("start"): cv.time,
    vol.Required("soc"): vol.All(vol.Coerce(int), vol.Range(0, 100)),
    vol.Optional("power"): vol.All(vol.Coerce(int), vol.Range(0, MAX_POWER_W)),
    vol.Optional("mode", default="none"): vol.In([m.value for m in SlotMode]),
})
APPLY_SCHEMA = vol.Schema({
    vol.Optional("slots"): vol.All(cv.ensure_list, [SLOT_SCHEMA], vol.Length(min=6, max=6)),
    vol.Optional("source", default="proposed"): vol.In(["proposed", "fallback"]),
})
SET_SLOT_SCHEMA = vol.Schema({
    vol.Required("slot"): vol.All(vol.Coerce(int), vol.Range(1, 6)),
    vol.Optional("start"): cv.time,
    vol.Optional("soc"): vol.All(vol.Coerce(int), vol.Range(0, 100)),
    vol.Optional("power"): vol.All(vol.Coerce(int), vol.Range(0, MAX_POWER_W)),
    vol.Optional("mode"): vol.In([m.value for m in SlotMode]),
})
REPLAN_SCHEMA = vol.Schema({vol.Optional("write", default=False): cv.boolean})


def _coordinator(hass: HomeAssistant) -> PrelumoCoordinator:
    entries = [e for e in hass.config_entries.async_loaded_entries(DOMAIN)]
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
    return entries[0].runtime_data


def _hhmm(t: time) -> int:
    return t.hour * 100 + t.minute


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    async def apply_plan(call: ServiceCall) -> None:
        coord = _coordinator(hass)
        try:
            if "slots" in call.data:
                default_power = int(min(MAX_POWER_W, coord.battery_params().max_discharge_kw * 1000))
                plan = DayPlan(tuple(
                    Slot(_hhmm(s["start"]), s.get("power", default_power), s["soc"]).with_mode(SlotMode(s["mode"]))
                    for s in call.data["slots"]
                ))
            elif call.data["source"] == "fallback":
                plan = coord.fallback_plan()
            else:
                plan = coord.plan_state.proposed
                if plan is None:
                    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_plan")
            await coord.async_apply(plan)
        except PlanError as err:
            raise ServiceValidationError(str(err)) from err

    async def read_plan(call: ServiceCall) -> ServiceResponse:
        coord = _coordinator(hass)
        plan = read_current_plan(hass, coord.inv)
        ps = coord.plan_state
        return {
            "current": plan.as_list() if plan else None,
            "proposed": ps.proposed.as_list() if ps.proposed else None,
            "fallback_active": ps.fallback_active,
            "write_decision": ps.write_decision,
        }

    async def set_slot(call: ServiceCall) -> None:
        coord = _coordinator(hass)
        plan = read_current_plan(hass, coord.inv)
        if plan is None:
            raise HomeAssistantError("Current inverter schedule is not readable")
        idx = call.data["slot"] - 1
        s = plan.slots[idx]
        new = Slot(
            _hhmm(call.data["start"]) if "start" in call.data else s.start,
            call.data.get("power", s.power), call.data.get("soc", s.soc), s.flags,
        )
        if "mode" in call.data:
            new = new.with_mode(SlotMode(call.data["mode"]))
        try:
            await coord.async_apply(plan.with_slot(idx, new))
        except PlanError as err:
            raise ServiceValidationError(str(err)) from err

    async def replan(call: ServiceCall) -> ServiceResponse:
        coord = _coordinator(hass)
        ps = await coord.async_replan(force_write=call.data["write"])
        return {
            "proposed": ps.proposed.as_list() if ps.proposed else None,
            "expected_savings": round(ps.result.expected_savings, 2) if ps.result else None,
            "write_decision": ps.write_decision,
            "stale_sources": ps.stale_sources,
            "error": ps.error,
        }

    hass.services.async_register(DOMAIN, SERVICE_APPLY_PLAN, apply_plan, APPLY_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_READ_PLAN, read_plan, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SERVICE_SET_SLOT, set_slot, SET_SLOT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_REPLAN, replan, REPLAN_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PrelumoConfigEntry) -> bool:
    coord = PrelumoCoordinator(hass, entry)
    await coord.async_setup()
    await coord.async_config_entry_first_refresh()
    entry.runtime_data = coord
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    # first plan once HA has started (sources from other integrations are loaded by then)
    async def _initial_plan(_event: Any = None) -> None:
        entry.async_create_background_task(hass, coord.async_replan(), f"{DOMAIN}_initial_plan")

    if hass.state is CoreState.running:
        await _initial_plan()
    else:
        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _initial_plan))
    return True


async def _reload(hass: HomeAssistant, entry: PrelumoConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PrelumoConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_shutdown()
    return ok
