"""One-shot: bump roadmap_state.json to v0.1.20.

Closes out P10_AI (80% -> 100%). One remaining task lands:

  * gan_synthetic -- brain/synthetic.py (regime-conditioned stochastic
                     OHLCV generator) with 31 tests. Stdlib-only
                     calibrated simulator (not a PyTorch GAN). Per-regime
                     profiles (TRENDING / RANGING / HIGH_VOL / LOW_VOL /
                     CRISIS / TRANSITION), AR(1) vol clustering, Gaussian
                     + Student-t tail mixture, OHLCV invariants enforced.
                     Augment mode interleaves synthetic bars with real
                     bars for scarce-regime backtesting.

Adds 31 tests (865 -> 896).

Rationale for parametric over neural:
  GAN-on-price is famously unstable; the failure mode is collapsing to
  look-like-real distributions that lack the CRISIS / HIGH_VOL tail
  structure we actually need. Parametric regime-conditioned simulation
  is more controllable, deterministic under seed, and more consistent
  with the project's stdlib-first posture (brain.regime is a decision
  tree, brain.rl_agent is a seeded baseline -- brain.synthetic follows
  the same shape).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"


def _find_task(phase: dict, task_id: str) -> dict:
    for t in phase["tasks"]:
        if t.get("id") == task_id:
            return t
    raise KeyError(f"task {task_id} not found in phase {phase.get('id')}")


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    sa["eta_engine_tests_passing"] = 896

    by_id = {p["id"]: p for p in state["phases"]}
    p10 = by_id["P10_AI"]
    p10["progress_pct"] = 100
    p10["status"] = "done"

    gan = _find_task(p10, "gan_synthetic")
    gan["status"] = "done"
    gan["note"] = (
        "brain/synthetic.py + 31 tests. Stdlib-only regime-conditioned "
        "stochastic OHLCV generator (chose calibrated simulator over "
        "unstable GAN; matches brain.regime / brain.rl_agent stdlib "
        "posture). Per-regime RegimeProfile (mu, sigma, vol_persistence, "
        "tail_weight, tail_df, intrabar_range_mult, base_volume, "
        "volume_return_sensitivity). Six PROFILES cover every RegimeType. "
        "AR(1) vol clustering, Gaussian+Student-t tail mixture for CRISIS, "
        "intrabar wicks enforce H>=max(O,C) and L<=min(O,C). "
        "SyntheticBarGenerator supports next_bar / generate_series / "
        "augment (interleaves synthetic bars after each real bar). "
        "Deterministic under seed. fit_profile_from_bars() calibrates "
        "mu/sigma/rho from a real series."
    )

    # New AI-layer shared artifact summary
    sa["eta_engine_p10_ai"] = {
        "timestamp_utc": now,
        "completed_tasks_final": [
            "regime_model",
            "ppo_sac_agent",
            "anomaly_drift",
            "multi_agent_orch",
            "gan_synthetic",
        ],
        "new_module": "eta_engine/brain/synthetic.py",
        "new_test_file": "tests/test_synthetic.py (31 tests)",
        "tests_new": 31,
        "profiles": {
            "TRENDING": "mu=+2e-4, sigma=0.25%, rho=0.20, tail_w=0.05",
            "RANGING": "mu=0, sigma=0.12%, rho=0.05, tail_w=0.02",
            "HIGH_VOL": "mu=0, sigma=0.60%, rho=0.55, tail_w=0.20",
            "LOW_VOL": "mu=0, sigma=0.06%, rho=0, tail_w=0",
            "CRISIS": "mu=-4e-4, sigma=1.20%, rho=0.75, tail_w=0.45, tail_df=4",
            "TRANSITION": "mu=0, sigma=0.30%, rho=0.30, tail_w=0.08",
        },
        "notes": (
            "Stdlib-only (random.Random + math). No PyTorch / numpy / "
            "scipy. Chose parametric simulator over neural GAN because "
            "GAN-on-price collapses to modal distributions and misses "
            "the exact tail regimes (CRISIS / HIGH_VOL) the augmentation "
            "is meant to supply. fit_profile_from_bars() provides a path "
            "to auto-calibrate when enough history is available."
        ),
    }

    # P10 done. Next-lowest is P9_ROLLOUT at 85% but live_tiny_size is
    # funding-blocked per operator (Tradovate $1000 requirement). Move
    # overall pct to 99 (already there) -- nothing else bumps.
    # Keep overall_progress_pct steady; it's a weighted phase average.
    state["overall_progress_pct"] = 99

    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"bumped roadmap_state.json to v0.1.20 at {now}")
    print("  tests_passing: 865 -> 896 (+31)")
    print("  P10_AI: 80% -> 100% (gan_synthetic -> done)")
    print("  overall_progress_pct: 99")


if __name__ == "__main__":
    main()
