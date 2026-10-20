from dataclasses import replace
from datetime import datetime, timedelta

from core.balance import FullChargeTracker
from core.optimizer import BatteryParams, HourInput, Strategy, optimize

D0 = datetime(2026, 10, 9, 0, 0)  # Friday


def test_tracker_due_and_observe():
    t = FullChargeTracker(7)
    assert t.due(D0)  # never seen full -> due
    assert t.observe(100, D0)
    assert not t.observe(100, D0 + timedelta(minutes=5))  # same event
    last = D0 + timedelta(minutes=5)  # last moment seen at 100 %
    assert not t.due(last + timedelta(days=6))
    assert t.due(last + timedelta(days=7))
    assert t.next_due() == last + timedelta(days=7)
    assert t.days_since(last + timedelta(days=2, hours=12)) == 2.5
    assert not t.observe(98, D0 + timedelta(days=8))
    assert not FullChargeTracker(0).due(D0)  # disabled


def _hours(buy, pv):
    return [HourInput(D0 + timedelta(hours=i), pv[i], 1.0, 0.0, buy[i], 0.3) for i in range(len(buy))]


P = BatteryParams(capacity_kwh=20, min_soc=10, max_soc=98, max_charge_kw=10, max_discharge_kw=10,
                  wear_cost=0.05, soc_step=2, max_grid_charge_price=0.65)


def test_everyday_limit_never_exceeded():
    hrs = _hours([0.65] * 6 + [1.45] * 18, [0] * 24)
    r = optimize(hrs, 20, P, Strategy.SELF_CONSUMPTION)
    assert max(h.soc_end for h in r.hours) <= 98


def test_full_charge_planned_cheapest_way_when_due():
    hrs = _hours([0.65] * 6 + [1.45] * 18, [0] * 24)
    due = replace(P, max_soc=100, require_full=True)
    r = optimize(hrs, 20, due, Strategy.SELF_CONSUMPTION)
    assert "full_charge_planned" in r.notes
    full_hours = [i for i, h in enumerate(r.hours) if h.soc_end >= 100]
    assert full_hours and all(hrs[i].buy <= 0.65 for i in range(full_hours[0] + 1))  # no peak buying


def test_full_charge_respects_price_cap_and_reports_impossible():
    hrs = _hours([1.45] * 24, [0] * 24)  # no cheap hours, no PV
    due = replace(P, max_soc=100, require_full=True)
    r = optimize(hrs, 20, due, Strategy.SELF_CONSUMPTION)
    assert "full_charge_not_possible_in_horizon" in r.notes
    assert all(h.grid_charge < 0.06 for h in r.hours)


# --- daily energy ledger: cycles, self-sufficiency, monthly totals -----------
def _ledger():
    from datetime import date
    from core.shadow import ShadowLedger
    led = ShadowLedger()
    for i, (house, pv, imp, exp, dis) in enumerate([
        (20.0, 10.0, 12.0, 2.0, 8.0),
        (20.0, 30.0, 4.0, 14.0, 10.0),
    ]):
        led.record_energy(date(2026, 3, 1 + i), house=house, pv=pv, grid_import=imp,
                          grid_export=exp, discharged=dis)
        led.record_actual(date(2026, 3, 1 + i), 5.0)
        led.record_plan(date(2026, 3, 1 + i), 4.0, 6.0)
    return led


def test_self_sufficiency_over_the_window():
    led = _ledger()
    assert led.self_sufficiency(2) == 60.0  # 40 kWh house, 16 bought
    assert led.self_sufficiency(1) == 80.0  # last day only: 20 house, 4 bought


def test_self_sufficiency_unknown_without_data():
    from core.shadow import ShadowLedger
    assert ShadowLedger().self_sufficiency() is None


def test_cycles_from_discharged_energy():
    led = _ledger()
    assert led.cycles(20.0, 2) == 0.9  # 18 kWh out of a 20 kWh battery
    assert led.cycles(0.0) is None


def test_monthly_totals():
    led = _ledger()
    m = led.by_month(20.0)["2026-03"]
    assert m["house"] == 40.0 and m["grid_import"] == 16.0
    assert m["cycles"] == 0.9
    assert m["self_sufficiency"] == 60.0
    assert m["actual_cost"] == 10.0
    assert m["expected_savings"] == 4.0


def test_energy_survives_storage_roundtrip():
    from core.shadow import ShadowLedger
    led = _ledger()
    back = ShadowLedger.from_dict(led.to_dict())
    assert back.self_sufficiency(2) == led.self_sufficiency(2)
    assert back.cycles(20.0, 2) == led.cycles(20.0, 2)
