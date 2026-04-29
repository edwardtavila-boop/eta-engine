from __future__ import annotations

from eta_engine.brain.pnl_drift import PageHinkleyDetector
from eta_engine.brain.sweep_firm_gate import (
    FirmVerdict,
    FirmVerdictCode,
    SweepCandidate,
    apply_firm_gate,
    filter_go_verdicts,
)


def _candidate() -> SweepCandidate:
    return SweepCandidate(
        strategy_id="mnq_orb_candidate",
        params={"range_minutes": 5},
        metrics={"oos_sharpe": 1.7},
        seed=42,
    )


def test_apply_firm_gate_falls_back_to_hold_when_runner_missing_or_empty() -> None:
    missing = apply_firm_gate(_candidate())
    empty = apply_firm_gate(_candidate(), board_runner=lambda _candidate: None)

    assert missing.code == FirmVerdictCode.HOLD
    assert missing.promotes is False
    assert "no board_runner" in missing.reasons[0]
    assert empty.code == FirmVerdictCode.HOLD
    assert "returned None" in empty.reasons[0]


def test_apply_firm_gate_returns_runner_verdict_and_filters_go_candidates() -> None:
    candidate = _candidate()
    go = FirmVerdict(
        code=FirmVerdictCode.GO,
        confidence=0.91,
        reasons=("six-agent board approved",),
        agent_summary={"jarvis": "GO"},
    )
    hold = FirmVerdict(code=FirmVerdictCode.HOLD, confidence=0.55)

    verdict = apply_firm_gate(candidate, board_runner=lambda _candidate: go)

    assert verdict == go
    assert verdict.promotes is True
    assert filter_go_verdicts([(candidate, verdict), (_candidate(), hold)]) == [candidate]


def test_apply_firm_gate_converts_runner_exception_to_safe_hold() -> None:
    def _raise(_candidate: SweepCandidate) -> FirmVerdict:
        raise RuntimeError("firm package unavailable")

    verdict = apply_firm_gate(_candidate(), board_runner=_raise)

    assert verdict.code == FirmVerdictCode.HOLD
    assert verdict.confidence == 0.5
    assert "firm package unavailable" in verdict.reasons[0]


def test_page_hinkley_flags_downward_pnl_drift_and_resets() -> None:
    detector = PageHinkleyDetector(delta=0.0, threshold=0.4)
    alarms = [detector.update(x) for x in [0.5, 0.5, 0.5, -0.5]]

    assert alarms[:-1] == [None, None, None]
    alarm = alarms[-1]
    assert alarm is not None
    assert alarm.direction == "down"
    assert alarm.cumulative > alarm.threshold
    assert alarm.n_observations == 4
    assert detector.n == 0


def test_page_hinkley_flags_upward_pnl_drift_and_reset_keeps_reusable_detector() -> None:
    detector = PageHinkleyDetector(delta=0.0, threshold=0.4)
    alarms = [detector.update(x) for x in [-0.5, -0.5, -0.5, 0.5]]

    assert alarms[-1] is not None
    assert alarms[-1].direction == "up"
    assert detector.n == 0

    clean = detector.update(0.1)
    assert clean is None
    assert detector.n == 1
