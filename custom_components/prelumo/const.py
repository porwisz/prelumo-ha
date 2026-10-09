"""Constants for Prelumo."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "prelumo"
TAGLINE: Final = "Prelumo — one step ahead of the sun"

PLATFORMS: Final = ["sensor", "binary_sensor", "select", "switch"]

UPDATE_INTERVAL: Final = timedelta(seconds=30)
PLAN_MINUTE: Final = 3  # hourly re-plan at HH:03
STORAGE_VERSION: Final = 1

# --- config (setup) ---------------------------------------------------
CONF_INVERTER_PREFIX: Final = "inverter_prefix"
CONF_WRITE_ACTION: Final = "write_action"
CONF_MODE_SELECT: Final = "mode_select"
CONF_MODE_EXPORT_FIRST: Final = "mode_export_first"
CONF_MODE_ZERO_EXPORT: Final = "mode_zero_export"
CONF_RCE_TODAY: Final = "rce_today"
CONF_RCE_TOMORROW: Final = "rce_tomorrow"
CONF_SOLCAST_TODAY: Final = "solcast_today"
CONF_SOLCAST_TOMORROW: Final = "solcast_tomorrow"
CONF_WEATHER: Final = "weather"
CONF_HP_POWER: Final = "heat_pump_power"
CONF_EV_SOC: Final = "ev_soc"
CONF_EV_LIMIT: Final = "ev_limit"
CONF_EV_PLUGGED: Final = "ev_plugged"
CONF_EV_POWER: Final = "ev_power"
CONF_EV_CALENDAR: Final = "ev_calendar"
CONF_EV_MANUAL_SOC: Final = "ev_manual_soc"
CONF_EV_MANUAL_READY: Final = "ev_manual_ready"
CONF_OUTAGE_ENTITY: Final = "outage_entity"
CONF_OUTAGE_THRESHOLD: Final = "outage_threshold"

DEFAULT_PREFIX: Final = "deye_falownik"
DEFAULT_WRITE_ACTION: Final = "esphome.deye_modbus_zapisz_harmonogram"
DEFAULT_MODE_SELECT: Final = "select.deye_falownik_ust_tryb_pracy"
DEFAULT_EXPORT_FIRST: Final = "Export First"
DEFAULT_ZERO_EXPORT: Final = "Zero Export To CT"
DEFAULTS: Final = {
    CONF_RCE_TODAY: "sensor.rce_pse_price",
    CONF_RCE_TOMORROW: "sensor.rce_pse_price_tomorrow",
    CONF_SOLCAST_TODAY: "sensor.solcast_pv_forecast_prognoza_na_dzisiaj",
    CONF_SOLCAST_TOMORROW: "sensor.solcast_pv_forecast_prognoza_na_jutro",
    CONF_HP_POWER: "sensor.sprsun_silnik_moc",
    CONF_EV_SOC: "sensor.testowe_tesla_ble_904d3c_battery",
    CONF_EV_LIMIT: "number.testowe_tesla_ble_904d3c_charging_limit",
    CONF_EV_PLUGGED: "binary_sensor.testowe_tesla_ble_904d3c_charger",
    CONF_EV_POWER: "sensor.testowe_tesla_ble_904d3c_charger_power",
    CONF_OUTAGE_ENTITY: "sensor.deye_falownik_siec_napiecie_l1",
    CONF_OUTAGE_THRESHOLD: 180.0,
}

# --- options ----------------------------------------------------------
OPT_CAPACITY: Final = "capacity_kwh"
OPT_MIN_SOC: Final = "min_soc"
OPT_MAX_SOC: Final = "max_soc"
OPT_MAX_CHARGE: Final = "max_charge_kw"
OPT_MAX_DISCHARGE: Final = "max_discharge_kw"
OPT_EFFICIENCY: Final = "roundtrip_efficiency"
OPT_WEAR: Final = "wear_cost"
OPT_GRID_IMPORT: Final = "grid_import_kw"
OPT_GRID_EXPORT: Final = "grid_export_kw"
OPT_SOC_STEP: Final = "soc_step"
OPT_PRICE_MORNING: Final = "price_morning_peak"
OPT_PRICE_AFTERNOON: Final = "price_afternoon_peak"
OPT_PRICE_OFF: Final = "price_off_peak"
OPT_MORNING_HOURS: Final = "morning_peak_hours"
OPT_AFTERNOON_SUMMER: Final = "afternoon_peak_summer"
OPT_AFTERNOON_WINTER: Final = "afternoon_peak_winter"
OPT_EV_CAPACITY: Final = "ev_capacity_kwh"
OPT_EV_CHARGER_KW: Final = "ev_charger_kw"
OPT_EV_DEFAULT_TARGET: Final = "ev_default_target"
OPT_HP_BASE_TEMP: Final = "hp_base_temp"
OPT_HISTORY_WEEKS: Final = "history_weeks"
OPT_HORIZON: Final = "horizon_hours"
OPT_MAX_WRITES: Final = "max_writes_per_day"
OPT_SOC_TOLERANCE: Final = "soc_tolerance"
OPT_FALLBACK_PLAN: Final = "fallback_plan"
OPT_STALE_HOURS: Final = "stale_hours"
OPT_MAX_CHARGE_PRICE: Final = "max_grid_charge_price"
OPT_MIN_SELL_SOC: Final = "min_sell_soc"
OPT_MIN_GAIN: Final = "min_gain"
OPT_MAX_CHARGE_SOC: Final = "max_charge_soc"
OPT_FULL_CHARGE_DAYS: Final = "full_charge_days"

OPTION_DEFAULTS: Final = {
    OPT_CAPACITY: 20.0,
    OPT_MIN_SOC: 10,
    OPT_MAX_SOC: 100,
    OPT_MAX_CHARGE: 10.0,
    OPT_MAX_DISCHARGE: 10.0,
    OPT_EFFICIENCY: 0.9,
    OPT_WEAR: 0.10,
    OPT_GRID_IMPORT: 17.0,
    OPT_GRID_EXPORT: 12.0,
    OPT_SOC_STEP: 2,
    OPT_PRICE_MORNING: 1.05,
    OPT_PRICE_AFTERNOON: 1.45,
    OPT_PRICE_OFF: 0.65,
    OPT_MORNING_HOURS: "7-13",
    OPT_AFTERNOON_SUMMER: "19-22",
    OPT_AFTERNOON_WINTER: "16-21",
    OPT_EV_CAPACITY: 75.0,
    OPT_EV_CHARGER_KW: 11.0,
    OPT_EV_DEFAULT_TARGET: 80,
    OPT_HP_BASE_TEMP: 15.0,
    OPT_HISTORY_WEEKS: 6,
    OPT_HORIZON: 36,
    OPT_MAX_WRITES: 6,
    OPT_SOC_TOLERANCE: 5,
    OPT_FALLBACK_PLAN: "05:00 20 none; 13:00 20 none; 16:00 20 none; 19:00 20 none; 22:00 20 none; 23:00 20 none",
    OPT_STALE_HOURS: 6,
    OPT_MAX_CHARGE_PRICE: 0.0,  # 0 = lowest G13 price
    OPT_MIN_SELL_SOC: 20,
    OPT_MIN_GAIN: 0.5,
    OPT_MAX_CHARGE_SOC: 98,  # everyday charge limit; the last % charge slowly
    OPT_FULL_CHARGE_DAYS: 7,  # full 100 % charge every N days (BMS balancing), 0 = off
}

# --- runtime switches / select (restored state) ------------------------
STRATEGY_OPTIONS: Final = ["self_consumption", "max_grid_trading"]

SERVICE_APPLY_PLAN: Final = "apply_plan"
SERVICE_READ_PLAN: Final = "read_plan"
SERVICE_SET_SLOT: Final = "set_slot"
SERVICE_REPLAN: Final = "replan"

ISSUE_STALE_DATA: Final = "stale_data"
ISSUE_WRITE_FAILED: Final = "write_failed"
