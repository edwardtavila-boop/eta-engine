"""Tests for the wave-20 diamond prop allocator (confluence-aware)."""

# ruff: noqa: N802, PLR2004
from __future__ import annotations


def _leaderboard(prop_ready: list[str], scores: dict[str, float]) -> dict:
    """Build a synthetic leaderboard receipt."""
    return {
        "prop_ready_bots": prop_ready,
        "leaderboard": [{"bot_id": b, "composite_score": s} for b, s in scores.items()],
    }


# ────────────────────────────────────────────────────────────────────
# BALANCED mode (33/33/33) — all 3 bots roughly equal
# ────────────────────────────────────────────────────────────────────


def test_balanced_when_scores_within_threshold() -> None:
    """All 3 PROP_READY bots have similar composite scores -> BALANCED 33/33/33."""
    from eta_engine.scripts import diamond_prop_allocator as pa

    lb = _leaderboard(
        prop_ready=["a", "b", "c"],
        scores={"a": 10.0, "b": 9.5, "c": 9.0},
    )
    receipt = pa.compute_allocation(lb, account_size=50_000.0)
    assert receipt.mode == "BALANCED"
    assert len(receipt.allocations) == 3
    for alloc in receipt.allocations:
        # 33.33% each, ~$16,666.67
        assert abs(alloc.weight_pct - 33.33) < 0.1
        assert abs(alloc.capital_usd - 16_666.67) < 1.0


# ────────────────────────────────────────────────────────────────────
# DOMINANT mode (50/25/25) — top is clearly best
# ────────────────────────────────────────────────────────────────────


def test_dominant_when_top_score_15x_median_other() -> None:
    """Top composite 1.5x+ median of other two -> DOMINANT 50/25/25."""
    from eta_engine.scripts import diamond_prop_allocator as pa

    lb = _leaderboard(
        prop_ready=["topbot", "b", "c"],
        scores={"topbot": 15.0, "b": 5.0, "c": 5.0},
    )
    receipt = pa.compute_allocation(lb, account_size=50_000.0)
    assert receipt.mode == "DOMINANT"
    by_id = {a.bot_id: a for a in receipt.allocations}
    assert by_id["topbot"].weight_pct == 50.0
    assert by_id["topbot"].capital_usd == 25_000.0
    assert by_id["b"].weight_pct == 25.0
    assert by_id["b"].capital_usd == 12_500.0
    assert by_id["c"].weight_pct == 25.0


def test_balanced_when_dominance_just_under_threshold() -> None:
    """Boundary: ratio < 1.5 stays BALANCED."""
    from eta_engine.scripts import diamond_prop_allocator as pa

    lb = _leaderboard(
        prop_ready=["a", "b", "c"],
        scores={"a": 14.0, "b": 10.0, "c": 10.0},  # ratio = 1.4
    )
    receipt = pa.compute_allocation(lb, account_size=50_000.0)
    assert receipt.mode == "BALANCED"


def test_dominant_threshold_is_configurable() -> None:
    """Operator can tighten/loosen the dominance threshold."""
    from eta_engine.scripts import diamond_prop_allocator as pa

    lb = _leaderboard(
        prop_ready=["a", "b", "c"],
        scores={"a": 12.0, "b": 10.0, "c": 10.0},  # ratio = 1.2
    )
    # Default 1.5x: BALANCED
    assert pa.compute_allocation(lb).mode == "BALANCED"
    # Looser 1.1x: DOMINANT (top is 1.2x median, beats 1.1)
    assert pa.compute_allocation(lb, dominance_threshold=1.1).mode == "DOMINANT"


# ────────────────────────────────────────────────────────────────────
# Degenerate input handling
# ────────────────────────────────────────────────────────────────────


def test_no_prop_ready_returns_degraded_with_no_allocations() -> None:
    from eta_engine.scripts import diamond_prop_allocator as pa

    lb = _leaderboard(prop_ready=[], scores={})
    receipt = pa.compute_allocation(lb, account_size=50_000.0)
    assert receipt.mode == "DEGRADED"
    assert receipt.allocations == []


def test_one_prop_ready_bot_gets_100pct_degraded() -> None:
    """Edge case: leaderboard only blesses 1 bot; it gets the whole account."""
    from eta_engine.scripts import diamond_prop_allocator as pa

    lb = _leaderboard(
        prop_ready=["solo"],
        scores={"solo": 10.0},
    )
    receipt = pa.compute_allocation(lb, account_size=50_000.0)
    assert receipt.mode == "DEGRADED"
    assert len(receipt.allocations) == 1
    assert receipt.allocations[0].weight_pct == 100.0
    assert receipt.allocations[0].capital_usd == 50_000.0


def test_two_prop_ready_bots_get_5050_degraded() -> None:
    """Edge case: 2 bots = 50/50 split (degraded BALANCED)."""
    from eta_engine.scripts import diamond_prop_allocator as pa

    lb = _leaderboard(
        prop_ready=["a", "b"],
        scores={"a": 10.0, "b": 8.0},
    )
    receipt = pa.compute_allocation(lb, account_size=50_000.0)
    assert receipt.mode == "DEGRADED"
    for alloc in receipt.allocations:
        assert alloc.weight_pct == 50.0
        assert alloc.capital_usd == 25_000.0


def test_extra_prop_ready_beyond_three_listed_in_notes() -> None:
    """If 4 PROP_READY bots, only top-3 get capital; 4th in notes."""
    from eta_engine.scripts import diamond_prop_allocator as pa

    lb = _leaderboard(
        prop_ready=["a", "b", "c", "d"],
        scores={"a": 15.0, "b": 10.0, "c": 8.0, "d": 5.0},
    )
    receipt = pa.compute_allocation(lb, account_size=50_000.0)
    bot_ids_with_capital = {a.bot_id for a in receipt.allocations}
    assert "d" not in bot_ids_with_capital
    assert any("d" in n for n in receipt.notes)


# ────────────────────────────────────────────────────────────────────
# Account-size scaling
# ────────────────────────────────────────────────────────────────────


def test_account_size_scales_capital_linearly() -> None:
    """Allocations are proportional to account_size."""
    from eta_engine.scripts import diamond_prop_allocator as pa

    lb = _leaderboard(
        prop_ready=["a", "b", "c"],
        scores={"a": 10.0, "b": 10.0, "c": 10.0},
    )
    r1 = pa.compute_allocation(lb, account_size=50_000.0)
    r2 = pa.compute_allocation(lb, account_size=100_000.0)
    cap1 = sum(a.capital_usd for a in r1.allocations)
    cap2 = sum(a.capital_usd for a in r2.allocations)
    assert abs(cap2 / cap1 - 2.0) < 0.001


# ────────────────────────────────────────────────────────────────────
# Snapshot
# ────────────────────────────────────────────────────────────────────


def test_run_writes_json_receipt(tmp_path: object, monkeypatch: object) -> None:
    """run() writes a JSON receipt at OUT_LATEST."""
    from pathlib import Path

    from eta_engine.scripts import diamond_prop_allocator as pa

    out_path = Path(tmp_path) / "out.json"  # type: ignore[arg-type]
    monkeypatch.setattr(pa, "OUT_LATEST", out_path)  # type: ignore[attr-defined]
    # Stub the leaderboard load to return our fixture
    monkeypatch.setattr(
        pa,
        "_load_leaderboard",
        lambda: _leaderboard(
            prop_ready=["a", "b", "c"],
            scores={"a": 10.0, "b": 10.0, "c": 10.0},
        ),
    )
    summary = pa.run(account_size=50_000.0)
    assert out_path.exists()
    import json

    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert on_disk["mode"] == summary["mode"]
    assert on_disk["account_size"] == 50_000.0
