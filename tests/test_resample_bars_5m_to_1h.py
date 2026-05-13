"""Tests for the 5m -> 1h resample script.

Why these exist
---------------
The 2026-05-07 audit revealed that ``mbt_sweep_reclaim``, ``met_sweep_reclaim``,
and ``mbt_overnight_gap`` were returning zero trades because their bots are
configured ``timeframe="1h"`` but only 5m bar files existed. The
``resample_bars_5m_to_1h`` script fills that gap. These tests pin down the
OHLCV aggregation math so a future "speed up the resample" rewrite can't
silently corrupt the historical bars feed.

The first iteration of the script had a tz-aware DatetimeIndex bug that
silently produced a "time" column of all-zeros (epoch 1970-01-01) -- the
test below for ``time-roundtrip`` catches exactly that regression.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pd = pytest.importorskip("pandas")

from eta_engine.scripts import resample_bars_5m_to_1h as resample_mod  # noqa: E402


def _write_5m_csv(path: Path, rows: list[tuple[int, float, float, float, float, float]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        w.writerows(rows)


def _patch_history_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(resample_mod, "HISTORY_DIR", tmp_path)


def test_resample_aggregates_one_full_hour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """12 5m bars covering an aligned hour produce one 1h bar at the right edge.

    Right-closed/right-labelled per IBKR convention -- a bar labelled 00:00
    contains trades from 23:00 (excl) to 00:00 (incl). To keep the test
    self-contained the input bars are aligned to an hour boundary so they
    fall into exactly one output bucket.
    """
    _patch_history_dir(monkeypatch, tmp_path)

    # Hour-aligned base (1_700_002_800 = 2023-11-14 22:20:00 UTC, exact
    # multiple of 3600). Bars at base+300, base+600, ..., base+3600 fall
    # cleanly into the right-closed bucket (base, base+3600] -> 1 bar.
    base = 1_700_002_800  # hour boundary
    rows = []
    for i in range(12):
        t = base + (i + 1) * 300  # 5m steps inside the hour
        # open=10+i, high=20+i, low=5+i, close=15+i, volume=100+i
        rows.append((t, 10 + i, 20 + i, 5 + i, 15 + i, 100 + i))
    _write_5m_csv(tmp_path / "TEST1_5m.csv", rows)

    out_rows = resample_mod.resample_one("TEST")
    assert out_rows == 1, "12 hour-aligned 5m bars should fold into exactly one 1h bar"

    out = pd.read_csv(tmp_path / "TEST1_1h.csv")
    assert len(out) == 1
    bar = out.iloc[0]
    assert int(bar["time"]) == base + 12 * 300, "time should label the right edge of the hour"
    assert bar["open"] == 10, "open == first 5m open"
    assert bar["close"] == 15 + 11, "close == last 5m close"
    assert bar["high"] == 20 + 11, "high == max(highs)"
    assert bar["low"] == 5, "low == min(lows)"
    assert bar["volume"] == sum(100 + i for i in range(12)), "volume == sum"


def test_resample_time_column_is_unix_epoch_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the first iteration silently emitted time=0 (1970-01-01)
    because of a tz-aware DatetimeIndex precision mismatch on resample."""
    _patch_history_dir(monkeypatch, tmp_path)

    # Two full hours worth of 5m bars starting at a known epoch.
    base = 1_729_461_900  # 2024-10-20 22:05:00 UTC
    rows = [(base + i * 300, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(24)]
    _write_5m_csv(tmp_path / "TEST1_5m.csv", rows)

    resample_mod.resample_one("TEST")
    out = pd.read_csv(tmp_path / "TEST1_1h.csv")

    # Every output time must be a positive epoch in the 21st century, NOT
    # zero / 1970. The previous bug produced all-zero timestamps.
    assert (out["time"] > 1_500_000_000).all(), f"time column produced sub-2017 timestamps: {out['time'].tolist()[:3]}"
    assert (out["time"] < 1_900_000_000).all()
    # First output bar: right-edge of the hour containing the first bar.
    # First 5m bar at 22:05 UTC -> first hour right-edge is 23:00 UTC.
    expected_first = 1_729_465_200  # 2024-10-20 23:00:00 UTC
    assert int(out["time"].iloc[0]) == expected_first


def test_resample_skips_missing_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing source -> log + return 0; do not write an empty/garbage file."""
    _patch_history_dir(monkeypatch, tmp_path)

    out = resample_mod.resample_one("DOES_NOT_EXIST")
    assert out == 0
    assert not (tmp_path / "DOES_NOT_EXIST1_1h.csv").exists()


def test_resample_dry_run_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--dry-run path returns the row count but skips file creation."""
    _patch_history_dir(monkeypatch, tmp_path)

    base = 1_700_002_800  # hour boundary
    rows = [(base + (i + 1) * 300, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(12)]
    _write_5m_csv(tmp_path / "TEST1_5m.csv", rows)

    out = resample_mod.resample_one("TEST", dry_run=True)
    assert out == 1
    assert not (tmp_path / "TEST1_1h.csv").exists(), "dry-run must not write the output file"
