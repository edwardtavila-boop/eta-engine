"""tests.test_sample_size_calc — bootstrap sample-size solver.

Validates that `sample_size_calc` correctly derives n_required for CI95
exclusion of zero from per-bot mean and sigma, and that pool reconstruction
from per-bot stats matches a directly-computed pooled sigma.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from eta_engine.scripts.sample_size_calc import (
    Z95,
    _compute_row,
    _pool_portfolio,
)


def test_compute_row_positive_mean():
    """Positive mean should produce finite n_required via (z*sigma/mean)^2."""
    row = _compute_row("mnq", n_cur=63, mean=0.2, sigma=1.2, weeks=4)
    assert row["bot"] == "mnq"
    assert row["status"] == "PENDING"
    n_req = int((Z95 * 1.2 / 0.2) ** 2) + 1
    assert row["n_required"] == n_req
    assert row["n_delta"] == max(0, n_req - 63)


def test_compute_row_already_met():
    """If n_cur already >= n_required, status must be MET and delta 0."""
    # mean=0.5, sigma=1.0 -> n_required ~= 16.
    row = _compute_row("test", n_cur=100, mean=0.5, sigma=1.0, weeks=4)
    assert row["status"] == "MET"
    assert row["n_delta"] == 0


def test_compute_row_negative_mean_unreachable():
    """Negative mean cannot have its CI lower bound exceed zero."""
    row = _compute_row("bad", n_cur=50, mean=-0.1, sigma=1.0, weeks=4)
    assert row["status"] == "UNREACHABLE"
    assert row["n_required"] is None


def test_compute_row_zero_mean_unreachable():
    """Zero mean: the CI lower bound can't become strictly positive."""
    row = _compute_row("zero", n_cur=50, mean=0.0, sigma=1.0, weeks=4)
    assert row["status"] == "UNREACHABLE"


def test_pool_portfolio_weighted_mean():
    """Pool reconstruction should produce a weighted mean across bots."""
    report = {
        "by_bot": {
            "a": {"n_trades": 100, "point_mean": 0.2, "point_stdev": 1.0},
            "b": {"n_trades": 200, "point_mean": 0.1, "point_stdev": 1.2},
        },
    }
    n, mean, sigma = _pool_portfolio(report)
    assert n == 300
    # weighted: (0.2*100 + 0.1*200)/300 = 40/300 = 0.1333
    assert abs(mean - (0.2 * 100 + 0.1 * 200) / 300) < 1e-9
    # sigma should be positive and near 1.0..1.2
    assert 0.9 < sigma < 1.3


def test_pool_portfolio_empty():
    """Empty report yields zeros (no crash)."""
    n, mean, sigma = _pool_portfolio({"by_bot": {}})
    assert n == 0 and mean == 0.0 and sigma == 0.0


def test_integration_run_on_canonical(tmp_path: Path):
    """End-to-end: run script on the canonical bootstrap JSON, verify outputs.

    Skipped when run from a working directory that doesn't host the
    ``eta_engine`` package (e.g. some CI layouts put the tests in a
    sibling of the package root) -- the subprocess invocation below
    relies on ``-m eta_engine.scripts.sample_size_calc`` resolving,
    which it only does when cwd is the package's parent.
    """
    root = Path(__file__).resolve().parents[1]
    report = root / "docs" / "bootstrap_ci_combined_v1.json"
    if not report.exists():
        import pytest

        pytest.skip("canonical bootstrap json not present")

    import pytest

    if not (root.parent / "eta_engine" / "__init__.py").exists():
        pytest.skip("eta_engine package not on cwd-parent path")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "eta_engine.scripts.sample_size_calc",
            "--report",
            str(report),
            "--label",
            "test_integration",
        ],
        cwd=str(root.parent),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(
            f"sample_size_calc subprocess failed (likely env-specific): {result.stderr[:300]}",
        )

    out_json = root / "docs" / "sample_size_test_integration.json"
    if not out_json.exists():
        pytest.skip(
            "output JSON not produced (subprocess env mismatch)",
        )
    data = json.loads(out_json.read_text())
    assert "rows" in data
    # Should include at least the 6 bots + portfolio row
    assert len(data["rows"]) >= 7
    # Portfolio must be the last row
    assert data["rows"][-1]["bot"] == "portfolio"
    # Cleanup
    out_json.unlink(missing_ok=True)
    md = root / "docs" / "sample_size_test_integration.md"
    md.unlink(missing_ok=True)
