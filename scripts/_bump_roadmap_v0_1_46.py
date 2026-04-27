"""One-shot: bump roadmap_state.json to v0.1.46.

ADAPTIVE SIZING ENGINE -- position size is now a function of
(regime x confluence x htf_bias x equity_band x prior_success) and
maps to tiered multipliers (CONVICTION 3.0x / 2.0x, STANDARD 1.0x,
REDUCED 0.5x, PROBE 0.25x, SKIP 0.0x).

Context
-------
The user's directive: "anything rated super high confidence of
success needs to be sized 2-3 times bigger than normal, anything
rated low chance or too much uncertainty not enough confluence gets
sized down ... the concept is if you know you know and you take your
shot ... sometimes you have to roll the dice when the odds are
stacked in your favor and you take out the sniper and shoot ... we
set our own risk model based on fine tuning and a plethora of
testing and make it self evolve with live data ... regime based ...
adaptable and learn to run from its first steps to become a
marathon runner ... some periods require being more aggressive as
odds are more certain, some periods require being more conservative
as things are unknown, some periods we completely down size due to
too much inconsistent uncertainty."

Everything before v0.1.46 computed size as risk_usd / stop_distance.
That's the same dollar risk whether the setup is a median-confluence
chop-trade or a sniper-shot trend continuation with every axis
aligned. The Apex predator thesis is that the best 5-10% of setups
deserve 2-3x the capital, and the worst 30% deserve probe sizing or
a skip. v0.1.46 makes that thesis executable.

What ships
----------
  * ``strategies/adaptive_sizing.py`` -- new. Pure-function axis
    scorers + weighted-sum tier mapping + hard overrides. No I/O,
    no async, no mutable state inside the sizer itself. The engine
    is a pure function over a SizingContext value object.

  * ``tests/test_strategies_adaptive_sizing.py`` -- 49 tests across
    10 classes covering axis scorers, equity-band classification,
    tier mapping (including the sniper-shot path at 3x and the
    risk-off path at SKIP), hard overrides, safety bounds,
    policy tuning, and self-evolving semantics (losing streak
    downsizes; critical drawdown suppresses conviction).

  * ``scripts/_bump_roadmap_v0_1_46.py`` -- this file.

API surface
-----------
  * ``class RegimeLabel(StrEnum)`` -- TRENDING / RANGING /
    TRANSITION / HIGH_VOL
  * ``class EquityBand(StrEnum)`` -- GROWTH / NEUTRAL / DRAWDOWN /
    CRITICAL
  * ``class SizeTier(StrEnum)`` -- CONVICTION / STANDARD / REDUCED /
    PROBE / SKIP
  * ``@dataclass PriorSuccessMetrics`` -- rolling n_trades, hit_rate,
    expectancy_r, avg_win_r, avg_loss_r, consecutive_losses,
    consecutive_wins. This is the feedback hook the engine uses to
    self-evolve on live trade outcomes.
  * ``@dataclass SizingContext`` -- asset/strategy/side/regime/
    confluence_score/htf_bias/equity_band/prior/base_risk_pct/
    kill_switch_active/session_allows_entries. All inputs to the
    sizer live on this one frozen value object.
  * ``@dataclass SizingPolicy`` -- axis weights + tier thresholds +
    multipliers + safety bounds. Tunable without code changes.
  * ``@dataclass SizingVerdict`` -- tier, multiplier,
    adjusted_risk_pct, total_score, axis_scores (dict), rationale
    (tuple of strings). Every decision is explainable.
  * ``compute_size(ctx, policy=DEFAULT_SIZING_POLICY) -> SizingVerdict``
    -- the pure-function sizer. Hard gates short-circuit to SKIP;
    otherwise weighted sum of axis scores -> tier mapping -> risk
    clamp.
  * ``classify_equity_band(equity, high_water, *, growth_threshold,
    drawdown_threshold, critical_threshold) -> EquityBand``
    -- equity/high_water ratio classifier. Used by the live bot
    to decide which EquityBand the sizer should see.
  * Axis scorers: ``score_regime``, ``score_confluence``,
    ``score_htf_agreement``, ``score_equity_band``,
    ``score_prior_success``.

Design notes
------------
  * Axis scores produce a bounded total in roughly [-0.95, +0.875]
    so the tier thresholds have a clear physical meaning as "% of
    the positive ceiling." CONVICTION 3.0x requires >= 0.65 (about
    74% of ceiling), CONVICTION 2.0x requires >= 0.45 (about 51%
    of ceiling), STANDARD >= 0.15, REDUCED >= -0.10, PROBE >= -0.35,
    below that -> SKIP.
  * Hard overrides come BEFORE axis computation. kill_switch_active
    or session_allows_entries=False produces SKIP with an explicit
    rationale. The sizer will never fire a kill-switched trade no
    matter how good the axis scores look.
  * Safety bounds: non-probe/non-skip tiers clamp the adjusted risk
    to [min_risk_pct, max_risk_pct]. PROBE stays small by construction
    (floor 0.01%). SKIP is exactly 0.
  * Self-evolving via PriorSuccessMetrics feedback. When the live
    trade outcome layer updates PriorSuccessMetrics, the next call
    to compute_size sees the new hit rate, expectancy, and streak
    counters. A run of losses downsizes automatically. A run of
    wins with positive expectancy upsizes. No manual knob
    required.
  * Equity-band classifier: high_water must be positive; equity may
    be zero or negative (you're just in a worse band). The ratio
    thresholds default to 1.02 / 0.95 / 0.90 which matches the
    user's "some periods we completely downsize due to too much
    inconsistent uncertainty" -> CRITICAL band locks out CONVICTION
    by capping the equity-band axis score.

Self-evolving contract
----------------------
The engine is a pure function today, but it's wired to self-evolve
tomorrow via two feedback loops:
  1. PriorSuccessMetrics is updated by the trade-outcome layer
     (not this module). A losing streak of 3 with negative
     expectancy pulls the prior-success axis deep negative, which
     pushes the next setup (same strategy) toward REDUCED/PROBE.
  2. classify_equity_band is called on every tick by the live bot.
     A drawdown from 1.02 -> 0.90 of high_water flips the equity
     band from GROWTH -> NEUTRAL -> DRAWDOWN -> CRITICAL,
     monotonically reducing the equity-band axis score.

Delta
-----
  * tests_passing: 2036 -> 2085 (+49 adaptive-sizing tests)
  * All pre-existing tests still pass unchanged
  * Ruff-clean on new module and its tests
  * No phase-level status changes (overall_progress_pct stays at 99)

Why this matters
----------------
Before v0.1.46 every signal was sized identically. The portfolio had
no way to bet bigger when the tape and the statistics both agreed,
and no way to shrink when the stats were saying "you're fooling
yourself." v0.1.46 installs the axis-weighted verdict; v0.1.47 will
wire the PERFORMANCE RETROSPECTIVE ENGINE that emits a structured
self-review ("what stopped working, what to adjust, deviation path")
on drawdown breach or regime shift and feeds it back into
PriorSuccessMetrics. Together those two bundles convert the bot
from a mechanical executor into a self-questioning, self-evolving
risk engine.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.46"
NEW_TESTS_ABS = 2085


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_46_adaptive_sizing_engine"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "ADAPTIVE SIZING ENGINE -- position size is a function "
            "of (regime x confluence x htf_bias x equity_band x "
            "prior_success) mapped to tiered multipliers "
            "(CONVICTION 3.0x/2.0x, STANDARD 1.0x, REDUCED 0.5x, "
            "PROBE 0.25x, SKIP 0.0x)."
        ),
        "theme": (
            "Install the Apex thesis: the best 5-10% of setups "
            "deserve 2-3x the capital, the worst 30% deserve probe "
            "sizing or a skip. Convert size from a static "
            "risk_usd/stop_distance formula into an axis-weighted, "
            "regime-aware, self-evolving decision with a full "
            "rationale trail. When odds are stacked, take the "
            "sniper shot. When stats say 'you're fooling yourself,' "
            "downsize or sit out."
        ),
        "operator_directive_quote": (
            "anything rated super high confidence of success needs "
            "to be sized 2-3 times bigger than normal, anything "
            "rated low chance or too much uncertainty not enough "
            "confluence gets sized down ... if you know you know "
            "and you take your shot ... sometimes you have to roll "
            "the dice when the odds are stacked in your favor and "
            "you take out the sniper and shoot ... make it self "
            "evolve with live data as time goes on, make this "
            "regime based as well, as with time the only thing "
            "guaranteed is change, it has to be adaptable and learn "
            "to run from its first steps to become a marathon "
            "runner."
        ),
        "artifacts_added": {
            "strategies": ["strategies/adaptive_sizing.py"],
            "tests": ["tests/test_strategies_adaptive_sizing.py"],
            "scripts": ["scripts/_bump_roadmap_v0_1_46.py"],
        },
        "artifacts_modified": {},
        "api_surface": {
            "RegimeLabel": ("StrEnum -- TRENDING / RANGING / TRANSITION / HIGH_VOL"),
            "EquityBand": ("StrEnum -- GROWTH / NEUTRAL / DRAWDOWN / CRITICAL"),
            "SizeTier": ("StrEnum -- CONVICTION / STANDARD / REDUCED / PROBE / SKIP"),
            "PriorSuccessMetrics": (
                "frozen dataclass -- n_trades, hit_rate, expectancy_r, "
                "avg_win_r, avg_loss_r, consecutive_losses, "
                "consecutive_wins. This is the feedback hook the "
                "engine uses to self-evolve on live trade outcomes."
            ),
            "SizingContext": (
                "frozen dataclass -- asset, strategy, side, regime, "
                "confluence_score, htf_bias, equity_band, prior, "
                "base_risk_pct=1.0, kill_switch_active=False, "
                "session_allows_entries=True. All sizer inputs "
                "live on one immutable value object."
            ),
            "SizingPolicy": (
                "frozen dataclass -- axis weights + tier thresholds + "
                "multipliers + safety bounds. Tunable without code "
                "changes. Defaults: weight_regime=0.25, "
                "weight_confluence=0.30, weight_htf=0.10, "
                "weight_equity=0.15, weight_prior=0.20."
            ),
            "SizingVerdict": (
                "frozen dataclass -- tier, multiplier, "
                "adjusted_risk_pct, total_score, axis_scores (dict), "
                "rationale (tuple[str, ...]). Every decision is "
                "explainable and auditable."
            ),
            "compute_size": (
                "(ctx: SizingContext, policy: SizingPolicy = "
                "DEFAULT_SIZING_POLICY) -> SizingVerdict -- pure "
                "function. Hard gates short-circuit to SKIP; "
                "otherwise weighted sum of axis scores -> tier "
                "mapping -> risk clamp."
            ),
            "classify_equity_band": (
                "(equity, high_water, *, growth_threshold=1.02, "
                "drawdown_threshold=0.95, critical_threshold=0.90) "
                "-> EquityBand -- raises ValueError for "
                "non-positive high_water."
            ),
            "score_regime": (
                "(regime, strategy) -> float in [-0.5, +1.0] -- "
                "rewards regime-strategy fit (e.g. TRENDING + "
                "MTF_TREND_FOLLOWING = +1.0; RANGING + trend "
                "strategy = -0.5)."
            ),
            "score_confluence": (
                "(confluence_score) -> float in [-1.0, +1.0] -- "
                "maps confluence_score linearly from the observed "
                "domain into [-1, +1]."
            ),
            "score_htf_agreement": (
                "(side, htf_bias) -> float in {-1.0, 0.0, +0.5} -- "
                "punishes counter-HTF trades; rewards alignment; "
                "neutral when htf_bias is None."
            ),
            "score_equity_band": (
                "(band) -> float in {+0.5, 0.0, -0.5, -1.0} -- "
                "GROWTH bonus, NEUTRAL neutral, DRAWDOWN caution, "
                "CRITICAL heavy penalty."
            ),
            "score_prior_success": (
                "(prior) -> float in [-1.0, +1.0] -- blends "
                "hit_rate+expectancy+streak into a single normalized "
                "score; demands min n_trades to escape neutral."
            ),
        },
        "design_notes": {
            "axis_weights_sum_to_1": (
                "regime=0.25, confluence=0.30, htf=0.10, equity=0.15, "
                "prior=0.20. Total=1.00. Confluence is the single "
                "heaviest axis because it already aggregates "
                "multiple signals under the hood; prior_success is "
                "the second heaviest because it's the self-evolving "
                "feedback signal."
            ),
            "tier_thresholds_as_pct_of_ceiling": (
                "Positive score ceiling is about +0.875. "
                "CONVICTION 3.0x at >=0.65 ~= 74% of ceiling (true "
                "sniper shots only). CONVICTION 2.0x at >=0.45 ~= "
                "51% of ceiling (high-conviction but not textbook). "
                "STANDARD at >=0.15 (the baseline go-live floor). "
                "REDUCED at >=-0.10. PROBE at >=-0.35. Below that "
                "is SKIP."
            ),
            "hard_overrides_short_circuit": (
                "kill_switch_active and session_allows_entries=False "
                "produce SKIP BEFORE any axis computation. The "
                "verdict rationale explicitly names the override so "
                "the observer can tell a kill-switched skip from a "
                "score-based skip."
            ),
            "safety_bounds_clamp_risk": (
                "Non-probe/non-skip tiers clamp adjusted_risk_pct "
                "to [min_risk_pct=0.10, max_risk_pct=5.00]. PROBE "
                "stays small by construction (floor 0.01%). SKIP is "
                "exactly 0. This prevents a pathological policy "
                "tune or a bad axis score from ever exceeding "
                "portfolio risk limits."
            ),
            "self_evolving_feedback": (
                "PriorSuccessMetrics is updated by the trade-outcome "
                "layer (not this module). A losing streak of 3 with "
                "negative expectancy pulls prior_success axis deep "
                "negative -> next setup same strategy -> REDUCED/"
                "PROBE. A run of wins with positive expectancy "
                "upsizes. No manual knob required."
            ),
            "equity_band_monotonic_with_drawdown": (
                "classify_equity_band maps equity/high_water to "
                "GROWTH (>=1.02), NEUTRAL (>=0.95), DRAWDOWN "
                "(>=0.90), CRITICAL (<0.90). Monotonic: as drawdown "
                "deepens the equity-band axis score decreases; "
                "combined with the weighted-sum gate, CRITICAL "
                "band + any other axis mildly negative flips "
                "STANDARD -> REDUCED. The user's 'some periods we "
                "completely downsize due to too much inconsistent "
                "uncertainty' path."
            ),
            "pure_function_zero_mutation": (
                "compute_size takes a frozen SizingContext and "
                "returns a frozen SizingVerdict. No mutable state "
                "inside the sizer itself. All feedback arrives via "
                "the PriorSuccessMetrics input. Trivially thread-"
                "safe; trivially replayable; trivially auditable."
            ),
            "rationale_trail_every_decision": (
                "SizingVerdict.rationale is a tuple of strings "
                "naming the dominant factors behind the tier call "
                "('regime trending confirms MTF_TREND_FOLLOWING', "
                "'confluence above sniper threshold', 'equity in "
                "drawdown band caps size'). Every decision is "
                "auditable without replaying the math."
            ),
        },
        "tier_structure": {
            "CONVICTION_3x": {
                "threshold_total_score": 0.65,
                "multiplier": 3.0,
                "rationale": (
                    "Sniper shot. Regime+confluence+htf+prior all "
                    "aligned. Equity band at least NEUTRAL. No "
                    "counter-HTF. This is the 'odds stacked in "
                    "your favor, take the shot' path."
                ),
            },
            "CONVICTION_2x": {
                "threshold_total_score": 0.45,
                "multiplier": 2.0,
                "rationale": (
                    "High conviction but not textbook. Typically "
                    "one axis is merely neutral rather than "
                    "positive, but the dominant axes are strong."
                ),
            },
            "STANDARD_1x": {
                "threshold_total_score": 0.15,
                "multiplier": 1.0,
                "rationale": ("Baseline tradeable setup. Meets gate but doesn't earn size bonus."),
            },
            "REDUCED_0_5x": {
                "threshold_total_score": -0.10,
                "multiplier": 0.5,
                "rationale": (
                    "Mildly negative net score. Trade-able but "
                    "with reduced risk. Typical in DRAWDOWN band "
                    "or on a short losing streak."
                ),
            },
            "PROBE_0_25x": {
                "threshold_total_score": -0.35,
                "multiplier": 0.25,
                "rationale": ("Uncertain conditions, but still worth a probe to feel the market. Floor 0.01% risk."),
            },
            "SKIP_0x": {
                "threshold_total_score": "below -0.35",
                "multiplier": 0.0,
                "rationale": (
                    "Too much inconsistency / counter-HTF / "
                    "critical drawdown / kill_switch_active. Sit "
                    "out and question the strategy."
                ),
            },
        },
        "test_coverage": {
            "tests_added": 49,
            "classes": {
                "TestRegimeScorer": 6,
                "TestConfluenceScorer": 6,
                "TestHtfAgreementScorer": 3,
                "TestEquityBandScorer": 1,
                "TestPriorSuccessScorer": 6,
                "TestClassifyEquityBand": 6,
                "TestTierMapping": 6,
                "TestHardOverrides": 2,
                "TestSafetyBounds": 4,
                "TestVerdictShape": 5,
                "TestPolicyTuning": 2,
                "TestSelfEvolvingSemantics": 2,
            },
            "notable_cases": [
                "sniper_shot_path: regime TRENDING + confluence "
                "high + htf aligned + prior winning + GROWTH band -> "
                "CONVICTION 3.0x",
                "risk_off_path: kill_switch_active -> SKIP regardless of axis scores",
                "critical_drawdown_suppresses_conviction: CRITICAL "
                "band can veto a high-confluence trade into "
                "REDUCED/PROBE",
                "losing_streak_downsizes: 3 consecutive_losses + negative expectancy -> REDUCED/PROBE on next setup",
                "safety_bounds_clamp: max_risk_pct=5.00 caps "
                "conviction trades even if the policy multiplier "
                "tries to push higher",
            ],
        },
        "ruff_clean_on": [
            "strategies/adaptive_sizing.py",
            "tests/test_strategies_adaptive_sizing.py",
        ],
        "phase_reconciliation": {
            "overall_progress_pct": 99,
            "status": (
                "unchanged -- still funding-gated on P9_ROLLOUT. "
                "The trading loop now has a pure-function, "
                "axis-weighted, regime-aware sizing engine ready "
                "to be wired into the live bots' _size_from_signal "
                "path. The engine itself is self-evolving via "
                "PriorSuccessMetrics feedback."
            ),
            "note": (
                "v0.1.47 will ship the PERFORMANCE RETROSPECTIVE "
                "ENGINE: on drawdown breach, regime shift, or a "
                "losing streak threshold, the engine emits a "
                "structured self-review ('what stopped working, "
                "what to adjust, deviation path') that feeds back "
                "into PriorSuccessMetrics and logs a regime-"
                "conditional playbook note. This closes the 'what "
                "should I have done differently? how should I "
                "deviate to return or exceed previous baseline?' "
                "loop from the operator's directive. v0.1.48+ "
                "wires compute_size() into MnqBot/EthPerpBot's "
                "_size_from_signal so tier multipliers actually "
                "drive live position size."
            ),
        },
        "python_touched": True,
        "jsx_touched": False,
        "tests_passing_before": prev_tests,
        "tests_passing_after": NEW_TESTS_ABS,
        "tests_new": NEW_TESTS_ABS - prev_tests,
    }

    milestones = state.setdefault("milestones", [])
    if isinstance(milestones, list):
        milestones.append(
            {
                "version": VERSION,
                "timestamp_utc": now,
                "title": (
                    "Adaptive Sizing Engine ships: position size "
                    "becomes a pure function of (regime x "
                    "confluence x htf x equity x prior_success) "
                    "mapped to tier multipliers "
                    "(CONVICTION 3.0x/2.0x, STANDARD 1.0x, REDUCED "
                    "0.5x, PROBE 0.25x, SKIP). Hard overrides "
                    "(kill_switch, session_off) short-circuit to "
                    "SKIP. Self-evolving via PriorSuccessMetrics "
                    "feedback. Sniper-shot path wired; risk-off "
                    "path wired. Rationale tuple on every verdict "
                    "for full audit trail."
                ),
                "tests_delta": NEW_TESTS_ABS - prev_tests,
                "tests_passing": NEW_TESTS_ABS,
            },
        )

    state["overall_progress_pct"] = state.get("overall_progress_pct", 99)

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"bumped roadmap_state.json to {VERSION} at {now}")
    print(f"  tests_passing: {prev_tests} -> {NEW_TESTS_ABS} ({NEW_TESTS_ABS - prev_tests:+d})")
    print(
        "  shipped: strategies/adaptive_sizing.py pure-function "
        "sizer + 49 tests. Tier-based multipliers wire the Apex "
        "'sniper shot when odds stacked, probe/skip when "
        "uncertain' thesis. Self-evolving via "
        "PriorSuccessMetrics feedback."
    )


if __name__ == "__main__":
    main()
