"""Master-tweaks tests -- P12_POLISH.master_tweaks."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eta_engine.core.master_tweaks import (
    RiskTag,
    Tweak,
    TweakPolicy,
    apply_tweak,
    apply_tweaks_bulk,
    classify_risk,
    propose_tweaks,
)
from eta_engine.core.parameter_sweep import CellScore, SweepCell

# ---------------------------------------------------------------------------
# classify_risk
# ---------------------------------------------------------------------------


def test_classify_risk_empty_proposal_is_safe() -> None:
    assert classify_risk({"a": 1.0}, {}) == RiskTag.SAFE


def test_classify_risk_under_10_pct_delta_is_safe() -> None:
    baseline = {"risk_pct": 0.010, "atr_mult": 2.0}
    proposal = {"risk_pct": 0.0105, "atr_mult": 2.1}  # 5%
    assert classify_risk(baseline, proposal) == RiskTag.SAFE


def test_classify_risk_between_10_and_35_pct_is_moderate() -> None:
    baseline = {"risk_pct": 0.010}
    proposal = {"risk_pct": 0.012}  # 20%
    assert classify_risk(baseline, proposal) == RiskTag.MODERATE


def test_classify_risk_over_35_pct_is_aggressive() -> None:
    baseline = {"risk_pct": 0.010}
    proposal = {"risk_pct": 0.015}  # 50%
    assert classify_risk(baseline, proposal) == RiskTag.AGGRESSIVE


def test_classify_risk_new_key_forces_at_least_moderate() -> None:
    baseline = {"risk_pct": 0.010}
    proposal = {"risk_pct": 0.0101, "atr_mult": 2.0}  # new key
    assert classify_risk(baseline, proposal) == RiskTag.MODERATE


def test_classify_risk_string_change_is_moderate_structural() -> None:
    baseline = {"mode": "conservative"}
    proposal = {"mode": "aggressive"}
    assert classify_risk(baseline, proposal) == RiskTag.MODERATE


def test_classify_risk_uses_max_across_keys() -> None:
    # risk_pct is 20% (moderate), atr_mult is 100% (aggressive) -> AGGRESSIVE
    baseline = {"risk_pct": 0.010, "atr_mult": 2.0}
    proposal = {"risk_pct": 0.012, "atr_mult": 4.0}
    assert classify_risk(baseline, proposal) == RiskTag.AGGRESSIVE


def test_classify_risk_zero_baseline_does_not_crash() -> None:
    baseline = {"bias": 0.0}
    proposal = {"bias": 0.1}
    # abs(0.1) / max(0, 1e-9) is huge -> AGGRESSIVE, but must not raise
    assert classify_risk(baseline, proposal) == RiskTag.AGGRESSIVE


# ---------------------------------------------------------------------------
# propose_tweaks
# ---------------------------------------------------------------------------


def _cell(params: dict, *, gate: bool = True, exp: float = 0.40, dd: float = 5.0) -> SweepCell:
    return SweepCell(
        params=params,
        score=CellScore(
            expectancy_r=exp,
            max_dd_pct=dd,
            win_rate=0.55,
            n_trades=100,
        ),
        gate_pass=gate,
        stability=0.05,
    )


def test_propose_tweaks_emits_one_per_winner() -> None:
    winners = {
        "mnq": _cell({"conf": 6.5, "risk": 0.010}),
        "eth_perp": _cell({"conf": 5.5, "risk": 0.008}),
    }
    baselines = {
        "mnq": {"conf": 6.0, "risk": 0.010},
        "eth_perp": {"conf": 5.5, "risk": 0.008},
    }
    tweaks = propose_tweaks(winners, baselines)
    assert len(tweaks) == 2
    by_bot = {t.bot: t for t in tweaks}
    assert by_bot["mnq"].proposal == {"conf": 6.5, "risk": 0.010}
    assert by_bot["eth_perp"].proposal == {"conf": 5.5, "risk": 0.008}


def test_propose_tweaks_carries_expected_metrics() -> None:
    winners = {"mnq": _cell({"conf": 6.5}, exp=0.55, dd=8.0)}
    tweaks = propose_tweaks(winners, {"mnq": {"conf": 6.0}})
    t = tweaks[0]
    assert t.expected_expectancy_r == 0.55
    assert t.expected_dd_pct == 8.0
    assert t.gate_pass is True


def test_propose_tweaks_tags_gate_fail_in_reason() -> None:
    winners = {"mnq": _cell({"conf": 6.5}, gate=False, exp=0.15)}
    tweaks = propose_tweaks(winners, {"mnq": {"conf": 6.0}})
    assert "closest-to-passing" in tweaks[0].reason


def test_propose_tweaks_tags_gate_pass_in_reason() -> None:
    winners = {"mnq": _cell({"conf": 6.5}, gate=True, exp=0.45)}
    tweaks = propose_tweaks(winners, {"mnq": {"conf": 6.0}})
    assert "gate-pass" in tweaks[0].reason


def test_propose_tweaks_handles_missing_baseline() -> None:
    winners = {"unknown": _cell({"conf": 6.5})}
    tweaks = propose_tweaks(winners, baselines={})
    # all params are structural -> MODERATE
    assert tweaks[0].risk_tag == RiskTag.MODERATE


def test_propose_tweaks_carries_custom_source() -> None:
    winners = {"mnq": _cell({"conf": 6.5})}
    tweaks = propose_tweaks(winners, {"mnq": {"conf": 6.0}}, source="tier_b_sweep")
    assert tweaks[0].source == "tier_b_sweep"


# ---------------------------------------------------------------------------
# apply_tweak
# ---------------------------------------------------------------------------


def test_apply_tweak_copies_proposal_into_baseline() -> None:
    baseline = {"conf": 6.0, "risk": 0.010, "unrelated": "keep"}
    tweak = Tweak(
        bot="mnq",
        proposal={"conf": 6.5},
        gate_pass=True,
        risk_tag=RiskTag.SAFE,
    )
    res = apply_tweak(baseline, tweak)
    assert res.applied is True
    assert res.new_config == {"conf": 6.5, "risk": 0.010, "unrelated": "keep"}


def test_apply_tweak_rejects_when_gate_required_and_not_pass() -> None:
    baseline = {"conf": 6.0}
    tweak = Tweak(
        bot="mnq",
        proposal={"conf": 6.1},
        gate_pass=False,
        risk_tag=RiskTag.SAFE,
    )
    res = apply_tweak(baseline, tweak, TweakPolicy(require_gate_pass=True))
    assert res.applied is False
    assert "did not pass gate" in res.reason
    assert res.new_config == baseline  # unchanged


def test_apply_tweak_allows_gate_fail_when_policy_relaxed() -> None:
    baseline = {"conf": 6.0}
    tweak = Tweak(
        bot="mnq",
        proposal={"conf": 6.1},
        gate_pass=False,
        risk_tag=RiskTag.SAFE,
    )
    res = apply_tweak(baseline, tweak, TweakPolicy(require_gate_pass=False))
    assert res.applied is True
    assert res.new_config["conf"] == 6.1


def test_apply_tweak_rejects_aggressive_by_default() -> None:
    baseline = {"conf": 6.0}
    tweak = Tweak(
        bot="mnq",
        proposal={"conf": 10.0},
        gate_pass=True,
        risk_tag=RiskTag.AGGRESSIVE,
    )
    res = apply_tweak(baseline, tweak)
    assert res.applied is False
    assert "AGGRESSIVE" in res.reason
    assert res.new_config == baseline


def test_apply_tweak_allows_aggressive_when_opted_in() -> None:
    baseline = {"conf": 6.0}
    tweak = Tweak(
        bot="mnq",
        proposal={"conf": 10.0},
        gate_pass=True,
        risk_tag=RiskTag.AGGRESSIVE,
    )
    # Relax both the max_relative_change AND the aggressive gate
    res = apply_tweak(
        baseline,
        tweak,
        TweakPolicy(allow_aggressive=True, max_relative_change=1.0),
    )
    assert res.applied is True
    assert res.new_config["conf"] == 10.0


def test_apply_tweak_rejects_params_that_exceed_relative_change_cap() -> None:
    baseline = {"conf": 6.0, "risk": 0.010}
    # conf +8.3% ok, risk +100% not ok
    tweak = Tweak(
        bot="mnq",
        proposal={"conf": 6.5, "risk": 0.020},
        gate_pass=True,
        risk_tag=RiskTag.MODERATE,
    )
    res = apply_tweak(baseline, tweak, TweakPolicy(max_relative_change=0.50))
    assert res.applied is True  # at least one param went through
    assert "risk" in res.rejected_params
    assert res.new_config["conf"] == 6.5
    assert res.new_config["risk"] == 0.010  # kept baseline


def test_apply_tweak_rejects_whole_tweak_when_every_param_exceeds_cap() -> None:
    baseline = {"conf": 6.0, "risk": 0.010}
    tweak = Tweak(
        bot="mnq",
        proposal={"conf": 12.0, "risk": 0.030},  # both massive
        gate_pass=True,
        risk_tag=RiskTag.MODERATE,
    )
    res = apply_tweak(baseline, tweak, TweakPolicy(max_relative_change=0.20))
    assert res.applied is False
    assert set(res.rejected_params) == {"conf", "risk"}
    assert res.new_config == baseline


def test_apply_tweak_preserves_non_proposed_baseline_keys() -> None:
    baseline = {"conf": 6.0, "risk": 0.010, "atr_mult": 2.0, "mode": "live"}
    tweak = Tweak(
        bot="mnq",
        proposal={"conf": 6.2},
        gate_pass=True,
        risk_tag=RiskTag.SAFE,
    )
    res = apply_tweak(baseline, tweak)
    assert res.new_config["atr_mult"] == 2.0
    assert res.new_config["mode"] == "live"


# ---------------------------------------------------------------------------
# apply_tweaks_bulk
# ---------------------------------------------------------------------------


def test_apply_tweaks_bulk_applies_per_bot_independently() -> None:
    baselines = {
        "mnq": {"conf": 6.0},
        "eth_perp": {"conf": 5.0},
    }
    tweaks = [
        Tweak(bot="mnq", proposal={"conf": 6.5}, gate_pass=True, risk_tag=RiskTag.SAFE),
        Tweak(bot="eth_perp", proposal={"conf": 5.2}, gate_pass=False, risk_tag=RiskTag.SAFE),
    ]
    result = apply_tweaks_bulk(baselines, tweaks, TweakPolicy(require_gate_pass=True))
    assert result["mnq"].applied is True
    assert result["eth_perp"].applied is False


def test_apply_tweaks_bulk_returns_result_per_tweak() -> None:
    baselines = {"mnq": {"conf": 6.0}, "eth_perp": {"conf": 5.0}}
    tweaks = [
        Tweak(bot="mnq", proposal={"conf": 6.1}, gate_pass=True, risk_tag=RiskTag.SAFE),
        Tweak(bot="eth_perp", proposal={"conf": 5.1}, gate_pass=True, risk_tag=RiskTag.SAFE),
    ]
    result = apply_tweaks_bulk(baselines, tweaks)
    assert set(result.keys()) == {"mnq", "eth_perp"}
    assert all(r.applied for r in result.values())


# ---------------------------------------------------------------------------
# TweakPolicy validation
# ---------------------------------------------------------------------------


def test_tweak_policy_rejects_non_positive_max_relative_change() -> None:
    with pytest.raises(ValidationError):
        TweakPolicy(max_relative_change=0)
    with pytest.raises(ValidationError):
        TweakPolicy(max_relative_change=-0.1)
