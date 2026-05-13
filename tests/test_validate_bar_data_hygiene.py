"""Tests for ``scripts/validate_bar_data_hygiene.py``."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eta_engine.scripts import validate_bar_data_hygiene as mod

CSV_HEADER = ["time", "open", "high", "low", "close", "volume"]


def _write_csv(path: Path, rows: list[list[object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        for r in rows:
            w.writerow(r)


def _bars(
    start_ts: int,
    n: int,
    *,
    interval: int = 3600,
    base_close: float = 100.0,
    drift: float = 0.0,
) -> list[list[object]]:
    """Generate ``n`` boring bars with tiny drift; each row has good OHLCV."""

    out: list[list[object]] = []
    for i in range(n):
        ts = start_ts + i * interval
        c = base_close + drift * i
        o = c * 0.999
        h = c * 1.001
        low = c * 0.998
        out.append([ts, o, h, low, c, 1000.0])
    return out


def _step_bars_to(rows: list[list[object]], start_idx: int, new_close: float) -> None:
    """In-place: rebuild rows from ``start_idx`` so close == ``new_close``."""

    for i in range(start_idx, len(rows)):
        c = new_close
        rows[i][1] = c * 0.999  # open
        rows[i][2] = c * 1.001  # high
        rows[i][3] = c * 0.998  # low
        rows[i][4] = c  # close


# ---------------------------------------------------------------------------
# Clean files
# ---------------------------------------------------------------------------


def test_clean_csv_yields_no_issues_and_pass(tmp_path: Path) -> None:
    p = tmp_path / "FAKE_1h.csv"
    # Use Mon Jan 6 2025 12:00 UTC so the first 30 bars stay inside a single
    # weekday session and don't accidentally span the Sat 00:00 weekend window.
    start = int(datetime(2025, 1, 6, 12, 0, tzinfo=UTC).timestamp())
    _write_csv(p, _bars(start, 30))

    report = mod.scan_file(p)

    assert report.error is None
    assert report.issues == []
    assert report.summary["total_issues"] == 0
    assert mod.overall_status([report]) == "PASS"


# ---------------------------------------------------------------------------
# Adjacent-jump detection
# ---------------------------------------------------------------------------


def test_single_5pct_jump_flagged_as_warn(tmp_path: Path) -> None:
    p = tmp_path / "FAKE_1h.csv"
    start = int(datetime(2025, 1, 6, 12, 0, tzinfo=UTC).timestamp())
    rows = _bars(start, 10)
    # Bar 7 (and onward) closes at 108 -> ~8% jump >> default 5% threshold.
    _step_bars_to(rows, start_idx=7, new_close=108.0)
    _write_csv(p, rows)

    report = mod.scan_file(p, threshold_pct=5.0)

    jumps = [i for i in report.issues if i.type == "adjacent_jump"]
    assert len(jumps) == 1
    assert jumps[0].magnitude_pct is not None
    assert jumps[0].magnitude_pct > 5.0
    assert mod.overall_status([report]) == "WARN"


def test_crypto_threshold_higher_than_futures(tmp_path: Path) -> None:
    """A 7% jump is flagged as futures but not as crypto (default thresholds)."""

    rows = _bars(int(datetime(2025, 1, 6, 12, 0, tzinfo=UTC).timestamp()), 10)
    _step_bars_to(rows, start_idx=5, new_close=107.0)

    fut = tmp_path / "ES1_1h.csv"
    cry = tmp_path / "BTC_1h.csv"
    _write_csv(fut, rows)
    _write_csv(cry, rows)

    fr = mod.scan_file(fut)
    cr = mod.scan_file(cry)

    fut_jumps = [i for i in fr.issues if i.type == "adjacent_jump"]
    cry_jumps = [i for i in cr.issues if i.type == "adjacent_jump"]
    assert len(fut_jumps) == 1
    assert cry_jumps == []


# ---------------------------------------------------------------------------
# OHLC sanity
# ---------------------------------------------------------------------------


def test_low_greater_than_high_flagged(tmp_path: Path) -> None:
    p = tmp_path / "FAKE_1h.csv"
    start = int(datetime(2025, 1, 6, 12, 0, tzinfo=UTC).timestamp())
    rows = _bars(start, 5)
    # row 2 (idx 1): set low > high
    rows[1][3] = 200.0  # low
    rows[1][2] = 100.0  # high
    _write_csv(p, rows)

    report = mod.scan_file(p)

    invalids = [i for i in report.issues if i.type == "ohlc_invalid"]
    assert any("low" in i.detail and "high" in i.detail for i in invalids)
    assert mod.overall_status([report]) == "FAIL"


def test_es1_5m_low_anomaly_pattern(tmp_path: Path) -> None:
    """Replay the ES1_5m.csv anomaly: close ~3875, low=31.75 — classic bad row."""

    p = tmp_path / "ES1_5m.csv"
    start = int(datetime(2025, 1, 6, 12, 0, tzinfo=UTC).timestamp())
    rows = _bars(start, 5, base_close=3875.0, interval=300)
    rows[2][3] = 31.75  # low far below reasonable
    _write_csv(p, rows)

    report = mod.scan_file(p)

    types = {i.type for i in report.issues}
    assert "ohlc_invalid" in types


# ---------------------------------------------------------------------------
# Volume sanity
# ---------------------------------------------------------------------------


def test_negative_volume_flagged(tmp_path: Path) -> None:
    p = tmp_path / "FAKE_1h.csv"
    start = int(datetime(2025, 1, 6, 12, 0, tzinfo=UTC).timestamp())
    rows = _bars(start, 5)
    rows[2][5] = -5.0
    _write_csv(p, rows)

    report = mod.scan_file(p)

    vol = [i for i in report.issues if i.type == "volume_invalid"]
    assert len(vol) == 1


# ---------------------------------------------------------------------------
# Duplicate / out-of-order timestamps
# ---------------------------------------------------------------------------


def test_duplicate_timestamps_flagged(tmp_path: Path) -> None:
    p = tmp_path / "FAKE_1h.csv"
    start = int(datetime(2025, 1, 6, 12, 0, tzinfo=UTC).timestamp())
    rows = _bars(start, 5)
    # Make row 3 a duplicate of row 2.
    rows[3][0] = rows[2][0]
    _write_csv(p, rows)

    report = mod.scan_file(p)

    dups = [i for i in report.issues if i.type == "duplicate_timestamp"]
    assert len(dups) == 1
    assert mod.overall_status([report]) == "FAIL"


def test_out_of_order_timestamps_flagged(tmp_path: Path) -> None:
    p = tmp_path / "FAKE_1h.csv"
    start = int(datetime(2025, 1, 6, 12, 0, tzinfo=UTC).timestamp())
    rows = _bars(start, 5)
    # Swap row 2 and row 3 timestamps so row 3's ts < row 2's.
    rows[2][0], rows[3][0] = rows[3][0], rows[2][0]
    _write_csv(p, rows)

    report = mod.scan_file(p)

    ooo = [i for i in report.issues if i.type == "out_of_order_timestamp"]
    assert len(ooo) >= 1
    assert mod.overall_status([report]) == "FAIL"


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------


def test_large_gap_flagged_for_crypto(tmp_path: Path) -> None:
    p = tmp_path / "BTC_1h.csv"
    start = int(datetime(2025, 1, 6, 12, 0, tzinfo=UTC).timestamp())
    rows = _bars(start, 5)
    # Skip ahead 10h between bar 2 and bar 3.
    rows[3][0] = int(rows[2][0]) + 10 * 3600
    rows[4][0] = int(rows[3][0]) + 3600
    _write_csv(p, rows)

    report = mod.scan_file(p, gap_intervals=3)

    gaps = [i for i in report.issues if i.type == "gap"]
    assert len(gaps) >= 1


def test_weekend_gap_not_flagged_for_futures(tmp_path: Path) -> None:
    p = tmp_path / "ES1_1h.csv"
    # Friday 21:00 UTC -> Sunday 23:00 UTC is a normal CME weekend window.
    fri = datetime(2025, 1, 10, 21, 0, tzinfo=UTC)
    rows = [
        [int(fri.timestamp()), 100, 100.5, 99.5, 100.2, 1000.0],
        [int((fri + timedelta(hours=50)).timestamp()), 100.2, 100.5, 99.5, 100.4, 1000.0],
    ]
    _write_csv(p, rows)

    report = mod.scan_file(p, gap_intervals=3)

    gaps = [i for i in report.issues if i.type == "gap"]
    assert gaps == []


# ---------------------------------------------------------------------------
# Discovery (--all-csvs)
# ---------------------------------------------------------------------------


def test_discover_csvs_finds_files_recursively(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.csv").write_text("dummy", encoding="utf-8")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "y.csv").write_text("dummy", encoding="utf-8")
    (tmp_path / "b" / "z.txt").write_text("not csv", encoding="utf-8")

    found = mod.discover_csvs([tmp_path])

    names = sorted(p.name for p in found)
    assert names == ["x.csv", "y.csv"]


# ---------------------------------------------------------------------------
# JSON output schema
# ---------------------------------------------------------------------------


def test_main_writes_valid_json_schema(tmp_path: Path) -> None:
    p = tmp_path / "FAKE_1h.csv"
    start = int(datetime(2025, 1, 6, 12, 0, tzinfo=UTC).timestamp())
    _write_csv(p, _bars(start, 10))
    out = tmp_path / "report.json"

    rc = mod.main(
        [
            "--files",
            str(p),
            "--workspace",
            str(tmp_path),
            "--output",
            str(out),
            "--quiet",
        ]
    )

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert set(payload.keys()) == {"scanned_at", "overall", "files"}
    assert payload["overall"] == "PASS"
    file_entry = payload["files"][0]
    assert set(file_entry.keys()) == {"path", "rows", "error", "summary", "issues"}
    assert file_entry["rows"] == 10


def test_main_returns_nonzero_on_warn(tmp_path: Path) -> None:
    p = tmp_path / "FAKE_1h.csv"
    start = int(datetime(2025, 1, 6, 12, 0, tzinfo=UTC).timestamp())
    rows = _bars(start, 8)
    _step_bars_to(rows, start_idx=5, new_close=110.0)
    _write_csv(p, rows)

    rc = mod.main(
        [
            "--files",
            str(p),
            "--workspace",
            str(tmp_path),
            "--quiet",
        ]
    )

    assert rc in (1, 2)  # WARN or FAIL — at minimum non-zero


# ---------------------------------------------------------------------------
# Rollover heuristic
# ---------------------------------------------------------------------------


def test_rollover_candidate_marks_continuous_front_month_at_eom(tmp_path: Path) -> None:
    p = tmp_path / "NG1_1h.csv"
    # Late-January timestamp -> NG monthly roll window.
    start = int(datetime(2025, 1, 27, 12, 0, tzinfo=UTC).timestamp())
    rows = _bars(start, 10)
    _step_bars_to(rows, start_idx=5, new_close=110.0)
    _write_csv(p, rows)

    report = mod.scan_file(p, threshold_pct=5.0)

    jumps = [i for i in report.issues if i.type == "adjacent_jump"]
    assert len(jumps) == 1
    assert jumps[0].rollover_candidate is True
    assert report.summary["rollover_candidates"] >= 1
