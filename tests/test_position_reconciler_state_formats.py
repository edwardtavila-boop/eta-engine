from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eta_engine.obs.position_reconciler import fetch_bot_positions


def _clear_reconcile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ETA_RECONCILE_DISABLED", raising=False)
    monkeypatch.delenv("ETA_RECONCILE_ALLOW_EMPTY_STATE", raising=False)


def test_fetch_bot_positions_reads_supervisor_open_position_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_reconcile_env(monkeypatch)
    path = tmp_path / "bots" / "mcl_sweep_reclaim" / "open_position.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "symbol": "MCLN6",
                "side": "SELL",
                "qty": 1,
            }
        ),
        encoding="utf-8",
    )

    assert fetch_bot_positions(tmp_path) == {"MCLN6": {"mcl_sweep_reclaim": -1.0}}


def test_fetch_bot_positions_reads_supervisor_aggregate_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_reconcile_env(monkeypatch)
    path = tmp_path / "supervisor_open_positions.json"
    path.write_text(
        json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "n_open": 1,
                "positions": [
                    {
                        "bot_id": "mbt_funding_basis",
                        "symbol": "MBT1",
                        "side": "LONG",
                        "qty": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert fetch_bot_positions(tmp_path) == {"MBT1": {"mbt_funding_basis": 1.0}}


def test_fetch_bot_positions_prefers_per_bot_open_position_over_aggregate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_reconcile_env(monkeypatch)
    (tmp_path / "supervisor_open_positions.json").write_text(
        json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "n_open": 1,
                "positions": [
                    {
                        "bot_id": "ng_sweep_reclaim",
                        "symbol": "NGM26",
                        "side": "LONG",
                        "qty": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    path = tmp_path / "bots" / "ng_sweep_reclaim" / "open_position.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "symbol": "NGM26",
                "side": "SELL",
                "qty": 2,
            }
        ),
        encoding="utf-8",
    )

    assert fetch_bot_positions(tmp_path) == {"NGM26": {"ng_sweep_reclaim": -2.0}}


def test_fetch_bot_positions_accepts_empty_supervisor_aggregate_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_reconcile_env(monkeypatch)
    (tmp_path / "supervisor_open_positions.json").write_text(
        json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "n_open": 0,
                "positions": [],
            }
        ),
        encoding="utf-8",
    )

    assert fetch_bot_positions(tmp_path) == {}
