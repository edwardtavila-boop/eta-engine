"""One-shot: bump roadmap_state.json to v0.1.38.

DECISION JOURNAL SINK -- RouterDecision audited on every dispatch.

Context
-------
v0.1.35 wired the AI-Optimized strategies into every bot's ``on_bar``
handler. v0.1.36 added the backtest harness so we can calibrate the
eligibility table on historical tape. v0.1.37 operationalised the
HIGH_VOL exclusion gate so the live policy obeys the cross-regime
OOS verdict.

v0.1.38 closes the observability loop: every live dispatch of
``policy_router.dispatch`` is now written to the unified
:class:`DecisionJournal` -- the same append-only JSONL feed that
kill-switch events, firm-board verdicts, and trade executions share.
Post-trade auditors and the rationale-miner finally have one source
of truth to grep.

What v0.1.38 adds
-----------------
  * ``obs/decision_journal.py`` -- new ``Actor.STRATEGY_ROUTER`` enum
    value (last row; the rest of the enum is untouched).
  * ``strategies/decision_sink.py`` (new, ~200 lines)
     - Pure helper ``router_decision_to_event(decision, *,
       outcome=Outcome.NOTED, links=None, include_candidates=False)
       -> JournalEvent`` mapping a :class:`RouterDecision` into a
       :class:`JournalEvent` with ``actor=STRATEGY_ROUTER``,
       ``intent=f"dispatch_{ASSET}"``, ``rationale=f"{winner.strategy.value}
       :{winner.side.value}"``, gate_checks reflecting eligible /
       actionable / flat state, and metadata carrying asset / winner /
       candidates_fired / eligible plus an opt-in full candidates list.
     - ``RouterDecisionSink`` dataclass with flags ``enabled``,
       ``include_candidates``, ``default_outcome``, ``also_log_flat``.
       ``emit(decision, *, outcome=None, links=None)`` writes exactly
       one row and swallows ``OSError`` so a disk hiccup can never
       crash the live bot loop.
  * ``strategies/engine_adapter.py`` -- ``RouterAdapter`` gains an
    optional ``decision_sink: RouterDecisionSink | None = None`` field.
    ``push_bar`` invokes ``self.decision_sink.emit(decision)`` after the
    router runs; sink failures are already swallowed so the hot path is
    crash-proof.
  * ``tests/test_strategies_decision_sink.py`` (new, +37 tests)

    Four test classes:
      - ``TestRouterDecisionToEvent`` -- pure-helper invariants: actor,
        intent, rationale, gate_checks (+eligible / +actionable /
        -flat), metadata shape, include_candidates opt-in. 16
      - ``TestRouterDecisionSinkBasics`` -- enabled=False, journal=None,
        flat-winner gating (default + also_log_flat=True), caller vs
        default_outcome precedence, include_candidates wiring,
        links pass-through. 10
      - ``TestRouterDecisionSinkRobustness`` -- OSError swallowing,
        non-OSError propagation. 2
      - ``TestRouterAdapterIntegration`` -- end-to-end: sink writes one
        row per tick, flat-gating, no-sink means no journal touch,
        kill-switch muted but sink still captures, disabled sink stays
        silent, event metadata round-trips asset + winner. 9

Delta
-----
  * tests_passing: 1795 -> 1832 (+37 new decision-sink tests)
  * Every pre-existing bot / strategy / journal test still passes
    unchanged
  * Ruff-clean on the three edited modules and the new test file
  * No phase-level status changes (overall_progress_pct stays at 99)

Why this matters
----------------
Before v0.1.38 the router's decisions were invisible after the bot
loop returned a Signal. Dashboards could only replay the signal the
bot chose to execute; they could not see vetoed candidates, flat
dispatches, or the gate_checks trail. With the sink wired every
dispatch is now one jsonl row indexed on ``actor=STRATEGY_ROUTER``
and ``intent=dispatch_{ASSET}`` -- the same shape the rationale-miner
already knows how to consume. When v0.1.39 composes the harness with
the WalkForwardEngine, the journal becomes the primary audit surface
for strategy calibration.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.38"
NEW_TESTS_ABS = 1832


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_38_decision_journal_sink"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": ("DECISION JOURNAL SINK -- RouterDecision audited on every dispatch via Actor.STRATEGY_ROUTER"),
        "theme": (
            "Close the observability loop between policy_router and the "
            "unified decision journal. Every live dispatch becomes one "
            "JSONL row the rationale-miner can grep, with asset, "
            "winner, eligible, and candidates_fired metadata, plus "
            "opt-in full candidate capture for offline research runs."
        ),
        "artifacts_added": {
            "strategies": ["strategies/decision_sink.py"],
            "tests": ["tests/test_strategies_decision_sink.py"],
            "scripts": ["scripts/_bump_roadmap_v0_1_38.py"],
        },
        "artifacts_modified": {
            "obs": ["obs/decision_journal.py (+Actor.STRATEGY_ROUTER)"],
            "strategies": [
                "strategies/engine_adapter.py (RouterAdapter.decision_sink + push_bar emit)",
            ],
        },
        "api_surface": {
            "Actor.STRATEGY_ROUTER": (
                "New enum value; join KILL_SWITCH / FIRM_BOARD / TRADE_ENGINE / RISK_GATE / ... in the unified log."
            ),
            "router_decision_to_event": (
                "(decision, *, outcome=Outcome.NOTED, links=None, include_candidates=False) -> JournalEvent"
            ),
            "RouterDecisionSink": (
                "journal, enabled=True, include_candidates=False, default_outcome=Outcome.NOTED, also_log_flat=False"
            ),
            "RouterDecisionSink.emit": (
                "(decision, *, outcome=None, links=None) -> JournalEvent | None  -- swallows OSError"
            ),
            "RouterAdapter.decision_sink": (
                "Optional sink attached at construction time; push_bar emits one row per dispatch when set."
            ),
        },
        "design_notes": {
            "pure_helper_first": (
                "router_decision_to_event has no I/O; it's a deterministic "
                "mapping callers can reuse for offline replays + admin "
                "audit payloads without touching a journal."
            ),
            "sink_never_crashes_bot": (
                "emit() swallows OSError (disk full, permission error). "
                "Any other exception still propagates so bugs are not "
                "hidden; observability outages stay observable."
            ),
            "flat_gating_default": (
                "also_log_flat=False by default -- the vast majority of "
                "ticks produce flat winners and logging them all would "
                "swamp the journal. Research runs flip the flag."
            ),
            "type_checking_import": (
                "engine_adapter only references RouterDecisionSink under "
                "TYPE_CHECKING, so bots that don't wire a sink pay zero "
                "import-time cost; the obs package stays optional."
            ),
        },
        "test_coverage": {
            "tests_added": 37,
            "classes": {
                "TestRouterDecisionToEvent": 16,
                "TestRouterDecisionSinkBasics": 10,
                "TestRouterDecisionSinkRobustness": 2,
                "TestRouterAdapterIntegration": 9,
            },
        },
        "ruff_clean_on": [
            "obs/decision_journal.py",
            "strategies/decision_sink.py",
            "strategies/engine_adapter.py",
            "tests/test_strategies_decision_sink.py",
        ],
        "phase_reconciliation": {
            "overall_progress_pct": 99,
            "status": (
                "unchanged -- still funding-gated on P9_ROLLOUT; the "
                "sink closes the observability gap so every live "
                "dispatch is now auditable offline."
            ),
            "note": (
                "v0.1.39 will compose the backtest harness with "
                "WalkForwardEngine + DSR gate to formally qualify each "
                "strategy per-asset, using the sink's journal rows as "
                "the lineage source. v0.1.40 will wire regime_allocator "
                "into the portfolio rebalancer so the PORTFOLIO-tier "
                "strategy actually drives capital allocation live."
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
                    "Decision journal sink wired to policy_router: "
                    "every live dispatch now produces one JournalEvent "
                    "with actor=STRATEGY_ROUTER, rationale, gate_checks "
                    "and candidates_fired metadata -- observability "
                    "loop closed."
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
        "  shipped: strategies/decision_sink.py + Actor.STRATEGY_ROUTER "
        "+ RouterAdapter.decision_sink field + 37 tests. Every live "
        "router dispatch now audited to docs/decision_journal.jsonl."
    )


if __name__ == "__main__":
    main()
