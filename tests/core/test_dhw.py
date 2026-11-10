from datetime import datetime, timedelta

import pytest

from core.dhw import (
    DhwBoost,
    DhwError,
    boost_kwh,
    heating_events,
    hourly_kwh,
    kwh_per_degree,
    merge_events,
    parse_boosts,
    power_kw,
    residual_profile,
    usual_start,
    usual_start_temp,
)

D = datetime(2026, 11, 9)


def _run(start: datetime, t0: float, t1: float, minutes: int, kw: float = 2.5):
    """Temperature and power points of one heating run (the tank reacts 3 min late)."""
    temp = [(start - timedelta(minutes=30), t0 + 0.2), (start, t0)]
    steps = 10
    for i in range(1, steps + 1):
        temp.append((start + timedelta(minutes=3 + (minutes - 3) * i / steps), t0 + (t1 - t0) * i / steps))
    temp.append((start + timedelta(minutes=minutes + 60), t1 - 1.5))
    power = [(start - timedelta(hours=1), 0.0), (start, kw), (start + timedelta(minutes=minutes), 0.0)]
    return temp, power


def test_parse_boosts():
    b = parse_boosts("03:00-07:00 50 40; 13:00-16:00 53 40 peak")
    assert b == [DhwBoost(3, 7, 50, 40), DhwBoost(13, 16, 53, 40, True)]
    assert b[1].key == "13-16" and b[1].covers(15) and not b[1].covers(16)
    assert DhwBoost(22, 6, 50, 40).covers(23) and DhwBoost(22, 6, 50, 40).covers(5)
    assert parse_boosts("") == []
    for bad in ("13:30-16:00 53 40", "13:00-16:00 53", "13:00-16:00 40 53", "13:00-16:00 53 40 often", "x"):
        with pytest.raises(DhwError):
            parse_boosts(bad)


def test_heating_events_measure_the_rise_and_its_energy():
    temp, power = _run(D.replace(hour=13, minute=4), 37.7, 44.4, 24)
    (e,) = heating_events(temp, power)
    assert e["t0"] == 37.7 and e["t1"] == 44.4
    assert e["kwh"] == pytest.approx(2.5 * 24 / 60, abs=0.02)
    assert datetime.fromisoformat(e["start"]) == D.replace(hour=13, minute=4)
    # sensor jitter and a climb still running are not runs
    noise = [(D + timedelta(minutes=i), 40 + 0.1 * (i % 2)) for i in range(60)]
    assert heating_events(noise, power) == []
    assert heating_events(temp[:8], power) == []


def test_learned_figures_and_profiles():
    temps, powers = [], []
    for day in range(4):
        d = D + timedelta(days=day)
        for start, t0, t1, mins in ((d.replace(hour=5), 36, 50, 80), (d.replace(hour=15), 40, 53, 70),
                                    (d.replace(hour=21, minute=10), 38, 41, 15)):
            t, p = _run(start, t0, t1, mins)
            temps += t
            powers += p
    events = heating_events(sorted(temps), sorted(powers))
    assert len(events) == 12
    assert kwh_per_degree(events) == pytest.approx(2.5 * 70 / 60 / 13, rel=0.1)
    assert power_kw(events) == pytest.approx(2.5, rel=0.1)
    morning, afternoon = DhwBoost(3, 7, 50, 40), DhwBoost(13, 16, 53, 40, True)
    assert usual_start(events, morning) == 5 and usual_start(events, afternoon) == 15
    assert usual_start_temp(events, afternoon) == 40
    assert usual_start(events, DhwBoost(8, 11, 50, 40)) is None
    # the evening top-up is no boost: it is the rest-of-day profile
    prof = residual_profile(events, [morning, afternoon], D + timedelta(days=4))
    assert set(prof) == {21} and prof[21] == pytest.approx(2.5 * 15 / 60, rel=0.25)
    # energy per clock hour: a run over two hours is split
    by_hour = hourly_kwh(events[:1])
    assert set(by_hour) == {D.replace(hour=4), D.replace(hour=5), D.replace(hour=6)}
    assert sum(by_hour.values()) == pytest.approx(events[0]["kwh"])
    # merging keeps one copy of a run and drops old ones
    merged = merge_events(events[:6], events[3:], D + timedelta(days=4))
    assert merged == events
    assert merge_events(events, [], D + timedelta(days=60)) == []


def test_boost_energy():
    b = DhwBoost(13, 16, 53, 40)
    assert boost_kwh(b, 43.0, 0.15) == pytest.approx(1.5)
    assert boost_kwh(b, None, 0.15) == pytest.approx(1.95)
    assert boost_kwh(b, 55.0, 0.15) == 0.0


def test_a_climb_without_the_compressor_is_no_heating_run():
    """After a shower the tank sensor climbs back as the water mixes (9 Nov 23:30-02:06:
    32.4 -> 36.2 deg C with 0.05 kWh) - such climbs made a reheat look half as costly."""
    from core.dhw import heated

    runs_t, runs_p = [], []
    for day in range(3):
        t, p = _run(D + timedelta(days=day, hours=13), 38, 53, 45)
        runs_t += t
        runs_p += p
    start = D.replace(hour=22)
    drift = [(start, 30.0)] + [(start + timedelta(minutes=10 * i), 30.0 + 1.0 * i) for i in range(1, 7)]
    drift.append((start + timedelta(hours=3), 34.0))
    # and a tank creeping up all morning before a run: the run is measured from its own start
    drift += [(D + timedelta(days=1, hours=h), 34.0 + 0.4 * (h - 1)) for h in range(2, 12)]
    blip = [(start - timedelta(hours=1), 0.0), (start + timedelta(minutes=20), 2.5),
            (start + timedelta(minutes=24), 0.0)]
    events = heating_events(sorted(runs_t + drift), sorted(runs_p + blip))
    assert len(events) == 4 and len(heated(events)) == 3
    assert [e["t0"] for e in heated(events)] == [38, 38, 38]
    assert kwh_per_degree(events) == pytest.approx(2.5 * 45 / 60 / 15, rel=0.05)
    assert power_kw(events) == pytest.approx(2.5, rel=0.05)


def test_a_tank_resting_at_the_top_does_not_stretch_the_run():
    temp, power = _run(D.replace(hour=14, minute=18), 44.0, 53.6, 40)
    top_at = temp[-2][0]
    temp[-1:-1] = [(top_at + timedelta(minutes=m), 53.6) for m in (5, 10, 20)]
    (e,) = heating_events(temp, power)
    assert datetime.fromisoformat(e["end"]) == top_at and e["on_h"] == pytest.approx(40 / 60, abs=0.02)
