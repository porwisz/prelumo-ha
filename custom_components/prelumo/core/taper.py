"""Learn the BMS charge taper (the last % charge slowly) from the SoC history.

For every climb to 100 % the minutes spent in each band (90-95, 95-98, 98-99, 99-100 %) are
measured. The planner uses the slowest of the recent charges, so a due 100 % is started early
enough even when the BMS balances the cells for over an hour (15 Oct: 99 -> 100 % took 78 min,
7 Oct: 31 min).

A climb that stalls in the last % (reaches 99 %, then drops without reaching 100 %) still counts:
its time at 99 % is a lower bound for that band (22 Oct: 50 min at 99 %, then the expensive
hours stopped the charge).
"""

from __future__ import annotations

from datetime import datetime, timedelta

BAND_EDGES = (90, 95, 98, 99, 100)
STALL_BAND = 99  # only the last % - a stop below it is an everyday charge limit, not the taper
DEFAULT_TAPER: tuple[tuple[int, float], ...] = ((90, 5.5), (95, 3.0), (98, 1.5), (99, 0.5))
MAX_BAND = timedelta(hours=3)  # longer = the charge was interrupted, not a taper
KEEP_EVENTS = 8
USE_EVENTS = 5


def _band_minutes(reach: dict[int, datetime]) -> dict[str, float]:
    mins: dict[str, float] = {}
    for a, b in zip(BAND_EDGES, BAND_EDGES[1:]):
        if a in reach and b in reach:
            d = reach[b] - reach[a]
            if timedelta(0) <= d <= MAX_BAND:
                mins[str(a)] = round(d.total_seconds() / 60, 1)
    return mins


def charge_events(points: list[tuple[datetime, float]]) -> list[dict]:
    """Minutes per band for each continuous climb from below 90 % to 100 %.

    ``points``: (time, SoC %) state changes, oldest first. A drop of the SoC restarts the climb;
    a climb that stalled at 99 % adds ``at_least`` (minutes spent at 99 % before the drop).
    """
    events: list[dict] = []
    reach: dict[int, datetime] = {}
    prev: float | None = None
    for t, soc in points:
        if prev is not None and soc < prev - 0.5:
            stall = t - reach[STALL_BAND] if STALL_BAND in reach and BAND_EDGES[0] in reach else None
            if stall is not None and timedelta(0) < stall <= MAX_BAND:
                events.append({
                    "at": reach[STALL_BAND].isoformat(), "minutes": _band_minutes(reach),
                    "at_least": {str(STALL_BAND): round(stall.total_seconds() / 60, 1)},
                })
            reach = {}
        lvl = int(soc)
        if prev is None:
            reach.setdefault(lvl, t)
        else:
            for k in range(int(prev) + 1, lvl + 1):
                reach.setdefault(k, t)
        prev = soc
        if lvl >= 100 and 100 in reach and BAND_EDGES[0] in reach:
            mins = _band_minutes(reach)
            if mins:
                events.append({"at": reach[100].isoformat(), "minutes": mins})
            reach = {}  # count each climb once
    return events


def merge_events(old: list[dict], new: list[dict]) -> list[dict]:
    by_at = {e["at"]: e for e in old}
    by_at.update({e["at"]: e for e in new})
    return [by_at[k] for k in sorted(by_at)][-KEEP_EVENTS:]


def taper_from_events(events: list[dict], capacity_kwh: float) -> tuple[tuple[int, float], ...]:
    """Battery-side kW per band: the slowest of the last charges (default where none measured).

    A stalled climb's ``at_least`` minutes cap the band's rate: it was at most that fast.
    """
    recent = events[-USE_EVENTS:]
    out = []
    for (a, b), (start, default_kw) in zip(zip(BAND_EDGES, BAND_EDGES[1:]), DEFAULT_TAPER):
        kwh = (b - a) / 100 * capacity_kwh
        rates = [kwh / (m / 60) for e in recent for key in ("minutes", "at_least")
                 if (m := e.get(key, {}).get(str(a))) and m > 0]
        out.append((start, round(min(rates), 2) if rates else default_kw))
    return tuple(out)
