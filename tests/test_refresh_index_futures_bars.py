from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from eta_engine.scripts import refresh_index_futures_bars as refresh


def test_refresh_symbol_merges_rows_and_reports_truth_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    existing_path = tmp_path / "NQ1_5m.csv"
    existing_path.write_text(
        "time,open,high,low,close,volume\n"
        "100,1,2,0.5,1.5,10\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(refresh, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(
        refresh,
        "_fetch_via_yfinance",
        lambda symbol, timeframe, period: [
            {"time": 100, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0},
            {"time": 200, "open": 2.0, "high": 3.0, "low": 1.5, "close": 2.5, "volume": 20.0},
        ],
    )

    result = refresh.refresh_symbol("NQ", history_root=tmp_path)

    assert result["ok"] is True
    assert result["source"] == "yfinance"
    assert result["rows_existing"] == 1
    assert result["rows_fetched"] == 2
    assert result["rows_new_unique"] == 1
    assert result["rows_total"] == 2
    assert "NQ1_5m.csv" in result["path"]
    assert existing_path.read_text(encoding="utf-8").count("\n") == 3


def test_run_refresh_summary_is_data_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(refresh, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(
        refresh,
        "refresh_symbol",
        lambda symbol, **kwargs: {
            "symbol": symbol,
            "ok": True,
            "coverage": {"latest_age_minutes": 12.5},
        },
    )

    payload = refresh.run_refresh(
        symbols=["NQ", "MNQ"],
        timeframe="5m",
        period=None,
        source="yfinance",
        history_root=tmp_path,
    )

    assert payload["kind"] == "eta_index_futures_bar_refresh"
    assert payload["summary"]["status"] == "PASS"
    assert payload["summary"]["order_action_allowed"] is False
    assert payload["summary"]["broker_backed"] is False
    assert "not broker PnL/proof" in payload["summary"]["truth_note"]
    assert "Never submits" in payload["summary"]["truth_note"]


def test_main_json_stdout_is_parseable_and_can_write_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    status_path = tmp_path / "var" / "eta_engine" / "state" / "index_futures_bar_refresh_latest.json"
    monkeypatch.setattr(refresh, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(refresh, "ETA_INDEX_FUTURES_BAR_REFRESH_STATUS_PATH", status_path)
    monkeypatch.setattr(
        refresh,
        "run_refresh",
        lambda **kwargs: {
            "kind": "eta_index_futures_bar_refresh",
            "schema_version": refresh.SCHEMA_VERSION,
            "generated_at_utc": "2026-05-15T00:00:00+00:00",
            "summary": {
                "status": "PASS",
                "ok_count": 2,
                "symbol_count": 2,
                "elapsed_ms": 1,
                "order_action_allowed": False,
                "broker_backed": False,
                "truth_note": refresh.TRUTH_NOTE,
            },
            "symbols": [],
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["refresh_index_futures_bars", "--json", "--history-root", str(tmp_path), "--write-default-status"],
    )

    rc = refresh.main()
    stdout = capsys.readouterr().out

    assert rc == 0
    assert json.loads(stdout)["kind"] == "eta_index_futures_bar_refresh"
    assert json.loads(status_path.read_text(encoding="utf-8"))["summary"]["status"] == "PASS"
