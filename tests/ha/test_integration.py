"""Integration tests against a real Home Assistant core (pytest-homeassistant-custom-component)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.prelumo.const import DEFAULTS, DOMAIN, OPTION_DEFAULTS

P = "sensor.deye_falownik_"
SLOTS = [("07:00", 12000, 20, "Brak (0)"), ("13:00", 12000, 100, "Siec (1)"), ("16:00", 12000, 20, "Brak (0)"),
         ("19:00", 12000, 40, "Sprzedaz (32)"), ("20:00", 12000, 20, "Brak (0)"), ("21:00", 12000, 40, "Siec (1)")]


def _seed(hass: HomeAssistant) -> list[ServiceCall]:
    hass.states.async_set(f"{P}bateria_soc", "55", {"unit_of_measurement": "%"})
    hass.states.async_set(f"{P}dom_pobor", "800", {"unit_of_measurement": "W"})
    hass.states.async_set(f"{P}siec_napiecie_l1", "232", {"unit_of_measurement": "V"})
    for n, (start, moc, soc, flagi) in enumerate(SLOTS, 1):
        hass.states.async_set(f"{P}harmonogram_{n}_start", start)
        hass.states.async_set(f"{P}harmonogram_{n}_moc", str(moc))
        hass.states.async_set(f"{P}harmonogram_{n}_soc", str(soc))
        hass.states.async_set(f"{P}harmonogram_{n}_flagi", flagi)
    hass.states.async_set("select.deye_falownik_ust_tryb_pracy", "Zero Export To CT",
                          {"options": ["Export First", "Zero Export To Load", "Zero Export To CT"]})
    now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    prices = []
    for i in range(-2, 40 * 4):
        t = now + timedelta(minutes=15 * i)
        prices.append({"dtime": (t + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S"),
                       "period": f"{t:%H:%M} - {(t + timedelta(minutes=15)):%H:%M}",
                       "rce_pln": 1.8 if t.hour in (18, 19) else 0.4, "business_date": f"{t:%Y-%m-%d}"})
    hass.states.async_set("sensor.rce_pse_price", "0.4", {"unit_of_measurement": "PLN/kWh", "prices": prices})
    hourly = [{"period_start": (now + timedelta(hours=i)).isoformat(),
               "pv_estimate": 3.0 if 9 <= (now + timedelta(hours=i)).hour < 16 else 0.0} for i in range(40)]
    hass.states.async_set("sensor.solcast_pv_forecast_prognoza_na_dzisiaj", "20", {"detailedHourly": hourly})
    calls: list[ServiceCall] = []

    async def _write(call: ServiceCall) -> None:
        calls.append(call)

    async def _select(call: ServiceCall) -> None:
        calls.append(call)
        hass.states.async_set("select.deye_falownik_ust_tryb_pracy", call.data["option"])

    hass.services.async_register("esphome", "deye_modbus_zapisz_harmonogram", _write)
    hass.services.async_register("select", "select_option", _select)
    return calls


async def test_config_flow(hass: HomeAssistant, recorder_mock) -> None:
    _seed(hass)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["type"] is FlowResultType.FORM
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"inverter_prefix": "nope",
        "write_action": "esphome.x", "mode_select": "select.deye_falownik_ust_tryb_pracy",
        "mode_export_first": "Export First", "mode_zero_export": "Zero Export To CT"})
    assert r["errors"] == {"inverter_prefix": "inverter_not_found"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"inverter_prefix": "deye_falownik",
        "write_action": "esphome.deye_modbus_zapisz_harmonogram", "mode_select": "select.deye_falownik_ust_tryb_pracy",
        "mode_export_first": "Export First", "mode_zero_export": "Zero Export To CT"})
    assert r["step_id"] == "sources"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["step_id"] == "ev"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["options"]["capacity_kwh"] == OPTION_DEFAULTS["capacity_kwh"]


@pytest.fixture
async def setup(hass: HomeAssistant, recorder_mock):
    calls = _seed(hass)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=dict(OPTION_DEFAULTS), data={
        "inverter_prefix": "deye_falownik", "write_action": "esphome.deye_modbus_zapisz_harmonogram",
        "mode_select": "select.deye_falownik_ust_tryb_pracy", "mode_export_first": "Export First",
        "mode_zero_export": "Zero Export To CT",
        "rce_today": "sensor.rce_pse_price", "solcast_today": "sensor.solcast_pv_forecast_prognoza_na_dzisiaj",
        "outage_entity": DEFAULTS["outage_entity"], "outage_threshold": 180.0,
    })
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)  # initial plan runs in background
    assert entry.runtime_data.plan_state.result is not None
    return entry, calls


async def test_entities_and_shadow_replan(hass: HomeAssistant, setup) -> None:
    entry, calls = setup
    coord = entry.runtime_data
    assert hass.states.get("switch.prelumo_shadow_mode").state == "on"
    assert hass.states.get("select.prelumo_strategy").state == "self_consumption"
    assert hass.states.get("sensor.prelumo_active_slot").attributes["slots"][3]["mode"] == "sell"
    assert hass.states.get("binary_sensor.prelumo_grid_outage").state == "off"

    resp = await hass.services.async_call(DOMAIN, "replan", {}, blocking=True, return_response=True)
    assert resp["error"] is None, resp
    assert resp["write_decision"] == ["shadow_mode"]
    assert len(resp["proposed"]) == 6
    assert not [c for c in calls if c.domain == "esphome"]  # shadow: nothing written
    await hass.async_block_till_done()
    st = hass.states.get("sensor.prelumo_proposed_plan")
    assert st.state == "ready" and len(st.attributes["hourly"]) == OPTION_DEFAULTS["horizon_hours"]
    assert coord.plan_state.result is not None


async def test_apply_plan_and_set_slot_write_once(hass: HomeAssistant, setup) -> None:
    entry, calls = setup
    await hass.services.async_call(DOMAIN, "apply_plan", {"source": "fallback"}, blocking=True)
    writes = [c for c in calls if c.domain == "esphome"]
    assert len(writes) == 1 and len(writes[0].data["start"]) == 6
    await hass.services.async_call(DOMAIN, "set_slot", {"slot": 4, "soc": 35, "mode": "none"}, blocking=True)
    writes = [c for c in calls if c.domain == "esphome"]
    assert writes[-1].data["soc"][3] == 35 and writes[-1].data["flagi"][3] == 0


async def test_read_plan(hass: HomeAssistant, setup) -> None:
    resp = await hass.services.async_call(DOMAIN, "read_plan", {}, blocking=True, return_response=True)
    assert resp["current"][1]["mode"] == "grid_charge"


async def test_stale_data_uses_fallback(hass: HomeAssistant, setup) -> None:
    entry, calls = setup
    hass.states.async_remove("sensor.rce_pse_price")
    resp = await hass.services.async_call(DOMAIN, "replan", {}, blocking=True, return_response=True)
    assert "sell_prices" in resp["stale_sources"]
    assert resp["write_decision"] == ["stale_inputs_keep_last_plan"]  # fresh plan kept first
    entry.runtime_data.plan_state.result = None  # last plan too old / missing
    resp = await hass.services.async_call(DOMAIN, "replan", {}, blocking=True, return_response=True)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.prelumo_fallback_active").state == "on"
    assert hass.states.get("sensor.prelumo_proposed_plan").state == "fallback"
    assert resp["write_decision"] == ["shadow_mode"]  # still no write in shadow mode


async def test_auto_write_once_then_no_rewrite(hass: HomeAssistant, setup) -> None:
    entry, calls = setup
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.prelumo_shadow_mode"}, blocking=True)
    await hass.async_block_till_done()
    resp = await hass.services.async_call(DOMAIN, "replan", {}, blocking=True, return_response=True)
    writes = [c for c in calls if c.domain == "esphome"]
    assert len(writes) == 1, resp
    # inverter now reports the written plan
    d = writes[0].data
    for n in range(6):
        hass.states.async_set(f"{P}harmonogram_{n + 1}_start", f"{d['start'][n] // 100:02d}:{d['start'][n] % 100:02d}")
        hass.states.async_set(f"{P}harmonogram_{n + 1}_soc", str(d["soc"][n]))
        hass.states.async_set(f"{P}harmonogram_{n + 1}_flagi", str(d["flagi"][n]))
        hass.states.async_set(f"{P}harmonogram_{n + 1}_moc", str(d["moc"][n]))
    resp = await hass.services.async_call(DOMAIN, "replan", {}, blocking=True, return_response=True)
    assert resp["write_decision"] == ["no_material_change"]
    assert len([c for c in calls if c.domain == "esphome"]) == 1
