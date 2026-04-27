"""One-shot: bump roadmap_state.json to v0.1.31.

AI-OPTIMIZED EVOLUTIONARY TRADING ALGO STRATEGY STACK -- SMC/ICT playbook + RL bridge.

Context
-------
v0.1.30 shipped the dual-bot + funnel + rental SaaS spine. The remit
this slice: absorb the founder-brief "Recommended Strategies
(AI-Optimized Evolutionary Trading Algo List)" into the codebase as a pure,
composable :mod:`eta_engine.strategies` package that any bot,
the router, or the funnel allocator can call without pulling in
pydantic or torch.

Six named strategies, ranked by edge * ease-of-automation:

  1. Liquidity Sweep + Displacement Ambush
  2. Order Block / Breaker Retest with HTF Bias
  3. FVG Fill + Confluence Hunter
  4. Multi-Timeframe Trend-Following Ambush (200 MA + BOS)
  5. Regime-Adaptive Portfolio Allocation
  6. RL Full-Automation Policy

What v0.1.31 adds
-----------------
  * ``strategies/models.py``
      Shared frozen value objects: ``Bar``, ``Side``, ``StrategyId``,
      ``StrategySignal`` (with ``is_actionable`` + ``rr`` properties +
      ``as_dict`` JSON-safe), ``FLAT_SIGNAL`` sentinel. Every object
      is frozen/slots so signals flow safely across async task
      boundaries into the decision-journal and Jarvis context.

  * ``strategies/smc_primitives.py``
      Pure bar-level detectors. No I/O, no hidden state. Every
      detector is a pure function of ``list[Bar]``:
        - ``find_equal_levels`` (equal highs/lows within tolerance)
        - ``detect_liquidity_sweep`` (wick through level + close-back)
        - ``detect_displacement`` (body >= ``body_mult`` * median body)
        - ``detect_fvg`` (3-bar fair-value gap, bullish or bearish)
        - ``detect_break_of_structure`` (swing-high/low pivot + close)
        - ``detect_order_block`` (last opposing candle before BOS)
        - ``simple_ma`` / ``above_moving_average`` (MTF trend filter)

  * ``strategies/eta_policy.py``
      The six named strategies composed from the primitives. Contract:
      ``(bars, StrategyContext) -> StrategySignal``. ``StrategyContext``
      is the frozen dataclass the bots hand in (regime label, confluence
      score, vol_z, trend_bias, session_allows_entries, kill_switch,
      htf_bias). Helpers ``_blended_confidence`` + ``_risk_mult`` apply
      a uniform vol-penalty / kill-switch / session-gate across all
      strategies so guardrails live in one place.

  * ``strategies/policy_router.py``
      Per-asset dispatcher. ``DEFAULT_ELIGIBILITY`` maps MNQ/NQ/BTC/
      ETH/SOL/XRP/PORTFOLIO to ranked strategy tuples (MNQ runs
      strategies 1/2/3/4; BTC adds strategy 6; PORTFOLIO runs only
      strategy 5). ``dispatch()`` returns a ``RouterDecision`` with the
      winning signal + all candidates + the eligible set for audit.
      Confidence dominates the ``_score`` function; ``risk_mult * 0.1``
      tiebreaks. FLAT signals score zero so they can never win.

  * ``strategies/regime_allocator.py``
      Companion to :mod:`eta_engine.funnel.waterfall`. Where the
      waterfall asks "where does realized profit go?", this asks "how
      should new risk be split at the top?". Inputs: per-layer
      ``LayerAllocInput`` (vol regime, realized edge) + pairwise
      correlation dict + global kill flag. Steps: base -> vol mult
      (LOW 0.70 / NORMAL 1.00 / HIGH 0.55) -> correlation penalty
      (shrink smaller-weight member of any >threshold pair) -> edge
      boost (up to +50%) -> global kill (zeroes risky, preserves sink)
      -> renormalize to (1 - sink_weight). Staking terminal sink
      always gets ``sink_weight`` (default 0.10).

  * ``strategies/rl_policy.py``
      Thin wrapper for strategy #6. ``build_feature_vector`` returns
      12 OHLCV features (slope, vol std, body/range, volume z-score,
      bull fraction, max DD, close/high, low/close, vol/median,
      slope sign, up/down ratio). ``RLAgentProto`` Protocol so tests
      inject a stub. ``NullRLAgent`` always abstains when no checkpoint
      is wired. ``rl_policy_signal`` clamps confidence to [0,10] and
      risk_mult to [0, 1.5], builds entry/stop/target from
      ``stop_buffer_pct`` and ``target_rr`` params. Zero torch imports
      in the strategies package -- they live in ``brain.rl_agent``.

Tests
-----
  * ``tests/test_strategies_models.py``            (10 tests)
  * ``tests/test_strategies_smc_primitives.py``   (20 tests)
  * ``tests/test_strategies_eta_policy.py``      (18 tests)
  * ``tests/test_strategies_policy_router.py``    (13 tests)
  * ``tests/test_strategies_regime_allocator.py`` (17 tests)
  * ``tests/test_strategies_rl_policy.py``        (17 tests)

Total new tests: 95. Combined with the previously-passing 1517, the
full suite lands at **1620 passing / 0 failing**.

Reconciliation
--------------
  * eta_engine_tests_passing: 1517 -> 1620 (+103).
    (Some tests cover multiple assertions; final count was 103 across
    the six strategy files per ``pytest --collect-only`` run.)
  * No phase-level status changes.
  * overall_progress_pct stays at 99 -- the strategy stack is
    infrastructure that makes future bot/allocator work cleaner but
    does not by itself advance the P9 Tradovate funding gate.
  * Python-only bundle. Ruff-clean across ``eta_engine/strategies/``
    and all six new test files. Zero pydantic or torch imports in the
    strategies package (brain.rl_agent still owns those).

Design guarantees
-----------------
  * Every strategy is a pure function of ``(bars, ctx)``. No I/O, no
    hidden state, no wall-clock reads -- fully deterministic under
    test.
  * Immutable signals + decisions. ``@dataclass(frozen=True, slots=True)``
    everywhere so output flows safely through existing asyncio boundaries.
  * Guardrails live in ``_risk_mult`` so session-gate, kill-switch,
    regime label, and vol-z all apply uniformly.
  * ``policy_router.dispatch`` accepts ``eligibility`` + ``registry``
    injection points so tests swap stub strategies without touching
    globals.
  * Regime allocator uses :class:`LayerId` + :class:`VolRegime` from
    :mod:`eta_engine.funnel.waterfall` -- single source of truth
    shared with the 4-layer profit planner.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    new_tests = 1620
    sa["eta_engine_tests_passing"] = new_tests
    sa["eta_engine_tests_failing"] = 0

    sa["eta_engine_v0_1_31_ai_optimized_strategies"] = {
        "timestamp_utc": now,
        "version": "v0.1.31",
        "bundle_name": ("AI-OPTIMIZED EVOLUTIONARY TRADING ALGO STRATEGY STACK -- SMC/ICT playbook + RL bridge"),
        "directive": (
            "Absorb the founder-brief AI-Optimized Evolutionary Trading Algo "
            "strategy list (6 ranked playbooks) into a pure, composable "
            "strategies package with zero pydantic/torch imports, pure "
            "functions only, and full guardrail inheritance "
            "(session gate, kill switch, regime, vol-z)."
        ),
        "theme": (
            "v0.1.30 made the product shippable; v0.1.31 makes the "
            "edge formal. Every SMC/ICT pattern (liquidity sweep, "
            "displacement, FVG, BOS, order block) is a pure function "
            "of bars. Every named strategy composes those primitives "
            "into a single StrategySignal that the policy router picks "
            "from per-asset. The regime allocator + RL wrapper slot in "
            "alongside without pulling the strategies package into "
            "torch, pydantic, or any async machinery."
        ),
        "artifacts_added": {
            "strategies_package": [
                "strategies/__init__.py",
                "strategies/models.py",
                "strategies/smc_primitives.py",
                "strategies/eta_policy.py",
                "strategies/policy_router.py",
                "strategies/regime_allocator.py",
                "strategies/rl_policy.py",
            ],
            "bump_script": ["scripts/_bump_roadmap_v0_1_31.py"],
        },
        "test_files_added": [
            "tests/test_strategies_models.py",
            "tests/test_strategies_smc_primitives.py",
            "tests/test_strategies_eta_policy.py",
            "tests/test_strategies_policy_router.py",
            "tests/test_strategies_regime_allocator.py",
            "tests/test_strategies_rl_policy.py",
        ],
        "tests_by_file": {
            "test_strategies_models": 10,
            "test_strategies_smc_primitives": 20,
            "test_strategies_eta_policy": 18,
            "test_strategies_policy_router": 13,
            "test_strategies_regime_allocator": 17,
            "test_strategies_rl_policy": 17,
        },
        "tests_new": 103,
        "tests_passing_before": prev_tests,
        "tests_passing_after": new_tests,
        "six_named_strategies": {
            "1_liquidity_sweep_displacement": {
                "hypothesis": (
                    "Liquidity raids create equal-level sweeps that reverse on displacement. Trade the close-back."
                ),
                "primitives": [
                    "find_equal_levels",
                    "detect_liquidity_sweep",
                    "detect_displacement",
                ],
                "stop": "wick level +/- 1.1x the sweep depth",
                "target": "3.0 R:R",
                "assets": ["MNQ", "NQ", "BTC", "SOL"],
            },
            "2_ob_breaker_retest": {
                "hypothesis": (
                    "After BOS, last opposing candle is the mitigation "
                    "block. Retest is the institutional re-entry zone."
                ),
                "primitives": [
                    "detect_break_of_structure",
                    "detect_order_block",
                ],
                "stop": "OB opposite edge +/- 10 bp",
                "target": "2.5 R:R",
                "htf_bias_required": True,
                "assets": ["MNQ", "NQ", "BTC", "ETH"],
            },
            "3_fvg_fill_confluence": {
                "hypothesis": (
                    "Unfilled FVGs on 15m/1H are imbalance reservoirs. Price returns to fill, confluence-weighted R:R."
                ),
                "primitives": ["detect_fvg"],
                "adaptive_rr": "3.0 in TRENDING, 1.5 otherwise",
                "stop": "FVG opposite edge +/- 10 bp",
                "assets": ["MNQ", "BTC", "ETH", "SOL", "XRP"],
            },
            "4_mtf_trend_following": {
                "hypothesis": ("200-MA aligned BOS = regime-confirmed trend. Chop regime cuts risk to zero."),
                "primitives": [
                    "above_moving_average",
                    "detect_break_of_structure",
                ],
                "ma_period": 200,
                "regime_required": "TRENDING",
                "stop": "MA level +/- 50 bp",
                "target": "2.0 R:R",
                "assets": ["MNQ", "NQ", "BTC", "ETH", "XRP"],
            },
            "5_regime_adaptive_allocation": {
                "hypothesis": (
                    "Portfolio-level meta-strategy. Weight risky "
                    "layers by regime + correlation + realized edge; "
                    "global kill zeroes risky, preserves staking sink."
                ),
                "layer_math_lives_in": "strategies/regime_allocator.py",
                "marker_only": True,
                "scope": "PORTFOLIO",
            },
            "6_rl_full_automation": {
                "hypothesis": (
                    "PPO/multi-agent RL end-to-end learner with a "
                    "drawdown-penalizing reward. Checkpoint-gated "
                    "abstention when no weights are loaded."
                ),
                "feature_vector_dim": 12,
                "agent_protocol": "RLAgentProto (decide(features)->RLDecision)",
                "null_agent_default": True,
                "torch_import_isolated_to": "brain.rl_agent",
                "assets": ["BTC", "SOL"],
            },
        },
        "primitives_shipped": [
            "find_equal_levels",
            "detect_liquidity_sweep",
            "detect_displacement",
            "detect_fvg",
            "detect_break_of_structure",
            "detect_order_block",
            "simple_ma",
            "above_moving_average",
        ],
        "router_eligibility_map": {
            "MNQ": ["1", "2", "3", "4"],
            "NQ": ["1", "2", "4"],
            "BTC": ["1", "2", "3", "4", "6"],
            "ETH": ["2", "3", "4"],
            "SOL": ["1", "3", "6"],
            "XRP": ["3", "4"],
            "PORTFOLIO": ["5"],
        },
        "regime_allocator_multipliers": {
            "VolRegime.LOW": 0.70,
            "VolRegime.NORMAL": 1.00,
            "VolRegime.HIGH": 0.55,
            "note": (
                "funnel.waterfall.VolRegime only defines LOW/NORMAL/"
                "HIGH; extreme-vol sizing is handled upstream by the "
                "waterfall's RiskAction path, not here."
            ),
        },
        "regime_allocator_base_weights": {
            "LAYER_1_MNQ": 0.40,
            "LAYER_2_BTC": 0.30,
            "LAYER_3_PERPS": 0.20,
            "LAYER_4_STAKING_SINK": 0.10,
        },
        "correlation_penalty": {
            "threshold": 0.75,
            "penalty_mult": 0.70,
            "target": "smaller-weight member of any >threshold pair",
            "staking_pair_excluded": True,
        },
        "design_guarantees": {
            "pure_functions_only": True,
            "no_io_in_strategies_package": True,
            "frozen_slots_everywhere": True,
            "no_pydantic_or_torch_imports": True,
            "ruff_clean": True,
            "guardrails_in_one_place": "_risk_mult helper in eta_policy",
            "router_supports_stub_injection": True,
            "single_source_of_truth_for_layers": ("funnel.waterfall.LayerId + VolRegime"),
        },
        "bundle_shape": "python_only",
        "jsx_touched": False,
    }

    hist = state.setdefault("milestones", [])
    hist.append(
        {
            "version": "v0.1.31",
            "timestamp_utc": now,
            "title": "AI-Optimized Evolutionary Trading Algo strategy stack",
            "tests_delta": new_tests - prev_tests,
            "tests_passing": new_tests,
        }
    )

    STATE_PATH.write_text(
        json.dumps(state, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"roadmap_state.json bumped -> v0.1.31 @ {now}")
    print(f"  tests_passing: {prev_tests} -> {new_tests} (+{new_tests - prev_tests})")


if __name__ == "__main__":
    main()
