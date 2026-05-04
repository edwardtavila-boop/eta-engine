"""Tests for JARVIS advanced layers v24-v27 (2026-05-04 wave-8).

Covers:
  v24 correlation throttle      — max-N concurrent same-side per class
  v25 per-class daily loss      — freeze class on cumulative realized PnL
  v26 fill-confirmation health  — degrade size when signals fire w/o entries
  v27 live-vs-lab sharpe drift  — degrade size when realized expectancy
                                  drifts from lab claim
  evaluate_advanced_stack       — full v23→v27 cascade entrypoint
  JarvisAdmin dispatch          — JARVIS_V3_ADVANCED env routes through stack
"""
from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from eta_engine.brain.jarvis_admin import (
    ActionRequest,
    ActionResponse,
    ActionType,
    SubsystemId,
    Verdict,
)
from eta_engine.brain.jarvis_context import (
    ActionSuggestion,
    EquitySnapshot,
    JarvisContext,
    JarvisSuggestion,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
    SessionPhase,
    SizingHint,
)


def _make_ctx() -> JarvisContext:
    return JarvisContext(
        ts=datetime.now(UTC),
        macro=MacroSnapshot(vix_level=16.0, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=50_000.0, daily_pnl=0.0,
            daily_drawdown_pct=0.0, open_positions=1, open_risk_r=0.5,
        ),
        regime=RegimeSnapshot(regime="TRENDING_UP", confidence=0.8, flipped_recently=False),
        journal=JournalSnapshot(
            kill_switch_active=False, autopilot_mode="ACTIVE",
            overrides_last_24h=0, recent_correlated_loss=False,
        ),
        suggestion=JarvisSuggestion(action=ActionSuggestion.TRADE, reason="t", confidence=0.7),
        session_phase=SessionPhase.MORNING,
        sizing_hint=SizingHint(size_mult=1.0, reason="t", session_phase=SessionPhase.MORNING),
    )


def _make_approved_resp() -> ActionResponse:
    return ActionResponse(
        request_id="test123",
        verdict=Verdict.APPROVED,
        reason="base approval",
        reason_code="trade_ok",
        jarvis_action=ActionSuggestion.TRADE,
        stress_composite=0.0,
        session_phase=SessionPhase.MORNING,
        size_cap_mult=1.0,
    )


# ─── v24 correlation throttle ────────────────────────────────────


class TestV24CorrelationThrottle:
    def setup_method(self) -> None:
        from eta_engine.brain.jarvis_v3.policies.v24_correlation_throttle import reset_state
        reset_state()

    def test_under_threshold_passes_through(self) -> None:
        from eta_engine.brain.jarvis_v3.policies.v24_correlation_throttle import evaluate_v24
        ctx = _make_ctx()
        base = _make_approved_resp()
        # Mock registry lookup to return a crypto bot
        with mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v24_correlation_throttle._resolve_class_and_side",
            return_value=("crypto", "long"),
        ):
            for _ in range(2):  # under default max=3
                req = ActionRequest(
                    subsystem=SubsystemId.BOT_BTC_HYBRID,
                    action=ActionType.SIGNAL_EMIT,
                    payload={"side": "long", "bot_id": "btc_hybrid"},
                    rationale="t",
                )
                resp = evaluate_v24(req, ctx, base_resp=base)
                assert resp.verdict == Verdict.APPROVED

    def test_over_threshold_throttles(self) -> None:
        from eta_engine.brain.jarvis_v3.policies.v24_correlation_throttle import evaluate_v24
        ctx = _make_ctx()
        base = _make_approved_resp()
        with mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v24_correlation_throttle._resolve_class_and_side",
            return_value=("crypto", "long"),
        ):
            req = ActionRequest(
                subsystem=SubsystemId.BOT_BTC_HYBRID,
                action=ActionType.SIGNAL_EMIT,
                payload={"side": "long", "bot_id": "btc_hybrid"},
                rationale="t",
            )
            # First 3 pass, 4th throttles
            for i in range(3):
                resp = evaluate_v24(req, ctx, base_resp=base)
                assert resp.verdict == Verdict.APPROVED, f"expected pass on call {i}"
            resp4 = evaluate_v24(req, ctx, base_resp=base)
            assert resp4.verdict == Verdict.DEFERRED
            assert resp4.reason_code == "v24_correlation_throttle"

    def test_unknown_class_passes_through(self) -> None:
        """If class can't be resolved, base verdict returns unchanged."""
        from eta_engine.brain.jarvis_v3.policies.v24_correlation_throttle import evaluate_v24
        ctx = _make_ctx()
        base = _make_approved_resp()
        with mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v24_correlation_throttle._resolve_class_and_side",
            return_value=("", "long"),
        ):
            for _ in range(10):  # No throttle should fire
                req = ActionRequest(
                    subsystem=SubsystemId.BOT_MNQ,
                    action=ActionType.SIGNAL_EMIT,
                    payload={"side": "long", "bot_id": "mnq"},
                    rationale="t",
                )
                resp = evaluate_v24(req, ctx, base_resp=base)
                assert resp.verdict == Verdict.APPROVED

    def test_different_sides_dont_throttle_each_other(self) -> None:
        from eta_engine.brain.jarvis_v3.policies.v24_correlation_throttle import evaluate_v24
        ctx = _make_ctx()
        base = _make_approved_resp()
        with mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v24_correlation_throttle._resolve_class_and_side",
            side_effect=[("crypto", "long"), ("crypto", "short")] * 5,
        ):
            for _ in range(5):
                req = ActionRequest(
                    subsystem=SubsystemId.BOT_BTC_HYBRID,
                    action=ActionType.SIGNAL_EMIT,
                    payload={"bot_id": "btc"},
                    rationale="t",
                )
                resp = evaluate_v24(req, ctx, base_resp=base)
                assert resp.verdict == Verdict.APPROVED


# ─── v25 class loss limit ────────────────────────────────────────


class TestV25ClassLossLimit:
    def setup_method(self) -> None:
        from eta_engine.brain.jarvis_v3.policies.v25_class_loss_limit import reset_cache
        reset_cache()

    def test_no_heartbeat_passes_through(self) -> None:
        from eta_engine.brain.jarvis_v3.policies.v25_class_loss_limit import evaluate_v25
        ctx = _make_ctx()
        base = _make_approved_resp()
        with mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v25_class_loss_limit._class_realized_pnl",
            return_value=None,
        ):
            req = ActionRequest(
                subsystem=SubsystemId.BOT_BTC_HYBRID,
                action=ActionType.SIGNAL_EMIT,
                payload={"side": "long", "bot_id": "btc_hybrid"},
                rationale="t",
            )
            resp = evaluate_v25(req, ctx, base_resp=base)
            assert resp.verdict == Verdict.APPROVED

    def test_class_pnl_above_limit_passes(self) -> None:
        from eta_engine.brain.jarvis_v3.policies.v25_class_loss_limit import evaluate_v25
        ctx = _make_ctx()
        base = _make_approved_resp()
        with mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v25_class_loss_limit._class_realized_pnl",
            return_value=-100.0,  # losing $100, default limit is -$300
        ), mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v25_class_loss_limit._resolve_class",
            return_value="crypto",
        ):
            req = ActionRequest(
                subsystem=SubsystemId.BOT_BTC_HYBRID,
                action=ActionType.SIGNAL_EMIT,
                payload={"side": "long", "bot_id": "btc_hybrid"},
                rationale="t",
            )
            resp = evaluate_v25(req, ctx, base_resp=base)
            assert resp.verdict == Verdict.APPROVED

    def test_class_pnl_below_limit_freezes(self) -> None:
        from eta_engine.brain.jarvis_v3.policies.v25_class_loss_limit import evaluate_v25
        ctx = _make_ctx()
        base = _make_approved_resp()
        with mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v25_class_loss_limit._class_realized_pnl",
            return_value=-500.0,  # below default -$300 limit
        ), mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v25_class_loss_limit._resolve_class",
            return_value="crypto",
        ):
            req = ActionRequest(
                subsystem=SubsystemId.BOT_BTC_HYBRID,
                action=ActionType.SIGNAL_EMIT,
                payload={"side": "long", "bot_id": "btc_hybrid"},
                rationale="t",
            )
            resp = evaluate_v25(req, ctx, base_resp=base)
            assert resp.verdict == Verdict.DEFERRED
            assert resp.reason_code == "v25_class_loss_freeze"


# ─── v26 fill-confirmation health ────────────────────────────────


class TestV26FillConfirmation:
    def setup_method(self) -> None:
        from eta_engine.brain.jarvis_v3.policies.v26_fill_confirmation import reset_cache
        reset_cache()

    def test_healthy_bot_passes_through(self) -> None:
        from eta_engine.brain.jarvis_v3.policies.v26_fill_confirmation import evaluate_v26
        ctx = _make_ctx()
        base = _make_approved_resp()
        # bot has signals AND entries — not degraded
        healthy_state = {
            "bot_id": "btc",
            "last_signal_at": datetime.now(UTC).isoformat(),
            "n_entries": 5,
        }
        with mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v26_fill_confirmation._bot_state",
            return_value=healthy_state,
        ):
            req = ActionRequest(
                subsystem=SubsystemId.BOT_BTC_HYBRID,
                action=ActionType.SIGNAL_EMIT,
                payload={"side": "long", "bot_id": "btc"},
                rationale="t",
            )
            resp = evaluate_v26(req, ctx, base_resp=base)
            assert resp.verdict == Verdict.APPROVED

    def test_degraded_bot_size_capped(self) -> None:
        from eta_engine.brain.jarvis_v3.policies.v26_fill_confirmation import evaluate_v26
        ctx = _make_ctx()
        base = _make_approved_resp()
        # signals firing but no entries → degraded
        old_signal = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        degraded_state = {
            "bot_id": "btc",
            "last_signal_at": old_signal,
            "n_entries": 0,
        }
        with mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v26_fill_confirmation._bot_state",
            return_value=degraded_state,
        ):
            req = ActionRequest(
                subsystem=SubsystemId.BOT_BTC_HYBRID,
                action=ActionType.SIGNAL_EMIT,
                payload={"side": "long", "bot_id": "btc"},
                rationale="t",
            )
            resp = evaluate_v26(req, ctx, base_resp=base)
            assert resp.verdict == Verdict.CONDITIONAL
            assert resp.reason_code == "v26_execution_degraded"
            assert resp.size_cap_mult == 0.50


# ─── v27 sharpe drift ────────────────────────────────────────────


class TestV27SharpeDrift:
    def setup_method(self) -> None:
        from eta_engine.brain.jarvis_v3.policies.v27_sharpe_drift import reset_cache
        reset_cache()

    def test_no_lab_stamp_passes_through(self) -> None:
        from eta_engine.brain.jarvis_v3.policies.v27_sharpe_drift import evaluate_v27
        ctx = _make_ctx()
        base = _make_approved_resp()
        with mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v27_sharpe_drift._lab_exp_r",
            return_value=None,
        ):
            req = ActionRequest(
                subsystem=SubsystemId.BOT_BTC_HYBRID,
                action=ActionType.SIGNAL_EMIT,
                payload={"side": "long", "bot_id": "no_lab_bot"},
                rationale="t",
            )
            resp = evaluate_v27(req, ctx, base_resp=base)
            assert resp.verdict == Verdict.APPROVED

    def test_few_exits_no_drift_check(self) -> None:
        """v27 doesn't penalize bots with too-small live samples."""
        from eta_engine.brain.jarvis_v3.policies.v27_sharpe_drift import evaluate_v27
        ctx = _make_ctx()
        base = _make_approved_resp()
        with mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v27_sharpe_drift._lab_exp_r",
            return_value=0.1,
        ), mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v27_sharpe_drift._bot_state",
            return_value={"n_exits": 3, "realized_pnl": -50.0},  # only 3 exits
        ):
            req = ActionRequest(
                subsystem=SubsystemId.BOT_BTC_HYBRID,
                action=ActionType.SIGNAL_EMIT,
                payload={"side": "long", "bot_id": "btc"},
                rationale="t",
            )
            resp = evaluate_v27(req, ctx, base_resp=base)
            assert resp.verdict == Verdict.APPROVED  # not enough exits to judge

    def test_drift_negative_live_pnl_caps_size(self) -> None:
        """Lab claimed positive expectancy but live is negative → size capped."""
        from eta_engine.brain.jarvis_v3.policies.v27_sharpe_drift import evaluate_v27
        ctx = _make_ctx()
        base = _make_approved_resp()
        with mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v27_sharpe_drift._lab_exp_r",
            return_value=0.1,
        ), mock.patch(
            "eta_engine.brain.jarvis_v3.policies.v27_sharpe_drift._bot_state",
            return_value={"n_exits": 20, "realized_pnl": -100.0},
        ):
            req = ActionRequest(
                subsystem=SubsystemId.BOT_BTC_HYBRID,
                action=ActionType.SIGNAL_EMIT,
                payload={"side": "long", "bot_id": "btc"},
                rationale="t",
            )
            resp = evaluate_v27(req, ctx, base_resp=base)
            assert resp.verdict == Verdict.CONDITIONAL
            assert resp.reason_code == "v27_sharpe_drift"
            assert resp.size_cap_mult == 0.50


# ─── advanced_stack integration ──────────────────────────────────


def test_evaluate_advanced_stack_runs_clean() -> None:
    """Smoke: advanced stack runs without crash even with no-bot-id payload."""
    from eta_engine.brain.jarvis_v3.policies.v27_sharpe_drift import (
        evaluate_advanced_stack,
    )
    ctx = _make_ctx()
    req = ActionRequest(
        subsystem=SubsystemId.BOT_MNQ,
        action=ActionType.SIGNAL_EMIT,
        payload={"side": "long"},
        rationale="t",
    )
    resp = evaluate_advanced_stack(req, ctx)
    assert resp.verdict in {Verdict.APPROVED, Verdict.CONDITIONAL,
                            Verdict.DENIED, Verdict.DEFERRED}


def test_jarvis_admin_advanced_flag_dispatch() -> None:
    """JARVIS_V3_ADVANCED=1 routes through the full stack in JarvisAdmin."""
    from eta_engine.brain.jarvis_admin import JarvisAdmin

    admin = JarvisAdmin()
    ctx = _make_ctx()
    req = ActionRequest(
        subsystem=SubsystemId.BOT_MNQ,
        action=ActionType.SIGNAL_EMIT,
        payload={"side": "long", "bot_id": "non_existent_bot_xyz"},
        rationale="t",
    )
    with mock.patch.dict(os.environ, {"JARVIS_V3_ADVANCED": "1"}):
        resp = admin.request_approval(req, ctx=ctx)
    assert resp.verdict in {Verdict.APPROVED, Verdict.CONDITIONAL,
                            Verdict.DENIED, Verdict.DEFERRED}
