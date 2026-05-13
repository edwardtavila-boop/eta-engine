"""Tests for ``eta_engine.scripts.fetch_mbt_met_bars``.

Mocks the HTTP layer (``_http_get_json``) so no network is touched.
Verifies CLI parsing, conid resolution, chunk planning, CSV format
compatibility with ``feeds.strategy_lab.engine._load_ohlcv``, and the
end-to-end ``run()`` path on a synthetic gateway response.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eta_engine.scripts import fetch_mbt_met_bars as mod


def test_module_imports_cleanly() -> None:
    """Smoke: module imports and exposes the expected public surface."""
    assert hasattr(mod, "build_parser")
    assert hasattr(mod, "fetch_bars")
    assert hasattr(mod, "resolve_front_month_conid")
    assert hasattr(mod, "canonical_bar_path")
    assert hasattr(mod, "plan_chunks")
    assert hasattr(mod, "run")


def test_parser_defaults_match_spec() -> None:
    """CLI defaults: --symbols MBT MET, --days 540, --timeframe 5m."""
    parser = mod.build_parser()
    args = parser.parse_args([])
    assert args.symbols == ["MBT", "MET"]
    assert args.days == 540
    assert args.timeframe == "5m"
    assert args.dry_run is False
    assert args.no_merge is False


def test_parser_accepts_symbols_and_days_flags() -> None:
    parser = mod.build_parser()
    args = parser.parse_args(["--symbols", "MBT", "--days", "365"])
    assert args.symbols == ["MBT"]
    assert args.days == 365


def test_parser_rejects_unsupported_symbol() -> None:
    parser = mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--symbols", "ZB"])


def test_canonical_bar_path_matches_lab_harness_pattern() -> None:
    """Path must match feeds.strategy_lab.engine._resolve_bar_path which
    looks for ``{SYMBOL}1_{TF}.csv`` under MNQ_HISTORY_ROOT."""
    p = mod.canonical_bar_path("MBT", "5m")
    assert p.name == "MBT1_5m.csv"
    assert p.parent.name == "history"

    p = mod.canonical_bar_path("MET", "1d")
    # Daily files use 'D' (without leading digit) per DataLibrary regex.
    assert p.name == "MET1_D.csv"


def test_plan_chunks_walks_window_backwards() -> None:
    end = datetime(2026, 5, 1, tzinfo=UTC)
    start = end - timedelta(days=30)
    plan = mod.plan_chunks(timeframe="5m", start=start, end=end)
    # 30 days * 1440 minutes = 43200 minutes / 5 = 8640 bars.
    # Chunk limit ≈ 900 bars; expect ≥ 9 chunks.
    assert len(plan) >= 9
    # First cursor is the end; subsequent cursors strictly earlier.
    assert plan[0][1] == end
    cursors = [c for _, c in plan]
    assert all(a > b for a, b in zip(cursors, cursors[1:], strict=False))


def test_plan_chunks_5m_540_days_ballpark() -> None:
    """Sanity-check chunk count for the operator-runbook scenario."""
    end = datetime(2026, 5, 1, tzinfo=UTC)
    start = end - timedelta(days=540)
    plan = mod.plan_chunks(timeframe="5m", start=start, end=end)
    # 540d * 288 bars/day = 155,520 bars / 900 = ~173 chunks.
    assert 150 <= len(plan) <= 200


def test_resolve_front_month_conid_picks_earliest_future_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Picks the earliest non-expired contract from /trsrv/futures."""
    captured: dict[str, str] = {}

    def fake_get(url: str, *, timeout: float = 15.0) -> dict[str, Any]:
        captured["url"] = url
        return {
            "MBT": [
                # Already expired
                {"conid": 111, "expirationDate": "20260301"},
                # Front month (next future expiry after 2026-05-07)
                {"conid": 222, "expirationDate": "20260626"},
                # Back month
                {"conid": 333, "expirationDate": "20260925"},
            ],
        }

    monkeypatch.setattr(mod, "_http_get_json", fake_get)
    conid = mod.resolve_front_month_conid("MBT", base_url="https://x/v1/api")
    assert conid == 222
    assert "symbols=MBT" in captured["url"]
    assert "exchange=CME" in captured["url"]


def test_resolve_front_month_conid_handles_empty_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "_http_get_json", lambda *_a, **_k: None)
    assert mod.resolve_front_month_conid("MBT") is None
    monkeypatch.setattr(mod, "_http_get_json", lambda *_a, **_k: {"MBT": []})
    assert mod.resolve_front_month_conid("MBT") is None


def test_normalize_rows_converts_ms_to_seconds() -> None:
    raw = [
        {"t": 1_700_000_000_000, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 100.0},
        # Junk row dropped
        {"t": 0, "o": 0, "h": 0, "l": 0, "c": 0, "v": 0},
    ]
    rows = mod._normalize_rows(raw)
    assert len(rows) == 1
    assert rows[0]["time"] == 1_700_000_000
    assert rows[0]["high"] == 2.0


def test_merge_with_existing_dedupes_by_time(tmp_path: Path) -> None:
    out_path = tmp_path / "MBT1_5m.csv"
    # Seed existing file
    existing = [
        {"time": 1000, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 10.0},
        {"time": 1300, "open": 1.05, "high": 1.2, "low": 1.0, "close": 1.15, "volume": 12.0},
    ]
    mod.write_csv(out_path, existing)

    new = [
        # Duplicate timestamp — should be skipped
        {"time": 1000, "open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0, "volume": 0.0},
        # New
        {"time": 1600, "open": 1.15, "high": 1.3, "low": 1.1, "close": 1.25, "volume": 14.0},
    ]
    merged, n_existing, n_new = mod.merge_with_existing(out_path, new)
    assert n_existing == 2
    assert n_new == 1
    assert [r["time"] for r in merged] == [1000, 1300, 1600]
    # Original row preserved (not overwritten by duplicate)
    assert merged[0]["high"] == pytest.approx(1.1)


def test_csv_format_matches_load_ohlcv_expectations(tmp_path: Path) -> None:
    """Output CSV must be loadable by feeds.strategy_lab.engine._load_ohlcv."""
    out_path = tmp_path / "MBT1_5m.csv"
    rows = [
        {"time": 1_700_000_000, "open": 50000.0, "high": 50100.0, "low": 49900.0, "close": 50050.0, "volume": 12.5},
        {"time": 1_700_000_300, "open": 50050.0, "high": 50200.0, "low": 50000.0, "close": 50150.0, "volume": 10.0},
    ]
    mod.write_csv(out_path, rows)

    # 1) Header matches expected schema
    with out_path.open() as f:
        header = next(csv.reader(f))
    assert header == ["time", "open", "high", "low", "close", "volume"]

    # 2) _load_ohlcv accepts the file
    from eta_engine.feeds.strategy_lab import engine as lab_engine

    loaded = lab_engine._load_ohlcv(out_path)
    assert loaded is not None
    assert list(loaded["time"]) == [1_700_000_000.0, 1_700_000_300.0]
    assert list(loaded["close"]) == [50050.0, 50150.0]


def test_run_dry_run_does_not_hit_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """--dry-run prints planned requests and writes no files."""

    # Spy: _http_get_json must NEVER be called in dry-run
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("network must not be hit in --dry-run")

    monkeypatch.setattr(mod, "_http_get_json", boom)
    rc = mod.run(
        [
            "--symbols",
            "MBT",
            "MET",
            "--days",
            "30",
            "--root",
            str(tmp_path),
            "--dry-run",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "MBT" in out
    assert "MET" in out
    # No CSV created
    assert not list(tmp_path.glob("*.csv"))


def test_run_writes_canonical_csv_with_mocked_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end run with a mocked Client Portal Gateway response."""
    # Plan: resolve conid (1 call) + history fetch (≥1 call). We feed one
    # history chunk that covers the whole window so the loop terminates
    # immediately on the next call (which returns no data).
    base_ms = int(datetime(2026, 4, 1, tzinfo=UTC).timestamp() * 1000)
    history_payload = {
        "data": [
            {
                "t": base_ms + i * 300_000,
                "o": 50000.0 + i,
                "h": 50100.0 + i,
                "l": 49900.0 + i,
                "c": 50050.0 + i,
                "v": 1.0,
            }
            for i in range(5)
        ],
    }
    futures_payload = {
        "MBT": [{"conid": 999_111, "expirationDate": "20260626"}],
    }
    call_log: list[str] = []
    history_call_count = {"n": 0}

    def fake_get(url: str, *, timeout: float = 15.0) -> Any:
        call_log.append(url)
        if "/trsrv/futures" in url:
            return futures_payload
        if "/marketdata/history" in url:
            history_call_count["n"] += 1
            # Return data on the first call only; second call returns empty
            # which terminates the chunked-fetch loop.
            return history_payload if history_call_count["n"] == 1 else {"data": []}
        return None

    monkeypatch.setattr(mod, "_http_get_json", fake_get)
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    rc = mod.run(
        [
            "--symbols",
            "MBT",
            "--days",
            "30",
            "--end",
            "2026-04-02",
            "--root",
            str(tmp_path),
        ]
    )
    assert rc == 0

    # CSV at canonical filename pattern
    csv_path = tmp_path / "MBT1_5m.csv"
    assert csv_path.exists()
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    # 5 bars from the synthetic payload
    assert len(rows) == 5
    assert rows[0]["time"] == str(base_ms // 1000)
    assert rows[0]["close"] == "50050.0"

    # /trsrv/futures called and /marketdata/history called at least once
    assert any("/trsrv/futures" in u for u in call_log)
    assert any("/marketdata/history" in u for u in call_log)


def test_run_returns_nonzero_when_gateway_returns_no_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If the gateway returns empty rows, run() exits 1 — the harness
    should fail loudly so the operator notices."""
    monkeypatch.setattr(
        mod,
        "_http_get_json",
        lambda url, **_kw: (
            {"MBT": [{"conid": 111, "expirationDate": "20260626"}]} if "/trsrv/futures" in url else {"data": []}
        ),
    )
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    rc = mod.run(
        [
            "--symbols",
            "MBT",
            "--days",
            "10",
            "--root",
            str(tmp_path),
        ]
    )
    assert rc == 1
