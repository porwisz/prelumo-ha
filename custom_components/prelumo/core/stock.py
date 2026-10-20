"""Average cost of the energy now in the battery (weighted-average cost accounting).

Observation only — nothing here feeds the optimiser. Charging adds energy at its
acquisition price (grid energy at the buy price of that moment, PV at 0 zł) and
discharging removes it at the running average, so ``avg`` answers "what did the kWh
sitting in the battery cost me". At min SoC the stock is reset: the battery is empty,
so whatever was bought before has been used up.

Energies are battery-side kWh — what the inverter's charge/discharge counters report —
and prices are PLN per battery-side kWh, so the charging loss is already inside them.
``cost_per_delivered_kwh`` adds the discharge loss and the wear cost: below that sell
price a sale loses money.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

EPS = 0.001  # kWh
MAX_TICK_KWH = 2.0  # a larger counter jump is a restart gap, not a tick: skip it


@dataclass(frozen=True)
class TickEnergy:
    """Battery-side kWh moved in one tick, and how much of the charge came from the grid."""

    charged: float
    discharged: float
    from_grid: float


def account_tick(
    charged: float, discharged: float, imported: float, house: float, pv: float, eta: float
) -> TickEnergy:
    """Split a tick's battery charging into grid-fed and PV-fed kWh (battery side).

    PV covers the house first; only the import left over from the house can have charged
    the battery. ``eta`` converts between the AC side (where the import is metered) and
    the battery side (where the charge counter is).
    """
    charged, discharged = max(0.0, charged), max(0.0, discharged)
    ac_in = charged / eta if eta > 0 else charged
    house_from_grid = max(0.0, house - pv)
    to_battery = max(0.0, imported - house_from_grid)
    from_grid = min(ac_in, to_battery) * eta
    return TickEnergy(charged, discharged, min(charged, from_grid))


@dataclass
class BatteryStock:
    """Energy in the battery above min SoC, with what it cost."""

    kwh: float = 0.0
    value_pln: float = 0.0
    grid_kwh: float = 0.0  # of ``kwh``: bought from the grid (the rest is PV)
    estimated: bool = True  # True until the first reset at min SoC makes the figure exact
    last_reset: str | None = None

    @property
    def avg(self) -> float | None:
        """PLN per battery-side kWh now stored; None when the battery is empty."""
        return self.value_pln / self.kwh if self.kwh > EPS else None

    @property
    def grid_share(self) -> float | None:
        return self.grid_kwh / self.kwh if self.kwh > EPS else None

    def cost_per_delivered_kwh(self, eta: float, wear: float = 0.0) -> float | None:
        """What a kWh taken out of the battery really costs (discharge loss + wear)."""
        avg = self.avg
        if avg is None or eta <= 0:
            return None
        return avg / eta + wear

    def charge(self, kwh: float, price: float, *, from_grid: bool = False) -> None:
        if kwh <= EPS:
            return
        self.kwh += kwh
        self.value_pln += kwh * price
        if from_grid:
            self.grid_kwh += kwh

    def discharge(self, kwh: float) -> None:
        if kwh <= EPS or self.kwh <= EPS:
            return
        keep = max(0.0, self.kwh - kwh) / self.kwh
        self.kwh *= keep
        self.value_pln *= keep
        self.grid_kwh *= keep

    def reset(self, when: datetime | None = None) -> None:
        """Battery drained to min SoC: nothing left, and from now on the figure is exact."""
        self.kwh = self.value_pln = self.grid_kwh = 0.0
        self.estimated = False
        if when is not None:
            self.last_reset = when.isoformat()

    def rescale(self, kwh_actual: float, seed_price: float | None = None) -> None:
        """Anchor the stock to the SoC reading, keeping the average price.

        Integrating counters drifts and restarts lose ticks; the SoC is the truth about
        *how much* is in there. With nothing accounted yet (first run) the stock is seeded
        at ``seed_price`` and counted as grid energy — the pessimistic assumption for the
        break-even price — and stays flagged as estimated.
        """
        if kwh_actual < EPS:
            self.kwh = self.value_pln = self.grid_kwh = 0.0
            return
        if self.kwh > EPS:
            f = kwh_actual / self.kwh
            self.kwh, self.value_pln, self.grid_kwh = kwh_actual, self.value_pln * f, self.grid_kwh * f
        elif seed_price is not None:
            self.kwh, self.value_pln, self.grid_kwh = kwh_actual, kwh_actual * seed_price, kwh_actual
            self.estimated = True

    def to_dict(self) -> dict:
        return {
            "kwh": round(self.kwh, 4),
            "value_pln": round(self.value_pln, 4),
            "grid_kwh": round(self.grid_kwh, 4),
            "estimated": self.estimated,
            "last_reset": self.last_reset,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> BatteryStock:
        if not data:
            return cls()
        return cls(
            kwh=float(data.get("kwh", 0.0)),
            value_pln=float(data.get("value_pln", 0.0)),
            grid_kwh=float(data.get("grid_kwh", 0.0)),
            estimated=bool(data.get("estimated", True)),
            last_reset=data.get("last_reset"),
        )
