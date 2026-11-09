"""Loads that may run at a time of Prelumo's choosing (hot water, later other devices).

A load needs ``kwh`` once inside a time window. Every allowed start hour is priced by running the
battery optimiser with the load added there; the cheapest start wins. The battery plan then
counts the load where it will really run: at the chosen hour when Prelumo starts it, at its
usual hour when Prelumo only watches.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

KEEP_GAIN = 0.05  # PLN; the previous choice stays unless another start is cheaper by more
TIE = 0.02  # PLN; starts this close to the cheapest are equal - the latest of them is taken
MIN_KWH = 0.05


@dataclass(frozen=True)
class FlexLoad:
    key: str
    kwh: float
    power_kw: float
    candidates: tuple[int, ...]  # hour indices (in the planning horizon) the run may start in
    usual: int | None = None  # where it runs when Prelumo does not start it
    fixed: int | None = None  # already running since this hour: not moved any more
    previous: int | None = None  # the last choice


@dataclass
class Placement:
    key: str
    kwh: float
    start: int | None = None  # the cheapest start (hour index); None = nothing to place
    usual: int | None = None
    cost_by_start: dict[int, float] = field(default_factory=dict)  # PLN above the cheapest start
    planned: int | None = None  # where the battery plan counts the load

    @property
    def saving_vs_usual(self) -> float | None:
        """PLN saved by the cheapest start against the usual one."""
        if self.start is None or self.usual is None or self.usual not in self.cost_by_start:
            return None
        return round(self.cost_by_start[self.usual] - self.cost_by_start[self.start], 2)


def spread(kwh: float, power_kw: float, start: int, n: int, first_fraction: float = 1.0) -> list[float]:
    """``kwh`` from hour ``start`` on at ``power_kw``; hour 0 has only ``first_fraction`` left."""
    out = [0.0] * n
    left = kwh
    for i in range(max(0, start), n):
        if left <= 1e-9:
            break
        room = power_kw * (first_fraction if i == 0 else 1.0)
        take = left if power_kw <= 0 else min(left, room)
        out[i] += take
        left -= take
    if left > 1e-9 and n:  # does not fit into the horizon: the rest counts in its last hour
        out[n - 1] += left
    return out


def window_starts(
    hours: list[datetime], now: datetime, window: tuple[int, int], duration_h: float,
    day_ok: Callable[[date], bool] = lambda d: True, done: date | None = None,
) -> tuple[list[int], date | None]:
    """Hour indices a run may start in, for the next window (clock hours [start, end)) still open.

    ``done``: the day whose window needs no run any more. A run should end inside the window;
    one too long for it starts at the window's first hour. Returns (indices, the window's day).
    """
    if not hours:
        return [], None
    index = {h: i for i, h in enumerate(hours)}
    first = hours[0].replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(-1, 3):
        day = first + timedelta(days=d)
        w0 = day.replace(hour=window[0])
        w1 = day.replace(hour=window[1] % 24) + timedelta(days=1 if window[1] <= window[0] or window[1] == 24 else 0)
        if w1 <= now or day.date() == done or not day_ok(day.date()):
            continue
        lo = max(w0, hours[0])
        starts = [index[h] for h in hours if lo <= h < w1 and h + timedelta(hours=duration_h) <= w1]
        if not starts and lo in index and lo < w1:
            starts = [index[lo]]
        if starts:
            return starts, day.date()
    return [], None


def place(
    loads: list[FlexLoad], evaluate: Callable[[list[float]], float], n: int,
    first_fraction: float = 1.0, controlled: Callable[[str], bool] = lambda key: True,
) -> tuple[list[float], list[Placement]]:
    """Price every allowed start of every load (one after another, earlier windows first).

    ``evaluate``: cost (PLN) of the best battery plan with the given extra kWh per hour.
    Returns (extra kWh per hour as the battery plan should count it, one Placement per load).
    """
    extra = [0.0] * n
    out: list[Placement] = []
    for load in sorted(loads, key=lambda x: min(x.candidates, default=n)):
        pl = Placement(load.key, round(load.kwh, 2), usual=load.usual)
        out.append(pl)
        if load.kwh < MIN_KWH or (not load.candidates and load.fixed is None):
            continue

        def add(start: int, kwh: float = load.kwh, kw: float = load.power_kw) -> list[float]:
            return [a + b for a, b in zip(extra, spread(kwh, kw, start, n, first_fraction))]

        if load.fixed is not None:
            pl.start = pl.planned = load.fixed
            extra = add(load.fixed)
            continue
        options = sorted(set(load.candidates) | ({load.usual} if load.usual is not None else set()))
        if len(options) == 1:
            pl.start, pl.cost_by_start = options[0], {options[0]: 0.0}
        else:
            cost = {s: evaluate(add(s)) for s in options}
            allowed = {s: cost[s] for s in load.candidates}
            best = min(allowed.values())
            pick = max(s for s, c in allowed.items() if c <= best + TIE)
            if load.previous in allowed and allowed[load.previous] <= best + KEEP_GAIN:
                pick = load.previous
            pl.start = pick
            pl.cost_by_start = {s: round(c - best, 3) for s, c in cost.items()}
        pl.planned = pl.start if controlled(load.key) or load.usual is None else load.usual
        extra = add(pl.planned)
    return extra, out
