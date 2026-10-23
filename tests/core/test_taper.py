from datetime import datetime, timedelta

from core.optimizer import BatteryParams, max_charge_kwh
from core.taper import DEFAULT_TAPER, charge_events, merge_events, taper_from_events

T0 = datetime(2026, 10, 15, 13, 0)


def climb(minutes_at: dict[int, float]) -> list[tuple[datetime, float]]:
    return [(T0 + timedelta(minutes=m), float(s)) for s, m in sorted(minutes_at.items())]


# 15 Oct: 90 % at 14:17, 95 % 14:25, 98 % 14:42, 99 % 14:53, 100 % 16:10
OCT15 = climb({56: 0, 90: 77, 91: 78, 94: 80, 95: 85, 96: 88, 97: 94, 98: 102, 99: 113, 100: 190})


def test_band_minutes_from_the_soc_history():
    ev = charge_events(OCT15)
    assert len(ev) == 1
    assert ev[0]["minutes"] == {"90": 8.0, "95": 17.0, "98": 11.0, "99": 77.0}


def test_a_drop_restarts_the_climb_and_long_gaps_are_ignored():
    pts = climb({85: 0, 92: 10, 80: 20, 90: 40, 95: 50, 98: 60, 99: 70, 100: 70 + 240})
    ev = charge_events(pts)
    assert ev[0]["minutes"] == {"90": 10.0, "95": 10.0, "98": 10.0}  # 99->100 in 4 h: interrupted


def test_slowest_recent_charge_sets_the_taper():
    fast = {"at": "2026-10-07T15:20", "minutes": {"90": 11, "95": 16, "98": 10, "99": 31}}
    slow = charge_events(OCT15)
    events = merge_events([fast], slow)
    taper = dict(taper_from_events(events, 26.25))
    assert abs(taper[99] - 0.2625 / (77 / 60)) < 0.01  # 77 min for the last %
    assert dict(taper_from_events([], 26.25)) == dict(DEFAULT_TAPER)
    p = BatteryParams(capacity_kwh=26.25, max_charge_kw=8, charge_taper=taper_from_events(events, 26.25))
    # from 80 %: one hour reaches only ~99 %, the last % needs over an hour more
    assert max_charge_kwh(80, p) / 0.2625 < 19.5
    assert max_charge_kwh(99, p) / 0.2625 < 0.8


# 22 Oct: 90 % at 14:31, 95 % 14:44, 98 % 15:03, 99 % 15:15, back to 98 % at 16:05 (no 100 %)
OCT22 = climb({47: 0, 90: 91, 92: 94, 95: 104, 98: 123, 99: 135}) + [(T0 + timedelta(minutes=185), 98.0)]


def test_a_climb_stalled_at_99_teaches_a_lower_bound():
    ev = charge_events(OCT22)
    assert len(ev) == 1
    assert ev[0]["minutes"] == {"90": 13.0, "95": 19.0, "98": 12.0}
    assert ev[0]["at_least"] == {"99": 50.0}
    # alone it caps the last %: at most 1 % in 50 min
    taper = dict(taper_from_events(ev, 26.25))
    assert abs(taper[99] - 0.2625 / (50 / 60)) < 0.01
    # with a slower complete charge the slowest still wins
    taper = dict(taper_from_events(merge_events(charge_events(OCT15), ev), 26.25))
    assert abs(taper[99] - 0.2625 / (77 / 60)) < 0.01


def test_a_stop_below_99_is_not_a_stall():
    # everyday limit: 94 % held, then the house discharges it - no taper information
    pts = climb({50: 0, 90: 20, 94: 30}) + [(T0 + timedelta(minutes=90), 93.0)]
    assert charge_events(pts) == []
