"""Base house load profile (weekday x hour), excluding heat pump and EV."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from statistics import median

DEFAULT_KWH = 0.5


def base_load_samples(
    house: dict[datetime, float],
    heat_pump: dict[datetime, float] | None = None,
    ev: dict[datetime, float] | None = None,
) -> dict[datetime, float]:
    """house - heat pump - EV, per hour (kWh), clamped at 0."""
    hp, evd = heat_pump or {}, ev or {}
    return {h: max(0.0, v - hp.get(h, 0.0) - evd.get(h, 0.0)) for h, v in house.items()}


class LoadProfile:
    def __init__(self, samples: dict[datetime, float] | None = None) -> None:
        self._wd_h: dict[tuple[int, int], float] = {}
        self._h: dict[int, float] = {}
        self._all = DEFAULT_KWH
        if samples:
            self.fit(samples)

    def fit(self, samples: dict[datetime, float]) -> None:
        by_wd_h: dict[tuple[int, int], list[float]] = {}
        by_h: dict[int, list[float]] = {}
        for dt, v in samples.items():
            by_wd_h.setdefault((dt.weekday(), dt.hour), []).append(v)
            by_h.setdefault(dt.hour, []).append(v)
        # need >= 2 samples for a weekday-specific value, otherwise fall back to the hour
        self._wd_h = {k: median(v) for k, v in by_wd_h.items() if len(v) >= 2}
        self._h = {k: median(v) for k, v in by_h.items()}
        if samples:
            self._all = median(samples.values())

    def predict(self, hours: Iterable[datetime]) -> list[float]:
        return [
            self._wd_h.get((h.weekday(), h.hour), self._h.get(h.hour, self._all)) for h in hours
        ]

    @property
    def sample_hours(self) -> int:
        return len(self._wd_h)
