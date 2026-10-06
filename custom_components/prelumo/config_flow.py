"""Config and options flow for Prelumo."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_EV_CALENDAR, CONF_EV_LIMIT, CONF_EV_MANUAL_READY, CONF_EV_MANUAL_SOC, CONF_EV_PLUGGED,
    CONF_EV_POWER, CONF_EV_SOC, CONF_HP_POWER, CONF_INVERTER_PREFIX, CONF_MODE_EXPORT_FIRST,
    CONF_MODE_SELECT, CONF_MODE_ZERO_EXPORT, CONF_OUTAGE_ENTITY, CONF_OUTAGE_THRESHOLD,
    CONF_RCE_TODAY, CONF_RCE_TOMORROW, CONF_SOLCAST_TODAY, CONF_SOLCAST_TOMORROW, CONF_WEATHER,
    CONF_WRITE_ACTION, DEFAULT_EXPORT_FIRST, DEFAULT_MODE_SELECT, DEFAULT_PREFIX,
    DEFAULT_WRITE_ACTION, DEFAULT_ZERO_EXPORT, DEFAULTS, DOMAIN, OPT_AFTERNOON_SUMMER,
    OPT_AFTERNOON_WINTER, OPT_CAPACITY, OPT_EFFICIENCY, OPT_EV_CAPACITY, OPT_EV_CHARGER_KW,
    OPT_EV_DEFAULT_TARGET, OPT_FALLBACK_PLAN, OPT_GRID_EXPORT, OPT_GRID_IMPORT, OPT_HISTORY_WEEKS,
    OPT_HORIZON, OPT_HP_BASE_TEMP, OPT_MAX_CHARGE, OPT_MAX_DISCHARGE, OPT_MAX_SOC, OPT_MAX_WRITES,
    OPT_MIN_SOC, OPT_MORNING_HOURS, OPT_PRICE_AFTERNOON, OPT_PRICE_MORNING, OPT_PRICE_OFF,
    OPT_SOC_STEP, OPT_SOC_TOLERANCE, OPT_STALE_HOURS, OPT_WEAR, OPTION_DEFAULTS,
)
from .core.plan import PlanError
from .core.planner import parse_fallback_plan


def _ent(domain: str | list[str]) -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


def _num(lo: float, hi: float, step: float, unit: str | None = None) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=lo, max=hi, step=step, unit_of_measurement=unit,
                                      mode=selector.NumberSelectorMode.BOX)
    )


def _opt(key: str, data: dict) -> Any:
    """vol.Optional with a suggested value (so cleared fields stay cleared)."""
    return vol.Optional(key, description={"suggested_value": data.get(key, DEFAULTS.get(key))})


def _hours_range(text: str) -> bool:
    try:
        a, b = (int(x) for x in text.split("-"))
    except ValueError:
        return False
    return 0 <= a < b <= 24


class PrelumoConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            prefix = user_input[CONF_INVERTER_PREFIX]
            if self.hass.states.get(f"sensor.{prefix}_bateria_soc") is None:
                errors[CONF_INVERTER_PREFIX] = "inverter_not_found"
            elif "." not in user_input[CONF_WRITE_ACTION]:
                errors[CONF_WRITE_ACTION] = "invalid_action"
            else:
                self._data.update(user_input)
                return await self.async_step_sources()
        schema = vol.Schema({
            vol.Required(CONF_INVERTER_PREFIX, default=DEFAULT_PREFIX): str,
            vol.Required(CONF_WRITE_ACTION, default=DEFAULT_WRITE_ACTION): str,
            vol.Required(CONF_MODE_SELECT, default=DEFAULT_MODE_SELECT): _ent("select"),
            vol.Required(CONF_MODE_EXPORT_FIRST, default=DEFAULT_EXPORT_FIRST): str,
            vol.Required(CONF_MODE_ZERO_EXPORT, default=DEFAULT_ZERO_EXPORT): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_sources(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_ev()
        d = self._data
        schema = vol.Schema({
            _opt(CONF_RCE_TODAY, d): _ent("sensor"),
            _opt(CONF_RCE_TOMORROW, d): _ent("sensor"),
            _opt(CONF_SOLCAST_TODAY, d): _ent("sensor"),
            _opt(CONF_SOLCAST_TOMORROW, d): _ent("sensor"),
            _opt(CONF_WEATHER, d): _ent("weather"),
            _opt(CONF_HP_POWER, d): _ent("sensor"),
            _opt(CONF_OUTAGE_ENTITY, d): _ent(["sensor", "binary_sensor"]),
            vol.Optional(CONF_OUTAGE_THRESHOLD, default=DEFAULTS[CONF_OUTAGE_THRESHOLD]): _num(0, 300, 1, "V"),
        })
        return self.async_show_form(step_id="sources", data_schema=schema)

    async def async_step_ev(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="Prelumo", data=self._data, options=dict(OPTION_DEFAULTS))
        d = self._data
        schema = vol.Schema({
            _opt(CONF_EV_SOC, d): _ent("sensor"),
            _opt(CONF_EV_LIMIT, d): _ent(["number", "sensor"]),
            _opt(CONF_EV_PLUGGED, d): _ent("binary_sensor"),
            _opt(CONF_EV_POWER, d): _ent("sensor"),
            _opt(CONF_EV_CALENDAR, d): _ent("calendar"),
            _opt(CONF_EV_MANUAL_SOC, d): _ent("input_number"),
            _opt(CONF_EV_MANUAL_READY, d): _ent("input_datetime"),
        })
        return self.async_show_form(step_id="ev", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PrelumoOptionsFlow()


class PrelumoOptionsFlow(OptionsFlow):
    """Battery, tariff, EV, planner and backup-plan parameters."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            for key in (OPT_MORNING_HOURS, OPT_AFTERNOON_SUMMER, OPT_AFTERNOON_WINTER):
                if not _hours_range(user_input[key]):
                    errors[key] = "invalid_hours"
            try:
                parse_fallback_plan(user_input[OPT_FALLBACK_PLAN])
            except (PlanError, ValueError):
                errors[OPT_FALLBACK_PLAN] = "invalid_plan"
            if user_input[OPT_MIN_SOC] >= user_input[OPT_MAX_SOC]:
                errors[OPT_MIN_SOC] = "invalid_soc_range"
            if not errors:
                return self.async_create_entry(data=user_input)
        o = {**OPTION_DEFAULTS, **self.config_entry.options, **(user_input or {})}

        def req(key: str) -> vol.Required:
            return vol.Required(key, default=o[key])

        schema = vol.Schema({
            req(OPT_CAPACITY): _num(1, 200, 0.1, "kWh"),
            req(OPT_MIN_SOC): _num(0, 100, 1, "%"),
            req(OPT_MAX_SOC): _num(0, 100, 1, "%"),
            req(OPT_MAX_CHARGE): _num(0.5, 30, 0.1, "kW"),
            req(OPT_MAX_DISCHARGE): _num(0.5, 30, 0.1, "kW"),
            req(OPT_EFFICIENCY): _num(0.5, 1, 0.01),
            req(OPT_WEAR): _num(0, 2, 0.01, "PLN/kWh"),
            req(OPT_GRID_IMPORT): _num(1, 100, 0.1, "kW"),
            req(OPT_GRID_EXPORT): _num(0, 100, 0.1, "kW"),
            req(OPT_PRICE_MORNING): _num(0, 5, 0.001, "PLN/kWh"),
            req(OPT_PRICE_AFTERNOON): _num(0, 5, 0.001, "PLN/kWh"),
            req(OPT_PRICE_OFF): _num(0, 5, 0.001, "PLN/kWh"),
            req(OPT_MORNING_HOURS): str,
            req(OPT_AFTERNOON_SUMMER): str,
            req(OPT_AFTERNOON_WINTER): str,
            req(OPT_EV_CAPACITY): _num(10, 200, 0.5, "kWh"),
            req(OPT_EV_CHARGER_KW): _num(1, 22, 0.1, "kW"),
            req(OPT_EV_DEFAULT_TARGET): _num(20, 100, 1, "%"),
            req(OPT_HP_BASE_TEMP): _num(5, 25, 0.5, "°C"),
            req(OPT_HISTORY_WEEKS): _num(1, 26, 1),
            req(OPT_HORIZON): _num(24, 48, 1, "h"),
            req(OPT_SOC_STEP): _num(1, 10, 1, "%"),
            req(OPT_MAX_WRITES): _num(1, 24, 1),
            req(OPT_SOC_TOLERANCE): _num(1, 50, 1, "%"),
            req(OPT_STALE_HOURS): _num(1, 48, 1, "h"),
            req(OPT_FALLBACK_PLAN): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
