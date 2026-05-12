"""Diamond protection invariants — three layers must hold.

The 9 diamond bots have proven multi-session profitability and are
protected from auto-deactivation.  Three orthogonal layers enforce
the protection so a single mistake in any one of them does not
silently kill a diamond:

  Layer 1: capital_allocator gives each diamond >= $2,000 minimum
           capital regardless of P&L, and "active" status regardless
           of profitability.
  Layer 2: kaizen_loop.run_loop() refuses to write a RETIRE override
           for any bot in DIAMOND_BOTS.
  Layer 3: per_bot_registry.is_active() returns True for any diamond
           regardless of source-level `deactivated: True` markers or
           kaizen sidecar overrides.

These tests are the contract.  Any change that breaks one of them
must be a deliberate code change — not a config drift, not a
sidecar override, not a CI bot's RETIRE recommendation.
"""

# ruff: noqa: N802, PLR2004
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.feeds.capital_allocator import (
    DIAMOND_BOTS,
    DIAMOND_MIN_CAPITAL,
)

if TYPE_CHECKING:
    import pytest
from eta_engine.strategies.per_bot_registry import (
    ASSIGNMENTS,
    StrategyAssignment,
    get_for_bot,
    is_active,
)

# ────────────────────────────────────────────────────────────────────
# The 15 expected diamonds (operator decision 2026-05-12 wave-14
# fleet expansion: conquer futures + commodities + crypto verticals)
# ────────────────────────────────────────────────────────────────────

EXPECTED_DIAMONDS = frozenset(
    {
        # Original 8
        "mnq_futures_sage",
        "nq_futures_sage",
        "cl_momentum",
        "mcl_sweep_reclaim",
        "mgc_sweep_reclaim",
        "eur_sweep_reclaim",
        "gc_momentum",
        "cl_macro",
        # 9th (canonical-data kaizen, m2k promotion)
        "m2k_sweep_reclaim",
        # 10th-15th (wave-14 fleet expansion)
        "met_sweep_reclaim",
        "mes_sweep_reclaim_v2",
        "eur_range",
        "ng_sweep_reclaim",
        "volume_profile_btc",
        "mes_sweep_reclaim",
    }
)


# ────────────────────────────────────────────────────────────────────
# Layer 0 — the registry list itself
# ────────────────────────────────────────────────────────────────────


def test_diamond_set_count_matches_expected() -> None:
    """The fleet count is fragile to silent additions/removals; pin it
    to the EXPECTED_DIAMONDS frozenset and update both together."""
    assert len(DIAMOND_BOTS) == len(EXPECTED_DIAMONDS) == 15


def test_diamond_set_matches_operator_decision_2026_05_12() -> None:
    """Tripwire: the operator-decreed set is locked.  Any change here
    is a deliberate revision of the diamond decision — should come
    with a commit message that names the bot and the rationale."""
    assert set(DIAMOND_BOTS) == set(EXPECTED_DIAMONDS), (
        f"DIAMOND_BOTS drifted from operator decision.\n"
        f"  expected: {sorted(EXPECTED_DIAMONDS)}\n"
        f"  found:    {sorted(DIAMOND_BOTS)}"
    )


def test_every_diamond_exists_in_assignments() -> None:
    """If a diamond name is in DIAMOND_BOTS but not in ASSIGNMENTS,
    capital allocation refers to a phantom bot."""
    bot_ids = {a.bot_id for a in ASSIGNMENTS}
    missing = DIAMOND_BOTS - bot_ids
    assert not missing, f"diamonds missing from registry: {sorted(missing)}"


def test_diamond_min_capital_is_at_least_2000() -> None:
    """Operator commitment: every diamond gets >= $2,000 even at full
    portfolio shutoff.  Lowering this is a safety-budget cut and must
    be a deliberate edit."""
    assert DIAMOND_MIN_CAPITAL >= 2000.0


# ────────────────────────────────────────────────────────────────────
# Layer 1 — capital_allocator floor
# ────────────────────────────────────────────────────────────────────


def test_capital_allocator_diamond_path_assigns_min_capital(tmp_path: Path) -> None:
    """A diamond bot with ZERO sessions and ZERO P&L still receives
    >= DIAMOND_MIN_CAPITAL and status=active."""
    from eta_engine.feeds.capital_allocator import compute_allocations

    # Build a ledger where a diamond has 2 sessions both flat ($0 P&L).
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(
        json.dumps(
            {
                "bot_sessions": {
                    "cl_momentum": [
                        {"pnl": 0.0},
                        {"pnl": 0.0},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    alloc = compute_allocations(ledger_path, total_capital=100_000.0)
    diamond_alloc = alloc.bots.get("cl_momentum")
    assert diamond_alloc is not None
    assert diamond_alloc.capital >= DIAMOND_MIN_CAPITAL
    assert diamond_alloc.status == "active"


def test_capital_allocator_non_diamond_unprofitable_is_paused(tmp_path: Path) -> None:
    """Sanity check: protection is targeted.  A non-diamond bot with
    zero P&L is paused (status='paused'), confirming that the diamond
    branch is what saves diamonds."""
    from eta_engine.feeds.capital_allocator import compute_allocations

    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(
        json.dumps(
            {
                "bot_sessions": {
                    "not_a_diamond_xyz": [
                        {"pnl": 0.0},
                        {"pnl": 0.0},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    alloc = compute_allocations(ledger_path, total_capital=100_000.0)
    bot_alloc = alloc.bots.get("not_a_diamond_xyz")
    if bot_alloc is not None:
        # If included in allocation, status must be paused.
        assert bot_alloc.status == "paused"


# ────────────────────────────────────────────────────────────────────
# Layer 2 — kaizen_loop refuses to RETIRE diamonds
# ────────────────────────────────────────────────────────────────────


def test_kaizen_loop_imports_diamond_bots() -> None:
    """run_loop() pulls DIAMOND_BOTS at call time — verify the import
    target exists and is a set-like object."""
    from eta_engine.feeds.capital_allocator import (
        DIAMOND_BOTS as _DIAMONDS_FROM_ALLOCATOR,
    )

    assert isinstance(_DIAMONDS_FROM_ALLOCATOR, (set, frozenset))
    assert len(_DIAMONDS_FROM_ALLOCATOR) == 15


def test_kaizen_loop_source_contains_diamond_skip_branch() -> None:
    """The diamond-protection branch in run_loop() must NOT be
    accidentally removed during refactors."""
    src = Path(__file__).resolve().parents[1] / "scripts" / "kaizen_loop.py"
    text = src.read_text(encoding="utf-8")
    assert "DIAMOND_BOTS" in text
    assert "PROTECTED_DIAMOND" in text
    assert "diamond_protected_count" in text


# ────────────────────────────────────────────────────────────────────
# Layer 3 — is_active() veto
# ────────────────────────────────────────────────────────────────────


def test_is_active_returns_true_for_every_diamond() -> None:
    """The strictest invariant: at supervisor startup, every diamond
    must report is_active()=True regardless of any deactivation
    state."""
    for bot_id in DIAMOND_BOTS:
        assignment = get_for_bot(bot_id)
        assert assignment is not None, f"diamond missing from registry: {bot_id}"
        assert is_active(assignment) is True, f"diamond {bot_id} reports is_active()=False — protection broken"


def test_is_active_ignores_deactivated_marker_on_diamond() -> None:
    """A synthetic StrategyAssignment with deactivated=True must
    still be is_active()=True when its bot_id is in DIAMOND_BOTS."""
    fake_diamond = StrategyAssignment(
        bot_id="cl_momentum",  # real diamond
        strategy_id="cl_momentum_v1",
        symbol="CL1",
        timeframe="1h",
        scorer_name="cl",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=365,
        step_days=90,
        min_trades_per_window=5,
        strategy_kind="commodity_momentum",
        rationale="test",
        extras={"deactivated": True, "deactivation_reason": "test poison"},
    )
    assert is_active(fake_diamond) is True


def test_is_active_still_disables_non_diamond_with_deactivated_marker() -> None:
    """Targeted protection: non-diamonds with deactivated=True must
    still report is_active()=False."""
    fake_non_diamond = StrategyAssignment(
        bot_id="not_a_diamond_xyz",
        strategy_id="test_v1",
        symbol="MNQ",
        timeframe="5m",
        scorer_name="mnq",
        confluence_threshold=0.0,
        block_regimes=frozenset(),
        window_days=90,
        step_days=30,
        min_trades_per_window=3,
        strategy_kind="test",
        rationale="test",
        extras={"deactivated": True, "deactivation_reason": "test"},
    )
    assert is_active(fake_non_diamond) is False


# ────────────────────────────────────────────────────────────────────
# Cross-layer integration
# ────────────────────────────────────────────────────────────────────


def test_no_diamond_is_currently_in_kaizen_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Even if a stale kaizen override sneaks in for a diamond,
    is_active() ignores it.  This test seeds a fake override file
    and confirms the diamond stays active."""
    from eta_engine.strategies import per_bot_registry

    fake_override = tmp_path / "kaizen_overrides.json"
    fake_override.write_text(
        json.dumps(
            {
                "deactivated": {
                    "mnq_futures_sage": {
                        "applied_at": "2026-05-12T00:00:00+00:00",
                        "reason": "synthetic test seed — protection should ignore",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(per_bot_registry, "_KAIZEN_OVERRIDES_PATH", fake_override)
    assignment = get_for_bot("mnq_futures_sage")
    assert assignment is not None
    assert is_active(assignment) is True


def test_diamond_protection_includes_at_least_5_robust_bots() -> None:
    """Documentation invariant: the operator's 2026-05-12 decision
    classified 5 ROBUST + 2 FRAGILE + 1 edge.  If the diamond set
    ever drops below 5 ROBUST bots, the average tier is degrading
    and the operator should review."""
    robust_diamonds = {
        "mnq_futures_sage",
        "nq_futures_sage",
        "cl_momentum",
        "mcl_sweep_reclaim",
        "mgc_sweep_reclaim",
    }
    # At least all 5 ROBUST must be in DIAMOND_BOTS.
    assert robust_diamonds.issubset(DIAMOND_BOTS), f"ROBUST diamonds missing: {robust_diamonds - DIAMOND_BOTS}"


def test_diamond_correlation_groups_documented() -> None:
    """The 8 diamonds include correlated pairs:
        - mnq_futures_sage / nq_futures_sage (same index)
        - cl_momentum / mcl_sweep_reclaim / cl_macro (same underlying CL)
        - mgc_sweep_reclaim / gc_momentum (same underlying GC)
    These groups must remain in DIAMOND_BOTS together OR be
    documented as a deliberate desync in the protection doc."""
    nq_group = {"mnq_futures_sage", "nq_futures_sage"}
    cl_group = {"cl_momentum", "mcl_sweep_reclaim", "cl_macro"}
    gc_group = {"mgc_sweep_reclaim", "gc_momentum"}
    # All groups intersect DIAMOND_BOTS in full (operator decision)
    assert nq_group.issubset(DIAMOND_BOTS)
    assert cl_group.issubset(DIAMOND_BOTS)
    assert gc_group.issubset(DIAMOND_BOTS)


def test_diamond_protection_doc_exists() -> None:
    """Operator-facing truth surface must exist."""
    doc = Path(__file__).resolve().parents[1] / "docs" / "DIAMOND_PROTECTION_2026_05_12.md"
    assert doc.exists()
    text = doc.read_text(encoding="utf-8")
    # Doc must enumerate all 8 diamonds
    for bot_id in EXPECTED_DIAMONDS:
        assert f"`{bot_id}`" in text, f"doc missing {bot_id}"
