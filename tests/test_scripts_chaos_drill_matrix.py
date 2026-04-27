"""Tests for scripts._chaos_drill_matrix."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from eta_engine.scripts._chaos_drill_matrix import (
    SURFACES,
    SafetySurface,
    coverage_report,
    main,
    render_markdown,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestSafetySurface:
    def test_has_drill_true_when_id_set(self):
        s = SafetySurface(surface="x", module_path="eta_engine.x", drill_id="d")
        assert s.has_drill is True

    def test_has_drill_false_when_none(self):
        s = SafetySurface(surface="x", module_path="eta_engine.x", drill_id=None)
        assert s.has_drill is False


class TestCoverageReport:
    def test_counts_correct(self):
        sample = (
            SafetySurface("a", "m.a", "d1"),
            SafetySurface("b", "m.b", None),
            SafetySurface("c", "m.c", "d2"),
        )
        r = coverage_report(sample)
        assert r.total == 3
        assert r.covered == 2
        assert r.missing == ("b",)
        assert r.coverage_pct == pytest.approx(66.7, abs=0.2)

    def test_full_coverage(self):
        sample = (
            SafetySurface("a", "m.a", "d1"),
            SafetySurface("b", "m.b", "d2"),
        )
        r = coverage_report(sample)
        assert r.coverage_pct == 100.0
        assert r.missing == ()

    def test_zero_coverage(self):
        sample = (SafetySurface("a", "m.a", None),)
        r = coverage_report(sample)
        assert r.coverage_pct == 0.0
        assert r.missing == ("a",)


class TestRenderMarkdown:
    def test_includes_table_header(self):
        r = coverage_report(SURFACES[:2])
        md = render_markdown(r)
        assert "# EVOLUTIONARY TRADING ALGO // Chaos Drill Coverage Matrix" in md
        assert "| Surface | Module | Drill | Status | Notes |" in md

    def test_includes_coverage_line(self):
        r = coverage_report(SURFACES[:2])
        md = render_markdown(r)
        assert "Coverage:" in md

    def test_missing_section_when_gaps(self):
        sample = (SafetySurface("gap_one", "m.gap", None),)
        r = coverage_report(sample)
        md = render_markdown(r)
        assert "## Missing drills" in md
        assert "gap_one" in md


class TestSurfacesConstant:
    def test_covers_all_existing_drills(self):
        drill_ids = {s.drill_id for s in SURFACES if s.drill_id is not None}
        assert {"breaker", "deadman", "push", "drift"}.issubset(drill_ids)

    def test_includes_new_critical_surfaces(self):
        names = {s.surface for s in SURFACES}
        for surface in (
            "kill_switch_runtime",
            "risk_engine",
            "firm_gate",
            "order_state_reconcile",
        ):
            assert surface in names


class TestMainCli:
    def test_writes_markdown_report(self, tmp_path: Path):
        out = tmp_path / "coverage.md"
        rc = main(["--output", str(out)])
        assert rc == 0
        assert out.exists()
        assert "Chaos Drill Coverage Matrix" in out.read_text()

    def test_writes_json_sidecar_when_flag(self, tmp_path: Path):
        out = tmp_path / "coverage.md"
        rc = main(["--output", str(out), "--json"])
        assert rc == 0
        json_out = out.with_suffix(".json")
        assert json_out.exists()
        payload = json.loads(json_out.read_text())
        assert "coverage_pct" in payload
        assert "details" in payload

    def test_fail_under_threshold(self, tmp_path: Path):
        # v0.1.56 CHAOS DRILL CLOSURE: coverage is now 100%, so --fail-under
        # above the current percentage (e.g., 100.5%) is what should exit 1.
        out = tmp_path / "coverage.md"
        rc = main(["--output", str(out), "--fail-under", "100.5"])
        assert rc == 1

    def test_fail_under_at_hundred_passes(self, tmp_path: Path):
        # Post-closure: every surface has a drill, so --fail-under 100 is met.
        out = tmp_path / "coverage.md"
        rc = main(["--output", str(out), "--fail-under", "100"])
        assert rc == 0

    def test_post_closure_full_coverage(self):
        r = coverage_report()
        assert r.covered == r.total, f"gaps remain: {r.missing}"
        assert r.coverage_pct == 100.0
        assert r.missing == ()
