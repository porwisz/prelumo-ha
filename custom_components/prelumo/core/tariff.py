"""Prices: G13 zone tariff for buying, RCE for selling. Pure Python."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum


class Zone(StrEnum):
    MORNING_PEAK = "morning_peak"
    AFTERNOON_PEAK = "afternoon_peak"
    OFF_PEAK = "off_peak"


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def polish_holidays(year: int) -> set[date]:
    e = _easter(year)
    fixed = [(1, 1), (1, 6), (5, 1), (5, 3), (8, 15), (11, 1), (11, 11), (12, 24), (12, 25), (12, 26)]
    days = {date(year, m, d) for m, d in fixed}
    days |= {e, e + timedelta(days=1), e + timedelta(days=49), e + timedelta(days=60)}
    return days


@dataclass(frozen=True)
class G13Tariff:
    """Hours are [start, end) local hours. Summer = April..September."""

    price_morning_peak: float  # PLN/kWh gross incl. distribution
    price_afternoon_peak: float
    price_off_peak: float
    morning_peak: tuple[int, int] = (7, 13)
    afternoon_peak_summer: tuple[int, int] = (19, 22)
    afternoon_peak_winter: tuple[int, int] = (16, 21)
    summer_months: frozenset[int] = field(default_factory=lambda: frozenset(range(4, 10)))

    def zone(self, dt: datetime) -> Zone:
        d = dt.date()
        if d.weekday() >= 5 or d in polish_holidays(d.year):
            return Zone.OFF_PEAK
        h = dt.hour
        if self.morning_peak[0] <= h < self.morning_peak[1]:
            return Zone.MORNING_PEAK
        ap = self.afternoon_peak_summer if dt.month in self.summer_months else self.afternoon_peak_winter
        if ap[0] <= h < ap[1]:
            return Zone.AFTERNOON_PEAK
        return Zone.OFF_PEAK

    def price(self, dt: datetime) -> float:
        return {
            Zone.MORNING_PEAK: self.price_morning_peak,
            Zone.AFTERNOON_PEAK: self.price_afternoon_peak,
            Zone.OFF_PEAK: self.price_off_peak,
        }[self.zone(dt)]


def hourly_buy_prices(tariff: G13Tariff, hours: list[datetime]) -> list[float]:
    return [tariff.price(h) for h in hours]


def hourly_sell_prices(
    rce: Mapping[datetime, float], hours: list[datetime], fallback: float | None = None
) -> list[float | None]:
    """Hourly sell price (PLN/kWh); averages sub-hourly entries; None if unknown."""
    buckets: dict[datetime, list[float]] = {}
    for ts, price in rce.items():
        buckets.setdefault(ts.replace(minute=0, second=0, microsecond=0), []).append(price)
    return [
        (sum(buckets[h]) / len(buckets[h])) if h in buckets else fallback for h in hours
    ]


_TIME_KEYS = ("dtime", "time", "start", "period_start", "datetime", "start_time", "from", "hour")
_PRICE_KEYS = ("rce_pln", "price", "value", "rce", "price_pln", "total")


def parse_rce_records(
    records: Iterable[Mapping], parse_dt, per_mwh: bool | None = None
) -> dict[datetime, float]:
    """Tolerant parser for list-of-dict price attributes -> PLN/kWh.

    ``per_mwh``: True/False from the entity unit; None = guess per value (|p| > 5).
    """
    out: dict[datetime, float] = {}
    for rec in records:
        if not isinstance(rec, Mapping):
            continue
        if "period" in rec and "business_date" in rec:  # rce_pse: dtime is the period END
            ts = f"{rec['business_date']} {str(rec['period']).split('-')[0].strip()}:00"
        else:
            ts = next((rec[k] for k in _TIME_KEYS if k in rec), None)
        price = next((rec[k] for k in _PRICE_KEYS if k in rec), None)
        if ts is None or price is None:
            continue
        dt = ts if isinstance(ts, datetime) else parse_dt(str(ts))
        if dt is None:
            continue
        try:
            p = float(price)
        except (TypeError, ValueError):
            continue
        if per_mwh or (per_mwh is None and abs(p) > 5):
            p /= 1000
        out[dt] = p
    return out
