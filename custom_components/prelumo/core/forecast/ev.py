"""Tesla charging forecast.

Stage 1 (forecast only): predict *when* and *how much* the car will charge so the
home-battery plan accounts for it. Need priority: manual target > calendar > learned
pattern. When the car is plugged in, the real SoC/limit decides the energy.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median


@dataclass(frozen=True)
class CarState:
    soc: float | None  # %
    limit: float | None  # %
    plugged: bool
    capacity_kwh: float = 75.0
    charger_kw: float = 11.0
    efficiency: float = 0.9  # AC->battery


@dataclass(frozen=True)
class Session:
    plug_in: datetime
    plug_out: datetime
    energy_kwh: float


@dataclass(frozen=True)
class EvNeed:
    energy_kwh: float  # energy drawn from the wall
    ready_by: datetime | None
    start: datetime  # earliest charging start
    source: str  # manual | calendar | plugged | pattern
    probability: float = 1.0


@dataclass
class EvForecast:
    hourly_kwh: list[float]
    needs: list[EvNeed] = field(default_factory=list)
    battery_allowed: list[bool] = field(default_factory=list)  # deadline at risk

    @property
    def total_kwh(self) -> float:
        return sum(self.hourly_kwh)


@dataclass
class EvPattern:
    """Per weekday: probability of a session, median plug-in hour, departure hour, kWh."""

    by_weekday: dict[int, dict[str, float]] = field(default_factory=dict)

    @classmethod
    def learn(cls, sessions: Iterable[Session], weeks: int) -> EvPattern:
        groups: dict[int, list[Session]] = {}
        for s in sessions:
            if s.energy_kwh > 1.0:
                groups.setdefault(s.plug_in.weekday(), []).append(s)
        out: dict[int, dict[str, float]] = {}
        for wd, ss in groups.items():
            out[wd] = {
                "probability": min(1.0, len(ss) / max(1, weeks)),
                "plug_in_hour": median(s.plug_in.hour + s.plug_in.minute / 60 for s in ss),
                "duration_h": median((s.plug_out - s.plug_in).total_seconds() / 3600 for s in ss),
                "energy_kwh": median(s.energy_kwh for s in ss),
            }
        return cls(out)

    def to_dict(self) -> dict:
        return {str(k): v for k, v in self.by_weekday.items()}

    @classmethod
    def from_dict(cls, data: dict | None) -> EvPattern:
        return cls({int(k): v for k, v in (data or {}).items()})


def car_energy_needed(car: CarState, target_soc: float | None = None) -> float:
    target = target_soc if target_soc is not None else car.limit
    if car.soc is None or target is None:
        return 0.0
    return max(0.0, (target - car.soc) / 100 * car.capacity_kwh / car.efficiency)


def _spread(
    hours: list[datetime], start: datetime, energy: float, kw: float, weight: float, out: list[float]
) -> None:
    """Charge at full power from ``start`` until energy is delivered (car default behaviour)."""
    remaining = energy
    for i, h in enumerate(hours):
        if remaining <= 0:
            break
        if h + timedelta(hours=1) <= start:
            continue
        frac = 1.0 if h >= start else (h + timedelta(hours=1) - start).total_seconds() / 3600
        e = min(remaining, kw * frac)
        out[i] += e * weight
        remaining -= e


def forecast_ev(
    hours: list[datetime],
    now: datetime,
    car: CarState,
    pattern: EvPattern | None = None,
    manual: tuple[float, datetime] | None = None,  # (target soc %, ready by)
    calendar: Iterable[tuple[datetime, float | None]] = (),  # (departure, target soc)
    grid_import_kw: float = 1e9,
    base_load: list[float] | None = None,
    default_target_soc: float = 80.0,
) -> EvForecast:
    hourly = [0.0] * len(hours)
    needs: list[EvNeed] = []
    horizon_end = hours[-1] + timedelta(hours=1) if hours else now
    cal = sorted((d, t) for d, t in calendar if now < d <= horizon_end)

    if manual is not None and manual[1] > now:
        tgt, ready = manual
        start = now if car.plugged else ready - timedelta(hours=12)
        needs.append(EvNeed(car_energy_needed(car, tgt), ready, max(now, start), "manual"))
    elif car.plugged:
        ready = cal[0][0] if cal else None
        tgt = cal[0][1] if cal and cal[0][1] is not None else None
        needs.append(EvNeed(car_energy_needed(car, tgt), ready, now, "calendar" if cal else "plugged"))
    elif cal:
        dep, tgt = cal[0]
        energy = car_energy_needed(car, tgt if tgt is not None else default_target_soc)
        if pattern and dep.weekday() in pattern.by_weekday:
            energy = max(energy, pattern.by_weekday[dep.weekday()]["energy_kwh"])
        plug = (dep - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        needs.append(EvNeed(energy, dep, max(now, plug), "calendar"))

    if not needs and pattern:
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        while day < horizon_end:
            p = pattern.by_weekday.get(day.weekday())
            if p:
                start = day + timedelta(hours=p["plug_in_hour"])
                if start > now:
                    needs.append(
                        EvNeed(
                            p["energy_kwh"],
                            start + timedelta(hours=p["duration_h"]),
                            start,
                            "pattern",
                            p["probability"],
                        )
                    )
            day += timedelta(days=1)

    for n in needs:
        _spread(hours, n.start, n.energy_kwh, car.charger_kw, n.probability, hourly)

    allowed = [False] * len(hours)
    base = base_load or [0.0] * len(hours)
    for n in needs:
        if n.ready_by is None or n.source == "pattern":
            continue
        idx = [i for i, h in enumerate(hours) if n.start <= h + timedelta(hours=1) and h < n.ready_by]
        grid_cap = sum(max(0.0, min(car.charger_kw, grid_import_kw - base[i])) for i in idx)
        if n.energy_kwh > grid_cap:  # cannot make it from grid alone -> battery may help
            for i in idx:
                allowed[i] = True
    return EvForecast(hourly, needs, allowed)
