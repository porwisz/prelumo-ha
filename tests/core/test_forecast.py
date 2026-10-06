from datetime import datetime, timedelta

from core.forecast.ev import CarState, EvPattern, Session, car_energy_needed, forecast_ev
from core.forecast.heatpump import HeatPumpModel
from core.forecast.load import LoadProfile, base_load_samples
from core.forecast.pv import PvBias, solcast_to_hourly

D0 = datetime(2026, 9, 7)  # Monday


def test_pv_bias_learns_and_applies():
    b = PvBias(alpha=0.5)
    h = datetime(2026, 10, 5, 12)
    b.learn([(h, 4.0, 2.0), (h, 4.0, 2.0)])
    assert 0.6 < b.factor(h) < 0.8
    assert b.apply([h], [4.0])[0] < 4.0
    b.learn([(h, 0.1, 5.0)])  # ignored, forecast too small
    assert PvBias.from_dict(b.to_dict()).factors == b.factors


def test_solcast_half_hour_to_hourly():
    recs = [{"period_start": "2026-10-05T12:00:00", "pv_estimate": 4.0},
            {"period_start": "2026-10-05T12:30:00", "pv_estimate": 2.0}]
    assert solcast_to_hourly(recs, datetime.fromisoformat) == {datetime(2026, 10, 5, 12): 3.0}


def test_load_profile_subtracts_hp_and_ev():
    house, hp, ev = {}, {}, {}
    for w in range(3):
        h = D0 + timedelta(weeks=w, hours=18)
        house[h], hp[h], ev[h] = 10.0, 2.0, 7.0
    prof = LoadProfile(base_load_samples(house, hp, ev))
    assert prof.predict([D0 + timedelta(weeks=5, hours=18)]) == [1.0]
    assert prof.predict([D0 + timedelta(hours=3)]) == [1.0]  # global fallback


def test_heat_pump_regression():
    energy, temp = {}, {}
    for i in range(72):
        h = D0 + timedelta(hours=i)
        t = 15 if i < 24 else 5 - (i % 10)
        temp[h] = t
        energy[h] = 0.3 + 0.15 * max(0, 15 - t)
    m = HeatPumpModel(15)
    m.fit(energy, temp)
    assert abs(m.coef - 0.15) < 0.01
    pred = m.predict([D0 + timedelta(days=5)], {D0 + timedelta(days=5): 0.0})[0]
    assert abs(pred - (0.3 + 2.25)) < 0.1


def _hours(now, n=24):
    return [now + timedelta(hours=i) for i in range(n)]


def test_ev_plugged_uses_soc_and_limit():
    car = CarState(soc=50, limit=80, plugged=True, capacity_kwh=75, charger_kw=11, efficiency=1.0)
    assert car_energy_needed(car) == 22.5
    fc = forecast_ev(_hours(D0), D0, car)
    assert fc.hourly_kwh[:3] == [11, 11, 0.5]
    assert not any(fc.battery_allowed)


def test_ev_manual_beats_calendar_and_flags_deadline_risk():
    car = CarState(soc=20, limit=80, plugged=True, capacity_kwh=75, charger_kw=11, efficiency=1.0)
    fc = forecast_ev(_hours(D0), D0, car, manual=(100, D0 + timedelta(hours=3)),
                     calendar=[(D0 + timedelta(hours=10), 60)], grid_import_kw=11)
    assert fc.needs[0].source == "manual"
    assert fc.battery_allowed[0] and fc.battery_allowed[2] and not fc.battery_allowed[5]


def test_ev_pattern_learned():
    sessions = [Session(D0 + timedelta(weeks=w, hours=18), D0 + timedelta(weeks=w, hours=31), 20.0)
                for w in range(4)]
    pat = EvPattern.learn(sessions, weeks=4)
    assert pat.by_weekday[0]["probability"] == 1.0
    car = CarState(soc=None, limit=None, plugged=False, charger_kw=11)
    fc = forecast_ev(_hours(D0 + timedelta(weeks=5)), D0 + timedelta(weeks=5), car, pattern=pat)
    assert fc.hourly_kwh[18] == 11 and fc.hourly_kwh[19] == 9
    assert EvPattern.from_dict(pat.to_dict()).by_weekday == pat.by_weekday
