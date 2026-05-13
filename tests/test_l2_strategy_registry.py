# ruff: noqa: N802
"""Tests for the L2 strategy registry + session-start hook (Phase 3)."""

from __future__ import annotations

from datetime import UTC, datetime

from eta_engine.strategies import l2_overlay
from eta_engine.strategies import l2_strategy_registry as reg


def test_registry_has_at_least_book_imbalance() -> None:
    assert any(s.bot_id == "mnq_book_imbalance_shadow" for s in reg.L2_STRATEGIES)


def test_get_strategy_lookup() -> None:
    entry = reg.get_l2_strategy("mnq_book_imbalance_shadow")
    assert entry is not None
    assert entry.strategy_id == "book_imbalance_v1"
    assert reg.get_l2_strategy("unknown") is None


def test_iter_default_returns_non_deactivated() -> None:
    active = reg.iter_active_l2_strategies()
    assert len(active) >= 1
    assert all(s.promotion_status != "deactivated" for s in active)


def test_iter_filters_by_status() -> None:
    paper = reg.iter_active_l2_strategies(statuses=("paper",))
    # Initially no strategy is in paper — all are shadow
    assert all(s.promotion_status == "paper" for s in paper)


def test_required_capture_symbols_includes_MNQ() -> None:
    syms = reg.required_capture_symbols()
    assert "MNQ" in syms


def test_required_capture_symbols_excludes_aggressor_flow() -> None:
    """aggressor_flow has capture_required=False (consumes bars only)."""
    # If aggressor_flow were the ONLY active strategy, MNQ wouldn't be
    # in the required set.  We verify by checking the entry directly.
    entry = reg.get_l2_strategy("mnq_aggressor_flow_shadow")
    assert entry is not None
    assert entry.capture_required is False


def test_session_start_hook_marks_captures_expected() -> None:
    l2_overlay.clear_captures_expected()
    now = datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC)
    summary = reg.session_start_hook(when=now)
    assert "MNQ" in summary["symbols_marked_expected"]
    assert summary["n_active_strategies"] >= 1
    # Confirm the overlay sentinel is set
    assert l2_overlay._captures_expected_today("MNQ", when=now) is True


def test_session_start_hook_idempotent() -> None:
    """Calling twice with same date should not double-add or error."""
    l2_overlay.clear_captures_expected()
    now = datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC)
    reg.session_start_hook(when=now)
    reg.session_start_hook(when=now)  # second call
    assert l2_overlay._captures_expected_today("MNQ", when=now) is True


def test_session_start_hook_can_be_filtered_to_paper_only() -> None:
    """When no strategy is in 'paper' status, hook produces empty list."""
    l2_overlay.clear_captures_expected()
    summary = reg.session_start_hook(statuses=("paper",))
    # All current strategies are shadow, so paper filter yields nothing
    assert summary["n_active_strategies"] == 0
    assert summary["symbols_marked_expected"] == []


def test_factories_produce_objects_with_evaluate_method() -> None:
    """Every factory must return an object with .evaluate or similar."""
    for entry in reg.L2_STRATEGIES:
        obj = entry.factory()
        # All L2 strategies expose either evaluate (snap-based) or
        # update (regime filter)
        assert hasattr(obj, "evaluate") or hasattr(obj, "update")


def test_falsification_criteria_present_for_all_entry_strategies() -> None:
    """Every strategy with max_qty_contracts > 0 must have
    falsification criteria — pre-committed retirement triggers."""
    for entry in reg.L2_STRATEGIES:
        if entry.max_qty_contracts > 0:
            assert entry.falsification, f"{entry.bot_id} missing falsification"
            assert (
                "retire_if_oos_sharpe_lt" in entry.falsification
                or "retire_after_n_days_shadow_loss" in entry.falsification
            )


def test_sizing_policy_hard_capped() -> None:
    """No strategy may have max_qty > 1 in shadow status — paper-soak
    discipline."""
    for entry in reg.L2_STRATEGIES:
        if entry.promotion_status == "shadow":
            assert entry.max_qty_contracts <= 1, f"{entry.bot_id} shadow status with qty>{entry.max_qty_contracts}"
