from datetime import datetime, time

import pytest

from core.plan import (
    FLAG_GRID_CHARGE, FLAG_SELL, DayPlan, PlanError, Slot, SlotMode, parse_flags, parse_hhmm,
)

START = [700, 1300, 1600, 1900, 2000, 2100]


def plan(start=START, power=None, soc=None, flags=None):
    return DayPlan.from_lists(start, power or [12000] * 6, soc or [20, 100, 20, 40, 20, 40],
                              flags or [0, 1, 0, 32, 0, 1])


def test_example_from_prompt_is_valid():
    p = plan()
    assert p.slots[1].mode is SlotMode.GRID_CHARGE
    assert p.slots[3].mode is SlotMode.SELL
    assert p.to_service_data()["flagi"] == [0, 1, 0, 32, 0, 1]


@pytest.mark.parametrize("start", [
    [500, 900, 1300, 1700, 2000, 100],   # one midnight crossing inside the list
    [0, 400, 800, 1200, 1600, 2000],     # crossing between slot 6 and 1
])
def test_valid_orders(start):
    plan(start=start)


@pytest.mark.parametrize("start", [
    [700, 1300, 1300, 1900, 2000, 2100],  # duplicate
    [700, 600, 1600, 1900, 100, 2100],    # two crossings
    [700, 1300, 1600, 1900, 2000, 700],   # slot 6 == slot 1
    [760, 1300, 1600, 1900, 2000, 2100],  # bad minutes
    [2400, 100, 200, 300, 400, 500],      # bad hour
])
def test_invalid_orders(start):
    with pytest.raises(PlanError):
        plan(start=start)


@pytest.mark.parametrize("field,value", [("power", 12001), ("soc", 101), ("flags", 64), ("soc", -1)])
def test_range_checks(field, value):
    kw = {"power": [12000] * 6, "soc": [20] * 6, "flags": [0] * 6}
    kw[field] = [value] + kw[field][1:]
    with pytest.raises(PlanError):
        DayPlan.from_lists(START, kw["power"], kw["soc"], kw["flags"])


def test_wrong_length():
    with pytest.raises(PlanError):
        DayPlan.from_lists(START[:5], [0] * 5, [0] * 5, [0] * 5)


def test_active_slot_and_wraparound():
    p = plan()
    assert p.active_index(time(12, 59)) == 0
    assert p.active_index(time(13, 0)) == 1
    assert p.active_index(time(23, 0)) == 5
    assert p.active_index(time(3, 0)) == 5  # before first start -> last slot of previous day
    p2 = plan(start=[500, 900, 1300, 1700, 2000, 100])
    assert p2.active_index(time(2, 0)) == 5
    assert p2.active_index(time(0, 30)) == 4


def test_next_change():
    p = plan()
    when, idx = p.next_change(datetime(2026, 10, 5, 21, 30))
    assert (when, idx) == (datetime(2026, 10, 6, 7, 0), 0)
    when, idx = p.next_change(datetime(2026, 10, 5, 13, 0))
    assert (when, idx) == (datetime(2026, 10, 5, 16, 0), 2)


def test_with_mode_preserves_unknown_bits():
    s = Slot(700, 1000, 50, 0b011100 | FLAG_GRID_CHARGE)
    sell = s.with_mode(SlotMode.SELL)
    assert sell.flags == 0b011100 | FLAG_SELL
    assert sell.with_mode(SlotMode.NONE).flags == 0b011100


def test_merge_preserved_flags():
    cur = plan(flags=[4, 8, 0, 16, 0, 2])
    new = plan(flags=[1, 0, 32, 0, 0, 0])
    assert [s.flags for s in new.merge_preserved_flags(cur).slots] == [5, 8, 32, 16, 0, 2]


def test_parsers():
    assert parse_hhmm("07:30") == 730
    assert parse_hhmm("1300") == 1300
    assert parse_flags("Sprzedaz (32)") == 32
    assert parse_flags("Siec (1)") == 1
    assert parse_flags("33") == 33
