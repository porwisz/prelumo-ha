import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backtest.py"


def _load():
    spec = importlib.util.spec_from_file_location("backtest", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backtest_reports_a_day(tmp_path, capsys):
    """A cheap afternoon with a 4.50 sell price: the optimum earns, the plain rules earn less."""
    day = tmp_path / "day.txt"
    day.write_text("2026-11-10 12 48  # first hour, SoC\n"
                   "0.9048 3.6 0.77 0.46\n0.6257 3.89 3.94 0.29\n0.6257 4.5 2.17 0.11\n"
                   "0.6257 4.39 2.72 0.02\n1.4961 3.4 1.69 0\n1.4961 3.18 1.08 0\n")
    params = tmp_path / "params.json"
    params.write_text(json.dumps({
        "capacity_kwh": 26.25, "min_soc": 20, "max_soc": 98, "max_charge_kw": 8, "soc_step": 2,
        "max_grid_charge_price": 0.6257, "battery_first_above": 0.6257, "min_sell_soc": 20,
        "arbitrage_min_margin": 0.5, "reserve_price_margin": 0.3, "not_a_field": 1}))
    bt = _load()
    assert bt.read_day(day)[1] == 48.0

    def total(*flags: str) -> float:
        assert bt.main([str(day), "--params", str(params), *flags]) == 0
        out = capsys.readouterr().out
        assert "not_a_field" in out  # unknown fields are named, not fatal
        return float(out.strip().splitlines()[-1].split()[2])  # the optimum column

    assert total() < total("--plain") - 1 < 0
