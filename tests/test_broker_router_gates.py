from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eta_engine.scripts.broker_router_gates import BrokerRouterGateEvaluator
from eta_engine.scripts.runtime_order_hold import OrderEntryHold


def _hold(tmp_path: Path) -> OrderEntryHold:
    return OrderEntryHold(active=False, source="test", path=tmp_path / "order_entry_hold.json")


def _helper(
    tmp_path: Path,
    *,
    load_build_default_chain=None,
    gate_bootstrap_enabled=lambda: False,
    readiness_enforced=lambda: False,
    readiness_snapshot_path=None,
    sync_gate_state=None,
) -> BrokerRouterGateEvaluator:
    state_root = tmp_path / "state"
    snapshot_path = readiness_snapshot_path or (tmp_path / "bot_strategy_readiness_latest.json")
    return BrokerRouterGateEvaluator(
        heartbeat_path=state_root / "broker_router_heartbeat.json",
        gate_pre_trade_path=state_root / "pre_trade_gate.json",
        gate_heat_state_path=state_root / "heat_state.json",
        gate_journal_path=state_root / "gate_journal.sqlite",
        normalize_gate_result=lambda row: dict(row),
        load_build_default_chain=load_build_default_chain or (lambda: lambda **kwargs: SimpleNamespace(evaluate=lambda: (True, []))),
        gate_bootstrap_enabled=gate_bootstrap_enabled,
        order_entry_hold=lambda: _hold(tmp_path),
        sync_gate_state=sync_gate_state or (lambda **kwargs: None),
        readiness_enforced=readiness_enforced,
        readiness_snapshot_path=lambda: snapshot_path,
        live_money_env="ETA_LIVE_MONEY",
        logger=logging.getLogger("test_broker_router_gates"),
    )


def test_collect_open_positions_collapses_net_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    helper = _helper(tmp_path)
    monkeypatch.setattr(
        "eta_engine.obs.position_reconciler.fetch_bot_positions",
        lambda: {"MNQ": {"alpha": 2.0, "beta": -1.0}, "ES": {"alpha": 0.0}},
        raising=False,
    )

    assert helper.collect_open_positions() == {"MNQ": 1}


def test_evaluate_gates_bootstrap_allows_import_failure(tmp_path: Path) -> None:
    helper = _helper(
        tmp_path,
        load_build_default_chain=lambda: (_ for _ in ()).throw(ImportError("missing gate chain")),
        gate_bootstrap_enabled=lambda: True,
    )

    rows = helper.evaluate_gates(SimpleNamespace(bot_id="alpha", symbol="MNQ", qty=1.0), None)

    assert rows[0]["gate"] == "import_error_bootstrap"
    assert rows[0]["allow"] is True


def test_readiness_denial_blocks_unapproved_paper_bot(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "bot_strategy_readiness_latest.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "bot_id": "alpha",
                        "can_paper_trade": False,
                        "can_live_trade": False,
                        "launch_lane": "research",
                        "data_status": "ready",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    helper = _helper(
        tmp_path,
        readiness_enforced=lambda: True,
        readiness_snapshot_path=snapshot_path,
    )

    denial = helper.readiness_denial(SimpleNamespace(bot_id="alpha", symbol="MNQ", qty=1.0))

    assert "not paper-approved" in denial
