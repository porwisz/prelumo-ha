"""Day plan model for a Deye-style 6-slot Time-of-Use schedule.

Pure Python, no Home Assistant imports. Validation mirrors the ESPHome
``zapisz_harmonogram`` action exactly, so a plan accepted here is accepted by the ESP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from enum import StrEnum

SLOT_COUNT = 6
MAX_POWER_W = 12000

FLAG_GRID_CHARGE = 1 << 0
FLAG_GEN_CHARGE = 1 << 1
FLAG_SELL = 1 << 5
# Bits we own; everything else (bit1, bits 2-4) is preserved from the inverter.
MANAGED_FLAGS = FLAG_GRID_CHARGE | FLAG_SELL


class PlanError(ValueError):
    """Plan does not pass validation."""


class SlotMode(StrEnum):
    NONE = "none"
    GRID_CHARGE = "grid_charge"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class Slot:
    start: int  # HHMM
    power: int  # W, max battery discharge power in the slot
    soc: int  # %, grid-charge target (bit0) or discharge floor
    flags: int = 0

    @property
    def mode(self) -> SlotMode:
        if self.flags & FLAG_SELL:
            return SlotMode.SELL
        if self.flags & FLAG_GRID_CHARGE:
            return SlotMode.GRID_CHARGE
        return SlotMode.NONE

    @property
    def start_minutes(self) -> int:
        return (self.start // 100) * 60 + self.start % 100

    @property
    def start_time(self) -> time:
        return time(self.start // 100, self.start % 100)

    def with_mode(self, mode: SlotMode) -> Slot:
        flags = self.flags & ~MANAGED_FLAGS
        if mode is SlotMode.GRID_CHARGE:
            flags |= FLAG_GRID_CHARGE
        elif mode is SlotMode.SELL:
            flags |= FLAG_SELL
        return replace(self, flags=flags)

    def as_dict(self) -> dict:
        return {
            "start": format_hhmm(self.start),
            "power": self.power,
            "soc": self.soc,
            "flags": self.flags,
            "mode": self.mode.value,
        }


@dataclass(frozen=True, slots=True)
class DayPlan:
    slots: tuple[Slot, ...]

    def __post_init__(self) -> None:
        validate(self.slots)

    # --- construction -------------------------------------------------
    @classmethod
    def from_lists(
        cls, start: list[int], power: list[int], soc: list[int], flags: list[int]
    ) -> DayPlan:
        if not len(start) == len(power) == len(soc) == len(flags) == SLOT_COUNT:
            raise PlanError("each list must have 6 elements")
        return cls(tuple(Slot(*v) for v in zip(start, power, soc, flags)))

    def to_service_data(self) -> dict[str, list[int]]:
        """Payload for ``esphome.<device>_zapisz_harmonogram``."""
        return {
            "start": [s.start for s in self.slots],
            "moc": [s.power for s in self.slots],
            "soc": [s.soc for s in self.slots],
            "flagi": [s.flags for s in self.slots],
        }

    def with_slot(self, index: int, slot: Slot) -> DayPlan:
        slots = list(self.slots)
        slots[index] = slot
        return DayPlan(tuple(slots))

    def merge_preserved_flags(self, current: DayPlan | None) -> DayPlan:
        """Keep bits we do not manage (1-4) as read from the inverter."""
        if current is None:
            return self
        return DayPlan(
            tuple(
                replace(new, flags=(new.flags & MANAGED_FLAGS) | (old.flags & ~MANAGED_FLAGS))
                for new, old in zip(self.slots, current.slots)
            )
        )

    # --- time queries -------------------------------------------------
    def active_index(self, now: datetime | time) -> int:
        t = now.time() if isinstance(now, datetime) else now
        minutes = t.hour * 60 + t.minute
        best, best_start = None, -1
        for i, s in enumerate(self.slots):
            if s.start_minutes <= minutes and s.start_minutes > best_start:
                best, best_start = i, s.start_minutes
        if best is None:  # before first start of the day -> slot with the latest start
            best = max(range(SLOT_COUNT), key=lambda i: self.slots[i].start_minutes)
        return best

    def active_slot(self, now: datetime | time) -> Slot:
        return self.slots[self.active_index(now)]

    def next_change(self, now: datetime) -> tuple[datetime, int]:
        """Datetime of the next slot boundary and the index of the slot starting then."""
        idx = (self.active_index(now) + 1) % SLOT_COUNT
        st = self.slots[idx].start_time
        cand = now.replace(hour=st.hour, minute=st.minute, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        return cand, idx

    def as_list(self) -> list[dict]:
        return [s.as_dict() for s in self.slots]


def validate(slots: tuple[Slot, ...] | list[Slot]) -> None:
    if len(slots) != SLOT_COUNT:
        raise PlanError("plan must have 6 slots")
    for i, s in enumerate(slots, 1):
        h, m = divmod(s.start, 100)
        if s.start < 0 or h > 23 or m > 59:
            raise PlanError(f"slot {i}: invalid time {s.start} (HHMM)")
        if not 0 <= s.power <= MAX_POWER_W:
            raise PlanError(f"slot {i}: power {s.power} outside 0-{MAX_POWER_W} W")
        if not 0 <= s.soc <= 100:
            raise PlanError(f"slot {i}: SoC {s.soc} outside 0-100")
        if not 0 <= s.flags <= 0x3F:
            raise PlanError(f"slot {i}: flags {s.flags} outside 0-63")
    drops = 0
    for i in range(SLOT_COUNT):
        cur, nxt = slots[i].start, slots[(i + 1) % SLOT_COUNT].start
        if nxt == cur:
            raise PlanError(f"slot {i + 1} and {(i + 1) % SLOT_COUNT + 1}: same start")
        if nxt < cur:
            drops += 1
    if drops != 1:
        raise PlanError("starts must increase cyclically (at most one midnight crossing)")


# --- parsing helpers (inverter sensor text) ---------------------------
_PAREN_NUM = re.compile(r"\((\d+)\)")


def parse_hhmm(value: str | int) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if ":" in text:
        h, m = text.split(":")[:2]
        return int(h) * 100 + int(m)
    return int(float(text))


def format_hhmm(value: int) -> str:
    return f"{value // 100:02d}:{value % 100:02d}"


def parse_flags(value: str | int) -> int:
    """'Sprzedaz (32)' -> 32; plain numbers pass through."""
    if isinstance(value, int):
        return value
    m = _PAREN_NUM.search(str(value))
    if m:
        return int(m.group(1))
    return int(float(value))


def parse_mode(value: str) -> SlotMode:
    return SlotMode(value)
