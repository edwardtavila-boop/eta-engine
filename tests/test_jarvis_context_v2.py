"""Tests for brain.jarvis_context v2 -- stress / session / sizing / alerts /
memory / engine / margins / explanation / playbook.

All v2 enrichment is additive to v1. Tests focus on:
  * composition math (weights sum to 1.0, binding_constraint = argmax)
  * session phase boundaries in America/New_York
  * sizing monotonicity in stress and action tier
  * alert escalation ladder per factor
  * margin arithmetic
  * memory trajectory classification (IMPROVING/FLAT/WORSENING/UNKNOWN)
  * engine tick ordering (trajectory BEFORE append)
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from eta_engine.brain.jarvis_context import (
    DD_KILL_THRESHOLD,
    DD_REDUCE_THRESHOLD,
    DD_STAND_ASIDE_THRESHOLD,
    OPEN_RISK_HARD_CAP_R,
    OVERRIDE_REVIEW_THRESHOLD,
    STRESS_WEIGHTS,
    ActionSuggestion,
    AlertLevel,
    EquitySnapshot,
    JarvisAlert,
    JarvisContext,
    JarvisContextBuilder,
    JarvisContextEngine,
    JarvisMargins,
    JarvisMemory,
    JarvisSuggestion,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
    SessionPhase,
    SizingHint,
    StressScore,
    Trajectory,
    TrajectoryState,
    build_explanation,
    build_playbook,
    build_snapshot,
    compute_margins,
    compute_session_phase,
    compute_sizing_hint,
    compute_stress_score,
    detect_alerts,
)

_ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- #
# Defaults (mirror of test_jarvis_context.py)
# --------------------------------------------------------------------------- #


def _ok_macro(
    *,
    bias: str = "neutral",
    hours: float | None = None,
    label: str | None = None,
) -> MacroSnapshot:
    return MacroSnapshot(
        vix_level=16.0,
        macro_bias=bias,
        next_event_label=label,
        hours_until_next_event=hours,
    )


def _ok_equity(
    *,
    dd_pct: float = 0.0,
    open_risk_r: float = 0.5,
    pnl: float = 0.0,
) -> EquitySnapshot:
    return EquitySnapshot(
        account_equity=50_000.0,
        daily_pnl=pnl,
        daily_drawdown_pct=dd_pct,
        open_positions=1,
        open_risk_r=open_risk_r,
    )


def _ok_regime(
    *,
    regime: str = "TRENDING_UP",
    flipped: bool = False,
    conf: float = 0.8,
) -> RegimeSnapshot:
    return RegimeSnapshot(
        regime=regime,
        confidence=conf,
        flipped_recently=flipped,
    )


def _ok_journal(
    *,
    kill: bool = False,
    mode: str = "ACTIVE",
    overrides: int = 0,
    corr: bool = False,
) -> JournalSnapshot:
    return JournalSnapshot(
        kill_switch_active=kill,
        autopilot_mode=mode,
        overrides_last_24h=overrides,
        blocked_last_24h=0,
        executed_last_24h=5,
        correlations_alert=corr,
    )


# --------------------------------------------------------------------------- #
# stress_score
# --------------------------------------------------------------------------- #


class TestStressScoreWeights:
    def test_weights_sum_to_one(self) -> None:
        assert math.isclose(sum(STRESS_WEIGHTS.values()), 1.0, abs_tol=1e-9)

    def test_all_clear_near_zero(self) -> None:
        s = compute_stress_score(
            _ok_macro(),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(),
        )
        assert s.composite < 0.05
        assert 0.0 <= s.composite <= 1.0

    def test_kill_drawdown_saturates_equity_component(self) -> None:
        s = compute_stress_score(
            _ok_macro(),
            _ok_equity(dd_pct=DD_KILL_THRESHOLD),
            _ok_regime(),
            _ok_journal(),
        )
        eq = next(c for c in s.components if c.name == "equity_dd")
        assert eq.value == pytest.approx(1.0)

    def test_crisis_macro_bias_saturates(self) -> None:
        s = compute_stress_score(
            _ok_macro(bias="crisis"),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(),
        )
        mb = next(c for c in s.components if c.name == "macro_bias")
        assert mb.value == pytest.approx(1.0)

    def test_binding_constraint_is_argmax(self) -> None:
        # Drive dd up while leaving others clean -- equity_dd should bind.
        s = compute_stress_score(
            _ok_macro(),
            _ok_equity(dd_pct=DD_KILL_THRESHOLD),
            _ok_regime(),
            _ok_journal(),
        )
        assert s.binding_constraint == "equity_dd"

    def test_binding_constraint_macro_when_imminent(self) -> None:
        # All else zero, event imminent -- macro_event should bind.
        s = compute_stress_score(
            _ok_macro(bias="neutral", hours=0.1, label="FOMC"),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(),
        )
        assert s.binding_constraint == "macro_event"

    def test_component_contribution_is_weight_times_value(self) -> None:
        s = compute_stress_score(
            _ok_macro(bias="crisis"),
            _ok_equity(dd_pct=0.02),
            _ok_regime(),
            _ok_journal(),
        )
        eq = next(c for c in s.components if c.name == "equity_dd")
        assert eq.contribution == pytest.approx(eq.weight * eq.value)

    def test_composite_equals_sum_of_contributions(self) -> None:
        s = compute_stress_score(
            _ok_macro(hours=2.0, label="FOMC"),
            _ok_equity(dd_pct=0.015, open_risk_r=2.0),
            _ok_regime(flipped=True),
            _ok_journal(overrides=2, corr=True),
        )
        total = sum(c.contribution for c in s.components)
        assert s.composite == pytest.approx(round(total, 4))


# --------------------------------------------------------------------------- #
# session_phase
# --------------------------------------------------------------------------- #


def _et(h: int, m: int, *, weekday: int = 0) -> datetime:
    """Construct a tz-aware ET datetime on a weekday (default Mon=0)."""
    # 2026-04-20 is a Monday.
    base = datetime(2026, 4, 20, 0, 0, tzinfo=_ET)
    base = base + timedelta(days=weekday)
    return base.replace(hour=h, minute=m)


class TestSessionPhase:
    def test_naive_datetime_treated_as_utc(self) -> None:
        # 09:00 UTC == 05:00 ET (DST) -> PREMARKET
        naive = datetime(2026, 4, 20, 9, 0)
        assert compute_session_phase(naive) == SessionPhase.PREMARKET

    def test_weekend_always_overnight(self) -> None:
        # Saturday at 10:00 ET -- would be OPEN_DRIVE if weekday
        sat = datetime(2026, 4, 18, 10, 0, tzinfo=_ET)
        assert compute_session_phase(sat) == SessionPhase.OVERNIGHT
        sun = datetime(2026, 4, 19, 13, 0, tzinfo=_ET)
        assert compute_session_phase(sun) == SessionPhase.OVERNIGHT

    def test_premarket_window(self) -> None:
        assert compute_session_phase(_et(4, 0)) == SessionPhase.PREMARKET
        assert compute_session_phase(_et(8, 30)) == SessionPhase.PREMARKET
        assert compute_session_phase(_et(9, 29)) == SessionPhase.PREMARKET

    def test_open_drive_window(self) -> None:
        assert compute_session_phase(_et(9, 30)) == SessionPhase.OPEN_DRIVE
        assert compute_session_phase(_et(10, 0)) == SessionPhase.OPEN_DRIVE
        assert compute_session_phase(_et(10, 29)) == SessionPhase.OPEN_DRIVE

    def test_morning_window(self) -> None:
        assert compute_session_phase(_et(10, 30)) == SessionPhase.MORNING
        assert compute_session_phase(_et(11, 59)) == SessionPhase.MORNING

    def test_lunch_window(self) -> None:
        assert compute_session_phase(_et(12, 0)) == SessionPhase.LUNCH
        assert compute_session_phase(_et(13, 29)) == SessionPhase.LUNCH

    def test_afternoon_window(self) -> None:
        assert compute_session_phase(_et(13, 30)) == SessionPhase.AFTERNOON
        assert compute_session_phase(_et(15, 29)) == SessionPhase.AFTERNOON

    def test_close_window(self) -> None:
        assert compute_session_phase(_et(15, 30)) == SessionPhase.CLOSE
        assert compute_session_phase(_et(15, 59)) == SessionPhase.CLOSE

    def test_overnight_after_close(self) -> None:
        assert compute_session_phase(_et(16, 0)) == SessionPhase.OVERNIGHT
        assert compute_session_phase(_et(23, 30)) == SessionPhase.OVERNIGHT
        assert compute_session_phase(_et(3, 59)) == SessionPhase.OVERNIGHT


# --------------------------------------------------------------------------- #
# sizing_hint
# --------------------------------------------------------------------------- #


class TestSizingHint:
    def test_kill_or_stand_aside_forces_zero(self) -> None:
        s = StressScore(
            composite=0.0,
            components=[],  # components list allowed empty; sum handled elsewhere
            binding_constraint="none",
        )
        # Bypass the empty-list validation by constructing via compute:
        s = compute_stress_score(
            _ok_macro(),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(),
        )
        for action in (ActionSuggestion.KILL, ActionSuggestion.STAND_ASIDE):
            h = compute_sizing_hint(s, SessionPhase.MORNING, action)
            assert h.size_mult == 0.0

    def test_low_stress_full_size_during_morning(self) -> None:
        s = compute_stress_score(
            _ok_macro(),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(),
        )
        h = compute_sizing_hint(s, SessionPhase.MORNING, ActionSuggestion.TRADE)
        assert h.size_mult == pytest.approx(1.0)

    def test_overnight_penalty_applies_to_trade(self) -> None:
        s = compute_stress_score(
            _ok_macro(),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(),
        )
        h = compute_sizing_hint(s, SessionPhase.OVERNIGHT, ActionSuggestion.TRADE)
        # overnight mult = 0.40, base = 1.00
        assert h.size_mult == pytest.approx(0.40)

    def test_stress_bands_monotonic_decrease(self) -> None:
        # Craft increasing stress -> increasing dd
        sizes = []
        for dd in (0.0, 0.01, 0.02, 0.03, 0.045):
            s = compute_stress_score(
                _ok_macro(),
                _ok_equity(dd_pct=dd),
                _ok_regime(),
                _ok_journal(),
            )
            # use MORNING to isolate stress band effect; use TRADE action
            h = compute_sizing_hint(s, SessionPhase.MORNING, ActionSuggestion.TRADE)
            sizes.append(h.size_mult)
        # Monotonically non-increasing
        for a, b in zip(sizes, sizes[1:], strict=False):
            assert b <= a + 1e-9

    def test_reduce_tier_caps_at_50(self) -> None:
        s = compute_stress_score(
            _ok_macro(),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(),
        )
        h = compute_sizing_hint(s, SessionPhase.MORNING, ActionSuggestion.REDUCE)
        assert h.size_mult <= 0.50 + 1e-9

    def test_review_tier_soft_caps_at_75(self) -> None:
        s = compute_stress_score(
            _ok_macro(),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(),
        )
        h = compute_sizing_hint(s, SessionPhase.MORNING, ActionSuggestion.REVIEW)
        assert h.size_mult <= 0.75 + 1e-9


# --------------------------------------------------------------------------- #
# alerts
# --------------------------------------------------------------------------- #


class TestAlerts:
    def test_no_alerts_when_all_clear(self) -> None:
        alerts = detect_alerts(
            _ok_macro(),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(),
        )
        assert alerts == []

    def test_dd_ladder_reduce_at_15pct(self) -> None:
        # DD = 0.015 -- 75% of 0.02 reduce threshold -> INFO
        alerts = detect_alerts(
            _ok_macro(),
            _ok_equity(dd_pct=0.015),
            _ok_regime(),
            _ok_journal(),
        )
        codes = [a.code for a in alerts]
        assert "dd_approaching_reduce" in codes

    def test_dd_ladder_escalates_to_critical_near_kill(self) -> None:
        # 80% of KILL = 0.04 -> CRITICAL
        alerts = detect_alerts(
            _ok_macro(),
            _ok_equity(dd_pct=0.042),
            _ok_regime(),
            _ok_journal(),
        )
        codes = [a.code for a in alerts]
        levels = [a.level for a in alerts]
        assert "dd_approaching_kill" in codes
        assert AlertLevel.CRITICAL in levels

    def test_kill_switch_is_critical(self) -> None:
        alerts = detect_alerts(
            _ok_macro(),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(kill=True),
        )
        assert any(a.level == AlertLevel.CRITICAL and a.code == "kill_switch_active" for a in alerts)

    def test_overrides_approaching_vs_at_threshold(self) -> None:
        # 1 below threshold -> INFO; at threshold -> WARN
        alerts_below = detect_alerts(
            _ok_macro(),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(overrides=OVERRIDE_REVIEW_THRESHOLD - 1),
        )
        alerts_at = detect_alerts(
            _ok_macro(),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(overrides=OVERRIDE_REVIEW_THRESHOLD),
        )
        assert any(a.code == "overrides_approaching_review" for a in alerts_below)
        assert any(a.code == "overrides_at_review" for a in alerts_at)

    def test_macro_event_three_level_ladder(self) -> None:
        # >1.5h and <=4h -> INFO; >1.0 and <=1.5 -> WARN; <=1.0 -> CRITICAL
        info = detect_alerts(
            _ok_macro(hours=3.0, label="FOMC"),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(),
        )
        warn = detect_alerts(
            _ok_macro(hours=1.2, label="FOMC"),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(),
        )
        crit = detect_alerts(
            _ok_macro(hours=0.5, label="FOMC"),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(),
        )
        assert any(a.code == "macro_event_upcoming" and a.level == AlertLevel.INFO for a in info)
        assert any(a.code == "macro_event_soon" and a.level == AlertLevel.WARN for a in warn)
        assert any(a.code == "macro_event_imminent" and a.level == AlertLevel.CRITICAL for a in crit)

    def test_alerts_sorted_by_severity_desc(self) -> None:
        alerts = detect_alerts(
            _ok_macro(hours=0.5, label="FOMC", bias="crisis"),
            _ok_equity(dd_pct=0.045, open_risk_r=4.0),
            _ok_regime(flipped=True, conf=0.2),
            _ok_journal(overrides=3, corr=True, mode="REQUIRE_ACK"),
        )
        severities = [a.severity for a in alerts]
        assert severities == sorted(severities, reverse=True)


# --------------------------------------------------------------------------- #
# margins
# --------------------------------------------------------------------------- #


class TestMargins:
    def test_headroom_all_positive_when_clean(self) -> None:
        m = compute_margins(_ok_equity(), _ok_journal())
        assert m.dd_to_reduce == pytest.approx(DD_REDUCE_THRESHOLD)
        assert m.dd_to_stand_aside == pytest.approx(DD_STAND_ASIDE_THRESHOLD)
        assert m.dd_to_kill == pytest.approx(DD_KILL_THRESHOLD)
        assert m.overrides_to_review == OVERRIDE_REVIEW_THRESHOLD
        assert m.open_risk_to_cap_r == pytest.approx(
            OPEN_RISK_HARD_CAP_R - 0.5,
        )

    def test_negative_margin_when_breached(self) -> None:
        m = compute_margins(
            _ok_equity(dd_pct=0.025, open_risk_r=4.0),
            _ok_journal(overrides=5),
        )
        assert m.dd_to_reduce < 0
        assert m.overrides_to_review < 0
        assert m.open_risk_to_cap_r < 0


# --------------------------------------------------------------------------- #
# playbook
# --------------------------------------------------------------------------- #


class TestPlaybook:
    def test_kill_playbook_present(self) -> None:
        sug = JarvisSuggestion(
            action=ActionSuggestion.KILL,
            reason="test",
            confidence=1.0,
        )
        steps = build_playbook(sug)
        assert any("flatten" in s.lower() for s in steps)
        assert any("cancel" in s.lower() for s in steps)

    def test_trade_playbook_mentions_a_plus(self) -> None:
        sug = JarvisSuggestion(
            action=ActionSuggestion.TRADE,
            reason="test",
            confidence=0.8,
        )
        steps = build_playbook(sug)
        assert any("A+" in s for s in steps)

    def test_open_drive_addendum(self) -> None:
        sug = JarvisSuggestion(
            action=ActionSuggestion.TRADE,
            reason="test",
            confidence=0.8,
        )
        steps = build_playbook(sug, session=SessionPhase.OPEN_DRIVE)
        assert any("first-hour" in s.lower() or "15m" in s for s in steps)

    def test_high_stress_addendum_when_trading(self) -> None:
        stress = StressScore(
            composite=0.50,
            components=[],
            binding_constraint="equity_dd",
        )
        # Build with compute to satisfy validators
        stress = compute_stress_score(
            _ok_macro(),
            _ok_equity(dd_pct=0.02),
            _ok_regime(),
            _ok_journal(),
        )
        sug = JarvisSuggestion(
            action=ActionSuggestion.TRADE,
            reason="test",
            confidence=0.8,
        )
        steps = build_playbook(sug, stress=stress, session=SessionPhase.MORNING)
        # Expect binding constraint note
        assert any("binding constraint" in s for s in steps)


# --------------------------------------------------------------------------- #
# explanation
# --------------------------------------------------------------------------- #


class TestExplanation:
    def test_explanation_mentions_action_and_stress(self) -> None:
        stress = compute_stress_score(
            _ok_macro(),
            _ok_equity(dd_pct=0.02),
            _ok_regime(),
            _ok_journal(),
        )
        margins = compute_margins(_ok_equity(dd_pct=0.02), _ok_journal())
        sug = JarvisSuggestion(
            action=ActionSuggestion.REDUCE,
            reason="dd 2%",
            confidence=0.75,
        )
        sizing = compute_sizing_hint(stress, SessionPhase.MORNING, sug.action)
        text = build_explanation(sug, stress, margins, SessionPhase.MORNING, sizing)
        assert "REDUCE" in text
        assert "stress" in text.lower()

    def test_explanation_single_string(self) -> None:
        stress = compute_stress_score(
            _ok_macro(),
            _ok_equity(),
            _ok_regime(),
            _ok_journal(),
        )
        margins = compute_margins(_ok_equity(), _ok_journal())
        sug = JarvisSuggestion(
            action=ActionSuggestion.TRADE,
            reason="green",
            confidence=0.8,
        )
        sizing = compute_sizing_hint(stress, SessionPhase.MORNING, sug.action)
        text = build_explanation(sug, stress, margins, SessionPhase.MORNING, sizing)
        assert isinstance(text, str)
        assert len(text) > 0


# --------------------------------------------------------------------------- #
# build_snapshot -- v2 enrichment plumbing
# --------------------------------------------------------------------------- #


class TestBuildSnapshotV2:
    def test_snapshot_populates_all_v2_fields(self) -> None:
        ts = datetime(2026, 4, 20, 14, 0, tzinfo=UTC)  # 10:00 ET -> OPEN_DRIVE
        ctx = build_snapshot(
            macro=_ok_macro(),
            equity=_ok_equity(dd_pct=0.005),
            regime=_ok_regime(),
            journal=_ok_journal(),
            ts=ts,
        )
        assert ctx.stress_score is not None
        assert ctx.session_phase is not None
        assert ctx.sizing_hint is not None
        assert ctx.margins is not None
        assert ctx.explanation
        assert ctx.playbook

    def test_snapshot_session_phase_drives_sizing(self) -> None:
        ts_over = datetime(2026, 4, 20, 22, 0, tzinfo=UTC)  # 18:00 ET overnight
        ts_morn = datetime(2026, 4, 20, 15, 0, tzinfo=UTC)  # 11:00 ET morning
        ctx_over = build_snapshot(
            macro=_ok_macro(),
            equity=_ok_equity(),
            regime=_ok_regime(),
            journal=_ok_journal(),
            ts=ts_over,
        )
        ctx_morn = build_snapshot(
            macro=_ok_macro(),
            equity=_ok_equity(),
            regime=_ok_regime(),
            journal=_ok_journal(),
            ts=ts_morn,
        )
        assert ctx_over.session_phase == SessionPhase.OVERNIGHT
        assert ctx_morn.session_phase == SessionPhase.MORNING
        assert ctx_over.sizing_hint.size_mult < ctx_morn.sizing_hint.size_mult


# --------------------------------------------------------------------------- #
# JarvisMemory
# --------------------------------------------------------------------------- #


def _ctx_for_memory(
    *,
    ts: datetime,
    dd: float,
    stress_comp: float,
    overrides: int = 0,
) -> JarvisContext:
    """Build a JarvisContext with arbitrary stress composite for memory tests."""
    stress = StressScore(
        composite=stress_comp,
        components=[],  # empty OK for this model
        binding_constraint="synthetic",
    )
    sug = JarvisSuggestion(
        action=ActionSuggestion.TRADE,
        reason="test",
        confidence=0.8,
    )
    return JarvisContext(
        ts=ts,
        macro=_ok_macro(),
        equity=_ok_equity(dd_pct=dd),
        regime=_ok_regime(),
        journal=_ok_journal(overrides=overrides),
        suggestion=sug,
        stress_score=stress,
        session_phase=SessionPhase.MORNING,
    )


class TestJarvisMemory:
    def test_maxlen_enforced(self) -> None:
        mem = JarvisMemory(maxlen=3)
        t0 = datetime(2026, 4, 20, 14, 0, tzinfo=UTC)
        for i in range(10):
            mem.append(
                _ctx_for_memory(
                    ts=t0 + timedelta(seconds=i),
                    dd=0.001 * i,
                    stress_comp=0.1,
                )
            )
        assert len(mem) == 3

    def test_trajectory_unknown_with_single_sample(self) -> None:
        mem = JarvisMemory()
        mem.append(
            _ctx_for_memory(
                ts=datetime(2026, 4, 20, 14, 0, tzinfo=UTC),
                dd=0.0,
                stress_comp=0.1,
            )
        )
        traj = mem.trajectory()
        assert traj.dd == TrajectoryState.UNKNOWN
        assert traj.stress == TrajectoryState.UNKNOWN

    def test_trajectory_worsening_dd(self) -> None:
        mem = JarvisMemory()
        t0 = datetime(2026, 4, 20, 14, 0, tzinfo=UTC)
        mem.append(_ctx_for_memory(ts=t0, dd=0.0, stress_comp=0.1))
        mem.append(
            _ctx_for_memory(
                ts=t0 + timedelta(minutes=5),
                dd=0.02,
                stress_comp=0.1,
            )
        )
        traj = mem.trajectory()
        assert traj.dd == TrajectoryState.WORSENING

    def test_trajectory_improving_dd(self) -> None:
        mem = JarvisMemory()
        t0 = datetime(2026, 4, 20, 14, 0, tzinfo=UTC)
        mem.append(_ctx_for_memory(ts=t0, dd=0.02, stress_comp=0.1))
        mem.append(
            _ctx_for_memory(
                ts=t0 + timedelta(minutes=5),
                dd=0.001,
                stress_comp=0.1,
            )
        )
        traj = mem.trajectory()
        assert traj.dd == TrajectoryState.IMPROVING

    def test_trajectory_flat_within_eps(self) -> None:
        mem = JarvisMemory()
        t0 = datetime(2026, 4, 20, 14, 0, tzinfo=UTC)
        mem.append(_ctx_for_memory(ts=t0, dd=0.010, stress_comp=0.20))
        mem.append(
            _ctx_for_memory(
                ts=t0 + timedelta(minutes=5),
                dd=0.011,
                stress_comp=0.21,
            )
        )
        traj = mem.trajectory()
        assert traj.dd == TrajectoryState.FLAT
        assert traj.stress == TrajectoryState.FLAT

    def test_overrides_velocity_per_24h(self) -> None:
        mem = JarvisMemory()
        t0 = datetime(2026, 4, 20, 14, 0, tzinfo=UTC)
        # 0 -> 2 overrides in 6 hours -> velocity = 8/24h
        mem.append(_ctx_for_memory(ts=t0, dd=0.0, stress_comp=0.1, overrides=0))
        mem.append(
            _ctx_for_memory(
                ts=t0 + timedelta(hours=6),
                dd=0.0,
                stress_comp=0.1,
                overrides=2,
            )
        )
        traj = mem.trajectory()
        assert traj.overrides_velocity_per_24h == pytest.approx(8.0, rel=1e-3)

    def test_rejects_maxlen_less_than_two(self) -> None:
        with pytest.raises(ValueError, match="maxlen"):
            JarvisMemory(maxlen=1)

    def test_snapshots_returns_list_copy(self) -> None:
        mem = JarvisMemory()
        t0 = datetime(2026, 4, 20, 14, 0, tzinfo=UTC)
        mem.append(_ctx_for_memory(ts=t0, dd=0.0, stress_comp=0.1))
        got = mem.snapshots()
        assert isinstance(got, list)
        assert len(got) == 1


# --------------------------------------------------------------------------- #
# JarvisContextEngine
# --------------------------------------------------------------------------- #


class _FakeMacro:
    def get_macro(self) -> MacroSnapshot:
        return _ok_macro()


class _FakeEquity:
    def __init__(self) -> None:
        self.dd = 0.0

    def get_equity(self) -> EquitySnapshot:
        return _ok_equity(dd_pct=self.dd)


class _FakeRegime:
    def get_regime(self) -> RegimeSnapshot:
        return _ok_regime()


class _FakeJournal:
    def __init__(self) -> None:
        self.overrides = 0

    def get_journal_snapshot(self) -> JournalSnapshot:
        return _ok_journal(overrides=self.overrides)


class TestJarvisContextEngine:
    def test_first_tick_has_no_trajectory(self) -> None:
        mac, eq, reg, j = _FakeMacro(), _FakeEquity(), _FakeRegime(), _FakeJournal()
        ticks = iter(
            [
                datetime(2026, 4, 20, 14, 0, tzinfo=UTC),
                datetime(2026, 4, 20, 14, 5, tzinfo=UTC),
            ]
        )
        builder = JarvisContextBuilder(
            macro_provider=mac,
            equity_provider=eq,
            regime_provider=reg,
            journal_provider=j,
            clock=lambda: next(ticks),
        )
        engine = JarvisContextEngine(builder=builder)
        ctx1 = engine.tick()
        assert ctx1.trajectory is None

    def test_second_tick_still_no_trajectory(self) -> None:
        # Trajectory needs >= 2 PRIOR samples in memory before it can fire.
        # tick 1 -> memory has 0 priors, tick 2 -> 1 prior -> still too few.
        # Only tick 3+ can report a non-empty trajectory.
        mac, eq, reg, j = _FakeMacro(), _FakeEquity(), _FakeRegime(), _FakeJournal()
        ticks = iter(
            [
                datetime(2026, 4, 20, 14, 0, tzinfo=UTC),
                datetime(2026, 4, 20, 14, 5, tzinfo=UTC),
            ]
        )
        builder = JarvisContextBuilder(
            macro_provider=mac,
            equity_provider=eq,
            regime_provider=reg,
            journal_provider=j,
            clock=lambda: next(ticks),
        )
        engine = JarvisContextEngine(builder=builder)
        engine.tick()
        eq.dd = 0.02  # drawdown worsens
        ctx2 = engine.tick()
        assert ctx2.trajectory is None

    def test_trajectory_visible_after_three_ticks(self) -> None:
        mac = _FakeMacro()
        eq = _FakeEquity()
        reg = _FakeRegime()
        j = _FakeJournal()
        times = [
            datetime(2026, 4, 20, 14, 0, tzinfo=UTC),
            datetime(2026, 4, 20, 14, 5, tzinfo=UTC),
            datetime(2026, 4, 20, 14, 10, tzinfo=UTC),
        ]
        it = iter(times)
        builder = JarvisContextBuilder(
            macro_provider=mac,
            equity_provider=eq,
            regime_provider=reg,
            journal_provider=j,
            clock=lambda: next(it),
        )
        engine = JarvisContextEngine(builder=builder)
        engine.tick()  # sample A: dd=0
        eq.dd = 0.015
        engine.tick()  # sample B: dd=0.015
        eq.dd = 0.03
        ctx3 = engine.tick()  # trajectory should see A->B worsening
        assert ctx3.trajectory is not None
        assert ctx3.trajectory.samples == 2
        assert ctx3.trajectory.dd == TrajectoryState.WORSENING

    def test_custom_memory_injectable(self) -> None:
        custom = JarvisMemory(maxlen=5)
        mac, eq, reg, j = _FakeMacro(), _FakeEquity(), _FakeRegime(), _FakeJournal()
        builder = JarvisContextBuilder(
            macro_provider=mac,
            equity_provider=eq,
            regime_provider=reg,
            journal_provider=j,
            clock=lambda: datetime(2026, 4, 20, 14, 0, tzinfo=UTC),
        )
        engine = JarvisContextEngine(builder=builder, memory=custom)
        engine.tick()
        assert custom is engine.memory
        assert len(custom) == 1


# --------------------------------------------------------------------------- #
# Model shape validation
# --------------------------------------------------------------------------- #


class TestModelShapes:
    def test_alert_severity_bounded(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            JarvisAlert(
                level=AlertLevel.INFO,
                code="x",
                message="y",
                severity=1.5,
            )

    def test_alert_requires_non_empty_code(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            JarvisAlert(
                level=AlertLevel.INFO,
                code="",
                message="y",
                severity=0.1,
            )

    def test_sizing_hint_bounded(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SizingHint(size_mult=1.5, reason="too big")

    def test_margins_can_be_negative(self) -> None:
        # Negative margins indicate breaches -- must be allowed.
        m = JarvisMargins(
            dd_to_reduce=-0.005,
            dd_to_stand_aside=0.005,
            dd_to_kill=0.025,
            overrides_to_review=-2,
            open_risk_to_cap_r=-1.0,
        )
        assert m.dd_to_reduce == -0.005

    def test_trajectory_defaults_unknown(self) -> None:
        t = Trajectory()
        assert t.dd == TrajectoryState.UNKNOWN
        assert t.stress == TrajectoryState.UNKNOWN
        assert t.samples == 0
