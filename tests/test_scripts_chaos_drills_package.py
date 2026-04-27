"""Tests for the :mod:`eta_engine.scripts.chaos_drills` package.

Each drill must:
* return a well-formed result dict (``drill`` / ``passed`` / ``details`` /
  ``observed`` / ``ts`` keys present),
* pass against its hand-built scenario (``passed`` is ``True``), and
* be registered in :data:`eta_engine.scripts.chaos_drill.DRILL_FUNCS`
  under the drill name returned in the result.

These tests are the regression net that makes the v0.1.56 CHAOS DRILL
CLOSURE a hard contract: if any drill silently starts returning
``passed=False``, pytest fails; if any drill stops being registered,
pytest fails; if the shared-shape helper drifts, pytest fails.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from eta_engine.scripts.chaos_drill import ALL_DRILLS, DRILL_FUNCS
from eta_engine.scripts.chaos_drills import (
    drill_cftc_nfa_compliance,
    drill_firm_gate,
    drill_kill_switch_runtime,
    drill_live_shadow_guard,
    drill_oos_qualifier,
    drill_order_state_reconcile,
    drill_pnl_drift,
    drill_risk_engine,
    drill_runtime_allowlist,
    drill_shadow_paper_tracker,
    drill_smart_router,
    drill_two_factor,
)
from eta_engine.scripts.chaos_drills._common import drill_result

if TYPE_CHECKING:
    from pathlib import Path


# Canonical drill-name → callable mapping for the parametrized contract test.
_NEW_DRILLS = (
    ("kill_switch_runtime", drill_kill_switch_runtime),
    ("risk_engine", drill_risk_engine),
    ("order_state_reconcile", drill_order_state_reconcile),
    ("cftc_nfa_compliance", drill_cftc_nfa_compliance),
    ("two_factor", drill_two_factor),
    ("smart_router", drill_smart_router),
    ("firm_gate", drill_firm_gate),
    ("oos_qualifier", drill_oos_qualifier),
    ("shadow_paper_tracker", drill_shadow_paper_tracker),
    ("live_shadow_guard", drill_live_shadow_guard),
    ("pnl_drift", drill_pnl_drift),
    ("runtime_allowlist", drill_runtime_allowlist),
)


class TestDrillResultHelper:
    def test_defaults_observed_to_empty_dict(self):
        r = drill_result("x", passed=True, details="ok")
        assert r["observed"] == {}

    def test_preserves_observed(self):
        r = drill_result("x", passed=False, details="nope", observed={"a": 1})
        assert r["observed"] == {"a": 1}

    def test_has_iso_timestamp(self):
        r = drill_result("x", passed=True, details="ok")
        # ISO-8601 with +00:00 suffix (UTC-aware).
        assert "T" in r["ts"]
        assert r["ts"].endswith("+00:00")


class TestDrillResultShape:
    """Every drill must return the standard 5-key result dict."""

    @pytest.mark.parametrize(("name", "drill"), _NEW_DRILLS)
    def test_shape(self, tmp_path: Path, name: str, drill):
        result = drill(tmp_path)
        for key in ("drill", "passed", "details", "observed", "ts"):
            assert key in result, f"{name} missing {key!r}"
        assert result["drill"] == name, f"{name} returned drill={result['drill']!r}"
        assert isinstance(result["passed"], bool)
        assert isinstance(result["details"], str) and result["details"]
        assert isinstance(result["observed"], dict)


class TestDrillPasses:
    """Each drill's hand-built scenario must score ``passed=True``."""

    @pytest.mark.parametrize(("name", "drill"), _NEW_DRILLS)
    def test_passes(self, tmp_path: Path, name: str, drill):
        result = drill(tmp_path)
        assert result["passed"] is True, f"{name} failed: {result['details']}  observed={result['observed']!r}"


class TestDrillRunnerRegistry:
    """The chaos_drill runner must know about every new drill."""

    @pytest.mark.parametrize(("name", "_"), _NEW_DRILLS)
    def test_in_all_drills(self, name: str, _):
        assert name in ALL_DRILLS

    @pytest.mark.parametrize(("name", "drill"), _NEW_DRILLS)
    def test_in_drill_funcs(self, name: str, drill):
        assert DRILL_FUNCS.get(name) is drill

    def test_all_drills_tuple_is_sixteen(self):
        # 4 pre-v0.1.56 drills + 12 closure drills = 16 total.
        assert len(ALL_DRILLS) == 16
        assert len(DRILL_FUNCS) == 16

    def test_no_duplicate_drill_names(self):
        assert len(set(ALL_DRILLS)) == len(ALL_DRILLS)


class TestOrderStateReconcileIdempotency:
    """Second reconcile pass must return the same actions (state convergence)."""

    def test_second_pass_still_passes(self, tmp_path: Path):
        # Running the drill twice in a row must remain green -- the drill
        # itself has an idempotency check baked in, so simply passing twice
        # is the contract.
        a = drill_order_state_reconcile(tmp_path)
        b = drill_order_state_reconcile(tmp_path)
        assert a["passed"] is True
        assert b["passed"] is True


class TestRuntimeAllowlistInvalidateSemantics:
    """Covers the invalidate() edge: advancing clock should NOT resurrect entry."""

    def test_passes_regardless_of_clock_state(self, tmp_path: Path):
        # The drill builds its own clock; running it back-to-back exercises
        # that the scenario is self-contained.
        first = drill_runtime_allowlist(tmp_path)
        second = drill_runtime_allowlist(tmp_path)
        assert first["passed"] is True
        assert second["passed"] is True
