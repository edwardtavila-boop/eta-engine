"""One-shot: bump roadmap_state.json to v0.1.28.

JARVIS LIVE SUPERVISION -- keep Jarvis optimized and observed end-to-end.

Context
-------
v0.1.27 shipped the "final revision" optimization pipeline. v0.1.28 is
the operational layer under it: a live supervisor + daemon so that
Jarvis, now the admin of the fleet (``brain.jarvis_admin``), cannot
silently go stale without the system alarming.

What v0.1.28 adds
-----------------
  * ``obs/jarvis_supervisor.py`` (new, ~440 lines)

    A health watchdog around ``JarvisContextEngine``:

      - ``JarvisHealth``        GREEN / YELLOW / RED tri-state
      - ``JarvisHealthReport``  pydantic snapshot (reasons, metrics,
                                last_tick_at, last_composite, binding,
                                memory_len)
      - ``SupervisorPolicy``    tunable thresholds:
            stale_after_s=300, dead_after_s=1800,
            dominance_run=10, flatline_threshold=0.05,
            flatline_run=10, dedup_prefix="jarvis_supervisor"
      - ``JarvisSupervisor``    wraps engine.tick() and exposes
            snapshot_health() + async alert() + async run()

    Detection rules (each sets a reason + escalates health):
      1. staleness: stale_s >= stale_after_s -> YELLOW;
                    stale_s >= dead_after_s  -> RED
      2. dominance: same ``binding_constraint`` for the last
                    ``dominance_run`` snapshots -> YELLOW
                    (weights need rebalancing)
      3. flatline:  composite <= ``flatline_threshold`` for
                    ``flatline_run`` snapshots -> YELLOW
                    (Jarvis may be blind)
      4. invalid:   composite is NaN, inf, or out of [0,1] -> RED

    Alerts use dedup_key = ``{prefix}::{health}::{reason_stem}`` so
    repeated alarms don't spam but distinct issues still page. Level
    mapping: RED=CRITICAL, YELLOW=WARN, GREEN=INFO.

  * ``scripts/jarvis_live.py`` (new, ~330 lines)

    Long-running daemon that keeps Jarvis TICKING live under
    supervision. Reads ``docs/premarket_inputs.json`` each tick
    (hot-reloadable), builds providers -> JarvisContextBuilder ->
    JarvisContextEngine -> JarvisSupervisor, runs at 60s cadence,
    fans out alerts through MultiAlerter (Telegram / Discord / Slack
    assembled from env, or dry-run if no transport configured).

    Per-tick outputs:
      - ``docs/jarvis_live_health.json``    (latest only)
      - ``docs/jarvis_live_log.jsonl``      (append-only history)

    SIGINT / SIGTERM wired to an asyncio.Event for graceful shutdown
    on POSIX; Windows falls back to KeyboardInterrupt.

  * ``tests/test_jarvis_supervisor.py`` (+28 tests, already in tree
    from the v0.1.27 test-count reconciliation).
  * ``tests/test_jarvis_live.py`` (new, +23 tests)

    Coverage: neutral inputs, load valid / missing / malformed /
    invalid-schema JSON, file-backed providers hot-reload semantics,
    health sink writes (latest + append log), alerter factory from
    env (none / telegram-only / all-three / partial-telegram-dropped),
    run_live bounded + stop-event + tick-exception swallow + close
    alerter + no-alert-on-green + alert-on-red, _build_default_supervisor
    end-to-end, and the main() CLI path.

Reconciliation
--------------
  * tests_passing: 1359 -> 1385 (+26). Of that delta, 23 come from the
    new ``tests/test_jarvis_live.py`` file; the remaining 3 are small
    engine-coverage additions that landed in-tree since the v0.1.27
    counter was set. Matches current ``pytest -q`` output precisely.
  * Ruff-clean on all four new/touched files; repo-wide legacy ruff
    debt untouched.
  * No phase-level status changes. P9_ROLLOUT remains at 85% (still
    blocked on $1000 Tradovate funding gate for API credential
    issuance).
  * overall_progress_pct: 99 (unchanged).
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
    new_tests = 1385
    sa["eta_engine_tests_passing"] = new_tests

    sa["eta_engine_v0_1_28_jarvis_live"] = {
        "timestamp_utc": now,
        "version": "v0.1.28",
        "bundle_name": "JARVIS LIVE SUPERVISION -- keep the admin ticking",
        "directive": "make sure jarvis stays optimized live",
        "theme": (
            "Jarvis is the admin of the fleet; every subsystem calls "
            "request_approval(). If it stops ticking, every gate falls "
            "through to stale policy silently. This bundle adds a "
            "supervisor + live daemon so drift, flatline, dominance, "
            "and staleness all page immediately."
        ),
        "modules": [
            "eta_engine/obs/jarvis_supervisor.py",
            "eta_engine/scripts/jarvis_live.py",
        ],
        "supervisor": {
            "public_api": [
                "JarvisHealth",
                "JarvisHealthReport",
                "SupervisorPolicy",
                "JarvisSupervisor",
            ],
            "health_ladder": "GREEN -> YELLOW -> RED",
            "policy_defaults": {
                "stale_after_s": 300.0,
                "dead_after_s": 1800.0,
                "dominance_run": 10,
                "flatline_threshold": 0.05,
                "flatline_run": 10,
                "dedup_prefix": "jarvis_supervisor",
            },
            "detection_rules": [
                "staleness: last_tick older than stale_after_s -> YELLOW; older than dead_after_s -> RED",
                "dominance: same binding_constraint for last N snapshots -> YELLOW (weights need rebalancing)",
                "flatline: composite <= flatline_threshold for N consecutive snapshots -> YELLOW (Jarvis may be blind)",
                "invalid: composite is NaN / inf / outside [0,1] -> RED",
            ],
            "alert_level_map": {
                "RED": "CRITICAL",
                "YELLOW": "WARN",
                "GREEN": "INFO",
            },
            "dedup_key_format": (
                "'{prefix}::{health}::{reason_stem}'  (reason_stem = "
                "first ':'-delimited segment of primary reason, keeps "
                "repeated alarms silent but distinct issues paging)"
            ),
        },
        "daemon": {
            "entrypoint": "python -m eta_engine.scripts.jarvis_live",
            "inputs_file": "docs/premarket_inputs.json (hot-reloaded)",
            "outputs": [
                "docs/jarvis_live_health.json  (latest snapshot)",
                "docs/jarvis_live_log.jsonl    (append-only history)",
            ],
            "cadence_default_s": 60.0,
            "alerter_env_vars": [
                "TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID -> TelegramAlerter",
                "DISCORD_WEBHOOK_URL -> DiscordAlerter",
                "SLACK_WEBHOOK_URL -> SlackAlerter",
                "(none) -> dry-run: health outputs only, no paging",
            ],
            "signal_handling": (
                "SIGINT/SIGTERM wired to asyncio.Event via "
                "loop.add_signal_handler; Windows falls back to "
                "KeyboardInterrupt via contextlib.suppress."
            ),
            "cli_args": [
                "--inputs PATH (default docs/premarket_inputs.json)",
                "--out-dir PATH (default docs/)",
                "--interval SECONDS (default 60.0)",
                "--max-ticks N (default: run forever)",
                "--log-level LEVEL (default INFO)",
            ],
        },
        "new_test_files": [
            "tests/test_jarvis_supervisor.py (28)",
            "tests/test_jarvis_live.py (23)",
        ],
        "jarvis_live_test_coverage": [
            "neutral_inputs validity",
            "load_inputs_file: missing / valid / malformed / invalid schema",
            "file-backed providers hot-reload on mtime change",
            "write_health: latest + append-log semantics + missing dir",
            "build_alerter_from_env: no-env, telegram-only, all-three, partial-telegram-dropped",
            "run_live: rejects nonpositive interval",
            "run_live: bounded by max_ticks",
            "run_live: stop_event exits loop promptly",
            "run_live: tick exception does not crash loop",
            "run_live: alerter.close() called on exit",
            "run_live: no alert on GREEN",
            "run_live: alert sent on RED (invalid composite)",
            "_build_default_supervisor: tickable against missing inputs",
            "_build_default_supervisor: default policy",
            "main(): CLI path with --max-ticks=1 writes health",
        ],
        "tests_new": new_tests - prev_tests,
        "tests_passing_before": prev_tests,
        "tests_passing_after": new_tests,
        "ruff_clean_on": [
            "obs/jarvis_supervisor.py",
            "scripts/jarvis_live.py",
            "tests/test_jarvis_supervisor.py",
            "tests/test_jarvis_live.py",
        ],
        "external_gate": (
            "P9_ROLLOUT remains at 85% pending $1000 Tradovate funded "
            "balance -- required to issue API credentials (app_id, "
            "secret, client_id). Supervisor + daemon are ready to run "
            "against live data the moment that gate clears."
        ),
    }

    state["overall_progress_pct"] = state.get("overall_progress_pct", 99)

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"bumped roadmap_state.json to v0.1.28 at {now}")
    print(f"  tests_passing: {prev_tests} -> {new_tests} ({new_tests - prev_tests:+d})")
    print("  shipped: obs/jarvis_supervisor.py + scripts/jarvis_live.py")
    print("  directive satisfied: 'make sure jarvis stays optimized live'")
    print("  P9_ROLLOUT still funding-blocked; everything else green.")


if __name__ == "__main__":
    main()
