"""Tests for wave-15 (JARVIS coordinates the fleet).

Covers:
  * fleet_allocator.py        -- joint allocation across bots
  * risk_budget_allocator.py  -- drawdown-aware envelope
  * divergence_detector.py    -- live vs backtest comparison
  * pair_arbitrage_scanner.py -- mean-reverting basis scanner
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─── fleet_allocator.py ───────────────────────────────────────────


def test_fleet_allocator_picks_highest_score_uncorrelated() -> None:
    from eta_engine.brain.jarvis_v3.fleet_allocator import (
        FleetRequest,
        allocate_fleet,
    )

    requests = [
        FleetRequest(bot_id="A", expected_r=3.0, direction="long"),
        FleetRequest(bot_id="B", expected_r=0.5, direction="long"),
        FleetRequest(bot_id="C", expected_r=2.5, direction="long"),
    ]
    alloc = allocate_fleet(
        requests,
        max_picks=2,
        correlation_matrix=[
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
    )
    picked = {e.bot_id for e in alloc.entries if e.size_multiplier > 0}
    # A and C have highest expected R
    assert "A" in picked
    assert "C" in picked


def test_fleet_allocator_diversifies_correlated_bots() -> None:
    from eta_engine.brain.jarvis_v3.fleet_allocator import (
        FleetRequest,
        allocate_fleet,
    )

    requests = [
        FleetRequest(bot_id="A", expected_r=2.0, direction="long"),
        FleetRequest(bot_id="B", expected_r=2.0, direction="long"),
        FleetRequest(bot_id="C", expected_r=1.5, direction="long"),
    ]
    alloc = allocate_fleet(
        requests,
        max_picks=2,
        correlation_matrix=[
            [1.0, 0.95, 0.0],
            [0.95, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        correlation_penalty=10.0,
    )
    picked = {e.bot_id for e in alloc.entries if e.size_multiplier > 0}
    # Should NOT pick both A and B (highly correlated)
    assert picked != {"A", "B"}


def test_fleet_allocator_handles_empty_requests() -> None:
    from eta_engine.brain.jarvis_v3.fleet_allocator import allocate_fleet

    alloc = allocate_fleet([], max_picks=2)
    assert alloc.entries == []
    assert alloc.method == "empty"


def test_fleet_allocator_greedy_path() -> None:
    from eta_engine.brain.jarvis_v3.fleet_allocator import (
        FleetRequest,
        allocate_fleet,
    )

    requests = [
        FleetRequest(bot_id="A", expected_r=2.0),
        FleetRequest(bot_id="B", expected_r=1.5),
    ]
    alloc = allocate_fleet(requests, max_picks=1, use_qubo=False)
    assert alloc.method == "greedy"
    assert alloc.n_picked == 1


# ─── risk_budget_allocator.py ─────────────────────────────────────


def test_risk_budget_default_when_no_trades(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.risk_budget_allocator import (
        current_envelope,
    )

    mult = current_envelope(log_path=tmp_path / "missing.jsonl")
    assert mult.multiplier == 1.0
    assert mult.n_trades_mtd == 0


def test_risk_budget_default_when_snapshot_and_log_missing(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.risk_budget_allocator import current_envelope

    mult = current_envelope(snapshot_path=tmp_path / "missing.json", log_path=tmp_path / "missing.jsonl")
    assert mult.multiplier == 1.0
    assert mult.n_trades_mtd == 0


def test_risk_budget_full_standdown_at_max_drawdown(tmp_path: Path) -> None:
    import json
    from datetime import UTC, datetime

    from eta_engine.brain.jarvis_v3.risk_budget_allocator import (
        current_envelope,
    )

    log = tmp_path / "trades.jsonl"
    now = datetime.now(UTC).isoformat()
    rows = [{"ts": now, "realized_r": -7.0, "bot_id": "x"}]
    log.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    mult = current_envelope(log_path=log)
    # MTD R = -7, max_drawdown_r = -6 -> stand down
    assert mult.multiplier == 0.0
    assert "STAND-DOWN" in mult.reason


def test_risk_budget_aggressive_when_above_threshold(tmp_path: Path) -> None:
    import json
    from datetime import UTC, datetime

    from eta_engine.brain.jarvis_v3.risk_budget_allocator import (
        current_envelope,
    )

    log = tmp_path / "trades.jsonl"
    now = datetime.now(UTC).isoformat()
    rows = [
        {"ts": now, "realized_r": 2.0, "bot_id": "x"},
        {"ts": now, "realized_r": 2.0, "bot_id": "x"},
        {"ts": now, "realized_r": 2.0, "bot_id": "x"},  # +6R total
    ]
    log.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    mult = current_envelope(log_path=log)
    # MTD = +6R > aggressive_threshold (+4R) -> > 1.0
    assert mult.multiplier > 1.0


def test_size_for_proposal_applies_envelope(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.risk_budget_allocator import (
        size_for_proposal,
    )

    adjusted, mult = size_for_proposal(
        base_size=2.0,
        log_path=tmp_path / "missing.jsonl",
    )
    assert adjusted == 2.0  # multiplier=1.0 in default state
    assert mult.multiplier == 1.0


# ─── divergence_detector.py ───────────────────────────────────────


def test_divergence_detector_returns_ok_when_no_baselines(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.divergence_detector import detect_divergence

    rep = detect_divergence(
        backtest_baselines={},
        log_path=tmp_path / "missing.jsonl",
    )
    assert rep.overall_status == "OK"
    assert rep.n_cells_compared == 0


def test_divergence_detector_flags_critical_gap(tmp_path: Path) -> None:
    import json
    from datetime import UTC, datetime

    from eta_engine.brain.jarvis_v3.divergence_detector import detect_divergence

    log = tmp_path / "trades.jsonl"
    now = datetime.now(UTC).isoformat()
    # Backtest expects +1.5R, live delivers -1.5R consistently
    rows = [
        {"ts": now, "bot_id": "A", "regime": "neutral", "realized_r": -1.5},
        {"ts": now, "bot_id": "A", "regime": "neutral", "realized_r": -1.4},
        {"ts": now, "bot_id": "A", "regime": "neutral", "realized_r": -1.6},
        {"ts": now, "bot_id": "A", "regime": "neutral", "realized_r": -1.5},
        {"ts": now, "bot_id": "A", "regime": "neutral", "realized_r": -1.4},
        {"ts": now, "bot_id": "A", "regime": "neutral", "realized_r": -1.6},
    ]
    log.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    rep = detect_divergence(
        backtest_baselines={("A", "neutral"): 1.5},
        log_path=log,
        min_trades_per_cell=5,
    )
    assert rep.n_criticals >= 1
    assert rep.overall_status == "CRITICAL"


def test_divergence_detector_to_dict_serializable(tmp_path: Path) -> None:
    import json

    from eta_engine.brain.jarvis_v3.divergence_detector import detect_divergence

    rep = detect_divergence(
        backtest_baselines={},
        log_path=tmp_path / "missing.jsonl",
    )
    s = json.dumps(rep.to_dict())
    assert "summary" in s


# ─── pair_arbitrage_scanner.py ────────────────────────────────────


def test_pair_scan_emits_no_signal_when_basis_in_band() -> None:
    from eta_engine.brain.jarvis_v3.pair_arbitrage_scanner import (
        PairSpec,
        scan_pair,
    )

    # Both legs follow the same trend; basis is stable
    spec = PairSpec(
        label="A_vs_B",
        leg_a="A",
        leg_b="B",
        prices_a=[100.0 + i * 0.1 for i in range(80)],
        prices_b=[200.0 + i * 0.2 for i in range(80)],
        lookback_bars=60,
        entry_z=2.0,
    )
    sig = scan_pair(spec)
    # Stable basis -> no signal
    assert sig is None


def test_pair_scan_emits_signal_when_basis_diverges() -> None:
    from eta_engine.brain.jarvis_v3.pair_arbitrage_scanner import (
        PairSpec,
        scan_pair,
    )

    # First 79 bars: stable. Last bar: A spikes way up (basis explodes)
    a = [100.0 + (i % 5) * 0.05 for i in range(79)] + [110.0]
    b = [200.0 + (i % 5) * 0.10 for i in range(80)]
    spec = PairSpec(
        label="diverge",
        leg_a="A",
        leg_b="B",
        prices_a=a,
        prices_b=b,
        lookback_bars=60,
        entry_z=1.5,
    )
    sig = scan_pair(spec)
    assert sig is not None
    assert abs(sig.z_score) > 1.5
    # Basis spiked up -> short A, long B
    assert sig.direction == "short_a_long_b"


def test_scan_pairs_ranks_by_abs_z() -> None:
    from eta_engine.brain.jarvis_v3.pair_arbitrage_scanner import (
        PairSpec,
        scan_pairs,
    )

    a = [100.0 + (i % 3) * 0.02 for i in range(79)] + [105.0]
    b = [200.0 + (i % 3) * 0.04 for i in range(80)]
    spec1 = PairSpec(
        label="big",
        leg_a="A",
        leg_b="B",
        prices_a=a,
        prices_b=b,
        lookback_bars=60,
        entry_z=1.0,
    )
    a2 = [100.0 + (i % 3) * 0.02 for i in range(79)] + [101.0]
    spec2 = PairSpec(
        label="small",
        leg_a="C",
        leg_b="D",
        prices_a=a2,
        prices_b=b,
        lookback_bars=60,
        entry_z=1.0,
    )
    rep = scan_pairs([spec2, spec1])
    if rep.n_signals >= 2:
        assert abs(rep.signals[0].z_score) >= abs(rep.signals[1].z_score)


def test_scan_pair_short_history_returns_none() -> None:
    from eta_engine.brain.jarvis_v3.pair_arbitrage_scanner import (
        PairSpec,
        scan_pair,
    )

    spec = PairSpec(
        label="short",
        leg_a="A",
        leg_b="B",
        prices_a=[1.0, 2.0],
        prices_b=[3.0, 4.0],
        lookback_bars=60,
    )
    assert scan_pair(spec) is None
