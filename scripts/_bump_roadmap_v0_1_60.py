"""One-shot: bump roadmap_state.json to v0.1.60.

APEX EVAL PREFLIGHT-HARDENING  --  surfaces the two v0.1.59 closure
invariants (R2 tick-cadence, R3 audit-log fsync) at preflight so the
operator can validate the config BEFORE live startup instead of
discovering the mis-config at the first tick.

Why this bundle exists
----------------------
v0.1.59 closed R2/R3 in runtime code -- the validator raises at live
boot, the audit log aborts on fsync failure. Both are correct-by-
construction, but both fail late: the operator learns about an under-
sized cushion by watching ``run_eta_live.py`` crash on startup, and
learns about a read-only ``state/`` volume by watching the first
freeze event crash the tracker with an untidy traceback.

``scripts.preflight`` is the designated single source of truth for
"can we go live NOW." It already surfaces secrets, venues, blackout
windows, the Firm verdict, and the Telegram alert path. It did not
surface the two closure invariants. v0.1.60 fixes that.

What ships
----------
preflight.check_tick_cadence  (R2 surface)
  * Reads ``configs/kill_switch.yaml``, extracts
    ``tier_a.apex_eval_preemptive.cushion_usd`` (default 500.0 when
    unset), and calls ``core.kill_switch_runtime.validate_apex_tick_cadence``
    with ``tick_interval_s=1.0`` (canonical live default) and
    ``live=True``. Failures propagate the exception text -- operator
    sees exactly which direction to move (raise cushion vs drop tick).

preflight.check_audit_log_readiness  (R3 surface)
  * Creates ``state/`` if missing, writes a tempfile, fsyncs, unlinks.
    Exercises the exact code path the ``TrailingDDAuditLog`` takes on
    every append. On Windows + OneDrive-synced volumes this is a real
    failure mode (reparse points, CloudFiles reparse, network-share
    fsync no-op surprises).

Coverage delta
--------------
  * ``tests/test_preflight.py`` -- 12 new tests:
      - 6 for ``check_tick_cadence``: missing yaml, broken yaml,
        sufficient cushion, too-tight cushion, empty-yaml default,
        negative cushion (invalid input).
      - 4 for ``check_audit_log_readiness``: happy path, missing-dir
        creation, file-collision at dir path, fsync-raises.
      - 2 for ``_run_async``: tick_cadence-red, audit_log-red, plus
        renamed ``test_run_async_prints_all_seven_check_rows`` to
        match the new 7-check shape.
  * Also refactored ``_run_async`` tests to use a shared
    ``_stub_all_checks_green`` helper so adding check #8 is a
    one-line diff instead of an N-file sweep.

Why only these two checks
-------------------------
R1 (broker-equity drift) is scaffolded-only in v0.1.59; wiring the
reconciler into preflight is blocked on broker adapter ``get_balance``
implementations, which are venue-specific and v0.2.x scope. R4 (CME
calendar session-day) is an in-process invariant -- there's nothing
external to probe at preflight time.

Design notes
------------
  * ``check_tick_cadence`` runs the validator with ``live=True``
    unconditionally. Rationale: preflight is the "about to go live"
    gate; running it in paper mode would mask the real question,
    which is "would this config pass the live-mode invariant." If
    the operator is explicitly running a paper session they don't
    need to run preflight at all.
  * Default cushion fallback is 500.0 (the pre-v0.1.59 config value).
    This means an empty/missing ``tier_a.apex_eval_preemptive`` block
    fails the check with the same error message the live runtime
    would produce, instead of silently falling back to a value that
    happens to pass.
  * ``check_audit_log_readiness`` uses the *tracker's* default state
    directory (``<repo>/state``), not a caller-supplied path. The
    tracker initialiser is what picks the default; reproducing that
    default here keeps the two in sync without a protocol.
  * Tempfile cleanup uses ``finally`` + ``missing_ok=True`` so a
    partial fsync failure doesn't leave litter in ``state/``.

Expected state changes
----------------------
  * ``roadmap_state.json`` version bumped to v0.1.60.
  * ``eta_engine_tests_passing`` reflects the full regression
    result (3881 passed, 3 skipped as of bump time).
  * New key ``eta_engine_v0_1_60_preflight_hardening`` with the
    full bundle ledger.
  * New milestone appended.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.60"
NEW_TESTS_ABS = 3881
PREFLIGHT_TESTS_ADDED = 12


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_60_preflight_hardening"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "APEX EVAL PREFLIGHT-HARDENING -- surface R2 tick-cadence "
            "and R3 audit-log fsync invariants at preflight so the "
            "operator validates the config BEFORE live startup "
            "instead of discovering the mis-config at the first tick."
        ),
        "theme": (
            "v0.1.59 closed R2/R3 in runtime code. Both are correct-"
            "by-construction but both fail LATE -- undersized cushion "
            "crashes run_eta_live at startup, read-only state volume "
            "crashes TrailingDDAuditLog at the first freeze event. "
            "scripts.preflight is the designated 'can we go live NOW' "
            "gate; it did not surface these two invariants. v0.1.60 "
            "wires them in so the operator sees red BEFORE flipping "
            "the switch."
        ),
        "operator_directive_quote": "continue all.",
        "modules_modified": [
            "eta_engine/scripts/preflight.py "
            "(two new checks: check_tick_cadence, "
            "check_audit_log_readiness; wired into _run_async "
            "results list; F841 unused-cfg fix in "
            "_check_venues_async)",
        ],
        "tests_added": [
            "eta_engine/tests/test_preflight.py "
            "(12 new tests across new functions plus helper-refactor "
            "of _run_async tests via _stub_all_checks_green. "
            "6 for check_tick_cadence, 4 for check_audit_log_readiness, "
            "2 for _run_async red-branch coverage, + "
            "test_run_async_prints_all_seven_check_rows replaces the "
            "old _five_check variant.)",
        ],
        "closure_surface_map": {
            "R2_tick_cadence": {
                "runtime_enforcer": (
                    "core/kill_switch_runtime.py::validate_apex_tick_cadence (live=True raises ApexTickCadenceError)"
                ),
                "preflight_surface": (
                    "scripts/preflight.py::check_tick_cadence "
                    "(reads configs/kill_switch.yaml cushion_usd, "
                    "invokes validator with tick=1.0s and live=True)"
                ),
                "failure_mode_without_preflight": (
                    "run_eta_live.load_runtime_config raises on "
                    "boot -- operator sees traceback with no "
                    "remediation context."
                ),
                "failure_mode_with_preflight": (
                    "preflight row prints red with validator's "
                    "exact remediation text: 'drop tick_interval_s, "
                    "or raise cushion_usd in configs/kill_switch.yaml "
                    "tier_a.apex_eval_preemptive'."
                ),
            },
            "R3_audit_log_fsync": {
                "runtime_enforcer": (
                    "core/trailing_dd_tracker.py::TrailingDDAuditLog.append (os.fsync per append; raises on OSError)"
                ),
                "preflight_surface": (
                    "scripts/preflight.py::check_audit_log_readiness "
                    "(mkdir state/, tempfile write, flush+fsync, "
                    "unlink)"
                ),
                "failure_mode_without_preflight": (
                    "First freeze event crashes the tracker "
                    "mid-runtime -- Apex floor enforcement stops; "
                    "no audit trail of WHY."
                ),
                "failure_mode_with_preflight": (
                    "preflight row prints red with OSError text -- "
                    "operator moves state/ to a non-OneDrive / "
                    "non-reparse-point volume before boot."
                ),
            },
        },
        "design_choices": {
            "live_mode_validator_at_preflight": (
                "check_tick_cadence calls the validator with "
                "live=True unconditionally. Rationale: preflight IS "
                "the 'about to go live' gate. Running it in paper "
                "mode would mask the real question -- 'would this "
                "config pass the live-mode invariant'. Operators "
                "doing a pure paper session don't run preflight."
            ),
            "default_cushion_fallback_500": (
                "If tier_a.apex_eval_preemptive.cushion_usd is "
                "missing/empty, fall back to 500.0 (the pre-v0.1.59 "
                "config value). This means an empty block fails the "
                "check with the same error the live runtime would "
                "produce -- no silent fallback to a passing value."
            ),
            "audit_dir_uses_tracker_default": (
                "check_audit_log_readiness probes <repo>/state, the "
                "tracker's default. Rationale: the tracker "
                "constructor picks the default; reproducing it here "
                "keeps the two in lockstep without introducing a "
                "shared-constants protocol. If a future release "
                "changes the default, the constant here (and its "
                "matching test monkeypatch) move in one diff."
            ),
            "tempfile_cleanup_in_finally": (
                "The happy path unlinks the tempfile before return; "
                "the finally block re-attempts with missing_ok=True "
                "so a partial fsync failure doesn't leave litter in "
                "state/. Belt and braces -- if the first unlink "
                "succeeded, the second is a no-op."
            ),
            "run_async_test_helper_refactor": (
                "_stub_all_checks_green centralises the green-stub "
                "setup that used to be duplicated across 4 test "
                "functions. Adding check #8 is now a one-line diff "
                "in the helper instead of an N-file sweep."
            ),
        },
        "scope_exclusions": {
            "R1_broker_equity_drift": (
                "Scaffolded in v0.1.59 but not surfaced at preflight. "
                "Wiring the reconciler here is blocked on broker "
                "adapter get_balance implementations (venue-specific, "
                "v0.2.x scope). When wired, a 5th closure-surface "
                "row lands here."
            ),
            "R4_session_day_calendar": (
                "In-process invariant -- nothing external to probe "
                "at preflight time. The calendar is tested offline "
                "and fails closed (unknown dates are non-trading "
                "days), so there is no live-boot risk."
            ),
        },
        "preflight_output_shape": {
            "check_count_before": 5,
            "check_count_after": 7,
            "new_checks": ["tick_cadence", "audit_log"],
            "ordering_rationale": (
                "tick_cadence and audit_log placed between "
                "firm_verdict and telegram so the ordering goes "
                "'can we connect -> is the market open -> are we "
                "risk-cleared -> is the config sane -> is the IO "
                "path sane -> can we alert'."
            ),
        },
        "tests_passing_before": prev_tests,
        "tests_passing_after": NEW_TESTS_ABS,
        "tests_delta_full_regression": NEW_TESTS_ABS - prev_tests,
        "tests_added_in_this_bundle": PREFLIGHT_TESTS_ADDED,
        "tests_delta_residual_from_other_modules": (NEW_TESTS_ABS - prev_tests - PREFLIGHT_TESTS_ADDED),
        "residual_delta_note": (
            "Full-regression delta > bundle-scope delta because "
            "several untracked test files in tests/ (added during "
            "earlier BTC / avengers / broker-paper work, not yet "
            "committed) now run under the full pytest collection. "
            "They are NOT part of v0.1.60 scope and will be "
            "attributed to their own future bundles. v0.1.60's "
            "specific new tests are the 12 in test_preflight.py."
        ),
        "ruff_green_new_code": True,
        "pre_existing_lint_debt": (
            "The full-repo ruff run reports 457 errors across "
            "other modules -- unchanged from v0.1.59. The two "
            "files touched in this bundle (scripts/preflight.py, "
            "tests/test_preflight.py) are both ruff-green."
        ),
    }

    milestones = state.setdefault("milestones", [])
    if isinstance(milestones, list):
        milestones.append(
            {
                "version": VERSION,
                "timestamp_utc": now,
                "title": (
                    "Preflight-hardening on v0.1.59 closures. "
                    "R2 tick-cadence and R3 audit-log fsync "
                    "invariants now surfaced at preflight so the "
                    "operator validates the config BEFORE live "
                    "startup instead of discovering the mis-config "
                    "at the first tick. +12 preflight tests. "
                    "Preflight now covers 7 checks (was 5)."
                ),
                "tests_delta": PREFLIGHT_TESTS_ADDED,
                "tests_passing": NEW_TESTS_ABS,
            },
        )

    state["overall_progress_pct"] = state.get("overall_progress_pct", 100)

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"bumped roadmap_state.json to {VERSION} at {now}")
    print(
        f"  tests_passing: {prev_tests} -> {NEW_TESTS_ABS} "
        f"({NEW_TESTS_ABS - prev_tests:+d} full, "
        f"{PREFLIGHT_TESTS_ADDED:+d} in-scope)",
    )


if __name__ == "__main__":
    main()
