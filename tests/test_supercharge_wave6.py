"""Tests for wave-6: v22 sage candidate, MultiPolicyDispatcher,
FeatureFlags, BotPreFlightMixin (2026-04-27).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

# ─── v22 sage candidate ───────────────────────────────────────────


def test_v22_passes_through_when_no_sage_bars() -> None:
    """v22 = identical to v17 when payload doesn't include sage_bars."""
    from eta_engine.brain.jarvis_admin import ActionResponse, ActionSuggestion, Verdict
    from eta_engine.brain.jarvis_context import SessionPhase
    from eta_engine.brain.jarvis_v3.policies import v22_sage_confluence as v22_mod

    base_resp = ActionResponse(
        request_id="r0",
        verdict=Verdict.APPROVED,
        reason="ok",
        reason_code="ok",
        jarvis_action=ActionSuggestion.TRADE,
        stress_composite=0.5,
        session_phase=SessionPhase.OPEN_DRIVE,
        binding_constraint="",
        size_cap_mult=None,
    )
    orig = v22_mod.evaluate_request
    try:
        v22_mod.evaluate_request = lambda req, ctx: base_resp  # type: ignore[assignment]

        from eta_engine.brain.jarvis_admin import ActionRequest, ActionType, SubsystemId

        # No sage_bars in payload
        req = ActionRequest(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
            payload={"side": "long"},  # no sage_bars
            rationale="t",
        )
        ctx = type("Ctx", (), {"stress_score": None, "session_phase": SessionPhase.OPEN_DRIVE})()
        out = v22_mod.evaluate_v22(req, ctx)
        assert out.verdict == Verdict.APPROVED
        assert out.size_cap_mult is None
    finally:
        v22_mod.evaluate_request = orig


def test_v22_passes_through_for_non_risk_adding_verdicts() -> None:
    """v22 doesn't try to modulate DENIED/DEFERRED."""
    from eta_engine.brain.jarvis_admin import ActionResponse, ActionSuggestion, Verdict
    from eta_engine.brain.jarvis_context import SessionPhase
    from eta_engine.brain.jarvis_v3.policies import v22_sage_confluence as v22_mod

    base_resp = ActionResponse(
        request_id="r0",
        verdict=Verdict.DENIED,
        reason="killed",
        reason_code="kill",
        jarvis_action=ActionSuggestion.STAND_ASIDE,
        stress_composite=0.95,
        session_phase=SessionPhase.OPEN_DRIVE,
        binding_constraint="dd",
        size_cap_mult=None,
    )
    orig = v22_mod.evaluate_request
    try:
        v22_mod.evaluate_request = lambda req, ctx: base_resp  # type: ignore[assignment]

        from eta_engine.brain.jarvis_admin import ActionRequest, ActionType, SubsystemId

        req = ActionRequest(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
            payload={"side": "long", "sage_bars": [{"close": 1.0}] * 30},
            rationale="t",
        )
        ctx = type("Ctx", (), {"stress_score": None, "session_phase": SessionPhase.OPEN_DRIVE})()
        out = v22_mod.evaluate_v22(req, ctx)
        # DENIED stays DENIED (sage doesn't override risk-blocking verdicts)
        assert out.verdict == Verdict.DENIED
    finally:
        v22_mod.evaluate_request = orig


# ─── MultiPolicyDispatcher ────────────────────────────────────────


def test_dispatch_all_returns_per_arm_verdicts() -> None:
    """dispatch_all should produce one PolicyVerdict per registered arm."""
    from eta_engine.brain.jarvis_v3 import policies  # noqa: F401  -- side-effect register
    from eta_engine.brain.jarvis_v3.candidate_policy import list_candidates
    from eta_engine.brain.jarvis_v3.multi_policy_dispatcher import dispatch_all

    # Use a stub req+ctx that won't trip the real evaluate_request --
    # we'll inject a known champion-response by monkeypatching one arm.
    class _StubReq:
        request_id = "test-req"
        payload: dict = {}

    # We can't safely call the REAL evaluate_request without a full ctx,
    # so this test only verifies the dispatcher's SHAPE: it runs every
    # registered arm and captures errors per-arm.
    class _StubCtx:
        pass

    result = dispatch_all(_StubReq(), _StubCtx())
    candidates = list_candidates()
    assert len(result.verdicts) == len(candidates)
    arm_ids = {v.arm_id for v in result.verdicts}
    assert "v17" in arm_ids
    # Each arm has either a verdict or an error
    for v in result.verdicts:
        assert v.arm_id
        assert v.verdict


def test_diff_matrix_is_json_serializable() -> None:
    """diff_matrix should produce a flat dict ready for JSON output."""
    import json

    from eta_engine.brain.jarvis_v3 import policies  # noqa: F401
    from eta_engine.brain.jarvis_v3.multi_policy_dispatcher import (
        diff_matrix,
        dispatch_all,
    )

    class _StubReq:
        request_id = "diff-test"
        payload: dict = {}

    result = dispatch_all(_StubReq(), object())
    matrix = diff_matrix(result)
    # Should round-trip through JSON
    serialized = json.dumps(matrix, default=str)
    assert "diff-test" in serialized
    assert "per_arm" in matrix
    assert "consensus_verdict" in matrix
    assert "consensus_size_cap_mult" in matrix


def test_pessimism_rank_orders_correctly() -> None:
    from eta_engine.brain.jarvis_v3.multi_policy_dispatcher import _verdict_pessimism_rank

    assert _verdict_pessimism_rank("DENIED") < _verdict_pessimism_rank("DEFERRED")
    assert _verdict_pessimism_rank("DEFERRED") < _verdict_pessimism_rank("CONDITIONAL")
    assert _verdict_pessimism_rank("CONDITIONAL") < _verdict_pessimism_rank("APPROVED")
    # Unknown -> very high (least pessimistic)
    assert _verdict_pessimism_rank("UNKNOWN") > _verdict_pessimism_rank("APPROVED")


# ─── FeatureFlags ─────────────────────────────────────────────────


def test_default_flags_match_design() -> None:
    from eta_engine.brain.feature_flags import is_enabled

    # Defaults: mostly read-only stuff is on, live-routing is off
    assert is_enabled("PRE_FLIGHT_CORRELATION") is True
    assert is_enabled("KAIZEN_DAILY_CLOSE") is True
    assert is_enabled("CRITIQUE_NIGHTLY") is True
    assert is_enabled("CALIBRATION_DAILY") is True
    assert is_enabled("ANOMALY_SCAN_15M") is True

    # Risk-bearing live flags: OFF by default
    assert is_enabled("BANDIT_LIVE_ROUTING") is False
    assert is_enabled("CONTEXTUAL_BANDIT") is False
    assert is_enabled("AUTO_PROMOTE") is False
    assert is_enabled("PER_BOT_PRE_FLIGHT") is False
    assert is_enabled("ONLINE_LEARNING") is False


def test_flag_overridable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.feature_flags import is_enabled

    monkeypatch.setenv("ETA_FF_BANDIT_LIVE_ROUTING", "true")
    assert is_enabled("BANDIT_LIVE_ROUTING") is True

    monkeypatch.setenv("ETA_FF_BANDIT_LIVE_ROUTING", "false")
    assert is_enabled("BANDIT_LIVE_ROUTING") is False


def test_flag_unknown_returns_false() -> None:
    from eta_engine.brain.feature_flags import is_enabled

    assert is_enabled("THIS_FLAG_DOES_NOT_EXIST") is False


def test_flags_snapshot_contains_all_known() -> None:
    from eta_engine.brain.feature_flags import ETA_FLAGS

    snap = ETA_FLAGS.snapshot()
    assert "BANDIT_LIVE_ROUTING" in snap
    assert "PRE_FLIGHT_CORRELATION" in snap
    assert isinstance(snap["BANDIT_LIVE_ROUTING"], bool)


def test_flags_diff_from_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.feature_flags import ETA_FLAGS

    # Without any env override -> diff is empty
    diff = ETA_FLAGS.diff_from_default()
    # Some env vars from previous tests may persist; just check shape
    assert isinstance(diff, dict)

    # Flip BANDIT_LIVE_ROUTING (default OFF) to ON
    monkeypatch.setenv("ETA_FF_BANDIT_LIVE_ROUTING", "yes")
    diff = ETA_FLAGS.diff_from_default()
    assert diff.get("BANDIT_LIVE_ROUTING") is True


# ─── BotPreFlightMixin ────────────────────────────────────────────


class _MixedBot:
    """Stub combining BotPreFlightMixin + a fake _ask_jarvis."""

    def __init__(self, *, ask_returns=(True, None, "ok")) -> None:
        self._ask_returns = ask_returns
        # Borrow the mixin method
        from eta_engine.brain.bot_preflight_mixin import BotPreFlightMixin

        self.gate_or_block = BotPreFlightMixin.gate_or_block.__get__(self)

    def _ask_jarvis(self, action, **payload):
        return self._ask_returns


def test_mixin_falls_back_to_ask_jarvis_when_flag_off() -> None:
    """With PER_BOT_PRE_FLIGHT default-off, mixin uses _ask_jarvis path."""
    bot = _MixedBot(ask_returns=(True, None, "approved"))
    decision = bot.gate_or_block(
        symbol="MNQ",
        side="long",
        confluence=8.0,
        fleet_positions={},
    )
    assert decision.allowed is True
    assert decision.reason == "legacy _ask_jarvis path"


def test_mixin_blocks_when_ask_jarvis_says_no() -> None:
    bot = _MixedBot(ask_returns=(False, None, "denied"))
    decision = bot.gate_or_block(
        symbol="MNQ",
        side="long",
        confluence=5.0,
        fleet_positions={},
    )
    assert decision.allowed is False
    assert decision.binding == "jarvis"
    assert decision.reason_code == "denied"


def test_mixin_routes_through_pre_flight_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With PER_BOT_PRE_FLIGHT=true, the correlation throttle activates."""
    monkeypatch.setenv("ETA_FF_PER_BOT_PRE_FLIGHT", "true")

    from eta_engine.bots.base_bot import BaseBot
    from eta_engine.brain.bot_preflight_mixin import BotPreFlightMixin

    class _PreFlightBot:
        # Compose: needs run_pre_flight (BaseBot) + gate_or_block (Mixin) +
        # _ask_jarvis (BaseBot)
        def __init__(self) -> None:
            from types import SimpleNamespace

            self.config = SimpleNamespace(name="x")
            self._jarvis = None
            self._journal = None
            self._online_updater = None
            self.gate_or_block = BotPreFlightMixin.gate_or_block.__get__(self)
            self.run_pre_flight = BaseBot.run_pre_flight.__get__(self)

        def _ask_jarvis(self, action, **payload):
            return True, None, "ok"

    bot = _PreFlightBot()
    # Open MNQ position then ask for NQ -> should hit correlation throttle
    decision = bot.gate_or_block(
        symbol="NQ",
        side="long",
        confluence=8.0,
        fleet_positions={"MNQ": 2.0},  # 0.99 corr
    )
    assert decision.allowed is False
    assert decision.binding == "correlation"
