"""PV forecast (Solcast) with a learned bias correction per (month, hour)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

MIN_FACTOR, MAX_FACTOR = 0.3, 1.7
MIN_FORECAST_KWH = 0.2  # ignore near-zero hours when learning (dawn/dusk noise)


@dataclass
class PvBias:
    """Multiplicative correction factor learned with an exponential moving average."""

    alpha: float = 0.1
    factors: dict[str, float] = field(default_factory=dict)

    @staticmethod
    def key(dt: datetime) -> str:
        return f"{dt.month}-{dt.hour}"

    def factor(self, dt: datetime) -> float:
        return self.factors.get(self.key(dt), 1.0)

    def learn(self, pairs: Iterable[tuple[datetime, float, float]]) -> int:
        """pairs: (hour, forecast_kwh, actual_kwh). Returns number of samples used."""
        n = 0
        for dt, fc, actual in pairs:
            if fc < MIN_FORECAST_KWH or actual < 0:
                continue
            ratio = min(MAX_FACTOR, max(MIN_FACTOR, actual / fc))
            k = self.key(dt)
            old = self.factors.get(k, 1.0)
            self.factors[k] = round(old + self.alpha * (ratio - old), 4)
            n += 1
        return n

    def apply(self, hours: list[datetime], forecast: list[float]) -> list[float]:
        return [max(0.0, f * self.factor(h)) for h, f in zip(hours, forecast)]

    def to_dict(self) -> dict:
        return {"alpha": self.alpha, "factors": dict(self.factors)}

    @classmethod
    def from_dict(cls, data: Mapping | None) -> PvBias:
        if not data:
            return cls()
        return cls(alpha=float(data.get("alpha", 0.1)), factors=dict(data.get("factors", {})))


def solcast_to_hourly(
    records: Iterable[Mapping], parse_dt, key: str = "pv_estimate"
) -> dict[datetime, float]:
    """Solcast ``detailedForecast`` (kW per 30-min period) -> kWh per hour."""
    out: dict[datetime, float] = {}
    recs = [r for r in records if isinstance(r, Mapping) and "period_start" in r]
    if not recs:
        return out
    starts = []
    for r in recs:
        dt = r["period_start"] if isinstance(r["period_start"], datetime) else parse_dt(str(r["period_start"]))
        if dt is not None:
            starts.append((dt, float(r.get(key, r.get("pv_estimate", 0.0)) or 0.0)))
    starts.sort()
    period_h = 0.5
    if len(starts) > 1:
        period_h = max(0.25, min(1.0, (starts[1][0] - starts[0][0]).total_seconds() / 3600))
    for dt, kw in starts:
        h = dt.replace(minute=0, second=0, microsecond=0)
        out[h] = out.get(h, 0.0) + kw * period_h
    return out
