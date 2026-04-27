"""Tests for brain.jarvis_cost_attribution."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from eta_engine.brain.jarvis_cost_attribution import (
    CostEvent,
    CostLedger,
    render_markdown,
    weekly_report,
    write_report,
)
from eta_engine.brain.model_policy import (
    COST_RATIO,
    ModelTier,
    TaskBucket,
    TaskCategory,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestCostEvent:
    def test_total_tokens_sums_io(self):
        e = CostEvent(
            task_category=TaskCategory.CODE_REVIEW,
            tier=ModelTier.SONNET,
            input_tokens=1000,
            output_tokens=500,
        )
        assert e.total_tokens == 1500

    def test_sonnet_equiv_opus_5x(self):
        e = CostEvent(
            task_category=TaskCategory.RED_TEAM_SCORING,
            tier=ModelTier.OPUS,
            input_tokens=100,
            output_tokens=100,
        )
        assert e.sonnet_equiv_units == 200 * COST_RATIO[ModelTier.OPUS]
        assert e.sonnet_equiv_units == 1000.0

    def test_sonnet_equiv_haiku_point_two(self):
        e = CostEvent(
            task_category=TaskCategory.LOG_PARSING,
            tier=ModelTier.HAIKU,
            input_tokens=100,
            output_tokens=100,
        )
        assert e.sonnet_equiv_units == pytest.approx(40.0)


class TestCostLedgerRecord:
    def test_record_looks_up_tier_from_category(self):
        ledger = CostLedger()
        ev = ledger.record(TaskCategory.CODE_REVIEW, input_tokens=100, output_tokens=50)
        assert ev.tier == ModelTier.SONNET
        assert ledger.events == (ev,)

    def test_record_architectural_gets_opus(self):
        ledger = CostLedger()
        ev = ledger.record(TaskCategory.RED_TEAM_SCORING, input_tokens=500, output_tokens=300)
        assert ev.tier == ModelTier.OPUS

    def test_record_grunt_gets_haiku(self):
        ledger = CostLedger()
        ev = ledger.record(TaskCategory.COMMIT_MESSAGE, input_tokens=80, output_tokens=40)
        assert ev.tier == ModelTier.HAIKU

    def test_record_explicit_tier_overrides_lookup(self):
        ledger = CostLedger()
        ev = ledger.record(
            TaskCategory.DEBUG,
            input_tokens=10,
            output_tokens=10,
            tier=ModelTier.OPUS,
        )
        assert ev.tier == ModelTier.OPUS

    def test_negative_tokens_rejected(self):
        ledger = CostLedger()
        with pytest.raises(ValueError):
            ledger.record(TaskCategory.CODE_REVIEW, input_tokens=-1, output_tokens=10)


class TestRollupCounts:
    def test_rollup_sums_tokens_and_events(self):
        ledger = CostLedger()
        ledger.record(TaskCategory.CODE_REVIEW, input_tokens=100, output_tokens=50)
        ledger.record(TaskCategory.CODE_REVIEW, input_tokens=200, output_tokens=100)
        report = ledger.rollup()
        assert report.n_events == 2
        assert report.total_tokens == 450

    def test_empty_ledger_rolls_up_zero(self):
        report = CostLedger().rollup()
        assert report.n_events == 0
        assert report.by_category == ()

    def test_opus_event_dominates_by_units(self):
        """A single Opus call outweighs many Haiku calls."""
        ledger = CostLedger()
        # 1 Opus call: 1000 tokens * 5.0 = 5000 units
        ledger.record(TaskCategory.RED_TEAM_SCORING, input_tokens=500, output_tokens=500)
        # 10 Haiku calls: 10 * 1000 tokens * 0.2 = 2000 units
        for _ in range(10):
            ledger.record(TaskCategory.LOG_PARSING, input_tokens=500, output_tokens=500)
        report = ledger.rollup()
        # ARCHITECTURAL bucket should be listed first in by_category
        assert report.by_category[0].bucket == TaskBucket.ARCHITECTURAL
        # And have higher units than the grunt work
        arch_units = next(b.sonnet_equiv_units for b in report.by_bucket if b.bucket == TaskBucket.ARCHITECTURAL)
        grunt_units = next(b.sonnet_equiv_units for b in report.by_bucket if b.bucket == TaskBucket.GRUNT)
        assert arch_units > grunt_units


class TestRollupWindow:
    def test_window_filter_excludes_earlier_events(self):
        ledger = CostLedger()
        old_ts = datetime.now(UTC) - timedelta(days=30)
        new_ts = datetime.now(UTC)
        ledger.record(
            TaskCategory.CODE_REVIEW,
            input_tokens=100,
            output_tokens=50,
            ts_utc=old_ts,
        )
        ledger.record(
            TaskCategory.CODE_REVIEW,
            input_tokens=200,
            output_tokens=100,
            ts_utc=new_ts,
        )
        report = ledger.rollup(
            window_start=datetime.now(UTC) - timedelta(days=7),
            window_end=datetime.now(UTC) + timedelta(seconds=1),
        )
        assert report.n_events == 1
        assert report.total_tokens == 300


class TestBucketPercentages:
    def test_bucket_pct_sums_to_100(self):
        ledger = CostLedger()
        ledger.record(TaskCategory.RED_TEAM_SCORING, input_tokens=100, output_tokens=100)
        ledger.record(TaskCategory.DEBUG, input_tokens=100, output_tokens=100)
        ledger.record(TaskCategory.LOG_PARSING, input_tokens=100, output_tokens=100)
        report = ledger.rollup()
        total_pct = sum(b.pct_of_grand_total for b in report.by_bucket)
        assert total_pct == pytest.approx(100.0, abs=0.3)

    def test_bucket_order_is_arch_routine_grunt(self):
        ledger = CostLedger()
        ledger.record(TaskCategory.LOG_PARSING, input_tokens=100, output_tokens=100)
        ledger.record(TaskCategory.RED_TEAM_SCORING, input_tokens=100, output_tokens=100)
        report = ledger.rollup()
        buckets = [b.bucket for b in report.by_bucket]
        assert buckets == [
            TaskBucket.ARCHITECTURAL,
            TaskBucket.ROUTINE,
            TaskBucket.GRUNT,
        ]


class TestWeeklyReport:
    def test_weekly_wrapper_uses_7d_window(self):
        ledger = CostLedger()
        week_start = datetime.now(UTC) - timedelta(days=7)
        # Inside window
        ledger.record(
            TaskCategory.CODE_REVIEW,
            input_tokens=100,
            output_tokens=50,
            ts_utc=week_start + timedelta(days=1),
        )
        # Outside window (too old)
        ledger.record(
            TaskCategory.CODE_REVIEW,
            input_tokens=100,
            output_tokens=50,
            ts_utc=week_start - timedelta(days=1),
        )
        report = weekly_report(ledger, week_start=week_start)
        assert report.n_events == 1


class TestRenderMarkdown:
    def test_header_and_bucket_sections_present(self):
        ledger = CostLedger()
        ledger.record(TaskCategory.RED_TEAM_SCORING, input_tokens=100, output_tokens=100)
        md = render_markdown(ledger.rollup())
        assert "# EVOLUTIONARY TRADING ALGO // JARVIS Cost Telemetry" in md
        assert "## Bucket summary" in md
        assert "## Per-category breakdown" in md
        assert "red_team_scoring" in md

    def test_empty_report_renders_placeholder(self):
        md = render_markdown(CostLedger().rollup())
        assert "_(no events)_" in md


class TestWriteReport:
    def test_writes_markdown(self, tmp_path: Path):
        ledger = CostLedger()
        ledger.record(TaskCategory.CODE_REVIEW, input_tokens=100, output_tokens=50)
        out = tmp_path / "cost.md"
        written = write_report(ledger.rollup(), out)
        assert written.exists()
        assert "JARVIS Cost Telemetry" in written.read_text()

    def test_writes_json_sidecar(self, tmp_path: Path):
        ledger = CostLedger()
        ledger.record(TaskCategory.RED_TEAM_SCORING, input_tokens=100, output_tokens=100)
        out = tmp_path / "cost.md"
        write_report(ledger.rollup(), out, also_json=True)
        payload = json.loads(out.with_suffix(".json").read_text())
        assert payload["n_events"] == 1
        assert any(b["bucket"] == "architectural" for b in payload["by_bucket"])
