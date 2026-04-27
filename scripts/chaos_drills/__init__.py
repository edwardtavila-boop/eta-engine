"""EVOLUTIONARY TRADING ALGO // scripts.chaos_drills.

One chaos drill per safety-critical surface.

Every drill exposes a callable ``drill(sandbox: Path) -> dict[str, Any]``
that returns the standard chaos-drill result dict::

    {
        "drill":    "<drill_id>",
        "passed":   bool,
        "details":  str,
        "observed": dict[str, Any],
        "ts":       iso8601 str,
    }

Register each drill in :mod:`eta_engine.scripts.chaos_drill`'s
``DRILL_FUNCS`` table so ``python -m eta_engine.scripts.chaos_drill all``
exercises it in the monthly drill run.
"""

from __future__ import annotations

from eta_engine.scripts.chaos_drills.cftc_nfa_compliance_drill import (
    drill_cftc_nfa_compliance,
)
from eta_engine.scripts.chaos_drills.firm_gate_drill import drill_firm_gate
from eta_engine.scripts.chaos_drills.kill_switch_runtime_drill import (
    drill_kill_switch_runtime,
)
from eta_engine.scripts.chaos_drills.live_shadow_guard_drill import (
    drill_live_shadow_guard,
)
from eta_engine.scripts.chaos_drills.oos_qualifier_drill import (
    drill_oos_qualifier,
)
from eta_engine.scripts.chaos_drills.order_state_reconcile_drill import (
    drill_order_state_reconcile,
)
from eta_engine.scripts.chaos_drills.pnl_drift_drill import drill_pnl_drift
from eta_engine.scripts.chaos_drills.risk_engine_drill import drill_risk_engine
from eta_engine.scripts.chaos_drills.runtime_allowlist_drill import (
    drill_runtime_allowlist,
)
from eta_engine.scripts.chaos_drills.shadow_paper_tracker_drill import (
    drill_shadow_paper_tracker,
)
from eta_engine.scripts.chaos_drills.smart_router_drill import drill_smart_router
from eta_engine.scripts.chaos_drills.two_factor_drill import drill_two_factor

__all__ = [
    "drill_cftc_nfa_compliance",
    "drill_firm_gate",
    "drill_kill_switch_runtime",
    "drill_live_shadow_guard",
    "drill_oos_qualifier",
    "drill_order_state_reconcile",
    "drill_pnl_drift",
    "drill_risk_engine",
    "drill_runtime_allowlist",
    "drill_shadow_paper_tracker",
    "drill_smart_router",
    "drill_two_factor",
]
