"""Hot water (DHW) heated by the heat pump: what a reheat costs and when it usually happens.

Learned from the tank temperature and the heat pump power: every climb of the tank temperature
is one heating run, the energy the compressor took meanwhile is its cost. From the runs:
kWh per degree, the heat pump's power while heating water, and the hourly energy to take out of
the heat pump's history, so that the space-heating model is fitted on heating alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median, median_low

DEFAULT_KWH_PER_DEG = 0.15  # ~300 l tank, COP ~2.3
DEFAULT_POWER_KW = 2.5
MIN_RISE = 2.0  # deg C; smaller climbs are sensor noise or a short top-up
RISE_START = 1.0  # deg C above the last low: the tank is being heated
RISE_END = 0.5  # deg C below the top: the run is over
MAX_RUN = timedelta(hours=4)
LEAD = timedelta(minutes=5)  # the tank sensor reacts after the compressor has started
KEEP_DAYS = 45


class DhwError(ValueError):
    pass


@dataclass(frozen=True)
class DhwBoost:
    """Heat the tank to ``target`` once a day inside clock hours [start, end), then set ``after``."""

    start: int
    end: int
    target: float
    after: float
    peak_only: bool = False  # only on days with an expensive afternoon zone to bridge

    @property
    def key(self) -> str:
        return f"{self.start:02d}-{self.end:02d}"

    @property
    def window(self) -> tuple[int, int]:
        return self.start, self.end

    def covers(self, hour: int) -> bool:
        if self.start < self.end:
            return self.start <= hour < self.end
        return hour >= self.start or hour < self.end


def parse_boosts(text: str) -> list[DhwBoost]:
    """'HH:00-HH:00 TARGET AFTER [peak]; ...' - e.g. '03:00-07:00 50 40; 13:00-16:00 53 40 peak'."""
    out = []
    for part in (p.strip() for p in text.replace("\n", ";").split(";")):
        if not part:
            continue
        bits = part.replace(",", " ").split()
        try:
            a, b = bits[0].split("-")
            (h0, m0), (h1, m1) = ([int(x) for x in t.split(":")] for t in (a, b))
            target, after = float(bits[1]), float(bits[2])
        except (ValueError, IndexError) as err:
            raise DhwError(f"bad entry: {part}") from err
        flags = [f.lower() for f in bits[3:]]
        if m0 or m1 or not (0 <= h0 <= 23 and 0 <= h1 <= 24) or h0 == h1 % 24 or any(f != "peak" for f in flags):
            raise DhwError(f"bad entry: {part}")
        if not (10 <= after <= target <= 70):
            raise DhwError(f"bad temperatures: {part}")
        out.append(DhwBoost(h0, h1, target, after, "peak" in flags))
    return out


def _energy(power: list[tuple[datetime, float]], t0: datetime, t1: datetime) -> float:
    """kWh between t0 and t1 of a power reading (kW) that holds its value until the next one."""
    kwh = 0.0
    for (a, kw), (b, _) in zip(power, [*power[1:], (t1, 0.0)]):
        lo, hi = max(a, t0), min(b, t1)
        if hi > lo:
            kwh += max(0.0, kw) * (hi - lo).total_seconds() / 3600
    return kwh


def heating_events(temp: list[tuple[datetime, float]], power: list[tuple[datetime, float]]) -> list[dict]:
    """One dict per climb of the tank temperature: start, end (ISO), t0, t1 (deg C), kwh.

    ``temp`` / ``power``: (time, value) state changes, oldest first; power in kW.
    """
    out: list[dict] = []
    low: tuple[datetime, float] | None = None
    top: tuple[datetime, float] | None = None  # set while a climb is running

    def close() -> None:
        if low is None or top is None:
            return
        rise, took = top[1] - low[1], top[0] - low[0]
        if rise >= MIN_RISE and took <= MAX_RUN:
            kwh = _energy(power, low[0] - LEAD, top[0])
            if kwh > 0.05:
                out.append({"start": low[0].isoformat(), "end": top[0].isoformat(),
                            "t0": round(low[1], 1), "t1": round(top[1], 1), "kwh": round(kwh, 3)})

    for t, v in temp:
        if top is None:
            if low is None or v <= low[1]:
                low = (t, v)
            elif v >= low[1] + RISE_START:
                top = (t, v)
        elif v >= top[1]:
            top = (t, v)
        elif v <= top[1] - RISE_END:
            close()
            low, top = (t, v), None
    return out  # a climb still running is left for the next look


def merge_events(old: list[dict], new: list[dict], now: datetime) -> list[dict]:
    """Known runs plus the new ones (by start time), the last KEEP_DAYS days, oldest first."""
    by_start = {e["start"]: e for e in old}
    by_start.update({e["start"]: e for e in new})
    limit = now - timedelta(days=KEEP_DAYS)
    return [e for k, e in sorted(by_start.items()) if _dt(e["start"]) >= limit]


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def kwh_per_degree(events: list[dict]) -> float | None:
    vals = [e["kwh"] / (e["t1"] - e["t0"]) for e in events if e["t1"] - e["t0"] >= MIN_RISE]
    return round(median(vals), 4) if len(vals) >= 3 else None


def power_kw(events: list[dict]) -> float | None:
    vals = []
    for e in events:
        h = (_dt(e["end"]) - _dt(e["start"]) + LEAD).total_seconds() / 3600
        if h >= 0.15:
            vals.append(e["kwh"] / h)
    return round(median(vals), 2) if len(vals) >= 3 else None


def hourly_kwh(events: list[dict]) -> dict[datetime, float]:
    """The runs' energy per clock hour (spread evenly over each run)."""
    out: dict[datetime, float] = {}
    for e in events:
        a, b = _dt(e["start"]) - LEAD, _dt(e["end"])
        total = (b - a).total_seconds()
        if total <= 0:
            continue
        h = a.replace(minute=0, second=0, microsecond=0)
        while h < b:
            part = (min(b, h + timedelta(hours=1)) - max(a, h)).total_seconds() / total
            out[h] = out.get(h, 0.0) + e["kwh"] * part
            h += timedelta(hours=1)
    return out


def _in_boost(e: dict, boost: DhwBoost) -> bool:
    """A run that looks like this boost: started in its window and reached about its target."""
    return boost.covers(_dt(e["start"]).hour) and e["t1"] >= boost.target - 3


def usual_start(events: list[dict], boost: DhwBoost) -> int | None:
    """Clock hour the boost has usually started in (None: never seen)."""
    hours = [_dt(e["start"]).hour for e in events if _in_boost(e, boost)]
    return median_low(hours) if hours else None


def usual_start_temp(events: list[dict], boost: DhwBoost) -> float | None:
    """Tank temperature the boost has usually started from."""
    temps = [e["t0"] for e in events if _in_boost(e, boost)]
    return round(median(temps), 1) if temps else None


def residual_profile(events: list[dict], boosts: list[DhwBoost], now: datetime) -> dict[int, float]:
    """kWh per clock hour of the reheats that are not a boost (after showers, keeping the
    temperature), averaged over the days the runs cover."""
    rest = [e for e in events if not any(_in_boost(e, b) for b in boosts)]
    if not events:
        return {}
    days = max(1, (now.date() - _dt(events[0]["start"]).date()).days)
    by_hour: dict[int, float] = {}
    for h, kwh in hourly_kwh(rest).items():
        by_hour[h.hour] = by_hour.get(h.hour, 0.0) + kwh
    return {h: round(v / days, 3) for h, v in sorted(by_hour.items())}


def boost_kwh(boost: DhwBoost, start_temp: float | None, per_degree: float) -> float:
    """Energy of one boost from ``start_temp`` (unknown: from the temperature kept after it)."""
    t0 = boost.after if start_temp is None else start_temp
    return max(0.0, boost.target - t0) * per_degree
