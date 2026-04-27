"""Tests for brain.jarvis_context -- continuous macro + risk loop."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eta_engine.brain.jarvis_context import (
    ActionSuggestion,
    EquitySnapshot,
    JarvisContext,
    JarvisContextBuilder,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
    build_snapshot,
    suggest_action,
)

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #


def _ok_macro() -> MacroSnapshot:
    return MacroSnapshot(vix_level=16.0, macro_bias="neutral")


def _ok_equity(
    *,
    dd_pct: float = 0.0,
    open_risk_r: float = 0.5,
    positions: int = 1,
    pnl: float = 0.0,
) -> EquitySnapshot:
    return EquitySnapshot(
        account_equity=50_000.0,
        daily_pnl=pnl,
        daily_drawdown_pct=dd_pct,
        open_positions=positions,
        open_risk_r=open_risk_r,
    )


def _ok_regime(regime: str = "TRENDING_UP", flipped: bool = False) -> RegimeSnapshot:
    return RegimeSnapshot(
        regime=regime,
        confidence=0.8,
        flipped_recently=flipped,
    )


def _ok_journal(
    *,
    kill: bool = False,
    mode: str = "ACTIVE",
    overrides: int = 0,
    corr_alert: bool = False,
) -> JournalSnapshot:
    return JournalSnapshot(
        kill_switch_active=kill,
        autopilot_mode=mode,
        overrides_last_24h=overrides,
        correlations_alert=corr_alert,
    )


# --------------------------------------------------------------------------- #
# Model validation
# --------------------------------------------------------------------------- #


def test_equity_rejects_negative_equity() -> None:
    with pytest.raises(ValidationError):
        EquitySnapshot(
            account_equity=-1.0,
            daily_pnl=0.0,
            daily_drawdown_pct=0.0,
            open_positions=0,
            open_risk_r=0.0,
        )


def test_equity_rejects_dd_over_100pct() -> None:
    with pytest.raises(ValidationError):
        EquitySnapshot(
            account_equity=1000.0,
            daily_pnl=0.0,
            daily_drawdown_pct=1.5,
            open_positions=0,
            open_risk_r=0.0,
        )


def test_regime_rejects_empty_regime() -> None:
    with pytest.raises(ValidationError):
        RegimeSnapshot(regime="", confidence=0.5)


def test_regime_rejects_bad_confidence() -> None:
    with pytest.raises(ValidationError):
        RegimeSnapshot(regime="R", confidence=1.5)


# --------------------------------------------------------------------------- #
# suggest_action: priority order
# --------------------------------------------------------------------------- #


def test_kill_when_kill_switch_active() -> None:
    s = suggest_action(
        _ok_macro(),
        _ok_equity(),
        _ok_regime(),
        _ok_journal(kill=True),
    )
    assert s.action == ActionSuggestion.KILL
    assert "kill-switch" in s.reason


def test_kill_when_dd_over_5pct() -> None:
    s = suggest_action(
        _ok_macro(),
        _ok_equity(dd_pct=0.06),
        _ok_regime(),
        _ok_journal(),
    )
    assert s.action == ActionSuggestion.KILL


def test_stand_aside_when_macro_event_in_30min() -> None:
    macro = MacroSnapshot(
        vix_level=15.0,
        next_event_label="FOMC 2026-05-01 14:00 ET",
        hours_until_next_event=0.5,
        macro_bias="neutral",
    )
    s = suggest_action(macro, _ok_equity(), _ok_regime(), _ok_journal())
    assert s.action == ActionSuggestion.STAND_ASIDE
    assert "FOMC" in s.reason


def test_stand_aside_when_autopilot_require_ack() -> None:
    s = suggest_action(
        _ok_macro(),
        _ok_equity(),
        _ok_regime(),
        _ok_journal(mode="REQUIRE_ACK"),
    )
    assert s.action == ActionSuggestion.STAND_ASIDE
    assert "ack" in s.reason.lower()


def test_stand_aside_when_dd_3pct() -> None:
    s = suggest_action(
        _ok_macro(),
        _ok_equity(dd_pct=0.035),
        _ok_regime(),
        _ok_journal(),
    )
    assert s.action == ActionSuggestion.STAND_ASIDE


def test_reduce_when_dd_2pct() -> None:
    s = suggest_action(
        _ok_macro(),
        _ok_equity(dd_pct=0.02),
        _ok_regime(),
        _ok_journal(),
    )
    assert s.action == ActionSuggestion.REDUCE


def test_reduce_when_open_risk_over_3r() -> None:
    s = suggest_action(
        _ok_macro(),
        _ok_equity(open_risk_r=3.5, positions=3),
        _ok_regime(),
        _ok_journal(),
    )
    assert s.action == ActionSuggestion.REDUCE
    assert "open risk" in s.reason.lower()


def test_reduce_when_crisis_regime() -> None:
    s = suggest_action(
        _ok_macro(),
        _ok_equity(),
        _ok_regime(regime="CRISIS"),
        _ok_journal(),
    )
    assert s.action == ActionSuggestion.REDUCE


def test_review_when_many_overrides() -> None:
    s = suggest_action(
        _ok_macro(),
        _ok_equity(),
        _ok_regime(),
        _ok_journal(overrides=5),
    )
    assert s.action == ActionSuggestion.REVIEW
    assert "override" in s.reason.lower()


def test_review_when_regime_flipped() -> None:
    s = suggest_action(
        _ok_macro(),
        _ok_equity(),
        _ok_regime(regime="TRENDING_DOWN", flipped=True),
        _ok_journal(),
    )
    assert s.action == ActionSuggestion.REVIEW


def test_review_when_correlations_alert() -> None:
    s = suggest_action(
        _ok_macro(),
        _ok_equity(),
        _ok_regime(),
        _ok_journal(corr_alert=True),
    )
    assert s.action == ActionSuggestion.REVIEW


def test_trade_when_all_green() -> None:
    s = suggest_action(
        _ok_macro(),
        _ok_equity(),
        _ok_regime(),
        _ok_journal(),
    )
    assert s.action == ActionSuggestion.TRADE
    assert "green" in s.reason.lower()


def test_kill_beats_stand_aside() -> None:
    # Both dd>5% and macro event -- kill wins
    macro = MacroSnapshot(
        next_event_label="CPI",
        hours_until_next_event=0.1,
    )
    s = suggest_action(
        macro,
        _ok_equity(dd_pct=0.06),
        _ok_regime(),
        _ok_journal(),
    )
    assert s.action == ActionSuggestion.KILL


def test_stand_aside_beats_reduce() -> None:
    # macro event + dd 2.5% -- stand aside wins (event priority)
    macro = MacroSnapshot(
        next_event_label="FOMC",
        hours_until_next_event=0.25,
    )
    s = suggest_action(
        macro,
        _ok_equity(dd_pct=0.025),
        _ok_regime(),
        _ok_journal(),
    )
    assert s.action == ActionSuggestion.STAND_ASIDE


def test_no_event_label_means_no_stand_aside() -> None:
    macro = MacroSnapshot(
        next_event_label=None,
        hours_until_next_event=0.1,
    )
    s = suggest_action(macro, _ok_equity(), _ok_regime(), _ok_journal())
    assert s.action == ActionSuggestion.TRADE


# --------------------------------------------------------------------------- #
# build_snapshot
# --------------------------------------------------------------------------- #


def test_build_snapshot_assembles_all_four() -> None:
    ctx = build_snapshot(
        macro=_ok_macro(),
        equity=_ok_equity(),
        regime=_ok_regime(),
        journal=_ok_journal(),
    )
    assert isinstance(ctx, JarvisContext)
    assert ctx.suggestion.action == ActionSuggestion.TRADE


def test_build_snapshot_preserves_ts() -> None:
    ts = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    ctx = build_snapshot(
        macro=_ok_macro(),
        equity=_ok_equity(),
        regime=_ok_regime(),
        journal=_ok_journal(),
        ts=ts,
    )
    assert ctx.ts == ts


def test_build_snapshot_attaches_notes() -> None:
    ctx = build_snapshot(
        macro=_ok_macro(),
        equity=_ok_equity(),
        regime=_ok_regime(),
        journal=_ok_journal(),
        notes=["new FOMC meeting detected"],
    )
    assert ctx.notes == ["new FOMC meeting detected"]


# --------------------------------------------------------------------------- #
# JarvisContextBuilder (provider wiring)
# --------------------------------------------------------------------------- #


class _MP:
    def get_macro(self) -> MacroSnapshot:
        return _ok_macro()


class _EP:
    def __init__(self) -> None:
        self.calls = 0

    def get_equity(self) -> EquitySnapshot:
        self.calls += 1
        return _ok_equity()


class _RP:
    def get_regime(self) -> RegimeSnapshot:
        return _ok_regime()


class _JP:
    def get_journal_snapshot(self) -> JournalSnapshot:
        return _ok_journal()


def test_builder_requires_all_four_providers() -> None:
    with pytest.raises(TypeError):
        JarvisContextBuilder(
            macro_provider=object(),  # type: ignore[arg-type]
            equity_provider=_EP(),
            regime_provider=_RP(),
            journal_provider=_JP(),
        )


def test_builder_snapshot_pulls_from_providers() -> None:
    ep = _EP()
    b = JarvisContextBuilder(
        macro_provider=_MP(),
        equity_provider=ep,
        regime_provider=_RP(),
        journal_provider=_JP(),
    )
    ctx1 = b.snapshot()
    ctx2 = b.snapshot()
    assert ep.calls == 2
    assert ctx1.suggestion.action == ActionSuggestion.TRADE
    assert ctx2.suggestion.action == ActionSuggestion.TRADE


def test_builder_injectable_clock() -> None:
    fixed = datetime(2026, 4, 17, 9, 30, tzinfo=UTC)
    b = JarvisContextBuilder(
        macro_provider=_MP(),
        equity_provider=_EP(),
        regime_provider=_RP(),
        journal_provider=_JP(),
        clock=lambda: fixed,
    )
    ctx = b.snapshot()
    assert ctx.ts == fixed
