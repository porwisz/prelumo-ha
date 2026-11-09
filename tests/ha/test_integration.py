"""Integration tests against a real Home Assistant core (pytest-homeassistant-custom-component)."""

from __future__ import annotations

from datetime import timedelta

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
    from unittest.mock import PropertyMock
    from unittest.mock import patch as _patch
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
    from unittest.mock import patch

    from homeassistant.util import dt as dt_util
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


async def test_options_flow_opens_and_saves(hass: HomeAssistant, setup) -> None:
    entry, _ = setup
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] is FlowResultType.FORM, r
    data = {k: v for k, v in OPTION_DEFAULTS.items()}
    data["min_soc"] = 20
    r = await hass.config_entries.options.async_configure(r["flow_id"], data)
    assert r["type"] is FlowResultType.CREATE_ENTRY, r
    assert entry.options["min_soc"] == 20


async def test_models_relearned_after_restart_same_day(hass: HomeAssistant, setup) -> None:
    """Models live in memory; a restart on a day already learned must not leave them empty."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.util import dt as dt_util
    entry, _ = setup
    coord = entry.runtime_data
    coord.models_ready = False  # what a fresh start looks like
    coord.last_learn = dt_util.now().date().isoformat()
    with patch("custom_components.prelumo.learning.learn_models", AsyncMock()) as learn:
        await coord.async_replan()
        learn.assert_awaited_once()
        assert coord.models_ready
        await coord.async_replan()
        learn.assert_awaited_once()  # not again the same day


async def test_export_blocked_at_a_low_sell_price(hass: HomeAssistant, setup) -> None:
    """A sell slot does not get Export First while this hour's RCE price is at or below the
    block threshold - checked live, so it also holds for a plan written an hour ago."""
    from unittest.mock import patch

    from homeassistant.util import dt as dt_util
    entry, _ = setup
    coord = entry.runtime_data
    t = dt_util.now().replace(hour=19, minute=30)
    with patch("custom_components.prelumo.coordinator.dt_util.now", return_value=t):
        await coord.async_refresh()
    assert coord.data.desired_mode == "Export First"
    assert coord.data.export_blocked is False
    with patch.object(coord, "block_export_price", return_value=9.99), \
            patch("custom_components.prelumo.coordinator.dt_util.now", return_value=t):
        await coord.async_refresh()
    assert coord.data.export_blocked is True
    assert coord.data.desired_mode == "Zero Export To CT"
    assert coord.data.sell_now is False


async def test_weather_alert_charges_to_full_ignoring_the_price_cap(hass: HomeAssistant, setup) -> None:
    """A weather warning lifts the everyday charge limit, the price cap and battery-first:
    keeping the lights on beats the price."""
    entry, _ = setup
    coord = entry.runtime_data
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "alert_entity": "binary_sensor.storm_alert"})
    await hass.async_block_till_done()
    coord = entry.runtime_data
    hass.states.async_set("binary_sensor.storm_alert", "off")
    await coord.async_refresh()
    assert coord.alert_charge() is None
    p = coord.battery_params()
    assert p.max_grid_charge_price is not None  # price cap in force
    assert p.battery_first_above is not None

    hass.states.async_set("binary_sensor.storm_alert", "on")
    await coord.async_replan()
    assert coord.alert_on is True
    alert = coord.alert_charge()
    assert alert is not None and alert.reason == "weather_alert" and alert.soc == 100
    p = coord.battery_params()
    assert p.max_soc == 100
    assert p.require_full is True
    assert p.max_grid_charge_price is None  # charge at any price
    assert p.battery_first_above is None
    assert hass.states.get("binary_sensor.prelumo_alert_charge").state == "on"


async def test_sell_prices_survive_a_restart(hass: HomeAssistant, setup) -> None:
    """Right after a restart the live export check must not be blind: without the stored prices
    it would refuse Export First until the first re-plan."""
    from homeassistant.util import dt as dt_util
    entry, _ = setup
    coord = entry.runtime_data
    await coord.async_replan()
    assert coord.sell_seen
    stored = {h.isoformat(): v for h, v in coord.sell_seen.items()}

    back = coord._load_sell_seen(stored)
    assert back == coord.sell_seen
    hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
    assert hour in back  # this hour's price, so the check works before the first re-plan

    coord.sell_seen = {}
    assert coord._export_blocked(dt_util.now()) is True  # what it looked like before
    coord.sell_seen = coord._load_sell_seen(stored)
    assert coord._export_blocked(dt_util.now()) is False


async def test_stored_sell_prices_older_than_the_window_are_dropped(hass: HomeAssistant, setup) -> None:
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    entry, _ = setup
    coord = entry.runtime_data
    old = (dt_util.now() - timedelta(days=4)).replace(minute=0, second=0, microsecond=0)
    fresh = dt_util.now().replace(minute=0, second=0, microsecond=0)
    back = coord._load_sell_seen({old.isoformat(): 0.5, fresh.isoformat(): 0.9, "nonsense": 1.0})
    assert back == {fresh: 0.9}


async def test_inverter_clock_off_raises_a_warning(hass: HomeAssistant, setup) -> None:
    """The inverter runs the slots by its own clock: an hour off (a missed DST change) is
    reported, a clock set right clears it, and a missing clock entity says nothing."""
    from homeassistant.helpers import issue_registry as ir
    entry, _ = setup
    coord = entry.runtime_data
    issues = ir.async_get(hass)
    assert hass.states.get("binary_sensor.prelumo_clock_drift").state == "unknown"  # no clock entity

    behind = dt_util.now() - timedelta(hours=1)
    hass.states.async_set(f"{P}zegar", behind.strftime("%Y-%m-%d %H:%M:%S"))
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert round(coord.data.clock_drift.total_seconds() / 60) == -60
    st = hass.states.get("binary_sensor.prelumo_clock_drift")
    assert st.state == "on" and st.attributes["drift_min"] == pytest.approx(-60, abs=0.1)
    issue = issues.async_get_issue(DOMAIN, "clock_drift")
    assert issue is not None and issue.translation_placeholders["minutes"] == "-60"

    hass.states.async_set(f"{P}zegar", (dt_util.now() + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"))
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.prelumo_clock_drift").state == "off"  # within 5 min
    assert issues.async_get_issue(DOMAIN, "clock_drift") is None


async def test_day_forecast_is_kept_and_judged_the_next_day(hass: HomeAssistant, setup) -> None:
    """The first plan of a day keeps what it expected; the next day it is compared with the meters."""
    from unittest.mock import AsyncMock, patch

    from custom_components.prelumo import learning

    entry, _ = setup
    coord = entry.runtime_data
    await coord.async_replan()
    today = dt_util.now().date()
    assert coord.day_forecast["date"] == today.isoformat()
    assert coord.day_forecast["load"] and set(coord.day_forecast["load"]) == set(coord.day_forecast["pv"])
    # the next day: every forecast hour metered 1 kWh of house use and 0.5 kWh of PV
    y0 = dt_util.start_of_local_day()
    hours = [dt_util.parse_datetime(k) for k in coord.day_forecast["load"]]
    with patch.object(learning.io, "hourly_kwh", AsyncMock(side_effect=[{h: 1.0 for h in hours}, {}])):
        await learning._judge_forecast(coord, y0, y0 + timedelta(days=1), {h: 0.5 for h in hours})
    day = coord.ledger.days[today.isoformat()]
    if len(hours) >= 12:
        assert day["load_act"] == len(hours) and "pv_err" in day
    else:
        assert "load_act" not in day


async def test_hot_water_boosts_are_planned_and_reported(hass: HomeAssistant, recorder_mock) -> None:
    """With a tank configured the plan carries the boosts as flexible loads and the sensor shows
    the best start of each, its energy and what it saves against the usual hour."""
    _seed(hass)
    hass.states.async_set("climate.tank", "auto", {"current_temperature": 41.0, "temperature": 40})
    hass.states.async_set("sensor.tank_temp", "41.0", {"unit_of_measurement": "°C"})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={
        "inverter_prefix": "deye_falownik", "write_action": "esphome.deye_modbus_zapisz_harmonogram",
        "mode_select": "select.deye_falownik_ust_tryb_pracy", "mode_export_first": "Export First",
        "mode_zero_export": "Zero Export To CT",
        "rce_today": "sensor.rce_pse_price", "solcast_today": "sensor.solcast_pv_forecast_prognoza_na_dzisiaj",
        "outage_entity": DEFAULTS["outage_entity"], "outage_threshold": 180.0,
        "dhw_climate": "climate.tank", "dhw_temperature": "sensor.tank_temp",
    }, options={**OPTION_DEFAULTS, "dhw_boosts": "03:00-07:00 50 40; 13:00-16:00 53 40"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coord = entry.runtime_data
    assert coord.dhw.configured and coord.dhw.controlled()  # the switch is on by default
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.prelumo_dhw_control"}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not coord.dhw.controlled()
    st = hass.states.get("sensor.prelumo_dhw_plan")
    boosts = st.attributes["boosts"]
    assert [b["window"] for b in boosts] in (["03:00-07:00", "13:00-16:00"], ["13:00-16:00", "03:00-07:00"])
    for b in boosts:
        assert b["kwh"] > 0 and b["best_start"] and b["planned_start"] == b["usual_start"]
        assert b["extra_cost_by_start"] and min(b["extra_cost_by_start"].values()) == 0
    assert st.state not in ("unknown", "unavailable")
    assert len(coord.plan_state.result.flex) == 2
    # the tank reaches the target inside a window: that window is done for the day
    now = dt_util.now().replace(hour=14, minute=10)
    hass.states.async_set("sensor.tank_temp", "52.6", {"unit_of_measurement": "°C"})
    assert coord.dhw.tick(now) and coord.dhw.done == {"13-16": now.date().isoformat()}
    assert not coord.dhw.tick(now)
    loads = coord.dhw.loads(now, [now.replace(minute=0) + timedelta(hours=i) for i in range(36)])
    afternoon = next(f for f in loads if f.key == "13-16")
    assert afternoon.candidates[0] >= 23  # tomorrow's window


async def test_no_hot_water_entity_means_no_boosts(hass: HomeAssistant, setup) -> None:
    entry, _ = setup
    coord = entry.runtime_data
    assert not coord.dhw.configured and coord.plan_state.result.flex == []
    assert hass.states.get("sensor.prelumo_dhw_plan").attributes["boosts"] == []


async def test_hot_water_boost_is_started_at_the_planned_hour_and_ended(hass: HomeAssistant, recorder_mock) -> None:
    """Switch on: the target is set when the planned start comes, the temperature to keep when
    the tank is hot; the window is then done for the day and nothing is set twice."""
    from unittest.mock import patch

    _seed(hass)
    hass.states.async_set("climate.tank", "auto", {"current_temperature": 41.0, "temperature": 40})
    hass.states.async_set("sensor.tank_temp", "41.0", {"unit_of_measurement": "°C"})
    sets: list[float] = []

    async def _set_temperature(call: ServiceCall) -> None:
        sets.append(call.data["temperature"])

    hass.services.async_register("climate", "set_temperature", _set_temperature)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={
        "inverter_prefix": "deye_falownik", "write_action": "esphome.deye_modbus_zapisz_harmonogram",
        "mode_select": "select.deye_falownik_ust_tryb_pracy", "mode_export_first": "Export First",
        "mode_zero_export": "Zero Export To CT",
        "rce_today": "sensor.rce_pse_price", "solcast_today": "sensor.solcast_pv_forecast_prognoza_na_dzisiaj",
        "outage_entity": DEFAULTS["outage_entity"], "outage_threshold": 180.0,
        "dhw_climate": "climate.tank", "dhw_temperature": "sensor.tank_temp",
    }, options={**OPTION_DEFAULTS, "dhw_boosts": "13:00-16:00 53 40"})
    entry.add_to_hass(hass)
    t = dt_util.now().replace(hour=9, minute=10, second=0, microsecond=0)
    with patch("custom_components.prelumo.coordinator.dt_util.now", return_value=t):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        coord = entry.runtime_data
        await coord.async_replan()
    (boost,) = coord.dhw.plan
    start = dt_util.parse_datetime(boost["best_start"])
    assert 13 <= start.hour <= 15 and boost["planned_start"] == boost["best_start"]
    dhw = coord.dhw
    sets.clear()
    await dhw.control(start - timedelta(minutes=1))
    assert sets == [] and not dhw.running
    await dhw.control(start + timedelta(minutes=1))
    assert sets == [53.0] and "13-16" in dhw.running
    await dhw.control(start + timedelta(minutes=2))
    assert sets == [53.0]  # set once; a value changed by hand meanwhile is left alone
    # still running: the next plan keeps it where it started
    hrs = [start + timedelta(hours=i) for i in range(36)]
    assert dhw.loads(start + timedelta(minutes=5), hrs)[0].fixed == 0
    hass.states.async_set("sensor.tank_temp", "52.9", {"unit_of_measurement": "°C"})
    await dhw.control(start + timedelta(minutes=40))
    assert sets == [53.0, 40.0] and not dhw.running and dhw.done == {"13-16": start.date().isoformat()}
    await dhw.control(start + timedelta(minutes=41))
    assert sets == [53.0, 40.0]
    # switch off: nothing is touched
    coord.dhw_control = False
    dhw.done.clear()
    await dhw.control(start + timedelta(minutes=42))
    assert sets == [53.0, 40.0]


async def test_hot_water_boost_ends_with_the_window_and_starts_late_without_a_plan(
    hass: HomeAssistant, recorder_mock
) -> None:
    _seed(hass)
    hass.states.async_set("climate.tank", "auto", {"current_temperature": 41.0, "temperature": 40})
    sets: list[float] = []

    async def _set_temperature(call: ServiceCall) -> None:
        sets.append(call.data["temperature"])

    hass.services.async_register("climate", "set_temperature", _set_temperature)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={
        "inverter_prefix": "deye_falownik", "write_action": "esphome.deye_modbus_zapisz_harmonogram",
        "mode_select": "select.deye_falownik_ust_tryb_pracy", "mode_export_first": "Export First",
        "mode_zero_export": "Zero Export To CT",
        "rce_today": "sensor.rce_pse_price", "solcast_today": "sensor.solcast_pv_forecast_prognoza_na_dzisiaj",
        "outage_entity": DEFAULTS["outage_entity"], "outage_threshold": 180.0, "dhw_climate": "climate.tank",
    }, options={**OPTION_DEFAULTS, "dhw_boosts": "13:00-16:00 53 40"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    dhw = entry.runtime_data.dhw
    dhw.plan, dhw.done, dhw.running = [], {}, {}
    sets.clear()
    day = dt_util.now().replace(minute=0, second=0, microsecond=0)
    await dhw.control(day.replace(hour=14, minute=30))
    assert sets == []  # no plan: wait for the window's last hour
    await dhw.control(day.replace(hour=15, minute=1))
    assert sets == [53.0]
    await dhw.control(day.replace(hour=15, minute=59))
    assert sets == [53.0]  # the tank (41 deg C) is not hot yet
    await dhw.control(day.replace(hour=16, minute=0))
    assert sets == [53.0, 40.0] and dhw.done["13-16"] == day.date().isoformat()
