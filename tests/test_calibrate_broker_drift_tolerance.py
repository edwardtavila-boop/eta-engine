"""Tests for ``scripts/calibrate_broker_drift_tolerance.py``.

H1 closure (Red Team v0.1.64 review): the calibrator consumes a
runtime log JSONL file and emits a recommended pair of asymmetric
tolerance values for ``BrokerEquityReconciler``.

Covers:
  * percentile math (interpolation between adjacent ranks)
  * directional partitioning (drift_usd > 0 -> below; < 0 -> above)
  * recommendation defaults (above_slack=2x, percentile=0.99)
  * empty / sparse log handling
  * non-tick / non-broker-equity entries skipped
  * malformed JSON lines tolerated
  * exit codes (0 ok, 2 missing log, 3 no samples)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure scripts/ is importable as a package.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.calibrate_broker_drift_tolerance import (  # noqa: E402
    DriftSamples,
    _percentile,
    _summarize,
    collect,
    main,
    recommend,
)

# ---------------------------------------------------------------------------
# Pure-math helpers
# ---------------------------------------------------------------------------


class TestPercentile:
    def test_empty_returns_zero(self):
        assert _percentile([], 0.5) == 0.0

    def test_single_returns_value(self):
        assert _percentile([42.0], 0.99) == 42.0

    def test_p0_is_min_p1_is_max(self):
        v = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _percentile(v, 0.0) == 1.0
        assert _percentile(v, 1.0) == 5.0

    def test_p50_of_odd_length_is_middle(self):
        v = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _percentile(v, 0.5) == pytest.approx(3.0)

    def test_interpolates_between_ranks(self):
        # [10, 20, 30, 40] -- p25 falls between rank 0 (10) and rank 1
        # (20). rank = 0.25 * 3 = 0.75. lo=0, hi=1, frac=0.75 -> 17.5.
        v = [10.0, 20.0, 30.0, 40.0]
        assert _percentile(v, 0.25) == pytest.approx(17.5)


class TestSummarize:
    def test_empty(self):
        s = _summarize([])
        assert s.n == 0
        assert s.p99 == 0.0

    def test_single(self):
        s = _summarize([7.0])
        assert s.n == 1
        assert s.p50 == 7.0
        assert s.p99 == 7.0
        assert s.max_ == 7.0


# ---------------------------------------------------------------------------
# collect() -- partitions log entries by drift sign
# ---------------------------------------------------------------------------


def _write_log(tmp_path: Path, lines: list[dict]) -> Path:
    p = tmp_path / "rt.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return p


def _tick(drift_usd: float | None, drift_pct: float | None = None) -> dict:
    return {
        "kind": "tick",
        "meta": {
            "broker_equity": {
                "reason": "within_tolerance",
                "in_tolerance": True,
                "drift_usd": drift_usd,
                "drift_pct_of_logical": drift_pct,
            },
        },
    }


class TestCollect:
    def test_partitions_by_sign(self, tmp_path: Path):
        log = _write_log(
            tmp_path,
            [
                _tick(50.0, 0.001),  # below: broker is $50 below logical
                _tick(-30.0, 0.0006),  # above: broker is $30 above logical
                _tick(75.0, 0.0015),  # below
                _tick(-10.0, 0.0002),  # above
            ],
        )
        below, above = collect(log)
        assert below.n == 2
        assert above.n == 2
        # Stored as absolute values for easier downstream stats
        assert below.usd == [50.0, 75.0]
        assert above.usd == [30.0, 10.0]

    def test_skips_non_tick_entries(self, tmp_path: Path):
        log = _write_log(
            tmp_path,
            [
                {"kind": "runtime_start"},
                {"kind": "boot_refused"},
                _tick(100.0, 0.002),
                {"kind": "no_active_bots"},
            ],
        )
        below, above = collect(log)
        assert below.n == 1
        assert above.n == 0

    def test_skips_no_data_ticks(self, tmp_path: Path):
        log = _write_log(
            tmp_path,
            [
                _tick(None),  # no_broker_data
                _tick(50.0),
                _tick(None),
            ],
        )
        below, above = collect(log)
        assert below.n == 1

    def test_skips_zero_drift_ticks(self, tmp_path: Path):
        # Exact-zero drift contributes no signal in either direction.
        log = _write_log(
            tmp_path,
            [
                _tick(0.0, 0.0),
                _tick(50.0),
            ],
        )
        below, above = collect(log)
        assert below.n == 1
        assert above.n == 0

    def test_tolerates_malformed_json_lines(self, tmp_path: Path):
        p = tmp_path / "rt.jsonl"
        good = json.dumps(_tick(50.0, 0.001))
        with p.open("w", encoding="utf-8") as fh:
            fh.write(good + "\n")
            fh.write("{ this is not valid json\n")
            fh.write(good + "\n")
        below, above = collect(p)
        assert below.n == 2  # Two valid lines; the malformed one is skipped

    def test_handles_missing_broker_equity_block(self, tmp_path: Path):
        log = _write_log(
            tmp_path,
            [
                {"kind": "tick", "meta": {}},  # tick without broker_equity
                {"kind": "tick"},  # tick without meta
                _tick(50.0),
            ],
        )
        below, above = collect(log)
        assert below.n == 1


# ---------------------------------------------------------------------------
# recommend() -- shape of the produced tolerance dict
# ---------------------------------------------------------------------------


class TestRecommend:
    def test_returns_p99_for_below_default(self):
        below = DriftSamples(
            "below",
            n=100,
            usd=[float(i) for i in range(1, 101)],
            pct=[i / 100_000 for i in range(1, 101)],
        )
        above = DriftSamples("above", n=0, usd=[], pct=[])
        rec = recommend(below, above, percentile=0.99, above_slack=2.0)
        # p99 of [1..100] is the value at rank 0.99 * 99 = 98.01.
        # That's 1% of the way from rank-98 (=99.0) to rank-99 (=100.0)
        # under linear interpolation -> 99.01.
        assert rec["tolerance_below_usd"] == pytest.approx(99.01)
        # No above samples -> None
        assert rec["tolerance_above_usd"] is None
        assert rec["tolerance_above_pct"] is None

    def test_above_slack_doubles_above_threshold(self):
        below = DriftSamples("below", n=0, usd=[], pct=[])
        above = DriftSamples(
            "above",
            n=10,
            usd=[10.0] * 10,
            pct=[0.001] * 10,
        )
        rec = recommend(below, above, percentile=0.99, above_slack=2.0)
        # All samples are 10.0 -> p99 = 10.0 -> recommendation = 20.0
        assert rec["tolerance_above_usd"] == pytest.approx(20.0)

    def test_empty_inputs_returns_all_none(self):
        below = DriftSamples("below", n=0, usd=[], pct=[])
        above = DriftSamples("above", n=0, usd=[], pct=[])
        rec = recommend(below, above)
        assert all(v is None for v in rec.values())


# ---------------------------------------------------------------------------
# main() -- CLI integration + exit codes
# ---------------------------------------------------------------------------


class TestMain:
    def test_missing_log_returns_2(self, tmp_path: Path):
        rc = main(["--log", str(tmp_path / "does_not_exist.jsonl")])
        assert rc == 2

    def test_no_samples_returns_3(self, tmp_path: Path):
        # File exists but has no broker_equity ticks -- can't calibrate.
        log = _write_log(
            tmp_path,
            [
                {"kind": "runtime_start"},
                _tick(None),  # no_broker_data
            ],
        )
        rc = main(["--log", str(log)])
        assert rc == 3

    def test_with_samples_returns_0(self, tmp_path: Path, capsys):
        log = _write_log(
            tmp_path,
            [
                _tick(50.0, 0.001),
                _tick(-30.0, 0.0006),
            ],
        )
        rc = main(["--log", str(log)])
        assert rc == 0
        captured = capsys.readouterr()
        assert "RECOMMENDED TOLERANCES" in captured.out
        assert "below" in captured.out.lower()

    def test_json_mode_emits_machine_dict(self, tmp_path: Path, capsys):
        log = _write_log(
            tmp_path,
            [
                _tick(50.0, 0.001),
                _tick(-30.0, 0.0006),
            ],
        )
        rc = main(["--log", str(log), "--json"])
        assert rc == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert "recommendation" in payload
        assert payload["samples"]["below_n"] == 1
        assert payload["samples"]["above_n"] == 1

    def test_invalid_percentile_rejected(self, tmp_path: Path, capsys):
        log = _write_log(tmp_path, [_tick(50.0, 0.001)])
        rc = main(["--log", str(log), "--percentile", "1.5"])
        assert rc == 2

    def test_invalid_above_slack_rejected(self, tmp_path: Path, capsys):
        log = _write_log(tmp_path, [_tick(50.0, 0.001)])
        rc = main(["--log", str(log), "--above-slack", "-1"])
        assert rc == 2
