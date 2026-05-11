from __future__ import annotations

import json
from pathlib import Path

from eta_engine.feeds import capital_allocator as allocator


def _write_ledger(path: Path, sessions: dict[str, list[dict[str, float]]]) -> None:
    path.write_text(json.dumps({"bot_sessions": sessions}), encoding="utf-8")


def test_capital_allocator_prioritizes_futures_and_commodities(tmp_path: Path) -> None:
    ledger = tmp_path / "paper_soak_ledger.json"
    _write_ledger(
        ledger,
        {
            "mnq_futures_sage": [{"pnl": 300.0}, {"pnl": 100.0}],
            "ng_sweep_reclaim": [{"pnl": 200.0}, {"pnl": 200.0}],
            "btc_hybrid": [{"pnl": 500.0}, {"pnl": 500.0}],
        },
    )

    allocation = allocator.compute_allocations(ledger, total_capital=100_000.0)

    assert allocation.futures_pool["capital"] == 100_000.0
    assert allocation.spot_pool["capital"] == 0.0
    assert allocation.bots["mnq_futures_sage"].capital == 50_000.0
    assert allocation.bots["ng_sweep_reclaim"].capital == 50_000.0
    assert allocation.bots["btc_hybrid"].pool == "spot"
    assert allocation.bots["btc_hybrid"].capital == 0.0


def test_capital_allocator_maps_cme_crypto_micros_to_futures_pool(tmp_path: Path) -> None:
    ledger = tmp_path / "paper_soak_ledger.json"
    _write_ledger(
        ledger,
        {
            "mbt_overnight_gap": [{"pnl": 50.0}, {"pnl": 50.0}],
            "met_momentum_reclaim": [{"pnl": 25.0}, {"pnl": 75.0}],
        },
    )

    allocation = allocator.compute_allocations(ledger, total_capital=20_000.0)

    assert allocator.classify_pool("mbt_overnight_gap") == "futures"
    assert allocator.classify_pool("met_momentum_reclaim") == "futures"
    assert allocation.leveraged_pool["capital"] == 0.0
    assert allocation.futures_pool["profitable_count"] == 2
    assert allocation.bots["mbt_overnight_gap"].capital == 10_000.0
    assert allocation.bots["met_momentum_reclaim"].capital == 10_000.0
