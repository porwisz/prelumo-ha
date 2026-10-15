"""Learn the BMS charge taper (the last % charge slowly) from the SoC history.

For every climb to 100 % the minutes spent in each band (90-95, 95-98, 98-99, 99-100 %) are
measured. The planner uses the slowest of the recent charges, so a due 100 % is started early
enough even when the BMS balances the cells for over an hour (15 Oct: 99 -> 100 % took 78 min,
7 Oct: 31 min).
"""

from __future__ import annotations

from datetime import datetime, timedelta

BAND_EDGES = (90, 95, 98, 99, 100)
DEFAULT_TAPER: tuple[tuple[int, float], ...] = ((90, 5.5), (95, 3.0), (98, 1.5), (99, 0.5))
MAX_BAND = timedelta(hours=3)  # longer = the charge was interrupted, not a taper
KEEP_EVENTS = 8
USE_EVENTS = 5


def charge_events(points: list[tuple[datetime, float]]) -> list[dict]:
    """Minutes per band for each continuous climb from below 90 % to 100 %.

    ``points``: (time, SoC %) state changes, oldest first. A drop of the SoC restarts the climb.
    """
    events: list[dict] = []
    reach: dict[int, datetime] = {}
    prev: float | None = None
    for t, soc in points:
        if prev is not None and soc < prev - 0.5:
            reach = {}
        lvl = int(soc)
        if prev is None:
            reach.setdefault(lvl, t)
        else:
            for k in range(int(prev) + 1, lvl + 1):
                reach.setdefault(k, t)
        prev = soc
        if lvl >= 100 and 100 in reach and BAND_EDGES[0] in reach:
            mins: dict[str, float] = {}
            for a, b in zip(BAND_EDGES, BAND_EDGES[1:]):
                if a in reach and b in reach:
                    d = reach[b] - reach[a]
                    if timedelta(0) <= d <= MAX_BAND:
                        mins[str(a)] = round(d.total_seconds() / 60, 1)
            if mins:
                events.append({"at": reach[100].isoformat(), "minutes": mins})
            reach = {}  # count each climb once
    return events


def merge_events(old: list[dict], new: list[dict]) -> list[dict]:
    by_at = {e["at"]: e for e in old}
    by_at.update({e["at"]: e for e in new})
    return [by_at[k] for k in sorted(by_at)][-KEEP_EVENTS:]


def taper_from_events(events: list[dict], capacity_kwh: float) -> tuple[tuple[int, float], ...]:
    """Battery-side kW per band: the slowest of the last charges (default where none measured)."""
    recent = events[-USE_EVENTS:]
    out = []
    for (a, b), (start, default_kw) in zip(zip(BAND_EDGES, BAND_EDGES[1:]), DEFAULT_TAPER):
        kwh = (b - a) / 100 * capacity_kwh
        rates = [kwh / (m / 60) for e in recent if (m := e["minutes"].get(str(a))) and m > 0]
        out.append((start, round(min(rates), 2) if rates else default_kw))
    return tuple(out)
