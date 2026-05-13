from __future__ import annotations

import json
from pathlib import Path

from eta_engine.feeds import capital_allocator as allocator


def _write_ledger(path: Path, sessions: dict[str, list[dict[str, float]]]) -> None:
    path.write_text(json.dumps({"bot_sessions": sessions}), encoding="utf-8")


def test_capital_allocator_prioritizes_futures_and_commodities(tmp_path: Path) -> None:
    """Default POOL_SPLIT routes 100% to futures (spot cellared).

    Wave-25 (2026-05-13) added env-overridable POOL_SPLIT via
    ETA_POOL_FUTURES_FRAC / ETA_POOL_SPOT_FRAC /
    ETA_POOL_LEVERAGED_FRAC so the operator can re-engage spot crypto
    inside the paper-test research portfolio without a code change.
    The defaults remain 100/0/0 — no surprise allocation drift.
    """
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


def test_capital_allocator_pool_split_env_override(monkeypatch, tmp_path: Path) -> None:
    """Setting ETA_POOL_*_FRAC env vars must shift the pool split for
    the next module load. This is how the operator turns spot crypto
    back on for the paper-test portfolio without editing source."""
    monkeypatch.setenv("ETA_POOL_FUTURES_FRAC", "0.7")
    monkeypatch.setenv("ETA_POOL_SPOT_FRAC", "0.3")
    monkeypatch.setenv("ETA_POOL_LEVERAGED_FRAC", "0.0")

    split = allocator._resolve_pool_split()
    assert split == {"futures": 0.7, "spot": 0.3, "leveraged": 0.0}


def test_capital_allocator_pool_split_invalid_env_falls_back(monkeypatch) -> None:
    """Garbage in env (non-numeric, negative, wrong sum) must NOT silently
    drift the split — fall back to defaults so production is safe."""
    monkeypatch.setenv("ETA_POOL_FUTURES_FRAC", "not-a-number")
    split = allocator._resolve_pool_split()
    assert split == {"futures": 1.0, "spot": 0.0, "leveraged": 0.0}

    monkeypatch.setenv("ETA_POOL_FUTURES_FRAC", "0.6")
    monkeypatch.setenv("ETA_POOL_SPOT_FRAC", "0.6")  # sums to 1.2, invalid
    monkeypatch.setenv("ETA_POOL_LEVERAGED_FRAC", "0.0")
    split = allocator._resolve_pool_split()
    assert split == {"futures": 1.0, "spot": 0.0, "leveraged": 0.0}


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
