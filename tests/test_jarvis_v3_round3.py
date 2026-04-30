"""Tests for JARVIS upgrades #18-#22 (2026-04-26).

Covers:
  - jarvis_journals (consolidated state/recs/anomalies, #19+#20)
  - jarvis_explainer (reason-code tutor, #21)
  - jarvis_daily_report (end-of-day report, #22)
  - jarvis_status CLI (unified entry point, #18)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from eta_engine.brain.jarvis_anomaly import Anomaly, AnomalySeverity
from eta_engine.brain.jarvis_daily_report import (
    generate_daily_report,
)
from eta_engine.brain.jarvis_daily_report import (
    render_markdown as render_daily,
)
from eta_engine.brain.jarvis_explainer import (
    KNOWN_REASON_CODES,
    explain,
)
from eta_engine.brain.jarvis_explainer import (
    render_markdown as render_explanation,
)
from eta_engine.brain.jarvis_journals import (
    AnomalyJournal,
    RecommendationJournal,
    StateJournal,
    replay_for_decision,
)
from eta_engine.brain.jarvis_recommender import (
    Recommendation,
    RecommendationLevel,
)
from eta_engine.brain.jarvis_session_state import (
    SessionStateSnapshot,
)

if TYPE_CHECKING:
    from pathlib import Path

# ── Upgrade #19/#20: journals (state, recs, anomalies) ───────────────────────


class TestRecommendationJournal:
    def test_empty_returns_none(self, tmp_path: Path) -> None:
        j = RecommendationJournal(path=tmp_path / "recs.jsonl")
        assert j.latest() is None
        assert j.read_all() == []

    def test_append_and_read(self, tmp_path: Path) -> None:
        j = RecommendationJournal(path=tmp_path / "recs.jsonl")
        recs = [
            Recommendation(
                level=RecommendationLevel.WARN,
                code="test_code",
                title="Test",
                rationale="testing",
            )
        ]
        j.append(recs)
        entries = j.read_all()
        assert len(entries) == 1
        assert entries[0]["n"] == 1
        assert entries[0]["recommendations"][0]["code"] == "test_code"


class TestAnomalyJournal:
    def test_empty_anomaly_list_does_not_bloat_journal(self, tmp_path: Path) -> None:
        j = AnomalyJournal(path=tmp_path / "anom.jsonl")
        j.append([])  # empty list
        assert j.read_all() == []

    def test_appends_when_anomalies_present(self, tmp_path: Path) -> None:
        j = AnomalyJournal(path=tmp_path / "anom.jsonl")
        anom = [
            Anomaly(
                severity=AnomalySeverity.WARN,
                code="test_anom",
                message="just a test",
            )
        ]
        j.append(anom)
        entries = j.read_all()
        assert len(entries) == 1
        assert entries[0]["n"] == 1


class TestStateJournalReplay:
    def test_replay_for_decision_compat(self, tmp_path: Path) -> None:
        j = StateJournal(path=tmp_path / "state.jsonl")
        snap = SessionStateSnapshot(cumulative_trials=42)
        j.append(snap)
        # The legacy free function delegates to journal
        future = datetime.now(UTC) + timedelta(seconds=1)
        active = replay_for_decision(future, journal=j)
        assert active is not None
        assert active["snapshot"]["cumulative_trials"] == 42


# ── Upgrade #21: explainer / tutor ───────────────────────────────────────────


class TestExplainer:
    def test_known_codes_have_explanations(self) -> None:
        # Every code in the registry returns a valid explanation
        for code, exp in KNOWN_REASON_CODES.items():
            assert exp.code == code
            assert exp.title
            assert exp.summary
            assert exp.triggers_when

    def test_unknown_code_returns_none(self) -> None:
        assert explain("definitely_not_a_real_code") is None

    def test_explain_slow_bleed_tripped_has_lessons(self) -> None:
        exp = explain("slow_bleed_tripped")
        assert exp is not None
        assert 14 in exp.lesson_refs
        assert 19 in exp.lesson_refs

    def test_explain_regime_choppy_links_to_lessons_28_29(self) -> None:
        exp = explain("regime_choppy_no_entries")
        assert exp is not None
        assert 28 in exp.lesson_refs
        assert 29 in exp.lesson_refs

    def test_render_markdown_includes_all_sections(self) -> None:
        exp = explain("research_reopens_search")
        assert exp is not None
        md = render_explanation(exp)
        assert exp.title in md
        assert "Summary" in md
        assert "What to do" in md
        assert "Playbook lessons" in md

    def test_known_codes_match_admin_reason_codes(self) -> None:
        """Every reason_code emitted by jarvis_admin v3 rules should
        have an explanation. Catches drift between code and docs."""
        v3_codes = {
            "slow_bleed_tripped",
            "slow_bleed_warning_cap",
            "research_reopens_search",
            "regime_choppy_no_entries",
            "regime_uncertain_cap",
            "gate_report_blocks_promote",
        }
        for code in v3_codes:
            assert explain(code) is not None, (
                f"reason_code '{code}' is emitted by jarvis_admin but has "
                f"no entry in jarvis_explainer.KNOWN_REASON_CODES"
            )


# ── Upgrade #22: daily report ────────────────────────────────────────────────


class TestDailyReport:
    def test_generates_with_required_fields(self, tmp_path: Path) -> None:
        report = generate_daily_report(
            state_journal_path=tmp_path / "state.jsonl",
            recs_journal_path=tmp_path / "recs.jsonl",
            anomaly_journal_path=tmp_path / "anom.jsonl",
            cost_ledger_path=None,
        )
        assert report.generated_at is not None
        assert report.health_verdict in ("HEALTHY", "DEGRADED", "UNHEALTHY")
        assert "phase" in report.current_state

    def test_render_markdown_contains_headers(self, tmp_path: Path) -> None:
        report = generate_daily_report(
            state_journal_path=tmp_path / "state.jsonl",
            recs_journal_path=tmp_path / "recs.jsonl",
            anomaly_journal_path=tmp_path / "anom.jsonl",
            cost_ledger_path=None,
        )
        md = render_daily(report)
        assert "# JARVIS Daily Report" in md
        assert "## Current state" in md
        assert "## Activity" in md

    def test_renders_demotion_savings_when_provided(self, tmp_path: Path) -> None:
        from eta_engine.brain.jarvis_cost_attribution import CostLedger
        from eta_engine.brain.model_policy import ModelTier, TaskCategory

        ledger = CostLedger()
        ledger.record(
            TaskCategory.RED_TEAM_SCORING,
            input_tokens=100,
            output_tokens=100,
            tier=ModelTier.SONNET,  # demoted from OPUS
        )
        ledger_path = tmp_path / "ledger.jsonl"
        ledger.save_to_jsonl(ledger_path)
        report = generate_daily_report(
            state_journal_path=tmp_path / "state.jsonl",
            recs_journal_path=tmp_path / "recs.jsonl",
            anomaly_journal_path=tmp_path / "anom.jsonl",
            cost_ledger_path=ledger_path,
        )
        assert report.demotion_savings_summary is not None
        md = render_daily(report)
        assert "Phase-aware routing savings" in md


# ── Upgrade #18: unified status CLI ──────────────────────────────────────────


class TestStatusCli:
    def test_default_status_returns_zero(self, capsys) -> None:
        from eta_engine.scripts.jarvis_status import main

        ret = main([])
        assert ret == 0
        out = capsys.readouterr().out
        assert "JARVIS STATUS" in out

    def test_health_subcommand(self, capsys) -> None:
        from eta_engine.scripts.jarvis_status import main

        ret = main(["--health"])
        out = capsys.readouterr().out
        # health verdict must be one of three values
        assert any(v in out for v in ("HEALTHY", "DEGRADED", "UNHEALTHY"))
        assert ret in (0, 1, 2)

    def test_recommend_subcommand(self, capsys) -> None:
        from eta_engine.scripts.jarvis_status import main

        ret = main(["--recommend"])
        assert ret == 0

    def test_explain_known_code(self, capsys) -> None:
        from eta_engine.scripts.jarvis_status import main

        ret = main(["--explain", "slow_bleed_tripped"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "Slow-bleed" in out

    def test_explain_unknown_code_errors(self, capsys) -> None:
        from eta_engine.scripts.jarvis_status import main

        ret = main(["--explain", "no_such_code_ever"])
        assert ret == 1
        err = capsys.readouterr().err
        assert "unknown reason_code" in err

    def test_daily_subcommand(self, capsys) -> None:
        from eta_engine.scripts.jarvis_status import main

        ret = main(["--daily"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "JARVIS Daily Report" in out

    def test_json_subcommand_machine_readable(self, capsys) -> None:
        from eta_engine.scripts.jarvis_status import main

        ret = main(["--json"])
        assert ret == 0
        out = capsys.readouterr().out
        import json

        payload = json.loads(out)
        assert "session_state" in payload
        assert "recommendations" in payload
        assert "health_verdict" in payload
        assert "operator_queue" in payload
        assert "summary" in payload["operator_queue"]
        assert "top_blockers" in payload["operator_queue"]

    def test_bot_strategy_readiness_summary_loads_snapshot(self, tmp_path: Path) -> None:
        from eta_engine.scripts.jarvis_status import build_bot_strategy_readiness_summary

        target = tmp_path / "bot_strategy_readiness_latest.json"
        target.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-04-29T20:00:00+00:00",
                    "source": "bot_strategy_readiness",
                    "summary": {
                        "total_bots": 3,
                        "blocked_data": 1,
                        "can_live_any": False,
                        "can_paper_trade": 2,
                        "launch_lanes": {"blocked_data": 1, "live_preflight": 1, "paper_soak": 1},
                    },
                    "rows": [
                        {
                            "bot_id": "btc_compression",
                            "launch_lane": "blocked_data",
                            "next_action": "Fetch missing critical data: bars:BTC/1h",
                            "can_paper_trade": False,
                            "can_live_trade": False,
                        },
                        {
                            "bot_id": "nq_daily_drb",
                            "launch_lane": "live_preflight",
                            "next_action": "Run per-bot promotion preflight",
                            "can_paper_trade": True,
                            "can_live_trade": False,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        payload = build_bot_strategy_readiness_summary(path=target, limit=2)

        assert payload["status"] == "ready"
        assert payload["generated_at"] == "2026-04-29T20:00:00+00:00"
        assert payload["summary"]["blocked_data"] == 1
        assert payload["top_actions"][0]["bot_id"] == "btc_compression"
        assert payload["top_actions"][0]["next_action"].startswith("Fetch missing critical data")

    def test_bot_strategy_readiness_summary_exposes_full_rows_beyond_action_limit(self, tmp_path: Path) -> None:
        from eta_engine.scripts.jarvis_status import build_bot_strategy_readiness_summary

        target = tmp_path / "bot_strategy_readiness_latest.json"
        target.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-04-29T20:00:00+00:00",
                    "source": "bot_strategy_readiness",
                    "summary": {
                        "total_bots": 3,
                        "blocked_data": 1,
                        "can_live_any": False,
                        "can_paper_trade": 2,
                        "launch_lanes": {"blocked_data": 1, "live_preflight": 1, "paper_soak": 1},
                    },
                    "rows": [
                        {
                            "bot_id": "btc_compression",
                            "strategy_id": "btc_compression_v1",
                            "launch_lane": "blocked_data",
                            "next_action": "Fetch missing critical data: bars:BTC/1h",
                            "can_paper_trade": False,
                            "can_live_trade": False,
                        },
                        {
                            "bot_id": "nq_daily_drb",
                            "strategy_id": "nq_daily_drb_v1",
                            "launch_lane": "live_preflight",
                            "next_action": "Run per-bot promotion preflight",
                            "can_paper_trade": True,
                            "can_live_trade": False,
                        },
                        {
                            "bot_id": "eth_compression",
                            "strategy_id": "eth_compression_v1",
                            "launch_lane": "paper_soak",
                            "next_action": "Run paper-soak and broker drift checks",
                            "can_paper_trade": True,
                            "can_live_trade": False,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        payload = build_bot_strategy_readiness_summary(path=target, limit=1)

        assert payload["row_count"] == 3
        assert len(payload["top_actions"]) == 1
        assert [row["bot_id"] for row in payload["rows"]] == [
            "btc_compression",
            "nq_daily_drb",
            "eth_compression",
        ]
        assert payload["rows"][1]["strategy_id"] == "nq_daily_drb_v1"

    def test_bot_strategy_readiness_summary_indexes_rows_by_bot_for_framework_clients(
        self,
        tmp_path: Path,
    ) -> None:
        from eta_engine.scripts.jarvis_status import build_bot_strategy_readiness_summary

        target = tmp_path / "bot_strategy_readiness_latest.json"
        target.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-04-29T20:00:00+00:00",
                    "source": "bot_strategy_readiness",
                    "summary": {"total_bots": 2, "launch_lanes": {"live_preflight": 1, "paper_soak": 1}},
                    "rows": [
                        {
                            "bot_id": "nq_daily_drb",
                            "strategy_id": "nq_daily_drb_v1",
                            "launch_lane": "live_preflight",
                            "can_paper_trade": True,
                            "can_live_trade": False,
                        },
                        {
                            "bot_id": "eth_compression",
                            "strategy_id": "eth_compression_v1",
                            "launch_lane": "paper_soak",
                            "can_paper_trade": True,
                            "can_live_trade": False,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        payload = build_bot_strategy_readiness_summary(path=target)

        assert sorted(payload["rows_by_bot"]) == ["eth_compression", "nq_daily_drb"]
        assert payload["rows_by_bot"]["nq_daily_drb"]["strategy_id"] == "nq_daily_drb_v1"
        assert payload["rows_by_bot"]["eth_compression"]["launch_lane"] == "paper_soak"

    def test_bot_strategy_readiness_summary_fails_soft_when_missing(self, tmp_path: Path) -> None:
        from eta_engine.scripts.jarvis_status import build_bot_strategy_readiness_summary

        payload = build_bot_strategy_readiness_summary(path=tmp_path / "missing.json")

        assert payload["status"] == "missing"
        assert payload["summary"] == {}
        assert payload["row_count"] == 0
        assert payload["rows"] == []
        assert payload["rows_by_bot"] == {}
        assert payload["top_actions"] == []

    def test_json_subcommand_surfaces_bot_strategy_readiness(self, monkeypatch, capsys) -> None:
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_bot_strategy_readiness_summary",
            lambda **_kwargs: {
                "source": "bot_strategy_readiness",
                "status": "ready",
                "summary": {"blocked_data": 0, "launch_lanes": {"live_preflight": 6}},
                "top_actions": [],
            },
        )

        ret = jarvis_status.main(["--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        readiness = payload["bot_strategy_readiness"]
        assert readiness["status"] == "ready"
        assert readiness["summary"]["launch_lanes"]["live_preflight"] == 6

    def test_default_status_prints_bot_strategy_readiness(self, monkeypatch, capsys) -> None:
        from eta_engine.scripts import jarvis_status

        monkeypatch.setattr(
            jarvis_status,
            "build_bot_strategy_readiness_summary",
            lambda **_kwargs: {
                "source": "bot_strategy_readiness",
                "status": "ready",
                "summary": {
                    "blocked_data": 0,
                    "can_live_any": False,
                    "can_paper_trade": 10,
                    "launch_lanes": {
                        "live_preflight": 6,
                        "non_edge": 1,
                        "paper_soak": 4,
                        "shadow_only": 4,
                    },
                },
                "top_actions": [],
            },
        )

        ret = jarvis_status.main([])

        assert ret == 0
        out = capsys.readouterr().out
        assert "Bot readiness:" in out
        assert "live_preflight=6" in out
        assert "paper_soak=4" in out
        assert "shadow_only=4" in out
        assert "non_edge=1" in out

    def test_json_subcommand_surfaces_operator_blockers(self, monkeypatch, capsys) -> None:
        from eta_engine.scripts import operator_action_queue
        from eta_engine.scripts.jarvis_status import main

        monkeypatch.setattr(
            operator_action_queue,
            "collect_items",
            lambda: [
                operator_action_queue.OpItem(
                    op_id="OP-1",
                    title="Fund IBKR primary account",
                    verdict=operator_action_queue.VERDICT_BLOCKED,
                    detail="IBKR creds absent",
                    where="IBKR portal",
                    evidence={},
                ),
                operator_action_queue.OpItem(
                    op_id="OP-18",
                    title="Resolve current VPS failover red/amber blockers",
                    verdict=operator_action_queue.VERDICT_BLOCKED,
                    detail="AMBER with 2 blocker(s)",
                    where="python -m eta_engine.scripts.vps_failover_summary --json",
                    evidence={
                        "overall_severity": "amber",
                        "blockers": [
                            {
                                "name": "secrets_present",
                                "next_commands": ["cp .env.example .env && chmod 600 .env"],
                            }
                        ],
                    },
                )
            ],
        )

        ret = main(["--json"])

        assert ret == 0
        import json

        payload = json.loads(capsys.readouterr().out)
        queue = payload["operator_queue"]
        assert queue["error"] is None
        assert queue["summary"]["BLOCKED"] == 2
        assert queue["top_blockers"][0]["op_id"] == "OP-18"
        assert queue["top_blockers"][0]["evidence"]["overall_severity"] == "amber"
        assert queue["top_blockers"][0]["next_actions"] == [
            "cp .env.example .env && chmod 600 .env"
        ]
        assert queue["next_actions"][0] == "cp .env.example .env && chmod 600 .env"
