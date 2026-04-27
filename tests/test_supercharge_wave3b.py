"""Tests for Tier-1 wave-3b: BaseBot helpers + v19/v20/v21 candidates (2026-04-27).

Covers:
  * BaseBot.run_pre_flight (delegates to bot_pre_flight composer)
  * BaseBot.record_fill_outcome (journal write + online-learning hook)
  * v19 drift-aware tightening
  * v20 overnight session tightening
  * v21 drawdown-proximity DEFER
  * 4-arm bandit registration (v18, v19, v20, v21 + champion v17)
"""
from __future__ import annotations

import pytest


# Reuse the duck-typed ctx stub from wave-3a tests
def _ctx(composite: float, *, binding: str = "vol", session: str = "OPEN_DRIVE"):
    from eta_engine.brain.jarvis_context import SessionPhase

    class _StressStub:
        def __init__(self, c: float, b: str) -> None:
            self.composite = c
            self.binding_constraint = b

    class _CtxStub:
        def __init__(self, c: float, b: str, sp: str) -> None:
            self.stress_score = _StressStub(c, b)
            try:
                self.session_phase = SessionPhase(sp)
            except ValueError:
                self.session_phase = SessionPhase.OPEN_DRIVE

    return _CtxStub(composite, binding, session)


def _req():
    from eta_engine.brain.jarvis_admin import ActionRequest, ActionType, SubsystemId
    return ActionRequest(
        subsystem=SubsystemId.BOT_MNQ,
        action=ActionType.ORDER_PLACE,
        payload={"side": "long", "qty": 2},
        rationale="test",
    )


def _stub_resp(verdict_value: str, *, binding: str = "", session: str = "OPEN_DRIVE",
               cap: float | None = 0.5, composite: float = 0.5):
    from eta_engine.brain.jarvis_admin import ActionResponse, ActionSuggestion, Verdict
    from eta_engine.brain.jarvis_context import SessionPhase
    return ActionResponse(
        request_id="r",
        verdict=Verdict(verdict_value),
        reason="test",
        reason_code="test",
        jarvis_action=ActionSuggestion.TRADE,
        stress_composite=composite,
        session_phase=SessionPhase(session),
        binding_constraint=binding,
        size_cap_mult=cap,
    )


# ─── v19 drift-aware ────────────────────────────────────────────────


def test_v19_tightens_on_drift_binding() -> None:
    from eta_engine.brain.jarvis_v3.policies import v19_drift_aware as v19_mod
    from eta_engine.brain.jarvis_v3.policies.v19_drift_aware import DRIFT_CAP, evaluate_v19

    base = _stub_resp("CONDITIONAL", binding="drift_score_high", cap=0.55)
    orig = v19_mod.evaluate_request
    try:
        v19_mod.evaluate_request = lambda req, ctx: base  # type: ignore[assignment]
        out = evaluate_v19(_req(), _ctx(0.5, binding="drift"))
        assert out.size_cap_mult == DRIFT_CAP
        assert any("v19_drift_tightened" in c for c in out.conditions)
    finally:
        v19_mod.evaluate_request = orig


def test_v19_passes_through_when_no_drift_binding() -> None:
    from eta_engine.brain.jarvis_v3.policies import v19_drift_aware as v19_mod

    base = _stub_resp("CONDITIONAL", binding="vol", cap=0.55)
    orig = v19_mod.evaluate_request
    try:
        v19_mod.evaluate_request = lambda req, ctx: base
        out = v19_mod.evaluate_v19(_req(), _ctx(0.5, binding="vol"))
        assert out.size_cap_mult == 0.55
        assert not any("v19_drift_tightened" in c for c in out.conditions)
    finally:
        v19_mod.evaluate_request = orig


def test_v19_promotes_approved_to_conditional_when_tightening() -> None:
    """If v17 said APPROVED (no cap) and v19 tightens, the verdict
    should become CONDITIONAL since cap < 1.0."""
    from eta_engine.brain.jarvis_v3.policies import v19_drift_aware as v19_mod
    from eta_engine.brain.jarvis_admin import Verdict
    from eta_engine.brain.jarvis_v3.policies.v19_drift_aware import DRIFT_CAP

    base = _stub_resp("APPROVED", binding="drift", cap=None, composite=0.55)
    orig = v19_mod.evaluate_request
    try:
        v19_mod.evaluate_request = lambda req, ctx: base
        out = v19_mod.evaluate_v19(_req(), _ctx(0.55, binding="drift"))
        assert out.verdict == Verdict.CONDITIONAL
        assert out.size_cap_mult == DRIFT_CAP
    finally:
        v19_mod.evaluate_request = orig


# ─── v20 overnight tighten ──────────────────────────────────────────


def test_v20_tightens_overnight_conditional() -> None:
    from eta_engine.brain.jarvis_v3.policies import v20_overnight_tighten as v20_mod
    from eta_engine.brain.jarvis_v3.policies.v20_overnight_tighten import OVERNIGHT_CAP

    base = _stub_resp("CONDITIONAL", session="OVERNIGHT", cap=0.50)
    orig = v20_mod.evaluate_request
    try:
        v20_mod.evaluate_request = lambda req, ctx: base
        out = v20_mod.evaluate_v20(_req(), _ctx(0.5, session="OVERNIGHT"))
        assert out.size_cap_mult == OVERNIGHT_CAP
        assert any("v20_overnight_tightened" in c for c in out.conditions)
    finally:
        v20_mod.evaluate_request = orig


def test_v20_does_not_touch_rth() -> None:
    from eta_engine.brain.jarvis_v3.policies import v20_overnight_tighten as v20_mod

    base = _stub_resp("CONDITIONAL", session="OPEN_DRIVE", cap=0.50)
    orig = v20_mod.evaluate_request
    try:
        v20_mod.evaluate_request = lambda req, ctx: base
        out = v20_mod.evaluate_v20(_req(), _ctx(0.5, session="OPEN_DRIVE"))
        assert out.size_cap_mult == 0.50  # unchanged
    finally:
        v20_mod.evaluate_request = orig


def test_v20_skips_non_conditional() -> None:
    from eta_engine.brain.jarvis_v3.policies import v20_overnight_tighten as v20_mod
    from eta_engine.brain.jarvis_admin import Verdict

    base = _stub_resp("APPROVED", session="OVERNIGHT", cap=None)
    orig = v20_mod.evaluate_request
    try:
        v20_mod.evaluate_request = lambda req, ctx: base
        out = v20_mod.evaluate_v20(_req(), _ctx(0.5, session="OVERNIGHT"))
        assert out.verdict == Verdict.APPROVED
        assert out.size_cap_mult is None  # unchanged (was None)
    finally:
        v20_mod.evaluate_request = orig


# ─── v21 drawdown-proximity DEFER ───────────────────────────────────


def test_v21_defers_on_drawdown_binding() -> None:
    from eta_engine.brain.jarvis_v3.policies import v21_drawdown_proximity as v21_mod
    from eta_engine.brain.jarvis_admin import Verdict

    base = _stub_resp("APPROVED", binding="drawdown_dominant", cap=None)
    orig = v21_mod.evaluate_request
    try:
        v21_mod.evaluate_request = lambda req, ctx: base
        out = v21_mod.evaluate_v21(_req(), _ctx(0.5, binding="drawdown"))
        assert out.verdict == Verdict.DEFERRED
        assert out.size_cap_mult == 0.0
        assert "v21_dd_proximity_defer" in out.reason_code
    finally:
        v21_mod.evaluate_request = orig


def test_v21_defers_on_kill_keyword() -> None:
    from eta_engine.brain.jarvis_v3.policies import v21_drawdown_proximity as v21_mod
    from eta_engine.brain.jarvis_admin import Verdict

    base = _stub_resp("CONDITIONAL", binding="approaching_kill_switch", cap=0.4)
    orig = v21_mod.evaluate_request
    try:
        v21_mod.evaluate_request = lambda req, ctx: base
        out = v21_mod.evaluate_v21(_req(), _ctx(0.7, binding="kill"))
        assert out.verdict == Verdict.DEFERRED
    finally:
        v21_mod.evaluate_request = orig


def test_v21_passes_through_non_dd_binding() -> None:
    from eta_engine.brain.jarvis_v3.policies import v21_drawdown_proximity as v21_mod
    from eta_engine.brain.jarvis_admin import Verdict

    base = _stub_resp("CONDITIONAL", binding="vol", cap=0.55)
    orig = v21_mod.evaluate_request
    try:
        v21_mod.evaluate_request = lambda req, ctx: base
        out = v21_mod.evaluate_v21(_req(), _ctx(0.5, binding="vol"))
        assert out.verdict == Verdict.CONDITIONAL  # unchanged
    finally:
        v21_mod.evaluate_request = orig


def test_v21_does_not_modify_already_denied() -> None:
    from eta_engine.brain.jarvis_v3.policies import v21_drawdown_proximity as v21_mod
    from eta_engine.brain.jarvis_admin import Verdict

    base = _stub_resp("DENIED", binding="dd")
    orig = v21_mod.evaluate_request
    try:
        v21_mod.evaluate_request = lambda req, ctx: base
        out = v21_mod.evaluate_v21(_req(), _ctx(0.5, binding="dd"))
        assert out.verdict == Verdict.DENIED  # unchanged
    finally:
        v21_mod.evaluate_request = orig


# ─── 4-arm bandit registration ──────────────────────────────────────


def test_bandit_register_default_wires_v17_18_19_20_21() -> None:
    from eta_engine.brain.jarvis_v3.bandit_harness import default_harness
    from eta_engine.brain.jarvis_v3.bandit_register_default import bandit_with_etas

    h = default_harness()
    h.arms.clear()
    h.champion_id = None

    h2 = bandit_with_etas()
    assert h is h2
    expected = {"v17", "v18", "v19", "v20", "v21"}
    assert expected <= set(h.arms.keys()), f"expected {expected} subset of {h.arms.keys()}"
    assert h.champion_id == "v17"


# ─── BaseBot.run_pre_flight + record_fill_outcome ────────────────────


class _StubBot:
    """Stand-in with what BaseBot helpers need."""

    def __init__(self) -> None:
        from types import SimpleNamespace
        self.config = SimpleNamespace(name="stub_bot")
        # Borrow the BaseBot helpers
        from eta_engine.bots.base_bot import BaseBot
        self.run_pre_flight = BaseBot.run_pre_flight.__get__(self)
        self.record_fill_outcome = BaseBot.record_fill_outcome.__get__(self)
        self.observe_fill_for_learning = BaseBot.observe_fill_for_learning.__get__(self)
        self._jarvis = None
        self._journal = None
        self._online_updater = None

    def _ask_jarvis(self, action, **payload):
        # Mimics the bot's helper -- legacy/test mode passes through
        return True, None, "no_jarvis"


def test_basebot_run_pre_flight_passes_through_when_no_jarvis() -> None:
    bot = _StubBot()
    decision = bot.run_pre_flight(
        symbol="MNQ",
        side="long",
        confluence=8.0,
        fleet_positions={},
    )
    assert decision.allowed is True


def test_basebot_run_pre_flight_blocks_on_correlation() -> None:
    bot = _StubBot()
    decision = bot.run_pre_flight(
        symbol="NQ",
        side="long",
        confluence=8.0,
        fleet_positions={"MNQ": 2.0},  # 0.99 correlation
    )
    assert decision.allowed is False
    assert decision.binding == "correlation"


def test_basebot_record_fill_outcome_with_attached_journal_and_updater(tmp_path) -> None:
    from eta_engine.brain.online_learning import OnlineUpdater
    from eta_engine.obs.decision_journal import DecisionJournal

    bot = _StubBot()
    bot._journal = DecisionJournal(tmp_path / "journal.jsonl", supabase_mirror=False)
    bot._online_updater = OnlineUpdater(bot_name="test", alpha=0.5)

    bot.record_fill_outcome(
        intent="open_mnq_long_close",
        r_multiple=1.5,
        feature_bucket="confluence_8",
    )
    # Journal got a row with realized_r in metadata
    rows = bot._journal.read_all()
    assert len(rows) == 1
    assert rows[0].metadata.get("realized_r") == 1.5
    # OnlineUpdater observed
    assert bot._online_updater.expected_r("confluence_8") == 1.5


def test_basebot_record_fill_outcome_is_noop_with_nothing_attached() -> None:
    """No journal + no updater -> still doesn't blow up."""
    bot = _StubBot()
    # No raise = pass
    bot.record_fill_outcome(intent="x", r_multiple=0.5)
