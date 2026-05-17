from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from eta_engine.data import audit as audit_module
from eta_engine.scripts import data_health_check as mod


def _req(*, kind: str, symbol: str, timeframe: str | None, critical: bool) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, symbol=symbol, timeframe=timeframe, critical=critical)


def _dataset(*, symbol: str, timeframe: str, path: Path, rows: int) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        timeframe=timeframe,
        path=path,
        row_count=rows,
        start_ts=datetime(2026, 5, 1, tzinfo=UTC),
        end_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )


def test_run_health_check_filters_bot_ids(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake_library = object()
    green_dataset = _dataset(symbol="BTC", timeframe="1h", path=Path("btc.csv"), rows=120)
    green_audit = SimpleNamespace(
        bot_id="btc_green",
        deactivated=False,
        available=[(_req(kind="bars", symbol="BTC", timeframe="1h", critical=True), green_dataset)],
        missing_critical=[],
        missing_optional=[],
    )
    red_audit = SimpleNamespace(
        bot_id="eth_red",
        deactivated=False,
        available=[],
        missing_critical=[_req(kind="bars", symbol="ETH", timeframe="1h", critical=True)],
        missing_optional=[],
    )

    monkeypatch.setattr(mod, "default_library", lambda: fake_library)
    monkeypatch.setattr(audit_module, "audit_all", lambda library: [green_audit, red_audit])

    rows = mod.run_health_check(bot_filter="btc_green")

    assert len(rows) == 1
    assert rows[0].bot_id == "btc_green"
    assert rows[0].status == "GREEN"
    assert rows[0].critical_available[0].symbol == "BTC"


def test_main_json_prints_summary(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    rows = [
        mod.BotHealthRow(
            bot_id="btc_green",
            status="GREEN",
            critical_available=[
                mod.DatasetSummary(
                    symbol="BTC",
                    timeframe="1h",
                    path="btc.csv",
                    row_count=120,
                    start_ts="2026-05-01",
                    end_ts="2026-05-15",
                    days_span=14.0,
                )
            ],
        ),
        mod.BotHealthRow(bot_id="eth_red", status="RED", critical_missing=["bars:ETH/1h"]),
    ]

    monkeypatch.setattr(mod, "run_health_check", lambda bot_filter=None: rows)

    rc = mod.main(["--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"] == {
        "green": 1,
        "amber": 0,
        "red": 1,
        "deactivated": 0,
        "unknown": 0,
        "total": 2,
    }
    assert payload["bots"][0]["critical_available"][0]["symbol"] == "BTC"
    assert payload["bots"][1]["critical_missing"] == ["bars:ETH/1h"]


def test_main_text_output_uses_ascii_markers(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    rows = [
        mod.BotHealthRow(bot_id="empty_bot", status="UNKNOWN"),
        mod.BotHealthRow(bot_id="warn_bot", status="AMBER", critical_missing=["bars:NQ/5m"]),
    ]
    catalog_dataset = _dataset(symbol="NQ", timeframe="5m", path=Path("nq.csv"), rows=42)
    fake_library = SimpleNamespace(list=lambda: [catalog_dataset])

    monkeypatch.setattr(mod, "run_health_check", lambda bot_filter=None: rows)
    monkeypatch.setattr(mod, "default_library", lambda: fake_library)

    rc = mod.main([])

    assert rc == 0
    output = capsys.readouterr().out
    assert "empty_bot" in output
    assert f" {mod.EMPTY_MARKER}" in output
    assert mod.CATALOG_ARROW in output
    assert "Global data catalog" in output
    assert "â" not in output
