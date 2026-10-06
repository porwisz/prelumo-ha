from datetime import date, datetime

from core.tariff import G13Tariff, Zone, hourly_sell_prices, parse_rce_records, polish_holidays

T = G13Tariff(1.0, 1.4, 0.6)


def test_easter_based_holidays_2026():
    h = polish_holidays(2026)
    assert date(2026, 4, 5) in h and date(2026, 4, 6) in h  # Easter Sun/Mon
    assert date(2026, 6, 4) in h  # Corpus Christi
    assert date(2026, 12, 24) in h


def test_zones_weekday_summer_and_winter():
    assert T.zone(datetime(2026, 6, 10, 8)) is Zone.MORNING_PEAK
    assert T.zone(datetime(2026, 6, 10, 17)) is Zone.OFF_PEAK
    assert T.zone(datetime(2026, 6, 10, 20)) is Zone.AFTERNOON_PEAK
    assert T.zone(datetime(2026, 12, 9, 17)) is Zone.AFTERNOON_PEAK
    assert T.zone(datetime(2026, 12, 9, 21)) is Zone.OFF_PEAK


def test_weekend_and_holiday_off_peak():
    assert T.zone(datetime(2026, 10, 10, 8)) is Zone.OFF_PEAK  # Saturday
    assert T.zone(datetime(2026, 11, 11, 8)) is Zone.OFF_PEAK  # holiday
    assert T.price(datetime(2026, 10, 5, 8)) == 1.0


def test_rce_parsing_and_hourly_average():
    recs = [{"dtime": "2026-10-05 18:15:00", "rce_pln": 600.0},
            {"dtime": "2026-10-05 18:30:00", "rce_pln": 800.0},
            {"bogus": 1}]
    parsed = parse_rce_records(recs, datetime.fromisoformat)
    h = datetime(2026, 10, 5, 18)
    assert hourly_sell_prices(parsed, [h, datetime(2026, 10, 5, 19)]) == [0.7, None]


def test_rce_unit_hint():
    recs = [{"time": "2026-10-05T10:00:00", "price": -3.0}]
    assert list(parse_rce_records(recs, datetime.fromisoformat, per_mwh=True).values()) == [-0.003]


def test_rce_pse_period_start_not_dtime():
    recs = [{"dtime": "2026-10-05 19:00:00", "period": "18:45 - 19:00", "rce_pln": 1.26,
             "business_date": "2026-10-05"},
            {"dtime": "2026-10-06 00:00:00", "period": "23:45 - 24:00", "rce_pln": 0.67,
             "business_date": "2026-10-05"}]
    parsed = parse_rce_records(recs, datetime.fromisoformat, per_mwh=False)
    assert set(parsed) == {datetime(2026, 10, 5, 18, 45), datetime(2026, 10, 5, 23, 45)}
