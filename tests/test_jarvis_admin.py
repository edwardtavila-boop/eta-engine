"""
EVOLUTIONARY TRADING ALGO  //  tests.test_jarvis_admin
==========================================
Integration tests for ``brain.jarvis_admin``.

Coverage matrix (by rule in evaluate_request):
  1. Operator-only actions            TestOperatorOnly       (4 tests)
  2. KILL tier blocks all / exits OK  TestKillTier           (5 tests)
  3. KILL_TRIP always allowed         TestKillTripAlways     (3 tests)
  4. STAND_ASIDE blocks risk-adding   TestStandAside         (5 tests)
  5. REDUCE conditional + size cap    TestReduce             (4 tests)
  6. REVIEW requires acknowledgement  TestReview             (4 tests)
  7. Session gates (OVERNIGHT/CLOSE)  TestSessionGates       (6 tests)
  8. TRADE tier approves              TestTrade              (3 tests)

Plus:
  * Request / response model shapes   TestModels             (4 tests)
  * Audit log roundtrip               TestAuditLog           (4 tests)
  * Engine-driven admin               TestEngineIntegration  (2 tests)
  * Factory ergonomics                TestFactory            (2 tests)

Total: 46 tests.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from eta_engine.brain.jarvis_admin import (
    ActionRequest,
    ActionResponse,
    ActionType,
    JarvisAdmin,
    SubsystemId,
    Verdict,
    evaluate_request,
    make_action_request,
)
from eta_engine.brain.jarvis_context import (
    ActionSuggestion,
    EquitySnapshot,
    JarvisContext,
    JarvisContextBuilder,
    JarvisContextEngine,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
    SessionPhase,
    build_snapshot,
)

if TYPE_CHECKING:
    from pathlib import Path

_ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Fixtures -- canned contexts at each tier + session
# ---------------------------------------------------------------------------


def _midday_ts() -> datetime:
    """A Wednesday 12:00 ET (LUNCH session) -- good neutral time."""
    return datetime(2026, 4, 15, 12, 0, tzinfo=_ET).astimezone(UTC)


def _overnight_ts() -> datetime:
    """Wednesday 22:00 ET -- OVERNIGHT session."""
    return datetime(2026, 4, 15, 22, 0, tzinfo=_ET).astimezone(UTC)


def _close_ts() -> datetime:
    """Wednesday 15:45 ET -- CLOSE session."""
    return datetime(2026, 4, 15, 15, 45, tzinfo=_ET).astimezone(UTC)


def _ctx_trade(ts: datetime | None = None) -> JarvisContext:
    """All gates green -> TRADE tier."""
    return build_snapshot(
        macro=MacroSnapshot(vix_level=17.0, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=50_000.0,
            daily_pnl=0.0,
            daily_drawdown_pct=0.0,
            open_positions=0,
            open_risk_r=0.0,
        ),
        regime=RegimeSnapshot(regime="TREND_UP", confidence=0.7),
        journal=JournalSnapshot(),
        ts=ts or _midday_ts(),
    )


def _ctx_kill(ts: datetime | None = None) -> JarvisContext:
    """kill-switch active -> KILL tier."""
    return build_snapshot(
        macro=MacroSnapshot(vix_level=17.0, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=50_000.0,
            daily_pnl=-3_000.0,
            daily_drawdown_pct=0.06,
            open_positions=0,
            open_risk_r=0.0,
        ),
        regime=RegimeSnapshot(regime="TREND_DOWN", confidence=0.7),
        journal=JournalSnapshot(kill_switch_active=True),
        ts=ts or _midday_ts(),
    )


def _ctx_stand_aside(ts: datetime | None = None) -> JarvisContext:
    """autopilot=REQUIRE_ACK -> STAND_ASIDE tier."""
    return build_snapshot(
        macro=MacroSnapshot(vix_level=17.0, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=50_000.0,
            daily_pnl=0.0,
            daily_drawdown_pct=0.0,
            open_positions=0,
            open_risk_r=0.0,
        ),
        regime=RegimeSnapshot(regime="TREND_UP", confidence=0.7),
        journal=JournalSnapshot(autopilot_mode="REQUIRE_ACK"),
        ts=ts or _midday_ts(),
    )


def _ctx_reduce(ts: datetime | None = None) -> JarvisContext:
    """daily dd 2.5% -> REDUCE tier."""
    return build_snapshot(
        macro=MacroSnapshot(vix_level=17.0, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=50_000.0,
            daily_pnl=-1_250.0,
            daily_drawdown_pct=0.025,
            open_positions=1,
            open_risk_r=1.0,
        ),
        regime=RegimeSnapshot(regime="TREND_UP", confidence=0.6),
        journal=JournalSnapshot(),
        ts=ts or _midday_ts(),
    )


def _ctx_review(ts: datetime | None = None) -> JarvisContext:
    """4 overrides in last 24h -> REVIEW tier."""
    return build_snapshot(
        macro=MacroSnapshot(vix_level=17.0, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=50_000.0,
            daily_pnl=0.0,
            daily_drawdown_pct=0.0,
            open_positions=0,
            open_risk_r=0.0,
        ),
        regime=RegimeSnapshot(regime="TREND_UP", confidence=0.6),
        journal=JournalSnapshot(overrides_last_24h=4),
        ts=ts or _midday_ts(),
    )


# ---------------------------------------------------------------------------
# 1. Operator-only actions
# ---------------------------------------------------------------------------


class TestOperatorOnly:
    def test_kill_reset_denied_for_bot(self) -> None:
        ctx = _ctx_kill()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.KILL_SWITCH_RESET,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.DENIED
        assert resp.reason_code == "operator_only_action"

    def test_gate_override_denied_for_agent(self) -> None:
        ctx = _ctx_trade()
        req = make_action_request(
            subsystem=SubsystemId.AGENT_PM,
            action=ActionType.GATE_OVERRIDE,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.DENIED
        assert resp.reason_code == "operator_only_action"

    def test_autopilot_resume_denied_for_framework(self) -> None:
        ctx = _ctx_trade()
        req = make_action_request(
            subsystem=SubsystemId.FRAMEWORK_AUTOPILOT,
            action=ActionType.AUTOPILOT_RESUME,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.DENIED
        assert resp.reason_code == "operator_only_action"

    def test_operator_can_invoke_operator_only(self) -> None:
        ctx = _ctx_trade()
        req = make_action_request(
            subsystem=SubsystemId.OPERATOR,
            action=ActionType.AUTOPILOT_RESUME,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.APPROVED


# ---------------------------------------------------------------------------
# 2. KILL tier
# ---------------------------------------------------------------------------


class TestKillTier:
    def test_kill_blocks_order_place(self) -> None:
        ctx = _ctx_kill()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.DENIED
        assert resp.reason_code == "kill_blocks_all"

    def test_kill_blocks_signal_emit(self) -> None:
        ctx = _ctx_kill()
        req = make_action_request(
            subsystem=SubsystemId.FRAMEWORK_CONFLUENCE,
            action=ActionType.SIGNAL_EMIT,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.DENIED
        assert resp.reason_code == "kill_blocks_all"

    def test_kill_allows_order_cancel(self) -> None:
        ctx = _ctx_kill()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_CANCEL,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.APPROVED
        assert resp.reason_code == "kill_exit_permitted"

    def test_kill_allows_position_flatten(self) -> None:
        ctx = _ctx_kill()
        req = make_action_request(
            subsystem=SubsystemId.FRAMEWORK_AUTOPILOT,
            action=ActionType.POSITION_FLATTEN,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.APPROVED
        assert resp.reason_code == "kill_exit_permitted"

    def test_kill_allows_operator_reset(self) -> None:
        ctx = _ctx_kill()
        req = make_action_request(
            subsystem=SubsystemId.OPERATOR,
            action=ActionType.KILL_SWITCH_RESET,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.APPROVED
        assert resp.reason_code == "operator_reset"


# ---------------------------------------------------------------------------
# 3. KILL_SWITCH_TRIP always allowed (even in TRADE tier)
# ---------------------------------------------------------------------------


class TestKillTripAlways:
    def test_trip_in_trade_tier(self) -> None:
        ctx = _ctx_trade()
        req = make_action_request(
            subsystem=SubsystemId.AUTOPILOT_WATCHDOG,
            action=ActionType.KILL_SWITCH_TRIP,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.APPROVED
        assert resp.reason_code == "kill_trip_always"

    def test_trip_in_reduce_tier(self) -> None:
        ctx = _ctx_reduce()
        req = make_action_request(
            subsystem=SubsystemId.FRAMEWORK_FIRM_ENGINE,
            action=ActionType.KILL_SWITCH_TRIP,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.APPROVED
        assert resp.reason_code == "kill_trip_always"

    def test_trip_already_killed(self) -> None:
        ctx = _ctx_kill()
        req = make_action_request(
            subsystem=SubsystemId.AUTOPILOT_WATCHDOG,
            action=ActionType.KILL_SWITCH_TRIP,
        )
        resp = evaluate_request(req, ctx)
        # kill_exit_permitted from rule 2 short-circuits; still APPROVED
        assert resp.verdict == Verdict.APPROVED


# ---------------------------------------------------------------------------
# 4. STAND_ASIDE
# ---------------------------------------------------------------------------


class TestStandAside:
    def test_stand_aside_blocks_order_place(self) -> None:
        ctx = _ctx_stand_aside()
        assert ctx.suggestion.action == ActionSuggestion.STAND_ASIDE
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.DENIED
        assert resp.reason_code == "stand_aside_blocks_risk"

    def test_stand_aside_blocks_signal_emit(self) -> None:
        ctx = _ctx_stand_aside()
        req = make_action_request(
            subsystem=SubsystemId.FRAMEWORK_CONFLUENCE,
            action=ActionType.SIGNAL_EMIT,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.DENIED

    def test_stand_aside_blocks_strategy_deploy(self) -> None:
        ctx = _ctx_stand_aside()
        req = make_action_request(
            subsystem=SubsystemId.AGENT_PM,
            action=ActionType.STRATEGY_DEPLOY,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.DENIED

    def test_stand_aside_allows_order_modify(self) -> None:
        ctx = _ctx_stand_aside()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_MODIFY,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.APPROVED
        assert resp.reason_code == "stand_aside_permits_nonrisk"

    def test_stand_aside_allows_order_cancel(self) -> None:
        ctx = _ctx_stand_aside()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_CANCEL,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.APPROVED


# ---------------------------------------------------------------------------
# 5. REDUCE
# ---------------------------------------------------------------------------


class TestReduce:
    def test_reduce_conditional_with_cap(self) -> None:
        ctx = _ctx_reduce()
        assert ctx.suggestion.action == ActionSuggestion.REDUCE
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.CONDITIONAL
        assert resp.reason_code == "reduce_size_cap"
        assert resp.size_cap_mult is not None
        assert resp.size_cap_mult <= 0.50 + 1e-9

    def test_reduce_cap_never_exceeds_live_sizing(self) -> None:
        """live sizing may already be below 0.50; cap must be min(live, 0.50)."""
        ctx = _ctx_reduce()
        assert ctx.sizing_hint is not None
        live = ctx.sizing_hint.size_mult
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        resp = evaluate_request(req, ctx)
        assert resp.size_cap_mult is not None
        assert resp.size_cap_mult <= min(live, 0.50) + 1e-9

    def test_reduce_conditions_include_no_pyramiding(self) -> None:
        ctx = _ctx_reduce()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        resp = evaluate_request(req, ctx)
        assert any("pyramid" in c.lower() for c in resp.conditions)

    def test_reduce_permits_order_cancel(self) -> None:
        ctx = _ctx_reduce()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_CANCEL,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.APPROVED
        assert resp.reason_code == "reduce_permits_nonrisk"


# ---------------------------------------------------------------------------
# 6. REVIEW
# ---------------------------------------------------------------------------


class TestReview:
    def test_review_without_ack_deferred(self) -> None:
        ctx = _ctx_review()
        assert ctx.suggestion.action == ActionSuggestion.REVIEW
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.DEFERRED
        assert resp.reason_code == "review_ack_required"

    def test_review_with_ack_conditional(self) -> None:
        ctx = _ctx_review()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
            review_acknowledged=True,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.CONDITIONAL
        assert resp.reason_code == "review_acked_with_cap"
        assert resp.size_cap_mult is not None
        assert resp.size_cap_mult <= 0.75 + 1e-9

    def test_review_acked_conditions_include_probation(self) -> None:
        ctx = _ctx_review()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
            review_acknowledged=True,
        )
        resp = evaluate_request(req, ctx)
        assert any("probation" in c.lower() for c in resp.conditions)

    def test_review_permits_order_modify(self) -> None:
        ctx = _ctx_review()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_MODIFY,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.APPROVED
        assert resp.reason_code == "review_permits_nonrisk"


# ---------------------------------------------------------------------------
# 7. Session gates
# ---------------------------------------------------------------------------


class TestSessionGates:
    def test_overnight_blocks_non_whitelisted_bot(self) -> None:
        ctx = _ctx_trade(ts=_overnight_ts())
        assert ctx.session_phase == SessionPhase.OVERNIGHT
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,  # NOT whitelisted overnight
            action=ActionType.ORDER_PLACE,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.DENIED
        assert resp.reason_code == "overnight_refused"

    def test_overnight_allows_whitelisted_bot_with_explicit(self) -> None:
        ctx = _ctx_trade(ts=_overnight_ts())
        req = make_action_request(
            subsystem=SubsystemId.BOT_CRYPTO_SEED,  # whitelisted
            action=ActionType.ORDER_PLACE,
            overnight_explicit=True,
        )
        resp = evaluate_request(req, ctx)
        # Should fall through to TRADE tier since crypto runs 24/7
        assert resp.verdict == Verdict.APPROVED

    def test_overnight_whitelisted_missing_explicit_denied(self) -> None:
        ctx = _ctx_trade(ts=_overnight_ts())
        req = make_action_request(
            subsystem=SubsystemId.BOT_CRYPTO_SEED,
            action=ActionType.ORDER_PLACE,
            # overnight_explicit NOT set
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.DENIED
        assert resp.reason_code == "overnight_refused"

    def test_close_blocks_order_place(self) -> None:
        ctx = _ctx_trade(ts=_close_ts())
        assert ctx.session_phase == SessionPhase.CLOSE
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.DENIED
        assert resp.reason_code == "close_no_new_entries"

    def test_close_blocks_signal_emit(self) -> None:
        ctx = _ctx_trade(ts=_close_ts())
        req = make_action_request(
            subsystem=SubsystemId.FRAMEWORK_CONFLUENCE,
            action=ActionType.SIGNAL_EMIT,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.DENIED
        assert resp.reason_code == "close_no_new_entries"

    def test_close_allows_order_cancel(self) -> None:
        """CLOSE session blocks NEW entries but not exits/cancellations."""
        ctx = _ctx_trade(ts=_close_ts())
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_CANCEL,
        )
        resp = evaluate_request(req, ctx)
        # falls through to TRADE tier
        assert resp.verdict == Verdict.APPROVED


# ---------------------------------------------------------------------------
# 8. TRADE tier (happy path)
# ---------------------------------------------------------------------------


class TestTrade:
    def test_trade_approves_order_place(self) -> None:
        ctx = _ctx_trade()
        assert ctx.suggestion.action == ActionSuggestion.TRADE
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        resp = evaluate_request(req, ctx)
        assert resp.verdict == Verdict.APPROVED
        assert resp.reason_code == "trade_ok"

    def test_trade_exposes_live_size_cap(self) -> None:
        ctx = _ctx_trade()
        assert ctx.sizing_hint is not None
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        resp = evaluate_request(req, ctx)
        assert resp.size_cap_mult == pytest.approx(ctx.sizing_hint.size_mult)

    def test_trade_stress_zero_binding_constraint_propagates(self) -> None:
        ctx = _ctx_trade()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        resp = evaluate_request(req, ctx)
        assert 0.0 <= resp.stress_composite <= 1.0
        assert resp.binding_constraint  # non-empty even when stress is 0


# ---------------------------------------------------------------------------
# 9. Request / response model shapes
# ---------------------------------------------------------------------------


class TestModels:
    def test_request_id_default_is_populated(self) -> None:
        req = ActionRequest(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        assert req.request_id
        assert len(req.request_id) == 12

    def test_request_ts_defaults_to_now(self) -> None:
        before = datetime.now(UTC)
        req = ActionRequest(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        after = datetime.now(UTC)
        assert before <= req.ts <= after

    def test_response_size_cap_range(self) -> None:
        ctx = _ctx_trade()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        resp = evaluate_request(req, ctx)
        assert resp.size_cap_mult is None or 0.0 <= resp.size_cap_mult <= 1.0

    def test_response_roundtrips_through_json(self) -> None:
        ctx = _ctx_trade()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        resp = evaluate_request(req, ctx)
        payload = resp.model_dump(mode="json")
        rebuilt = ActionResponse.model_validate(payload)
        assert rebuilt.verdict == resp.verdict
        assert rebuilt.reason_code == resp.reason_code
        assert rebuilt.request_id == resp.request_id


# ---------------------------------------------------------------------------
# 10. Audit log roundtrip
# ---------------------------------------------------------------------------


class TestAuditLog:
    def test_audit_writes_jsonl_when_path_set(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        admin = JarvisAdmin(audit_path=audit)
        ctx = _ctx_trade()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        admin.request_approval(req, ctx=ctx)
        assert audit.exists()
        lines = audit.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["request"]["subsystem"] == SubsystemId.BOT_MNQ.value
        assert rec["response"]["verdict"] == Verdict.APPROVED.value

    def test_no_audit_when_path_none(self) -> None:
        admin = JarvisAdmin()
        ctx = _ctx_trade()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        # should not raise; no file-system side effects
        resp = admin.request_approval(req, ctx=ctx)
        assert resp.verdict == Verdict.APPROVED

    def test_audit_tail_returns_recent_records(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        admin = JarvisAdmin(audit_path=audit)
        ctx = _ctx_trade()
        for i in range(5):
            req = make_action_request(
                subsystem=SubsystemId.BOT_MNQ,
                action=ActionType.ORDER_PLACE,
                rationale=f"test-{i}",
            )
            admin.request_approval(req, ctx=ctx)
        tail = admin.audit_tail(3)
        assert len(tail) == 3
        rationales = [r["request"]["rationale"] for r in tail]
        assert rationales == ["test-2", "test-3", "test-4"]

    def test_audit_tail_on_empty_returns_list(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        admin = JarvisAdmin(audit_path=audit)
        # no requests made yet
        assert admin.audit_tail(10) == []


# ---------------------------------------------------------------------------
# 11. Engine integration
# ---------------------------------------------------------------------------


class TestEngineIntegration:
    def test_admin_without_engine_or_ctx_raises(self) -> None:
        admin = JarvisAdmin()
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        with pytest.raises(RuntimeError, match="engine"):
            admin.request_approval(req)

    def test_admin_with_engine_ticks_per_request(self) -> None:
        """When no ctx is supplied, admin should call engine.tick()."""
        macro = MacroSnapshot(vix_level=17.0, macro_bias="neutral")
        equity = EquitySnapshot(
            account_equity=50_000.0,
            daily_pnl=0.0,
            daily_drawdown_pct=0.0,
            open_positions=0,
            open_risk_r=0.0,
        )
        regime = RegimeSnapshot(regime="TREND_UP", confidence=0.7)
        journal = JournalSnapshot()

        class _Providers:
            def get_macro(self) -> MacroSnapshot:
                return macro

            def get_equity(self) -> EquitySnapshot:
                return equity

            def get_regime(self) -> RegimeSnapshot:
                return regime

            def get_journal_snapshot(self) -> JournalSnapshot:
                return journal

        providers = _Providers()
        # Freeze clock at Tue 2026-04-14 10:30 AM ET (MORNING session) so the
        # overnight session gate does not flip the verdict depending on wall
        # clock time when the suite is run.
        frozen_now = datetime(2026, 4, 14, 14, 30, tzinfo=UTC)  # 10:30 ET
        builder = JarvisContextBuilder(
            macro_provider=providers,
            equity_provider=providers,
            regime_provider=providers,
            journal_provider=providers,
            clock=lambda: frozen_now,
        )
        engine = JarvisContextEngine(builder=builder)
        admin = JarvisAdmin(engine=engine)
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
        )
        resp = admin.request_approval(req)
        assert resp.verdict == Verdict.APPROVED
        # second call should re-tick (not cached)
        resp2 = admin.request_approval(req)
        assert resp2.verdict == Verdict.APPROVED


# ---------------------------------------------------------------------------
# 12. Factory ergonomics
# ---------------------------------------------------------------------------


class TestFactory:
    def test_make_action_request_stuffs_kwargs_into_payload(self) -> None:
        req = make_action_request(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
            rationale="test",
            side="long",
            qty=2,
            stop=100.0,
        )
        assert req.payload == {"side": "long", "qty": 2, "stop": 100.0}
        assert req.rationale == "test"

    def test_make_action_request_empty_payload_ok(self) -> None:
        req = make_action_request(
            subsystem=SubsystemId.OPERATOR,
            action=ActionType.KILL_SWITCH_RESET,
        )
        assert req.payload == {}
        assert req.rationale == ""
