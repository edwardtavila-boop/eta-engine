"""One-shot: bump roadmap_state.json to v0.1.16.

Closes four P12_POLISH in_progress/pending tasks by:
  * Building core/parameter_sweep.py (generic grid + walk-forward + Pareto)
  * Building core/master_tweaks.py (risk-tagged tweak proposal + apply)
  * Adding 23 tests for scripts/preflight.py
  * Rewiring tests/harness_open.py from stub to real logic backed by the two
    new core modules

Closes:
  P12_POLISH.open_testing       -> done  (18 new tests + real logic)
  P12_POLISH.parameter_sweep    -> done  (38 new tests + new core module)
  P12_POLISH.master_tweaks      -> done  (25 new tests + new core module)
  P12_POLISH.go_live_checklist  -> done  (23 new tests for preflight.py)

Bumps tests_passing 648 -> 752, P12_POLISH 20% -> 95%, overall 97 -> 98.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"


def _set_task_status(phase: dict, task_id: str, status: str, note: str | None = None) -> None:
    for t in phase["tasks"]:
        if t.get("id") == task_id:
            t["status"] = status
            if note:
                t["note"] = note
            return
    raise KeyError(f"task {task_id} not found in phase {phase.get('id')}")


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    # Overall counters
    state["last_updated"] = now
    state["last_updated_utc"] = now
    state["overall_progress_pct"] = 98

    # Shared artifacts
    sa = state["shared_artifacts"]
    sa["eta_engine_tests_passing"] = 752
    sa["eta_engine_tests_failing"] = 0

    sa["eta_engine_p12_polish_closed"] = {
        "timestamp_utc": now,
        "new_modules": [
            "eta_engine/core/parameter_sweep.py",
            "eta_engine/core/master_tweaks.py",
        ],
        "rewired_modules": [
            "eta_engine/tests/harness_open.py",
        ],
        "new_test_files": [
            "tests/test_parameter_sweep.py (38 tests)",
            "tests/test_master_tweaks.py (25 tests)",
            "tests/test_preflight.py (23 tests)",
            "tests/test_harness_open.py (18 tests)",
        ],
        "tests_new": 104,
        "phase_gaps_closed": [
            "P12_POLISH.open_testing",
            "P12_POLISH.parameter_sweep",
            "P12_POLISH.master_tweaks",
            "P12_POLISH.go_live_checklist",
        ],
        "api_surface_covered": {
            "SweepGrid.iter_combinations": "Cartesian-product iteration over SweepParam axes; cardinality()",
            "run_sweep": "grid + scorer -> [SweepCell] with per-cell gate_pass + walk-forward stability (pstdev)",
            "Gate.evaluate": "expectancy/dd/trades/win_rate thresholds; defaults match paper_phase_requirements",
            "rank_cells": "deterministic 5-key sort (gate, exp desc, dd asc, stab asc, trades desc)",
            "pick_winner": "top-ranked passer; falls back to closest-to-passing",
            "pareto_frontier": "non-dominated on (exp up, dd down, stab down); O(n^2) but exact",
            "walk_forward_windows": "(train_start, train_end, test_start, test_end) sliding windows",
            "classify_risk": "SAFE/MODERATE/AGGRESSIVE from max relative delta across keys (10%/35% thresholds)",
            "propose_tweaks": "per-bot SweepCell -> Tweak with risk tag + reason + expected metrics",
            "apply_tweak": "gate_pass required + aggressive policy gate + per-param max_relative_change cap",
            "harness_open.run_parameter_sweep": "param_ranges + scorer -> ranked dicts (best-first)",
            "harness_open.run_forward_test_comparator": "two scorers, signed edge_r + edge_pct",
            "harness_open.run_regime_slice_evaluator": "scorer + bars + regimes -> per-regime metrics",
            "harness_open.summarize_sweep": "n_cells / n_pass / best / median / winner",
            "preflight.check_secrets": "REQUIRED_KEYS validation via SecretsManager",
            "preflight.check_venues": "config.json parse + graceful fallback",
            "preflight.check_blackout_window": "session_filter.is_news_blackout wrapper",
            "preflight.check_firm_verdict": "KILL/NO_GO sentinel check (case-insensitive)",
            "preflight.check_telegram": "async round-trip via TelegramAlerter",
            "preflight._run_async": "5-check orchestration; exit 0 = GO, 1 = NO-GO",
        },
        "notes": (
            "core/parameter_sweep.py is a pure-python engine: no backtest dependency, plug a scorer in. "
            "core/master_tweaks.py is config-plane only -- does not touch live positions. "
            "harness_open.py went from TODO-stub to real wiring over parameter_sweep. "
            "preflight.py was already implemented; added 23 monkeypatch-based tests to lock its contract."
        ),
    }

    # Phases: close P12_POLISH tasks
    by_id = {p["id"]: p for p in state["phases"]}

    _set_task_status(
        by_id["P12_POLISH"],
        "open_testing",
        "done",
        (
            "tests/harness_open.py rewired over core.parameter_sweep + "
            "18 dedicated tests (test_harness_open.py) covering parameter "
            "sweep, Pareto frontier, forward-test comparator, regime slicing, "
            "and end-to-end bot-shaped flow"
        ),
    )
    _set_task_status(
        by_id["P12_POLISH"],
        "parameter_sweep",
        "done",
        (
            "core/parameter_sweep.py + tests/test_parameter_sweep.py -- "
            "generic Cartesian-product grid with Gate thresholds, 5-key "
            "deterministic rank, Pareto frontier on (expectancy, dd, "
            "stability), walk-forward window generator; 38 dedicated tests "
            "covering axis validation, gate eval, ranking tie-breaks, "
            "Pareto correctness, and sliding windows"
        ),
    )
    _set_task_status(
        by_id["P12_POLISH"],
        "master_tweaks",
        "done",
        (
            "core/master_tweaks.py + tests/test_master_tweaks.py -- "
            "risk-classified (SAFE/MODERATE/AGGRESSIVE) tweak proposals "
            "with TweakPolicy gates (require_gate_pass, allow_aggressive, "
            "max_relative_change per-param cap); 25 dedicated tests "
            "covering risk classification thresholds, bulk apply, and "
            "per-param rejection"
        ),
    )
    _set_task_status(
        by_id["P12_POLISH"],
        "go_live_checklist",
        "done",
        (
            "scripts/preflight.py + tests/test_preflight.py -- 5-check "
            "preflight gate (secrets / venues / blackout / firm verdict / "
            "telegram); 23 dedicated tests covering per-check happy path + "
            "failure modes + full async orchestration via monkeypatch"
        ),
    )
    by_id["P12_POLISH"]["progress_pct"] = 95

    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"bumped roadmap_state.json to v0.1.16 at {now}")
    print("  P12_POLISH: 20% -> 95%")
    print("  tests_passing: 648 -> 752")
    print("  overall: 97% -> 98%")


if __name__ == "__main__":
    main()
