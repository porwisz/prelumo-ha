"""Heat pump (space heating + DHW) energy forecast from outdoor temperature.

Model per hour:  kWh = a * max(0, base_temp - T) + dhw[hour]
``a`` is fitted by least squares through the origin on heating-degree-hours after
subtracting the hourly DHW profile (median of hours with no heating demand).
"""

from __future__ import annotations

from datetime import datetime
from statistics import median


class HeatPumpModel:
    def __init__(self, base_temp: float = 15.0) -> None:
        self.base_temp = base_temp
        self.coef = 0.0  # kWh per degree-hour
        self.dhw: dict[int, float] = {}

    def _hdh(self, t: float) -> float:
        return max(0.0, self.base_temp - t)

    def fit(self, energy: dict[datetime, float], temp: dict[datetime, float]) -> None:
        pairs = [(h, e, temp[h]) for h, e in energy.items() if h in temp]
        if not pairs:
            return
        # DHW profile: hours without heating demand; fallback: hourly minimum
        no_heat: dict[int, list[float]] = {}
        all_by_h: dict[int, list[float]] = {}
        for h, e, t in pairs:
            all_by_h.setdefault(h.hour, []).append(e)
            if self._hdh(t) <= 0:
                no_heat.setdefault(h.hour, []).append(e)
        self.dhw = {
            hr: median(no_heat[hr]) if no_heat.get(hr) else min(vals) for hr, vals in all_by_h.items()
        }
        num = den = 0.0
        for h, e, t in pairs:
            x = self._hdh(t)
            if x <= 0:
                continue
            y = max(0.0, e - self.dhw.get(h.hour, 0.0))
            num += x * y
            den += x * x
        self.coef = num / den if den else 0.0

    def predict(self, hours: list[datetime], temp_forecast: dict[datetime, float]) -> list[float]:
        out = []
        last_t = next(iter(temp_forecast.values()), self.base_temp) if temp_forecast else self.base_temp
        for h in hours:
            t = temp_forecast.get(h, last_t)
            last_t = t
            out.append(self.coef * self._hdh(t) + self.dhw.get(h.hour, 0.0))
        return out

    def to_dict(self) -> dict:
        return {"base_temp": self.base_temp, "coef": self.coef, "dhw": self.dhw}
