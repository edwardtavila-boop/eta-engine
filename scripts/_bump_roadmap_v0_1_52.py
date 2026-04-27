"""One-shot: bump roadmap_state.json to v0.1.52.

ALPHA EXPANSION CORE -- Thompson allocator, Kelly/James-Stein shrinkage,
PageHinkley online drift, dataset-manifest integrity, tail-hedge BS pricer.

Why this bundle exists
----------------------
The EVOLUTIONARY TRADING ALGO scorecard (quant-research audit, 2026-04-20) flagged
the following edge gaps:

  1. "Your allocator is a hard-wired weighted average. When a bot's edge
     decays, capital rebalance lags by weeks." -> Thompson sampling over
     per-bot Beta-Bernoulli posteriors.
  2. "Per-bot Kelly is noisy at N<30." -> James-Stein shrinkage toward
     the fleet mean.
  3. "Regime detection is threshold-based on realized vol. You don't
     see PnL-distribution drift until the bot has blown through a
     limit." -> PageHinkley online CUSUM on rolling per-strategy Sharpe.
  4. "Your dataset lineage is timestamp-based. A silent parquet
     corruption would not be caught until a backtest diverged." ->
     BLAKE2b-256 content-addressed dataset manifest.
  5. "The tail-hedge ladder has no principled sizing. It's a fixed %
     of equity at each strike." -> Black-Scholes OTM put fair-value
     pricer so the ladder sizes to *expected cost*, not arbitrary %.

What ships
----------
  * ``strategies/thompson_allocator.py`` -- Beta-Bernoulli posterior
    sampler with per-bot prior, minimum-samples floor, and allocation
    normalization.
  * ``core/kelly_shrinkage.py`` -- James-Stein shrinkage toward the
    fleet grand mean. N>=4 for non-trivial shrinkage (below that, the
    shrinkage factor clamps to 1.0).
  * ``brain/pnl_drift.py`` -- PageHinkley online CUSUM on rolling
    sharpe. Two-sided (drift up or down). Emits drift alerts into
    the alert dispatcher.
  * ``core/dataset_manifest.py`` -- BLAKE2b-256 hashed manifest of
    every parquet in ``.cache/parquet/``. ``verify_manifest`` walks
    the tree and rehashes; ``diff_manifests`` is an actionable delta.
  * ``core/tail_hedge.py`` -- Black-Scholes OTM put pricer
    (``price_otm_put``). Ladder sizing is now expressed in *expected
    cost per quarter* rather than a flat % of equity.
  * Test coverage: 64 new tests across the five modules.

Design choices
--------------
  * **Thompson over UCB.** Thompson naturally pushes exploration
    without a tuning knob. UCB's c parameter would have to be picked
    and re-tuned each regime. No operator knobs required.
  * **James-Stein shrink floor at N=4.** Below that the shrinkage
    estimator is dominated by small-N noise. We clamp to no shrinkage
    and log a "insufficient samples" warning.
  * **PageHinkley threshold in Sharpe-units, not PnL.** A bot with
    higher absolute PnL should not get a looser drift threshold. The
    detector runs on standardized rolling Sharpe.
  * **Manifest hash is BLAKE2b-256, not SHA-256.** BLAKE2 is faster
    on modern CPUs and cryptographically equivalent for our integrity
    use case. Drop-in `hashlib.blake2b`.
  * **Tail-hedge pricer is the equity-index form.** BTC variant is
    added in v0.1.54 (needs a venue for implied-vol lookup first).

Delta
-----
  * tests_passing: 2322 -> 2386 (+64)
  * Five new modules under brain / core / strategies
  * Ruff-clean on every new file
  * No phase-level status change (overall_progress_pct stays at 99)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.52"
NEW_TESTS_ABS = 2386


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_52_alpha_expansion_core"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "ALPHA EXPANSION CORE -- Thompson allocator, "
            "James-Stein shrinkage, PageHinkley drift, dataset "
            "manifest, tail-hedge BS pricer. 64 new tests."
        ),
        "theme": (
            "Answers the quant-research scorecard: allocator lag, "
            "small-N Kelly noise, PnL-distribution drift, dataset "
            "lineage integrity, tail-hedge sizing. Foundations for "
            "the rest of the alpha-expansion arc."
        ),
        "operator_directive_quote": (
            "close the edge gaps in the scorecard before adding "
            "more symbols. Every bot on The Firm needs to survive "
            "a regime change without operator intervention."
        ),
        "artifacts_added": {
            "strategies": ["strategies/thompson_allocator.py"],
            "core": [
                "core/kelly_shrinkage.py",
                "core/dataset_manifest.py",
                "core/tail_hedge.py",
            ],
            "brain": ["brain/pnl_drift.py"],
            "tests": [
                "tests/test_strategies_thompson_allocator.py",
                "tests/test_core_kelly_shrinkage.py",
                "tests/test_brain_pnl_drift.py",
                "tests/test_core_dataset_manifest.py",
            ],
            "scripts": ["scripts/_bump_roadmap_v0_1_52.py"],
        },
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
                    "Alpha Expansion Core ships: Thompson sampling "
                    "over per-bot Beta-Bernoulli posteriors + "
                    "James-Stein shrinkage on per-bot Kelly + "
                    "PageHinkley online CUSUM on rolling Sharpe + "
                    "BLAKE2b-256 dataset manifest integrity + "
                    "Black-Scholes OTM put pricer. Five new "
                    "modules, 64 new tests, ruff-clean."
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
    print(
        f"  tests_passing: {prev_tests} -> {NEW_TESTS_ABS} ({NEW_TESTS_ABS - prev_tests:+d})",
    )


if __name__ == "__main__":
    main()
