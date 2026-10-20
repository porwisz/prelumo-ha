"""Average cost of the energy in the battery."""

from datetime import datetime

from core.stock import BatteryStock, account_tick


def test_empty_stock_has_no_price():
    s = BatteryStock()
    assert s.avg is None
    assert s.grid_share is None
    assert s.cost_per_delivered_kwh(0.95) is None


def test_weighted_average_of_grid_and_pv():
    s = BatteryStock()
    s.charge(5.0, 0.70, from_grid=True)
    s.charge(5.0, 0.0)  # PV is free
    assert s.avg == 0.35
    assert s.grid_share == 0.5


def test_discharge_keeps_the_average_and_the_grid_share():
    s = BatteryStock()
    s.charge(6.0, 0.80, from_grid=True)
    s.charge(2.0, 0.0)
    avg, share = s.avg, s.grid_share
    s.discharge(4.0)
    assert s.kwh == 4.0
    assert s.avg == avg
    assert s.grid_share == share


def test_discharging_more_than_there_is_empties_the_stock():
    s = BatteryStock()
    s.charge(3.0, 0.5, from_grid=True)
    s.discharge(10.0)
    assert s.kwh == 0.0
    assert s.value_pln == 0.0
    assert s.avg is None


def test_reset_at_min_soc_makes_the_figure_exact():
    s = BatteryStock()
    s.charge(4.0, 0.9, from_grid=True)
    assert s.estimated
    s.reset(datetime(2026, 10, 20, 5, 0))
    assert (s.kwh, s.value_pln, s.grid_kwh) == (0.0, 0.0, 0.0)
    assert not s.estimated
    assert s.last_reset == "2026-10-20T05:00:00"


def test_delivered_cost_adds_the_discharge_loss_and_wear():
    s = BatteryStock()
    s.charge(10.0, 0.60, from_grid=True)
    assert s.cost_per_delivered_kwh(0.95, 0.10) == 0.60 / 0.95 + 0.10


def test_rescale_anchors_the_amount_and_keeps_the_price():
    s = BatteryStock()
    s.charge(10.0, 0.50, from_grid=True)
    s.charge(10.0, 0.0)
    s.rescale(10.0)  # SoC says half of what the counters booked
    assert s.kwh == 10.0
    assert s.avg == 0.25
    assert s.grid_share == 0.5


def test_rescale_of_an_empty_stock_seeds_the_price_as_grid_energy():
    s = BatteryStock()
    s.rescale(8.0, seed_price=0.65)
    assert s.kwh == 8.0
    assert s.avg == 0.65
    assert s.grid_share == 1.0
    assert s.estimated


def test_rescale_to_nothing_empties_the_stock():
    s = BatteryStock()
    s.charge(5.0, 0.5, from_grid=True)
    s.rescale(0.0)
    assert s.kwh == 0.0
    assert s.avg is None


def test_roundtrip_through_storage():
    s = BatteryStock()
    s.charge(7.0, 0.42, from_grid=True)
    s.reset(datetime(2026, 1, 2, 3, 4))
    s.charge(2.0, 0.11, from_grid=True)
    back = BatteryStock.from_dict(s.to_dict())
    assert back.to_dict() == s.to_dict()
    assert back.avg == s.avg


# --- source split ------------------------------------------------------------
def test_charging_from_pv_surplus_is_free():
    # 2 kWh PV, house took 0.5, battery got 1.0 (battery side), nothing imported
    t = account_tick(charged=1.0, discharged=0.0, imported=0.0, house=0.5, pv=2.0, eta=0.95)
    assert t.from_grid == 0.0
    assert t.charged == 1.0


def test_charging_from_the_grid_at_night():
    # no PV; the house took 0.3 from the grid and the rest of the import charged the battery
    t = account_tick(charged=1.0, discharged=0.0, imported=0.3 + 1.0 / 0.95, house=0.3, pv=0.0, eta=0.95)
    assert round(t.from_grid, 3) == 1.0


def test_import_for_the_house_is_not_charged_to_the_battery():
    # PV (1.0) covers the house (0.6) and part of the charging; only 0.4 was imported
    t = account_tick(charged=0.76, discharged=0.0, imported=0.4, house=0.6, pv=1.0, eta=0.95)
    assert round(t.from_grid, 3) == round(0.4 * 0.95, 3)
    assert round(t.charged - t.from_grid, 3) == round(0.76 - 0.38, 3)  # the rest is free PV


def test_mixed_tick_splits_the_charge():
    # import 1.0, house 0.4 with no PV -> 0.6 AC went to the battery; the battery took 1.0
    t = account_tick(charged=1.0, discharged=0.0, imported=1.0, house=0.4, pv=0.0, eta=0.95)
    assert round(t.from_grid, 3) == round(0.6 * 0.95, 3)


def test_discharge_is_passed_through():
    t = account_tick(charged=0.0, discharged=1.5, imported=0.0, house=2.0, pv=0.5, eta=0.95)
    assert t.discharged == 1.5
    assert t.from_grid == 0.0
