"""Tests for data.library — local CSV catalog + schema detection."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eta_engine.data.library import (
    DataLibrary,
    DatasetMeta,
    _parse_filename,
)


# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("MNQ1_5m.csv", ("MNQ1", "5m", "history")),
        ("NQ1_4h.csv", ("NQ1", "4h", "history")),
        ("MNQ1_D.csv", ("MNQ1", "D", "history")),
        ("MNQ1_W.csv", ("MNQ1", "W", "history")),
        ("MNQ1_1s.csv", ("MNQ1", "1s", "history")),
        ("mnq_5m.csv", ("MNQ", "5m", "main")),
        ("mnq_1m.csv", ("MNQ", "1m", "main")),
        ("mnq_es1_5.csv", ("ES1", "5m", "main")),
        ("mnq_dxy_1.csv", ("DXY", "1m", "main")),
        ("mnq_vix_5.csv", ("VIX", "5m", "main")),
        ("mnq_tick_1.csv", ("TICK", "1m", "main")),
        ("README.txt", None),
        ("random.csv", None),
        ("mnq_5m.csv.bak", None),
    ],
)
def test_parse_filename(name: str, expected) -> None:  # type: ignore[no-untyped-def]
    assert _parse_filename(Path(name)) == expected


# ---------------------------------------------------------------------------
# Discovery + listing
# ---------------------------------------------------------------------------


def _write_main_csv(path: Path, rows: list[tuple[str, float, float, float, float, float]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp_utc", "epoch_s", "open", "high", "low", "close", "volume", "session"])
        for ts, o, h, low, c, v in rows:
            w.writerow([ts, "", o, h, low, c, v, "RTH"])


def _write_history_csv(path: Path, rows: list[tuple[int, float, float, float, float, float]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for ts, o, h, low, c, v in rows:
            w.writerow([ts, o, h, low, c, v])


@pytest.fixture()
def fake_roots(tmp_path: Path) -> tuple[Path, Path]:
    """Mimic C:\\mnq_data\\ + C:\\mnq_data\\history\\ with tiny CSVs."""
    main = tmp_path / "main"
    history = tmp_path / "history"
    main.mkdir()
    history.mkdir()

    # main shape: 3 rows of 5m MNQ
    _write_main_csv(
        main / "mnq_5m.csv",
        [
            ("2026-01-01T00:00:00Z", 100.0, 101.0, 99.0, 100.5, 1000.0),
            ("2026-01-01T00:05:00Z", 100.5, 101.5, 100.0, 101.0, 1500.0),
            ("2026-01-01T00:10:00Z", 101.0, 102.0, 100.5, 101.5, 1200.0),
        ],
    )
    # main shape: 2 rows of ES1 5m (correlated)
    _write_main_csv(
        main / "mnq_es1_5.csv",
        [
            ("2026-01-01T00:00:00Z", 5000.0, 5005.0, 4995.0, 5002.0, 800.0),
            ("2026-01-01T00:05:00Z", 5002.0, 5008.0, 5001.0, 5006.0, 900.0),
        ],
    )
    # history shape: 4 rows of 1h MNQ
    _write_history_csv(
        history / "MNQ1_1h.csv",
        [
            (1735689600, 100.0, 101.0, 99.0, 100.5, 10_000.0),
            (1735693200, 100.5, 101.5, 100.0, 101.0, 12_000.0),
            (1735696800, 101.0, 102.0, 100.5, 101.5, 11_000.0),
            (1735700400, 101.5, 102.5, 101.0, 102.0, 13_000.0),
        ],
    )
    # noise file we expect to ignore
    (main / "README.txt").write_text("nothing to see", encoding="utf-8")

    return main, history


def test_discover_finds_all_known_shapes(fake_roots) -> None:  # type: ignore[no-untyped-def]
    lib = DataLibrary(roots=fake_roots)
    keys = {d.key for d in lib.list()}
    assert keys == {"MNQ/5m/main", "ES1/5m/main", "MNQ1/1h/history"}


def test_discover_skips_unrecognised_files(fake_roots) -> None:  # type: ignore[no-untyped-def]
    lib = DataLibrary(roots=fake_roots)
    paths = [d.path.name for d in lib.list()]
    assert "README.txt" not in paths


def test_metadata_row_count_and_range(fake_roots) -> None:  # type: ignore[no-untyped-def]
    lib = DataLibrary(roots=fake_roots)
    mnq_5m = lib.get(symbol="MNQ", timeframe="5m")
    assert mnq_5m is not None
    assert mnq_5m.row_count == 3
    assert mnq_5m.start_ts == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert mnq_5m.end_ts == datetime(2026, 1, 1, 0, 10, tzinfo=UTC)


def test_history_metadata_uses_epoch_seconds(fake_roots) -> None:  # type: ignore[no-untyped-def]
    lib = DataLibrary(roots=fake_roots)
    mnq_1h = lib.get(symbol="MNQ1", timeframe="1h")
    assert mnq_1h is not None
    assert mnq_1h.row_count == 4
    assert mnq_1h.schema_kind == "history"


# ---------------------------------------------------------------------------
# Filtering + querying
# ---------------------------------------------------------------------------


def test_list_filters_by_symbol(fake_roots) -> None:  # type: ignore[no-untyped-def]
    lib = DataLibrary(roots=fake_roots)
    es = lib.list(symbol="ES1")
    assert len(es) == 1
    assert es[0].symbol == "ES1"


def test_list_filters_by_timeframe(fake_roots) -> None:  # type: ignore[no-untyped-def]
    lib = DataLibrary(roots=fake_roots)
    fm = lib.list(timeframe="5m")
    assert {d.symbol for d in fm} == {"MNQ", "ES1"}


def test_list_filters_by_schema_kind(fake_roots) -> None:  # type: ignore[no-untyped-def]
    lib = DataLibrary(roots=fake_roots)
    only_history = lib.list(schema_kind="history")
    assert len(only_history) == 1
    assert only_history[0].symbol == "MNQ1"


def test_get_returns_none_when_no_match(fake_roots) -> None:  # type: ignore[no-untyped-def]
    lib = DataLibrary(roots=fake_roots)
    assert lib.get(symbol="DOES_NOT_EXIST", timeframe="5m") is None


def test_symbols_and_timeframes(fake_roots) -> None:  # type: ignore[no-untyped-def]
    lib = DataLibrary(roots=fake_roots)
    assert lib.symbols() == ["ES1", "MNQ", "MNQ1"]
    assert "5m" in lib.timeframes()
    assert "1h" in lib.timeframes()


# ---------------------------------------------------------------------------
# Bar loading
# ---------------------------------------------------------------------------


def test_load_bars_main_schema(fake_roots) -> None:  # type: ignore[no-untyped-def]
    lib = DataLibrary(roots=fake_roots)
    ds = lib.get(symbol="MNQ", timeframe="5m")
    assert ds is not None
    bars = lib.load_bars(ds)
    assert len(bars) == 3
    assert bars[0].symbol == "MNQ"
    assert bars[0].close == pytest.approx(100.5)


def test_load_bars_history_schema(fake_roots) -> None:  # type: ignore[no-untyped-def]
    lib = DataLibrary(roots=fake_roots)
    ds = lib.get(symbol="MNQ1", timeframe="1h")
    assert ds is not None
    bars = lib.load_bars(ds)
    assert len(bars) == 4
    assert bars[0].symbol == "MNQ1"
    assert bars[-1].close == pytest.approx(102.0)


def test_load_bars_respects_limit(fake_roots) -> None:  # type: ignore[no-untyped-def]
    lib = DataLibrary(roots=fake_roots)
    ds = lib.get(symbol="MNQ1", timeframe="1h")
    assert ds is not None
    assert len(lib.load_bars(ds, limit=2)) == 2


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def test_summary_markdown_lists_every_dataset(fake_roots) -> None:  # type: ignore[no-untyped-def]
    lib = DataLibrary(roots=fake_roots)
    md = lib.summary_markdown()
    assert "MNQ" in md
    assert "ES1" in md
    assert "MNQ1" in md
    assert md.count("|") > 8  # has table rows


def test_summary_jarvis_payload_is_serialisable(fake_roots) -> None:  # type: ignore[no-untyped-def]
    import json

    lib = DataLibrary(roots=fake_roots)
    payload = lib.summary_jarvis_payload()
    s = json.dumps(payload)
    assert "MNQ" in s
    assert all("rows" in entry for entry in payload)


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_missing_root_does_not_crash(tmp_path: Path) -> None:
    lib = DataLibrary(roots=[tmp_path / "nonexistent"])
    assert lib.list() == []


def test_empty_csv_skipped_not_crashed(tmp_path: Path) -> None:
    main = tmp_path / "main"
    main.mkdir()
    (main / "mnq_5m.csv").write_text(
        "timestamp_utc,epoch_s,open,high,low,close,volume,session\n",
        encoding="utf-8",
    )
    lib = DataLibrary(roots=[main])
    assert lib.list() == []  # zero data rows -> no metadata, skip
