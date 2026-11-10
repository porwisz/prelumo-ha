#!/usr/bin/env python3
"""Backtest: run the optimiser over recorded days and report what each day would cost.

    python scripts/backtest.py days/*.txt [--params params.json] [--core DIR] [--plain] [--plan]

A day file is plain text: the first line "YYYY-MM-DD HH SOC" (first hour, battery % at its
start), then one line per hour "buy sell load pv [ev]" (PLN/kWh, PLN/kWh, kWh, kWh, kWh). The
numbers are the ``slot_hourly`` attribute of ``sensor.prelumo_proposed_plan`` (the forecast a
plan was made on) or metered values.

``--core DIR`` runs another checkout's ``custom_components/prelumo`` (e.g. a git worktree of
an older tag), so two versions can be compared on the same days. ``--plain`` switches the
price rules off (reserve by price, charging above the cap for a sale).

Reported per day, in PLN (negative = earned): the cost without an optimiser, the hourly
optimum, and the same plan as the inverter would run it hour by hour (its hard-rule breaks
listed). Slots (6 per schedule) and forecast errors are not modelled: a comparison of rules
on equal inputs, not a forecast of the bill.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields, replace
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_CORE = Path(__file__).resolve().parent.parent / "custom_components" / "prelumo"


def read_day(path: Path) -> tuple[datetime, float, list[tuple[float, ...]]]:
    lines = [ln.split("#")[0].split() for ln in path.read_text().splitlines()]
    lines = [ln for ln in lines if ln]
    day, hour, soc = lines[0]
    start = datetime.fromisoformat(day) + timedelta(hours=int(hour))
    rows = [tuple(float(x) for x in ln) for ln in lines[1:]]
    if any(len(r) not in (4, 5) for r in rows):
        raise ValueError(f"{path}: each hour is 'buy sell load pv [ev]'")
    return start, float(soc), rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("days", nargs="+", type=Path)
    ap.add_argument("--params", type=Path, help="JSON with BatteryParams fields")
    ap.add_argument("--core", type=Path, default=DEFAULT_CORE)
    ap.add_argument("--plain", action="store_true", help="price rules off")
    ap.add_argument("--no-arbitrage", action="store_true")
    ap.add_argument("--plan", action="store_true", help="print the hourly plan of each day")
    ap.add_argument("--cycles", type=float, default=0.0, help="full battery cycles a day (0 = no limit)")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(args.core))
    from core import optimizer as opt
    from core.compress import CAP_PENALTY, _expand, _segments, simulate

    known = {f.name for f in fields(opt.BatteryParams)}
    given = json.loads(args.params.read_text()) if args.params else {}
    if "charge_taper" in given:
        given["charge_taper"] = tuple(tuple(x) for x in given["charge_taper"])
    if args.plain:
        given.pop("reserve_price_margin", None)
    skipped = sorted(set(given) - known)
    p = opt.BatteryParams(**{k: v for k, v in given.items() if k in known})
    arbitrage = not args.no_arbitrage
    mark = getattr(opt, "charge_for_sale", None)

    print(f"core: {args.core}" + (f"  (not in this version: {', '.join(skipped)})" if skipped else ""))
    print(f"{'day':<12}{'no optimiser':>14}{'optimum':>10}{'as run':>10}{'cycles':>8}  rule breaks")
    total = [0.0, 0.0, 0.0]
    for path in args.days:
        start, soc, rows = read_day(path)
        hours = [opt.HourInput(start + timedelta(hours=i), r[3], r[2], r[4] if len(r) > 4 else 0.0, r[0], r[1])
                 for i, r in enumerate(rows)]
        if mark is not None and arbitrage and not args.plain:
            hours = mark(hours, [True] * len(hours), p)
        base = opt.simulate_baseline(hours, soc, p)
        def run(hs: list, _soc: float = soc) -> object:
            return opt.optimize(hs, _soc, p, opt.Strategy.MAX_GRID_TRADING, arbitrage, grid_energy=True)

        if args.cycles > 0:
            budget = dict.fromkeys({h.start.date() for h in hours}, args.cycles * p.capacity_kwh)
            hours, r, _ = opt.cap_discharge(hours, budget, p.capacity_kwh, run)
        else:
            r = run(hours)
        cycles = sum(max(0, h.soc_start - h.soc_end) for h in r.hours) / 100
        best = r.total_cost - r.terminal_value
        issues: list[str] = []
        as_run = simulate(hours, _expand(_segments(r.hours, hours, p)), soc, replace(p, require_full=False),
                       grid_energy=True, violations=issues)[0] - CAP_PENALTY * len(issues)
        for i, v in enumerate((base, best, as_run)):
            total[i] += v
        print(f"{start:%Y-%m-%d}  {base:>12.2f}{best:>10.2f}{as_run:>10.2f}{cycles:>8.2f}  {', '.join(issues)}")
        if args.plan:
            marks = {"sell": "S", "grid_charge": "C", "normal": "."}
            print("    " + " ".join(f"{h.start:%H}{marks[h.mode.value]}{h.soc_end}" for h in r.hours))
    print(f"{'total':<12}{total[0]:>14.2f}{total[1]:>10.2f}{total[2]:>10.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
