"""Portfolio correlation analyzer tests — P3_PROOF portfolio_corr.

Covers :func:`eta_engine.backtest.portfolio_correlation.analyze`:
* validation (empty dict, length mismatch, single bot)
* pair-key determinism (alphabetical)
* pairwise report fields (max/min, mean_offdiag, eff_n)
* redundancy flag firing above threshold
* effective-N collapses when all bots move together
"""

from __future__ import annotations

import numpy as np
import pytest

from eta_engine.backtest.portfolio_correlation import (
    PortfolioCorrelationReport,
    analyze,
    as_dict,
)

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_analyze_rejects_empty_dict() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        analyze({})


def test_analyze_rejects_length_mismatch() -> None:
    series = {
        "a": np.zeros(100),
        "b": np.zeros(50),
    }
    with pytest.raises(ValueError, match="length mismatch"):
        analyze(series)


def test_analyze_rejects_single_bot() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        analyze({"solo": np.zeros(100)})


# ---------------------------------------------------------------------------
# Pair key + baseline shape
# ---------------------------------------------------------------------------


def test_pair_keys_are_alphabetical_and_unique() -> None:
    rng = np.random.default_rng(0)
    series = {
        "mnq": rng.normal(0.0, 0.01, size=200),
        "eth": rng.normal(0.0, 0.01, size=200),
        "sol": rng.normal(0.0, 0.01, size=200),
    }
    report = analyze(series)
    # 3 bots → 3 unordered pairs
    assert len(report.pairwise_correlations) == 3
    for pair in report.pairwise_correlations:
        left, right = pair.split("~")
        assert left < right, f"pair {pair!r} not alphabetical"


def test_report_fields_populated() -> None:
    rng = np.random.default_rng(1)
    series = {
        "a": rng.normal(0.0, 0.01, size=200),
        "b": rng.normal(0.0, 0.01, size=200),
    }
    report = analyze(series)
    assert isinstance(report, PortfolioCorrelationReport)
    assert report.bot_names == ["a", "b"]
    assert report.sample_count == 200
    assert report.max_pair == "a~b"
    assert report.min_pair == "a~b"
    # Mean off-diag for 1 pair = that pair's r
    assert report.mean_offdiag == pytest.approx(report.pairwise_correlations["a~b"])


# ---------------------------------------------------------------------------
# Redundancy & effective-N
# ---------------------------------------------------------------------------


def test_redundant_pair_flag_fires_above_threshold() -> None:
    rng = np.random.default_rng(3)
    base = rng.normal(0.0, 0.01, size=300)
    series = {
        "twin_a": base,
        "twin_b": base + rng.normal(0.0, 1e-6, size=300),  # near-identical
        "uncorr": rng.normal(0.0, 0.01, size=300),
    }
    report = analyze(series, high_corr_threshold=0.80)
    assert report.worst_redundant_pair == "twin_a~twin_b"
    assert any(f.startswith("redundant_pair:") for f in report.flags)


def test_no_redundancy_flag_when_all_low_corr() -> None:
    rng = np.random.default_rng(5)
    series = {
        "a": rng.normal(0.0, 0.01, size=400),
        "b": rng.normal(0.0, 0.01, size=400),
        "c": rng.normal(0.0, 0.01, size=400),
    }
    report = analyze(series, high_corr_threshold=0.80)
    assert report.worst_redundant_pair is None
    assert not any(f.startswith("redundant_pair:") for f in report.flags)


def test_effective_n_collapses_when_all_identical() -> None:
    base = np.linspace(-1.0, 1.0, 200)
    series = {f"bot_{i}": base + np.random.default_rng(i).normal(0.0, 1e-8, size=200) for i in range(4)}
    report = analyze(series)
    # Near-perfect correlation → eff_n should drop close to 1.0 (not N=4)
    assert report.eff_n_bots < 1.5
    assert "low_effective_n" in " ".join(report.flags)


def test_effective_n_near_n_when_independent() -> None:
    rng = np.random.default_rng(17)
    series = {f"bot_{i}": rng.normal(0.0, 0.01, size=500) for i in range(4)}
    report = analyze(series)
    # Independent returns should give eff_n close to 4
    assert report.eff_n_bots > 3.0


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_as_dict_returns_json_safe() -> None:
    rng = np.random.default_rng(19)
    series = {"a": rng.normal(0.0, 0.01, 100), "b": rng.normal(0.0, 0.01, 100)}
    report = analyze(series)
    payload = as_dict(report)
    assert payload["bot_names"] == ["a", "b"]
    assert payload["sample_count"] == 100
    assert "pairwise_correlations" in payload
    assert "eff_n_bots" in payload
