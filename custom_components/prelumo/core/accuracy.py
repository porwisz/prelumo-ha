"""How good were the forecasts: a finished day's forecast against what was metered."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

MIN_HOURS = 12  # a day with fewer forecast hours (restart in the afternoon) is not judged


def day_error(forecast: Mapping[datetime, float], actual: Mapping[datetime, float]) -> dict[str, float] | None:
    """Totals (kWh) of one day over the hours both sides have: forecast, actual, sum of |error|."""
    hours = [h for h in forecast if h in actual]
    if len(hours) < MIN_HOURS:
        return None
    return {
        "fc": round(sum(forecast[h] for h in hours), 3),
        "act": round(sum(actual[h] for h in hours), 3),
        "err": round(sum(abs(forecast[h] - actual[h]) for h in hours), 3),
    }


def summary(days: Iterable[Mapping[str, float]], name: str) -> dict[str, float] | None:
    """Over the given days: hourly error as % of the metered energy and the bias (+ = forecast
    too high). ``name``: the key prefix in the ledger (``load`` / ``pv``)."""
    rows = [d for d in days if f"{name}_act" in d]
    act = sum(d[f"{name}_act"] for d in rows)
    if not rows or act <= 0:
        return None
    return {
        "error_pct": round(sum(d[f"{name}_err"] for d in rows) / act * 100, 1),
        "bias_pct": round((sum(d[f"{name}_fc"] for d in rows) - act) / act * 100, 1),
        "days": len(rows),
    }
