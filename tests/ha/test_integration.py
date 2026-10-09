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
    from unittest.mock import PropertyMock, patch as _patch
    _p = _patch.object(type(entry.runtime_data), "opt", new_callable=PropertyMock,
                       return_value={**OPTION_DEFAULTS, "min_gain": 0.0})  # any improvement counts
    _p.start()
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
    assert resp["write_decision"][0] in ("no_material_change", "gain_below_threshold", "min_interval"), resp
    assert len([c for c in calls if c.domain == "esphome"]) == 1
    _p.stop()


async def test_shadow_mode_never_touches_work_mode(hass: HomeAssistant, setup) -> None:
    entry, calls = setup
    coord = entry.runtime_data
    # 19:30 is inside the Sell slot -> Prelumo wants Export First
    from homeassistant.util import dt as dt_util
    from unittest.mock import patch
    t = dt_util.now().replace(hour=19, minute=30)
    with patch("custom_components.prelumo.coordinator.dt_util.now", return_value=t):
        await coord.async_refresh()
    assert coord.data.desired_mode == "Export First"
    assert coord.data.mode_action == "shadow"
    assert hass.states.get("select.deye_falownik_ust_tryb_pracy").state == "Zero Export To CT"
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.prelumo_shadow_mode"}, blocking=True)
    from unittest.mock import AsyncMock
    coord.writer.ensure_mode = AsyncMock(return_value=True)
    with patch("custom_components.prelumo.coordinator.dt_util.now", return_value=t):
        await coord.async_refresh()
    assert coord.data.mode_action == "set"
    coord.writer.ensure_mode.assert_awaited_once_with("Export First")


async def test_diagnostics(hass: HomeAssistant, setup) -> None:
    from custom_components.prelumo.diagnostics import async_get_config_entry_diagnostics
    entry, _ = setup
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["config"]["rce_today"] == "sensor.rce_pse_price"
    assert diag["controls"]["shadow_mode"] is True
    assert len(diag["plan"]["proposed"]) == 6 and diag["plan"]["hourly"]
    assert diag["inverter"]["current_plan"][3]["mode"] == "sell"


async def test_reconfigure(hass: HomeAssistant, setup) -> None:
    entry, _ = setup
    r = await entry.start_reconfigure_flow(hass)
    assert r["step_id"] == "reconfigure"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {
        "write_action": "esphome.deye_modbus_zapisz_harmonogram",
        "mode_select": "select.deye_falownik_ust_tryb_pracy",
        "rce_today": "sensor.rce_pse_price", "weather": "weather.home", "outage_threshold": 170})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reconfigure_successful"
    assert entry.data["weather"] == "weather.home"
    assert entry.data["outage_threshold"] == 170
    assert "solcast_today" not in entry.data  # cleared field removed


async def test_first_plan_without_started_event(hass: HomeAssistant, recorder_mock) -> None:
    """A hung integration can keep HA from 'running'; Prelumo must still plan once inputs exist."""
    from homeassistant.core import CoreState
    _seed(hass)
    hass.set_state(CoreState.starting)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=dict(OPTION_DEFAULTS), data={
        "inverter_prefix": "deye_falownik", "write_action": "esphome.deye_modbus_zapisz_harmonogram",
        "mode_select": "select.deye_falownik_ust_tryb_pracy", "mode_export_first": "Export First",
        "mode_zero_export": "Zero Export To CT", "rce_today": "sensor.rce_pse_price",
        "solcast_today": "sensor.solcast_pv_forecast_prognoza_na_dzisiaj"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coord = entry.runtime_data
    await coord.async_refresh()  # the 30 s loop
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coord.plan_state.computed_at is not None
    assert not coord.plan_state.fallback_active


async def test_full_charge_monitoring(hass: HomeAssistant, setup) -> None:
    entry, _ = setup
    coord = entry.runtime_data
    assert hass.states.get("binary_sensor.prelumo_full_charge_due").state == "on"  # never full yet
    assert coord.battery_params().max_soc == 100 and coord.battery_params().require_full
    hass.states.async_set(f"{P}bateria_soc", "100")
    await coord.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coord.full.last_full is not None
    assert hass.states.get("binary_sensor.prelumo_full_charge_due").state == "off"
    assert coord.battery_params().max_soc == 98 and not coord.battery_params().require_full
    st = hass.states.get("sensor.prelumo_last_full_charge")
    assert st.attributes["interval_days"] == 7 and st.attributes["due"] is False
