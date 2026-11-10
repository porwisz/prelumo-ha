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
    sell: list[float] | None = None,
) -> tuple[list[float], list[Placement]]:
    """Price every allowed start of every load (one after another, earlier windows first).

    ``evaluate``: cost (PLN) of the best battery plan with the given extra kWh per hour.
    ``sell``: sell price per hour. Among starts that cost the same the one whose hours have the
    lowest sell price is taken (then the latest): when the plan sells nothing today, the load
    still keeps out of the hours a sale would pay best in, should the plan change.
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
            def worth(start: int, kwh: float = load.kwh, kw: float = load.power_kw) -> float:
                """What the load's energy would earn if sold in the hours of this start (PLN)."""
                if sell is None:
                    return 0.0
                return round(sum(e * max(0.0, p) for e, p in zip(spread(kwh, kw, start, n, first_fraction), sell)), 2)

            pick = min((s for s, c in allowed.items() if c <= best + TIE), key=lambda s: (worth(s), -s))
            prev = load.previous
            if prev in allowed and allowed[prev] <= best + KEEP_GAIN and worth(prev) <= worth(pick) + KEEP_GAIN:
                pick = prev
            pl.start = pick
            pl.cost_by_start = {s: round(c - best, 3) for s, c in cost.items()}
        # not started by Prelumo: counted at its usual hour, or not at all when that is unknown
        # (its consumption is then part of the learned house profile)
        pl.planned = pl.start if controlled(load.key) else load.usual
        if pl.planned is not None:
            extra = add(pl.planned)
    return extra, out


class LoadError(ValueError):
    pass


@dataclass(frozen=True)
class TimedLoad:
    """A device that runs once a day for a fixed energy at a fixed power, inside a window."""

    name: str
    start: int  # clock hours [start, end)
    end: int
    kwh: float
    power_kw: float
    entity: str | None = None  # turned on for the run and off after it
    peak_only: bool = False  # only on working days

    @property
    def key(self) -> str:
        return f"load:{self.name}"

    @property
    def window(self) -> tuple[int, int]:
        return self.start, self.end

    @property
    def duration(self) -> timedelta:
        return timedelta(hours=self.kwh / self.power_kw)

    def covers(self, hour: int) -> bool:
        if self.start < self.end:
            return self.start <= hour < self.end
        return hour >= self.start or hour < self.end


def parse_loads(text: str) -> list[TimedLoad]:
    """'name HH:00-HH:00 kWh kW [entity_id] [peak]; ...' - e.g.
    'dishwasher 22:00-06:00 1.2 2.0 switch.dishwasher; pool 10:00-16:00 3 1.5'."""
    out: list[TimedLoad] = []
    for part in (p.strip() for p in text.replace("\n", ";").split(";")):
        if not part:
            continue
        bits = part.split()
        try:
            name = bits[0]
            a, b = bits[1].split("-")
            (h0, m0), (h1, m1) = ([int(x) for x in t.split(":")] for t in (a, b))
            kwh, kw = float(bits[2].replace(",", ".")), float(bits[3].replace(",", "."))
        except (ValueError, IndexError) as err:
            raise LoadError(f"bad entry: {part}") from err
        rest = bits[4:]
        entity = next((x for x in rest if "." in x), None)
        flags = [x.lower() for x in rest if x != entity]
        if (m0 or m1 or not (0 <= h0 <= 23 and 0 <= h1 <= 24) or h0 == h1 % 24 or kwh <= 0 or kw <= 0
                or any(f != "peak" for f in flags) or any(x.name == name for x in out)):
            raise LoadError(f"bad entry: {part}")
        out.append(TimedLoad(name, h0, h1, kwh, kw, entity, "peak" in flags))
    return out
