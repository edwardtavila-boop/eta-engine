from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eta_engine.scripts import fetch_onchain_history as mod
from eta_engine.scripts import workspace_roots


def test_sol_daily_series_uses_coingecko_and_defillama(monkeypatch) -> None:
    day = datetime(2026, 4, 29, tzinfo=UTC)
    ts_ms = int(day.timestamp() * 1000)
    ts_s = int(day.timestamp())

    def fake_http_json(url: str, *, timeout: float = 10.0) -> object | None:
        _ = timeout
        if "coins/solana/market_chart" in url:
            return {
                "prices": [[ts_ms, 150.0]],
                "market_caps": [[ts_ms, 75_000_000_000.0]],
                "total_volumes": [[ts_ms, 4_000_000_000.0]],
            }
        if "historicalChainTvl/Solana" in url:
            return [{"date": ts_s, "tvl": 9_000_000_000.0}]
        return None

    monkeypatch.setattr(mod, "_http_json", fake_http_json)
    monkeypatch.setattr(mod.time, "sleep", lambda *_args, **_kwargs: None)

    series = mod._sol_daily_series(days=365)

    assert series[day.date()] == {
        "price_usd": 150.0,
        "market_cap_usd": 75_000_000_000.0,
        "volume_usd": 4_000_000_000.0,
        "chain_tvl_usd": 9_000_000_000.0,
    }


def test_sol_onchain_writer_uses_expected_filename_and_columns(tmp_path: Path) -> None:
    day = datetime(2026, 4, 29, tzinfo=UTC).date()
    out = tmp_path / mod._filename("SOL")

    mod.write_csv(
        out,
        {
            day: {
                "price_usd": 150.0,
                "market_cap_usd": 75_000_000_000.0,
                "volume_usd": 4_000_000_000.0,
                "chain_tvl_usd": 9_000_000_000.0,
            },
        },
        mod._COLUMNS_BY_SYMBOL["SOL"],
    )

    with out.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))

    assert out.name == "SOLONCHAIN_D.csv"
    assert rows[0] == [
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "price_usd",
        "market_cap_usd",
        "volume_usd",
        "chain_tvl_usd",
    ]
    assert rows[1][1:6] == ["150.0", "150.0", "150.0", "150.0", "4.0"]


def test_main_clamps_future_dated_provider_rows(monkeypatch, tmp_path: Path) -> None:
    today = datetime(2026, 4, 29, tzinfo=UTC).date()
    future = datetime(2026, 4, 30, tzinfo=UTC).date()

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None) -> datetime:  # type: ignore[no-untyped-def]
            return datetime(2026, 4, 29, 20, tzinfo=tz or UTC)

    monkeypatch.setattr(mod, "datetime", FakeDateTime)
    monkeypatch.setattr(
        mod,
        "_sol_daily_series",
        lambda _days: {
            today: {"price_usd": 150.0},
            future: {"price_usd": 151.0},
        },
    )
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", tmp_path.parent)

    rc = mod.main(["--symbol", "SOL", "--days", "2", "--root", str(tmp_path)])

    assert rc == 0
    with (tmp_path / "SOLONCHAIN_D.csv").open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert datetime.fromtimestamp(int(rows[0]["time"]), UTC).date() == today


def test_main_rejects_output_root_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_workspace = tmp_path / "workspace"
    outside_workspace = tmp_path / "outside"
    fake_workspace.mkdir()
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", fake_workspace)

    with pytest.raises(SystemExit) as exc:
        mod.main(["--symbol", "SOL", "--root", str(outside_workspace)])

    assert exc.value.code == 2
