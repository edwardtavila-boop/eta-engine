from __future__ import annotations

import csv
from datetime import date
from typing import TYPE_CHECKING

from eta_engine.scripts import hydrate_canonical_market_data as hydrate
from eta_engine.scripts.hydrate_canonical_market_data import (
    CryptoPlan,
    ImportCandidate,
    _canonical_history_name_from_databento,
    _canonical_history_name_from_main,
    _convert_main_to_history,
    _resample_rows,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_canonical_history_name_from_databento_normalizes_legacy_names() -> None:
    assert _canonical_history_name_from_databento("mnq1_5m.csv") == "MNQ1_5m.csv"
    assert _canonical_history_name_from_databento("nq_1m.csv") == "NQ1_1m.csv"
    assert _canonical_history_name_from_databento("es_5m.csv") == "ES1_5m.csv"
    assert _canonical_history_name_from_databento("vix_yf_d.csv") == "VIX_D.csv"


def test_canonical_history_name_from_main_promotes_root_futures_names() -> None:
    assert _canonical_history_name_from_main("mnq_5m.csv") == "MNQ1_5m.csv"
    assert _canonical_history_name_from_main("mnq_es1_5.csv") == "ES1_5m.csv"
    assert _canonical_history_name_from_main("nq_D.csv") == "NQ1_D.csv"


def test_convert_main_to_history_rewrites_schema(tmp_path: Path) -> None:
    source = tmp_path / "nq_D.csv"
    target = tmp_path / "history" / "NQ1_D.csv"
    with source.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["timestamp_utc", "epoch_s", "open", "high", "low", "close", "volume", "session"],
        )
        writer.writerow(["2026-01-01T00:00:00Z", 1735689600, 100.0, 101.0, 99.0, 100.5, 1000, "ETH"])
        writer.writerow(["2026-01-02T00:00:00Z", 1735776000, 100.5, 102.0, 100.0, 101.5, 1200, "ETH"])

    rows = _convert_main_to_history(source, target)

    assert rows == 2
    assert target.exists()
    with target.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        materialized = list(reader)
    assert reader.fieldnames == ["time", "open", "high", "low", "close", "volume"]
    assert materialized[0]["time"] == "1735689600"
    assert materialized[1]["close"] == "101.5"


def test_convert_main_to_history_skips_empty_target(tmp_path: Path) -> None:
    source = tmp_path / "empty.csv"
    target = tmp_path / "history" / "MNQ1_5m.csv"
    source.write_text(
        "timestamp_utc,epoch_s,open,high,low,close,volume,session\n",
        encoding="utf-8",
    )

    rows = _convert_main_to_history(source, target)

    assert rows == 0
    assert not target.exists()


def test_import_futures_dry_run_does_not_write_target(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.csv"
    target = tmp_path / "history" / "MNQ1_5m.csv"
    source.write_text("time,open,high,low,close,volume\n1,1,1,1,1,1\n", encoding="utf-8")
    candidate = ImportCandidate(
        source=source,
        target=target,
        source_kind="history",
        note="test",
        row_count=1,
    )

    monkeypatch.setattr(hydrate, "MNQ_HISTORY_ROOT", tmp_path / "unused")
    monkeypatch.setattr(hydrate, "ensure_dir", lambda path: path)
    monkeypatch.setattr(
        hydrate,
        "_collect_futures_candidates",
        lambda *, max_legacy_files=200: {target: candidate},
    )
    monkeypatch.setattr(hydrate, "_probe_rows", lambda path, source_kind: 0)

    imported, skipped = hydrate._import_futures(dry_run=True)

    assert (imported, skipped) == (0, 1)
    assert not target.exists()


def test_collect_futures_candidates_respects_legacy_probe_limit(tmp_path: Path, monkeypatch) -> None:
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    for name in ("mnq1_5m.csv", "nq_1m.csv", "es_5m.csv"):
        (legacy_root / name).write_text("time,open,high,low,close,volume\n1,1,1,1,1,1\n", encoding="utf-8")

    monkeypatch.setattr(hydrate, "MNQ_HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(hydrate, "MNQ_DATA_ROOT", tmp_path / "canonical")
    monkeypatch.setattr(hydrate, "_iter_legacy_databento_dirs", lambda: [legacy_root])
    monkeypatch.setattr(hydrate, "_probe_rows", lambda path, source_kind: 1)

    candidates = hydrate._collect_futures_candidates(max_legacy_files=2)

    assert len(candidates) == 2

    candidates = hydrate._collect_futures_candidates(max_legacy_files=0)

    assert len(candidates) == 3


def test_crypto_price_dry_run_does_not_fetch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hydrate, "CRYPTO_HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(hydrate, "ensure_dir", lambda path: path)
    monkeypatch.setattr(hydrate, "_CRYPTO_BAR_PLAN", (CryptoPlan("BTC", "1h", 1),))

    def fail_fetch(**_: object) -> list[list[float]]:
        raise AssertionError("dry-run must not fetch")

    monkeypatch.setattr(hydrate, "fetch_crypto_bars", fail_fetch)

    written, skipped = hydrate._fetch_crypto_prices(dry_run=True)

    assert (written, skipped) == (0, 1)


def test_onchain_dry_run_does_not_fetch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hydrate, "CRYPTO_ONCHAIN_ROOT", tmp_path)

    def fail_fetch(days: int) -> dict[date, dict[str, float]]:
        raise AssertionError("dry-run must not fetch on-chain series")

    monkeypatch.setattr(hydrate, "_ONCHAIN_FETCHERS", {"ETH": fail_fetch})

    written, skipped = hydrate._fetch_free_onchain_series(dry_run=True)

    assert (written, skipped) == (0, 1)
    assert not any(tmp_path.iterdir())


def test_onchain_fetch_writes_canonical_btc_and_eth(monkeypatch, tmp_path: Path) -> None:
    today = date.today()
    monkeypatch.setattr(hydrate, "CRYPTO_ONCHAIN_ROOT", tmp_path)
    monkeypatch.setattr(
        hydrate,
        "_ONCHAIN_FETCHERS",
        {
            "BTC": lambda days: {today: {"price_usd": 100.0}},
            "ETH": lambda days: {today: {"price_usd": 10.0, "chain_tvl_usd": 1.0}},
        },
    )
    monkeypatch.setattr(
        hydrate,
        "_ONCHAIN_COLUMNS",
        {
            "BTC": ["price_usd"],
            "ETH": ["price_usd", "chain_tvl_usd"],
        },
    )

    written, skipped = hydrate._fetch_free_onchain_series()

    assert (written, skipped) == (2, 0)
    assert (tmp_path / "BTCONCHAIN_D.csv").exists()
    assert (tmp_path / "ETHONCHAIN_D.csv").exists()


def test_resample_rows_aggregates_to_hourly() -> None:
    rows = [
        {"time": 1735689600, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0},
        {"time": 1735691400, "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0, "volume": 20.0},
    ]

    out = _resample_rows(rows, "1h")

    assert len(out) == 1
    assert out[0]["open"] == 1.0
    assert out[0]["high"] == 2.5
    assert out[0]["low"] == 0.5
    assert out[0]["close"] == 2.0
    assert out[0]["volume"] == 30.0


def test_crypto_bar_plan_backfills_missing_intraday_feeds() -> None:
    plan = {(item.symbol, item.timeframe) for item in hydrate._CRYPTO_BAR_PLAN}
    assert ("BTC", "1m") in plan
    assert ("BTC", "5m") in plan
    assert ("SOL", "5m") in plan
