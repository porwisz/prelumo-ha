"""Sell guard: while selling, a house drawing more than forecast limits the sale.

A sell slot discharges at its power until its floor; a house above the forecast eats the energy
the plan sold, so the floor comes early and the house buys the rest of the slot (often at the
expensive price), or the energy kept for the following expensive hours is gone. The guard
measures the house (without the EV) over the last minutes and, when it is clearly above the
forecast, re-plans the hours of the running sale with the measured draw: the optimiser then
sells less (a higher floor) or stops the sale.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

WINDOW = timedelta(minutes=15)  # measured mean over this long
MIN_SPAN = timedelta(minutes=5)  # at least this much measured before it counts
# The draw of the last minutes says little about the afternoon (10 Nov: a heat pump run at 08:15
# counted for all 10 h of a long sell slot - no sale fitted the day any more). The plan is made
# again every few minutes, so the running hour and the next are enough.
MAX_HOURS = 2


class LoadMeter:
    """Rolling mean of the house draw (kW, without the EV), sampled every update."""

    def __init__(self, window: timedelta = WINDOW) -> None:
        self.window = window
        self._s: deque[tuple[datetime, float]] = deque()

    def add(self, now: datetime, kw: float | None) -> None:
        if kw is not None:
            self._s.append((now, max(0.0, kw)))
        while self._s and now - self._s[0][0] > self.window:
            self._s.popleft()

    def mean_kw(self, now: datetime, min_span: timedelta = MIN_SPAN) -> float | None:
        """Mean over the window, or None until ``min_span`` of samples is there."""
        recent = [(t, v) for t, v in self._s if now - t <= self.window]
        if not recent or now - recent[0][0] < min_span:
            return None
        return sum(v for _, v in recent) / len(recent)


@dataclass(frozen=True)
class SellBoost:
    measured_kw: float
    forecast_kw: float
    hours: int  # hours of the running sale re-planned with the measured draw


def boost_sell_load(
    base_load: list[float], heat_pump: list[float], selling: list[bool], measured_kw: float | None,
    margin: float,
) -> tuple[list[float], SellBoost | None]:
    """``base_load`` with the hours of the running sale raised to the measured draw.

    ``selling``: per hour from now, is the sale running (the first ``True`` run is used, it must
    start now; at most MAX_HOURS of it). Applies only when the draw is above the forecast of this hour by ``margin``.
    """
    if measured_kw is None or not base_load or not selling or not selling[0]:
        return base_load, None
    forecast = base_load[0] + heat_pump[0]
    if measured_kw <= forecast * (1 + margin) + 1e-9:
        return base_load, None
    out = list(base_load)
    n = 0
    while n < min(len(out), len(selling), MAX_HOURS) and selling[n]:
        out[n] += max(0.0, measured_kw - (base_load[n] + heat_pump[n]))
        n += 1
    return out, SellBoost(round(measured_kw, 2), round(forecast, 2), n)
