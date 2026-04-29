from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.data.audit import BotAudit
from eta_engine.data.library import DataLibrary
from eta_engine.data.requirements import DataRequirement
from eta_engine.scripts import announce_data_library
from eta_engine.scripts.workspace_roots import (
    ETA_DATA_INVENTORY_SNAPSHOT_PATH,
    ETA_RUNTIME_STATE_DIR,
)


def _write_history_csv(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["time", "open", "high", "low", "close", "volume"])
        writer.writerow([1_735_689_600, 100.0, 101.0, 99.0, 100.5, 10_000.0])
        writer.writerow([1_735_693_200, 100.5, 101.5, 100.0, 101.0, 12_000.0])


def _write_history_csv_at(path: Path, timestamps: list[datetime]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["time", "open", "high", "low", "close", "volume"])
        for idx, ts in enumerate(timestamps):
            px = 100.0 + idx
            writer.writerow([int(ts.timestamp()), px, px + 1.0, px - 1.0, px + 0.5, 10_000.0])


def _write_main_csv_at(path: Path, timestamps: list[datetime]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp_utc", "epoch_s", "open", "high", "low", "close", "volume", "session"])
        for idx, ts in enumerate(timestamps):
            px = 100.0 + idx
            writer.writerow([ts.isoformat(), int(ts.timestamp()), px, px + 1.0, px - 1.0, px + 0.5, 10_000.0, "ETH"])


def test_default_snapshot_path_is_canonical_runtime_state() -> None:
    assert announce_data_library._DEFAULT_SNAPSHOT == ETA_DATA_INVENTORY_SNAPSHOT_PATH
    assert announce_data_library._DEFAULT_SNAPSHOT.parent == ETA_RUNTIME_STATE_DIR
    assert announce_data_library._DEFAULT_SNAPSHOT.name == "data_inventory_latest.json"


def test_build_inventory_snapshot_includes_dataset_and_bot_coverage(tmp_path: Path) -> None:
    history = tmp_path / "history"
    history.mkdir()
    _write_history_csv(history / "BTC_1h.csv")
    lib = DataLibrary(roots=[history])
    dataset = lib.get(symbol="BTC", timeframe="1h")
    assert dataset is not None

    available_req = DataRequirement("bars", "BTC", "1h")
    optional_req = DataRequirement("correlation", "BTC", "1h", critical=False)
    missing_req = DataRequirement("funding", "BTC", "8h")
    missing_optional_req = DataRequirement("sentiment", "BTC", "D", critical=False)
    audits = [
        BotAudit(
            bot_id="btc_test",
            available=[(available_req, dataset), (optional_req, dataset)],
            missing_critical=[missing_req],
            missing_optional=[missing_optional_req],
            sources_hint=("scripts/fetch_funding_rates.py",),
        ),
        BotAudit(bot_id="xrp_perp", deactivated=True),
    ]

    payload = announce_data_library.build_inventory_snapshot(
        lib,
        audits,
        generated_at=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
    )

    assert payload["schema_version"] == 1
    assert payload["generated_at"] == "2026-04-29T12:00:00+00:00"
    assert payload["dataset_count"] == 1
    assert payload["datasets"][0]["symbol"] == "BTC"
    assert payload["datasets"][0]["freshness"]["status"] == "stale"
    assert payload["datasets"][0]["freshness"]["age_days"] == 483.46
    assert payload["freshness"]["counts"] == {"fresh": 0, "warm": 0, "stale": 1}
    assert payload["freshness"]["stale"][0]["key"] == "BTC/1h/history"
    assert payload["bot_coverage"]["total"] == 2
    assert payload["bot_coverage"]["blocked_count"] == 1
    assert payload["bot_coverage"]["deactivated_count"] == 1
    assert payload["bot_coverage"]["deactivated"] == ["xrp_perp"]
    assert payload["bot_coverage"]["blocked"]["btc_test"]["missing_critical"][0] == {
        "kind": "funding",
        "symbol": "BTC",
        "timeframe": "8h",
        "critical": True,
        "note": "",
    }
    assert payload["bot_coverage"]["critical_freshness"]["status_counts"] == {
        "fresh": 0,
        "warm": 0,
        "stale": 0,
        "blocked": 1,
        "deactivated": 1,
    }
    assert payload["bot_coverage"]["critical_freshness"]["blocked_bots"][0]["bot_id"] == "btc_test"
    assert payload["bot_coverage"]["optional_freshness"]["status_counts"] == {
        "fresh": 0,
        "warm": 0,
        "stale": 1,
        "missing": 0,
        "none": 0,
        "deactivated": 1,
    }
    assert payload["bot_coverage"]["optional_freshness"]["stale_bots"][0]["bot_id"] == "btc_test"
    assert payload["bot_coverage"]["optional_freshness"]["missing_bots"][0]["bot_id"] == "btc_test"
    assert payload["bot_coverage"]["items"][0]["critical_freshness"]["status"] == "blocked"
    assert payload["bot_coverage"]["items"][0]["critical_freshness"]["counts"] == {
        "fresh": 0,
        "warm": 0,
        "stale": 1,
        "missing": 1,
    }
    assert payload["bot_coverage"]["items"][0]["optional_freshness"]["status"] == "stale"
    assert payload["bot_coverage"]["items"][0]["optional_freshness"]["counts"] == {
        "fresh": 0,
        "warm": 0,
        "stale": 1,
        "missing": 1,
    }
    available_dataset = payload["bot_coverage"]["items"][0]["available"][0]["dataset"]
    assert available_dataset["key"] == "BTC/1h/history"
    assert available_dataset["freshness"]["status"] == "stale"
    stale_requirement = payload["bot_coverage"]["items"][0]["critical_freshness"]["stale"][0]
    assert stale_requirement["requirement"]["symbol"] == "BTC"
    assert stale_requirement["dataset"]["key"] == "BTC/1h/history"


def test_inventory_snapshot_separates_raw_and_canonical_freshness(tmp_path: Path) -> None:
    main = tmp_path / "main"
    history = tmp_path / "history"
    main.mkdir()
    history.mkdir()
    generated_at = datetime(2026, 4, 29, 20, 0, tzinfo=UTC)

    _write_main_csv_at(
        main / "mnq_es1_5.csv",
        [
            datetime(2026, 4, 14, 19, 0, tzinfo=UTC),
            datetime(2026, 4, 14, 19, 5, tzinfo=UTC),
        ],
    )
    _write_history_csv_at(
        history / "ES1_5m.csv",
        [
            datetime(2026, 4, 29, 19, 25, tzinfo=UTC),
            datetime(2026, 4, 29, 19, 30, tzinfo=UTC),
            datetime(2026, 4, 29, 19, 35, tzinfo=UTC),
        ],
    )

    payload = announce_data_library.build_inventory_snapshot(
        DataLibrary(roots=[main, history]),
        audits=[],
        generated_at=generated_at,
    )

    freshness = payload["freshness"]
    assert freshness["counts"] == {"fresh": 1, "warm": 0, "stale": 1}
    assert freshness["canonical_counts"] == {"fresh": 1, "warm": 0, "stale": 0}
    assert freshness["canonical_stale"] == []
    assert freshness["superseded_stale"][0]["key"] == "ES1/5m/main"
    assert freshness["superseded_stale"][0]["superseded_by"]["key"] == "ES1/5m/history"
    assert freshness["superseded_stale"][0]["superseded_by"]["freshness"]["status"] == "fresh"


def test_inventory_payload_marks_proxy_and_synthetic_resolution(tmp_path: Path) -> None:
    history = tmp_path / "history"
    history.mkdir()
    _write_history_csv_at(
        history / "FEAR_GREEDMACRO_D.csv",
        [datetime(2026, 4, 29, tzinfo=UTC)],
    )
    _write_history_csv_at(
        history / "SOLONCHAIN_D.csv",
        [datetime(2026, 4, 29, tzinfo=UTC)],
    )
    lib = DataLibrary(roots=[history])
    fear_greed = lib.get(symbol="FEAR_GREEDMACRO", timeframe="D")
    sol_onchain = lib.get(symbol="SOLONCHAIN", timeframe="D")
    assert fear_greed is not None
    assert sol_onchain is not None

    audits = [
        BotAudit(
            bot_id="proxy_test",
            available=[
                (DataRequirement("sentiment", "BTC", "1h", critical=False), fear_greed),
                (DataRequirement("onchain", "SOL", None, critical=False), sol_onchain),
            ],
        )
    ]

    payload = announce_data_library.build_inventory_snapshot(
        lib,
        audits,
        generated_at=datetime(2026, 4, 29, 20, 0, tzinfo=UTC),
    )

    available = payload["bot_coverage"]["items"][0]["available"]
    sentiment = available[0]["resolution"]
    assert sentiment == {
        "mode": "proxy",
        "requested_symbol": "BTC",
        "requested_timeframe": "1h",
        "expected_dataset_symbol": "BTCSENT",
        "dataset_symbol": "FEAR_GREEDMACRO",
        "dataset_timeframe": "D",
        "quality_note": "crypto-wide Fear & Greed proxy for symbol-specific sentiment",
    }
    onchain = available[1]["resolution"]
    assert onchain["mode"] == "synthetic"
    assert onchain["expected_dataset_symbol"] == "SOLONCHAIN"
    assert onchain["dataset_symbol"] == "SOLONCHAIN"


def test_write_inventory_snapshot_creates_parent_and_pretty_json(tmp_path: Path) -> None:
    target = tmp_path / "var" / "eta_engine" / "state" / "data_inventory_latest.json"
    payload = {
        "schema_version": 1,
        "generated_at": "2026-04-29T12:00:00+00:00",
        "datasets": [],
        "bot_coverage": {"blocked_count": 0},
    }

    written = announce_data_library.write_inventory_snapshot(target, payload)

    assert written == target
    assert target.exists()
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == payload
    assert target.read_text(encoding="utf-8").endswith("\n")
