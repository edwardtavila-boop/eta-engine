"""Unit tests for scripts.run_cross_regime_validation.

Covers the regime-selectivity gate logic in isolation (no engine run
required) plus one full end-to-end smoke test that exercises the
whole script and confirms the artifacts land.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from eta_engine.scripts import run_cross_regime_validation as rcr

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Synthetic per-regime bodies: shape matches what _apply_gate expects.
# ---------------------------------------------------------------------------


def _regime_body(
    *,
    is_exp: float = 0.5,
    is_trades: int = 40,
    oos_exp: float = 0.5,
    oos_trades: int = 25,
) -> dict:
    return {
        "expected_label": "TRENDING",
        "classifier_label_for_axes": "TRENDING",
        "label_axes_agree": True,
        "is": {
            "trades": is_trades,
            "expectancy_r": is_exp,
            "win_rate": 0.6,
            "profit_factor": 2.0,
            "sharpe": 1.5,
            "max_dd_pct": 5.0,
            "total_return_pct": 15.0,
        },
        "oos": {
            "trades": oos_trades,
            "expectancy_r": oos_exp,
            "win_rate": 0.55,
            "profit_factor": 1.8,
            "sharpe": 1.2,
            "max_dd_pct": 6.0,
            "total_return_pct": 10.0,
        },
        "degradation_r": rcr._degradation(is_exp, oos_exp),
    }


# ---------------------------------------------------------------------------
# Gate unit tests
# ---------------------------------------------------------------------------


def test_gate_passes_with_one_robust_regime() -> None:
    per = {
        "TRENDING": _regime_body(
            is_exp=1.0,
            is_trades=50,
            oos_exp=1.0,
            oos_trades=25,
        ),
        "RANGING": _regime_body(
            is_exp=0.05,
            is_trades=30,
            oos_exp=0.04,
            oos_trades=12,
        ),
    }
    passed, gate = rcr._apply_gate(per)
    assert passed is True
    assert gate["live_tradeable_regimes"] == ["TRENDING"]
    assert gate["overfit_red_flags"] == []


def test_gate_fails_when_no_regime_meets_bar() -> None:
    per = {
        "RANGING": _regime_body(
            is_exp=0.05,
            is_trades=30,
            oos_exp=0.05,
            oos_trades=10,
        ),
        "LOW_VOL": _regime_body(
            is_exp=0.10,
            is_trades=30,
            oos_exp=0.08,
            oos_trades=12,
        ),
    }
    passed, gate = rcr._apply_gate(per)
    assert passed is False
    assert gate["any_live_tradeable"] is False
    assert "no regime cleared live-trade gate" in gate["reasons"][0]


def test_gate_fails_on_sign_flip_overfit() -> None:
    per = {
        "TRENDING": _regime_body(
            is_exp=1.0,
            is_trades=50,
            oos_exp=1.0,
            oos_trades=25,
        ),
        "HIGH_VOL": _regime_body(
            is_exp=0.30,
            is_trades=40,
            oos_exp=-0.40,
            oos_trades=15,
        ),
    }
    passed, gate = rcr._apply_gate(per)
    # Even though TRENDING is robust, HIGH_VOL sign-flip = overfit red flag
    assert passed is False
    assert gate["no_overfit_collapse"] is False
    assert any("HIGH_VOL" in r for r in gate["overfit_red_flags"])


def test_gate_tolerates_selectivity_without_sign_flip() -> None:
    # RANGING edge weakens OOS but stays positive -- not overfit, just
    # regime-selective. Shouldn't red-flag.
    per = {
        "TRENDING": _regime_body(
            is_exp=1.0,
            is_trades=50,
            oos_exp=1.0,
            oos_trades=25,
        ),
        "RANGING": _regime_body(
            is_exp=0.40,
            is_trades=40,
            oos_exp=0.05,
            oos_trades=15,
        ),
    }
    passed, gate = rcr._apply_gate(per)
    assert passed is True
    assert gate["overfit_red_flags"] == []


def test_gate_reports_non_tradeable_reasons_explicitly() -> None:
    per = {
        "TRENDING": _regime_body(
            is_exp=1.0,
            is_trades=50,
            oos_exp=1.0,
            oos_trades=25,
        ),
        "RANGING": _regime_body(
            is_exp=0.30,
            is_trades=40,
            oos_exp=0.05,
            oos_trades=12,
        ),
    }
    _, gate = rcr._apply_gate(per)
    ranging_entry = next(
        (e for e in gate["non_tradeable_regimes"] if e["regime"] == "RANGING"),
        None,
    )
    assert ranging_entry is not None
    joined = "; ".join(ranging_entry["reasons"])
    # Should mention both the expectancy gate AND the min-trades gate
    assert "OOS exp" in joined
    assert "OOS trades" in joined


# ---------------------------------------------------------------------------
# Helpers: _degradation, _split_bars
# ---------------------------------------------------------------------------


def test_degradation_positive_when_oos_worse() -> None:
    assert rcr._degradation(1.0, 0.5) == 0.5


def test_degradation_negative_when_oos_better() -> None:
    assert rcr._degradation(0.5, 1.0) == -1.0


def test_degradation_handles_near_zero_is() -> None:
    # |IS| < 1e-9 -> special cases
    assert rcr._degradation(0.0, 0.0) == 0.0
    assert rcr._degradation(0.0, 1.0) == -9.99


def test_split_bars_70_30() -> None:
    bars = list(range(100))  # use ints as stand-in for BarData
    is_bars, oos_bars = rcr._split_bars(bars, is_frac=0.70)  # type: ignore[arg-type]
    assert len(is_bars) == 70
    assert len(oos_bars) == 30
    assert is_bars[-1] == 69
    assert oos_bars[0] == 70


# ---------------------------------------------------------------------------
# Regime spec sanity: each declared axes object should classify to the
# expected label so the harness doesn't silently test the wrong regime.
# ---------------------------------------------------------------------------


def test_regime_specs_axes_classify_correctly() -> None:
    from eta_engine.brain.regime import classify_regime  # noqa: PLC0415

    for spec in rcr._specs():
        actual = classify_regime(spec.axes)
        assert actual == spec.expected_label, (
            f"{spec.name} axes classify as {actual.value}, expected {spec.expected_label.value}"
        )


# ---------------------------------------------------------------------------
# Smoke: run the whole script and confirm artifacts exist + JSON is valid.
# Takes ~15s so kept to a single integration-grade test.
# ---------------------------------------------------------------------------


@pytest.mark.slow()
def test_full_run_writes_artifacts_and_exits_cleanly(tmp_path: Path) -> None:
    # Run the script; it writes into eta_engine/docs/cross_regime/
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "eta_engine.scripts.run_cross_regime_validation",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(_repo_root()),
    )
    # Exit 0 or 2 (PASS or gate FAIL) both indicate the harness RAN
    # correctly. Exit 3 would mean internal error.
    assert result.returncode in (0, 2), f"returncode={result.returncode}, stderr={result.stderr[:500]}"
    assert "cross-regime validation:" in result.stdout

    # Artifacts landed under eta_engine/docs/cross_regime/
    artifacts = _repo_root() / "eta_engine" / "docs" / "cross_regime"
    json_path = artifacts / "cross_regime_validation.json"
    md_path = artifacts / "cross_regime_validation.md"
    assert json_path.exists()
    assert md_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["spec_id"] == "CROSS_REGIME_OOS_v1"
    assert set(payload["per_regime"].keys()) == {
        "TRENDING",
        "RANGING",
        "HIGH_VOL",
        "LOW_VOL",
    }
    # Every regime must report both IS and OOS summaries
    for rg, body in payload["per_regime"].items():
        assert body["is"]["trades"] >= 0, rg
        assert body["oos"]["trades"] >= 0, rg
        assert "expectancy_r" in body["is"]
        assert "expectancy_r" in body["oos"]


def _repo_root() -> Path:
    """Return the Base/ directory (parent of eta_engine/)."""
    from pathlib import Path  # noqa: PLC0415

    return Path(__file__).resolve().parents[2]
