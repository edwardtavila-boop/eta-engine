"""One-shot: bump roadmap_state.json to v0.1.25.

Ships the FULL JARVIS BUNDLE -- 10 modules of operator-discipline
infrastructure mapped 1:1 to Edward's eta-engine principles
(A+ only, process-over-outcome, decision log, financial Jarvis,
never on autopilot, cadence of review, stress testing, risk
discipline, override discipline, continuous learning).

The bundle is orthogonal to the P0..P12 phase structure (it is
cross-cutting discipline + observability, not a roadmap phase), so
it is recorded as a shared_artifacts entry plus a +123 test bump.

Modules shipped
---------------
  1. core/trade_grader.py           -- post-trade A+/B/C/D/F report card
  2. obs/decision_journal.py        -- append-only JSONL decision log
  3. brain/jarvis_context.py        -- continuous macro+risk snapshot +
                                       priority-ordered action suggestion
  4. core/principles_checklist.py   -- 10-item principles self-audit
  5a. scripts/daily_premarket.py    -- 07:00 ET JarvisContext briefing
  5b. scripts/monthly_deep_review.py-- grading + MAE/MFE + rationales
                                       + rule-based tweak proposals
  6. backtest/exit_quality.py       -- MAE/MFE R-analysis heatmap
  7. obs/gate_override_telemetry.py -- block/override counters + rate
  8. obs/autopilot_watchdog.py      -- REQUIRE_ACK on stale positions,
                                       FROZEN sticky after flatten
  9. brain/rationale_miner.py       -- phrase clustering of decision
                                       rationales -> winners-minus-losers
 10. scripts/weekly_review.py       -- integrated the 10-item
                                       checklist: stub, load, report

Command Center integration
--------------------------
firm-tracker skill (artifact_template.jsx) grew a "Jarvis" tab +
top-bar JARVIS <ACTION> pill + Command Center Jarvis/Discipline KPIs,
pulling from docs/premarket_latest.json, docs/weekly_checklist_latest.json,
and docs/monthly_review_latest.json.

Tests
-----
+123 new tests. Existing suite rolled forward.
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
    new_tests = prev_tests + 123
    sa["eta_engine_tests_passing"] = new_tests

    sa["eta_engine_v0_1_25_jarvis"] = {
        "timestamp_utc": now,
        "version": "v0.1.25",
        "bundle_name": "JARVIS -- principles loop",
        "theme": (
            "Operator-discipline infrastructure: every eta-engine "
            "principle gets a measurable loop. A+ only -> TradeGrader. "
            "Process over outcome -> DecisionJournal + checklist. "
            "Financial Jarvis -> JarvisContext + daily_premarket. "
            "Never on autopilot -> AutopilotWatchdog. "
            "Cadence of review -> weekly_review + monthly_deep_review. "
            "Stress testing -> principles checklist index 6. "
            "Risk discipline -> gate_override_telemetry. "
            "Override discipline -> gate_override_telemetry rate gauge. "
            "Continuous learning -> rationale_miner + exit_quality."
        ),
        "modules": [
            "eta_engine/core/trade_grader.py",
            "eta_engine/obs/decision_journal.py",
            "eta_engine/brain/jarvis_context.py",
            "eta_engine/core/principles_checklist.py",
            "eta_engine/scripts/daily_premarket.py",
            "eta_engine/scripts/monthly_deep_review.py",
            "eta_engine/backtest/exit_quality.py",
            "eta_engine/obs/gate_override_telemetry.py",
            "eta_engine/obs/autopilot_watchdog.py",
            "eta_engine/brain/rationale_miner.py",
        ],
        "weekly_review_integration": (
            "scripts/weekly_review.py gained _load_checklist / "
            "_write_checklist_stub / _write_checklist_report and a "
            "--checklist-answers CLI arg. Produces "
            "docs/weekly_checklist_latest.{json,txt}."
        ),
        "jarvis_action_vocabulary": [
            "TRADE",
            "STAND_ASIDE",
            "REDUCE",
            "REVIEW",
            "KILL",
        ],
        "jarvis_priority_order": [
            "1. KILL   -- kill_switch OR daily_dd >= 5%",
            "2. STAND_ASIDE -- macro event < 1h OR REQUIRE_ACK OR daily_dd >= 3%",
            "3. REDUCE -- daily_dd >= 2% OR open_risk > 3R OR macro_bias = CRISIS",
            "4. REVIEW -- overrides >= 3 OR regime flipped OR correlations_alert",
            "5. TRADE  -- all gates green",
        ],
        "watchdog_policy": {
            "ack_ttl_sec": 1800,
            "tighten_after_sec": 3600,
            "tighten_factor": 0.75,
            "max_age_sec": 7200,
            "alert_ladder": [
                "REQUIRE_ACK -> TIGHTEN_STOP -> FORCE_FLATTEN",
            ],
            "mode_ladder": [
                "ACTIVE -> REQUIRE_ACK -> FROZEN (sticky post-flatten)",
            ],
        },
        "checklist_principles_fixed_order": [
            "0 a_plus_only",
            "1 process_over_outcome",
            "2 decision_log",
            "3 consult_jarvis",
            "4 never_autopilot",
            "5 cadence_of_review",
            "6 stress_testing",
            "7 risk_discipline",
            "8 override_discipline",
            "9 continuous_learning",
        ],
        "grading_bands": {
            "A+": 0.95,
            "A": 0.85,
            "B": 0.75,
            "C": 0.60,
            "D": 0.40,
            "F": 0.0,
        },
        "new_test_files": [
            "tests/test_jarvis_context.py (25)",
            "tests/test_principles_checklist.py (19)",
            "tests/test_daily_premarket.py (6)",
            "tests/test_monthly_deep_review.py (7)",
            "tests/test_gate_override_telemetry.py (12)",
            "tests/test_autopilot_watchdog.py (18)",
            "tests/test_rationale_miner.py (29)",
            "tests/test_weekly_review_checklist.py (7)",
        ],
        "tests_new": 123,
        "tests_passing_before": prev_tests,
        "tests_passing_after": new_tests,
        "new_report_files_under_docs": [
            "premarket_latest.json",
            "premarket_latest.txt",
            "premarket_log.jsonl",
            "weekly_checklist_latest.json",
            "weekly_checklist_latest.txt",
            "monthly_review_latest.json",
            "monthly_review_latest.txt",
            "monthly_review_<YYYY>_<MM>.json",
            "monthly_review_<YYYY>_<MM>.txt",
        ],
        "new_prometheus_metrics": [
            "apex_gate_blocks_total",
            "apex_gate_overrides_total",
            "apex_gate_override_rate",
        ],
        "command_center_integration": {
            "skill": "firm-tracker",
            "artifact": (".claude/skills/firm-tracker/references/artifact_template.jsx"),
            "changes": [
                "Added JARVIS constant block (premarket + checklist + "
                "monthly + bundle) with source-file mapping comment.",
                "Added JARVIS_PRINCIPLES constant (10 slug+question).",
                "Added JarvisTab component: action banner, 6 KPI tiles "
                "(Discipline/Equity/DD/OpenRisk/Overrides/Autopilot), "
                "10-principles report card, context snapshot, bundle "
                "status, monthly deep review.",
                "Added jarvisActionColor helper (TRADE/STAND_ASIDE/REDUCE/REVIEW/KILL -> pill color + hex).",
                "Added 'Jarvis' to TABS array (2nd tab).",
                "Added JARVIS top-bar pill showing current action.",
                "Command Center tab now shows Jarvis + Discipline KPIs in an expanded 7-column KPI row.",
            ],
            "docs_sources_updated": [
                "data_sources.md documents premarket_latest.json / "
                "weekly_checklist_latest.json / "
                "monthly_review_latest.json",
            ],
        },
    }

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"bumped roadmap_state.json to v0.1.25 at {now}")
    print(f"  tests_passing: {prev_tests} -> {new_tests} (+123)")
    print("  shared_artifacts.eta_engine_v0_1_25_jarvis written")
    print("  10 modules shipped; Command Center Jarvis tab integrated")


if __name__ == "__main__":
    main()
