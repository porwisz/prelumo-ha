"""Diagnostics: full configuration, learned models and the last plan in one download."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import PrelumoConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: PrelumoConfigEntry) -> dict[str, Any]:
    c = entry.runtime_data
    ps = c.plan_state
    d = c.data
    r = ps.result
    return {
        "config": c.conf,  # effective sources incl. defaults
        "options": c.opt,
        "controls": {
            "strategy": c.strategy.value,
            "grid_arbitrage": c.arbitrage,
            "shadow_mode": c.shadow,
            "auto_mode": c.auto_mode,
        },
        "inverter": {
            "soc": d.soc if d else None,
            "outage": d.outage if d else None,
            "current_plan": d.current_plan.as_list() if d and d.current_plan else None,
            "active_slot": (d.active_index + 1) if d and d.active_index is not None else None,
            "desired_mode": d.desired_mode if d else None,
            "mode_action": d.mode_action if d else None,
            "current_mode": c.writer.current_mode(),
        },
        "plan": {
            "computed_at": ps.computed_at.isoformat() if ps.computed_at else None,
            "proposed": ps.proposed.as_list() if ps.proposed else None,
            "fallback_active": ps.fallback_active,
            "stale_sources": ps.stale_sources,
            "write_decision": ps.write_decision,
            "last_write": ps.last_write.isoformat() if ps.last_write else None,
            "error": ps.error,
            "expected_cost": r.slot_plan_cost if r else None,
            "baseline_cost": r.baseline_cost if r else None,
            "notes": r.notes if r else None,
            "hourly": [
                {**h.as_dict(), "pv": i.pv, "load": i.load, "ev": i.ev, "buy": i.buy, "sell": i.sell}
                for h, i in zip(r.optimum.hours, r.hours)
            ] if r else None,
        },
        "models": {
            "pv_bias": c.pv_bias.to_dict(),
            "heat_pump": c.hp_model.to_dict(),
            "ev_pattern": c.ev_pattern.to_dict(),
            "load_profile_hours": c.load_profile.sample_hours,
            "last_learn": c.last_learn,
        },
        "ev_forecast": {
            "total_kwh": ps.ev.total_kwh,
            "needs": [
                {"kwh": n.energy_kwh, "ready_by": n.ready_by.isoformat() if n.ready_by else None,
                 "source": n.source, "probability": n.probability} for n in ps.ev.needs
            ],
        } if ps.ev else None,
        "shadow_ledger": c.ledger.to_dict(),
        "write_history": [t.isoformat() for t in c.guard.history],
    }
