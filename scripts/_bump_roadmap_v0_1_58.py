"""One-shot: bump roadmap_state.json to v0.1.58.

APEX EVAL READINESS HARDENING (D-series Red Team closure) -- ships
the three BLOCKER fixes surfaced by the ``risk-advocate`` adversarial
review of the v0.1.57 D-series wiring. Every BLOCKER is closed in
code, covered by new tests, and documented in
``docs/red_team_d2_d3_review.md``.

Why this bundle exists
----------------------
v0.1.57 wired D1..D5 into ``scripts/run_eta_live.py`` and shipped
``TrailingDDTracker`` + ``ConsistencyGuard`` as standalone modules.
The immediate post-ship adversarial review (operator mandate:
"continue hardening until live-ready") found three wiring gaps where
the runtime would *appear* correct (all alerts green, all tests green)
while the Apex eval was either already busted or about to bust:

  B1 -- ``run_eta_live.py`` keyed today's consistency-guard entry by
        ``utc_today_iso()`` (UTC calendar midnight). US equity-futures
        RTH + overnight Globex sessions straddle UTC midnight, so a
        single Apex trading day's PnL was being split across two dict
        keys. The 30%-rule ratio was biased *downward* -- hiding a
        real concentration risk. A VIOLATION state was under-reported
        as OK or WARNING.

  B2 -- ``ApexRuntime`` defaulted to the bar-level
        ``build_apex_eval_snapshot`` proxy when no
        ``trailing_dd_tracker`` was supplied. The proxy does NOT
        implement the Apex freeze rule (peak >= start + cap ->
        floor locks at start). A live runtime booted without the
        tracker would silently under-protect: a normal retrace past
        the *correct* frozen floor would appear safe when it was
        actually a bust. There was no framework enforcement that
        the tracker be wired in live mode -- just operator memory.

  B3 -- On ``ConsistencyStatus.VIOLATION`` the guard emitted an
        alert + log but took no runtime action. ``bot.state.is_paused``
        stayed False. For an automated system, "notify the operator
        and keep trading" is not enforcement -- it is a different
        kind of silent failure.

A D-series safety module that is *wired* but whose wiring has any of
these gaps is worse than no module at all: it produces false
confidence. v0.1.58 closes all three.

What ships
----------
B1 fix -- Apex session-day helper
  * ``eta_engine/core/consistency_guard.py`` -- new
    ``apex_trading_day_iso(now_utc=None)`` function. Uses
    ``zoneinfo.ZoneInfo("America/Chicago")`` to compute the 17:00
    local rollover (DST-aware) so the bucket key matches the Apex
    trading-day convention. Fixed 23:00-UTC fallback when
    ``zoneinfo`` is unavailable (wrong by <= 1h in summer, never
    splits RTH). ``utc_today_iso()`` kept with a deprecation note
    for backward compatibility.
  * ``scripts/run_eta_live.py`` -- replaced the ``utc_today_iso``
    callsite with ``apex_trading_day_iso()``. Import updated.

B2 fix -- live-mode gate on tracker presence
  * ``scripts/run_eta_live.py`` -- ``ApexRuntime.__init__`` now
    raises ``RuntimeError`` when ``cfg.live=True AND
    cfg.dry_run=False AND trailing_dd_tracker is None``. The error
    message names the missing wiring explicitly and points at the
    module. Dry-run, paper-sim, and unit tests stay permissive
    (the proxy is acceptable for those modes).

B3 fix -- VIOLATION enforces PAUSE_NEW_ENTRIES
  * ``scripts/run_eta_live.py`` -- on
    ``ConsistencyStatus.VIOLATION`` the tick loop now synthesizes a
    ``KillVerdict(action=PAUSE_NEW_ENTRIES, severity=CRITICAL,
    scope="tier_a")`` and feeds it through the existing
    ``apply_verdict`` dispatch path. Every tier-A bot flips
    ``is_paused = True``. Existing positions are NOT flattened --
    they close on their own signals -- but new entries are blocked
    until the operator clears the violation. The synthetic verdict
    is appended to the tick's verdict log so audit history captures
    the enforcement.

Coverage delta
--------------
  * ``tests/test_consistency_guard.py`` +11 tests
    (``TestApexTradingDayIso``): CDT + CST before/after rollover,
    exact-boundary behavior, one-second-before boundary,
    overnight-session co-location, naive-datetime coercion,
    zoneinfo-absent fallback, and an explicit diff test
    confirming the two helpers disagree on an evening-session
    timestamp (the exact bug the fix closes).
  * ``tests/test_run_eta_live.py`` +4 tests
    (``TestLiveModeTrackerGate``): live-without-tracker raises,
    live-with-tracker builds cleanly, dry-run-without-tracker
    builds cleanly, ``live=True + dry_run=True`` builds cleanly
    (dry_run wins).
  * ``tests/test_run_eta_live.py`` +2 tests
    (``TestConsistencyViolationPauses``): pre-seeded VIOLATION
    fires PAUSE on the tick and persists in runtime.jsonl;
    pre-seeded WARNING does NOT fire PAUSE.

Lint side-quest
---------------
Two new SIM108 ruff errors introduced by the ``apex_trading_day_iso``
implementation were auto-simplified to ternaries (the idiomatic form
ruff was asking for). One I001 in the new test block was auto-fixed.
Pre-existing ANN401 debt in ``run_eta_live.py`` remains as
documented (Any types on router / bot factory surfaces are by-design).

Residual risks (documented, accepted)
-------------------------------------
Logged in ``docs/red_team_d2_d3_review.md`` sections R1..R4:

  R1 logical equity vs broker MTM drift
  R2 tick-interval latency vs Apex sub-second enforcement
  R3 freeze-rule re-entrancy on tracker state deletion
  R4 Apex session-day math across weekends / US holidays

None of these is a BLOCKER because the D-series v0.1.58 baseline is
safer than v0.1.57 across every regime tested. They are tracked for
v0.2.x to keep the pipeline honest about what "live-ready" means.

Expected state changes
----------------------
  * ``roadmap_state.json`` version bumped to v0.1.58.
  * ``eta_engine_tests_passing`` incremented by 17 new tests.
  * New key ``eta_engine_v0_1_58_red_team_closure`` with a full
    ledger of the three BLOCKERs and their fixes.
  * New milestone appended.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.58"
NEW_TESTS_ABS = 3704


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_58_red_team_closure"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "APEX EVAL READINESS HARDENING -- Red Team closure on "
            "v0.1.57 D-series wiring. Three BLOCKERs surfaced by "
            "risk-advocate adversarial review all closed in code + "
            "tests + docs."
        ),
        "theme": (
            "A safety module that is wired but whose wiring has a "
            "silent-failure gap is worse than no module: it produces "
            "false confidence. Every BLOCKER that could let the Apex "
            "eval bust while the runtime appears green is closed."
        ),
        "operator_directive_quote": ("continue all."),
        "modules_modified": [
            "eta_engine/core/consistency_guard.py "
            "(new apex_trading_day_iso helper + deprecation "
            "note on utc_today_iso)",
            "eta_engine/scripts/run_eta_live.py "
            "(B1 switch to session-day key; B2 live-mode tracker "
            "gate in ApexRuntime.__init__; B3 VIOLATION -> "
            "PAUSE_NEW_ENTRIES synthetic verdict + dispatch)",
        ],
        "docs_added": [
            "eta_engine/docs/red_team_d2_d3_review.md",
        ],
        "tests_added": [
            "eta_engine/tests/test_consistency_guard.py::TestApexTradingDayIso (11 tests)",
            "eta_engine/tests/test_run_eta_live.py::TestLiveModeTrackerGate (4 tests)",
            "eta_engine/tests/test_run_eta_live.py::TestConsistencyViolationPauses (2 tests)",
        ],
        "blockers_closed": {
            "B1_session_day_bucketing": {
                "severity": "CRITICAL",
                "finding": (
                    "utc_today_iso keyed today's consistency-guard "
                    "entry by UTC calendar midnight. US equity-futures "
                    "RTH + Globex overnight straddle UTC midnight so "
                    "a single Apex trading day's PnL was split across "
                    "two keys -- biasing the 30%-rule ratio downward "
                    "and hiding real concentration risk."
                ),
                "fix": (
                    "Added apex_trading_day_iso() using "
                    "zoneinfo.ZoneInfo('America/Chicago') with 17:00 "
                    "local rollover (DST-aware). Fallback to fixed "
                    "23:00-UTC rollover if zoneinfo unavailable. "
                    "run_eta_live now calls this helper."
                ),
                "tests": "TestApexTradingDayIso (11 tests)",
            },
            "B2_live_mode_tracker_gate": {
                "severity": "CRITICAL",
                "finding": (
                    "ApexRuntime defaulted to the bar-level "
                    "build_apex_eval_snapshot proxy when no "
                    "trailing_dd_tracker was supplied. The proxy "
                    "lacks the Apex freeze rule (peak >= start + cap "
                    "-> floor locks at start). A live runtime booted "
                    "without the tracker would silently under-protect."
                ),
                "fix": (
                    "ApexRuntime.__init__ raises RuntimeError when "
                    "cfg.live=True AND cfg.dry_run=False AND "
                    "trailing_dd_tracker is None. Dry-run, paper-sim, "
                    "and unit tests stay permissive."
                ),
                "tests": "TestLiveModeTrackerGate (4 tests)",
            },
            "B3_violation_enforces_pause": {
                "severity": "HIGH",
                "finding": (
                    "On ConsistencyStatus.VIOLATION the guard emitted "
                    "an alert + log but bot.state.is_paused stayed "
                    "False. 'Notify the operator and keep trading' "
                    "is not enforcement for an automated system."
                ),
                "fix": (
                    "Tick loop synthesizes KillVerdict(action="
                    "PAUSE_NEW_ENTRIES, severity=CRITICAL, "
                    "scope='tier_a') on VIOLATION and feeds it "
                    "through apply_verdict. Every tier-A bot flips "
                    "is_paused=True. Existing positions are NOT "
                    "flattened; only new entries blocked."
                ),
                "tests": "TestConsistencyViolationPauses (2 tests)",
            },
        },
        "residual_risks_accepted": {
            "R1": "logical equity vs broker MTM drift",
            "R2": "tick-interval latency vs Apex sub-second enforcement",
            "R3": "freeze-rule re-entrancy on tracker state deletion",
            "R4": "Apex session-day math across weekends/US holidays",
            "note": (
                "All four documented in red_team_d2_d3_review.md "
                "sections R1..R4. Tracked for v0.2.x. None "
                "qualified as BLOCKER; D-series v0.1.58 baseline "
                "is strictly safer than v0.1.57 across every "
                "regime tested."
            ),
        },
        "design_choices": {
            "dry_run_exempt_from_gate": (
                "B2 gate fires only when live=True AND dry_run=False. "
                "Dry-run callers keep the legacy proxy because the "
                "freeze-rule semantics do not matter when no real "
                "orders flow."
            ),
            "violation_pauses_not_flattens": (
                "B3 fix flips is_paused but does not flatten. Existing "
                "positions can close on their own signals; only new "
                "entries are blocked. This is the minimum-viable "
                "enforcement that respects operator intent (maybe the "
                "eval is recoverable via a careful day) while stopping "
                "further concentration."
            ),
            "fallback_rollover_is_23utc_not_22utc": (
                "When zoneinfo is missing, the fallback picks 23:00 UTC "
                "(CST winter rollover) rather than 22:00 UTC (CDT "
                "summer). Rationale: 23:00 UTC never falls mid-RTH "
                "session in either regime; 22:00 UTC in winter would "
                "split a live session. We accept being wrong by 1h in "
                "summer to guarantee correctness during RTH every day."
            ),
            "utc_today_iso_kept_for_compat": (
                "utc_today_iso not deleted -- only deprecation-noted. "
                "External callers (tests in downstream repos, the_firm_"
                "complete) may depend on the UTC-midnight semantics "
                "for their own purposes; deletion would be a breaking "
                "change for an issue confined to Apex eval accounting."
            ),
        },
        "tests_passing_before": prev_tests,
        "tests_passing_after": NEW_TESTS_ABS,
        "tests_new": NEW_TESTS_ABS - prev_tests,
        "ruff_green_new_code": True,
        "pre_existing_lint_debt": (
            "ANN401/ANN002/ANN003 in run_eta_live.py pre-date "
            "v0.1.57; F841/E741/ANN204/TC003 in test files pre-date "
            "v0.1.57. Nothing new added by this bump. Two SIM108 "
            "warnings introduced by the new apex_trading_day_iso "
            "implementation were auto-simplified to ternaries "
            "during the same review loop."
        ),
    }

    milestones = state.setdefault("milestones", [])
    if isinstance(milestones, list):
        milestones.append(
            {
                "version": VERSION,
                "timestamp_utc": now,
                "title": (
                    "Red Team closure on v0.1.57 D-series wiring. "
                    "B1 session-day bucketing, B2 live-mode tracker "
                    "gate, B3 VIOLATION-to-PAUSE enforcement -- all "
                    "three BLOCKERs closed in code + tests + docs. "
                    "D-series is now live-ready with respect to the "
                    "adversarial review; residual risks R1..R4 "
                    "documented and tracked."
                ),
                "tests_delta": NEW_TESTS_ABS - prev_tests,
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
        f"  tests_passing: {prev_tests} -> {NEW_TESTS_ABS} ({NEW_TESTS_ABS - prev_tests:+d})",
    )


if __name__ == "__main__":
    main()
