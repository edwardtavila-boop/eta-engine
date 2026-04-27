"""Smart order router tests — P5_EXEC smart_router."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eta_engine.core.smart_router import ParentOrder, route


def _parent(qty: float = 100.0, limit: float | None = 500.0) -> ParentOrder:
    return ParentOrder(
        symbol="MNQ",
        side="buy",
        total_qty=qty,
        limit_price=limit,
    )


def _now() -> datetime:
    return datetime(2026, 4, 17, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_rejects_zero_qty() -> None:
    with pytest.raises(ValueError, match="total_qty must be positive"):
        route(
            ParentOrder(symbol="MNQ", side="buy", total_qty=0.0),
            "iceberg",
            now=_now(),
        )


def test_rejects_unknown_policy() -> None:
    with pytest.raises(ValueError, match="unknown policy"):
        route(_parent(), "vwap", now=_now())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Iceberg
# ---------------------------------------------------------------------------


def test_iceberg_splits_into_reveal_chunks() -> None:
    parent = ParentOrder(
        symbol="MNQ",
        side="buy",
        total_qty=100.0,
        limit_price=500.0,
        reveal_size=25.0,
    )
    plan = route(parent, "iceberg", now=_now())
    assert plan.policy == "iceberg"
    assert len(plan.children) == 4
    assert all(c.qty == 25.0 for c in plan.children)
    assert plan.remainder_qty == 0.0


def test_iceberg_handles_remainder() -> None:
    parent = ParentOrder(
        symbol="MNQ",
        side="buy",
        total_qty=100.0,
        limit_price=500.0,
        reveal_size=30.0,
    )
    plan = route(parent, "iceberg", now=_now())
    # 30 + 30 + 30 + 10 = 100
    assert len(plan.children) == 4
    assert plan.children[-1].qty == pytest.approx(10.0)


def test_iceberg_rejects_zero_reveal() -> None:
    parent = ParentOrder(
        symbol="MNQ",
        side="buy",
        total_qty=100.0,
        reveal_size=0.0,
    )
    with pytest.raises(ValueError, match="iceberg reveal_size"):
        route(parent, "iceberg", now=_now())


# ---------------------------------------------------------------------------
# TWAP
# ---------------------------------------------------------------------------


def test_twap_slices_evenly() -> None:
    parent = ParentOrder(
        symbol="MNQ",
        side="sell",
        total_qty=100.0,
        limit_price=500.0,
        num_slices=5,
        duration_seconds=300,
    )
    plan = route(parent, "twap", now=_now())
    assert len(plan.children) == 5
    assert all(c.qty == pytest.approx(20.0) for c in plan.children)
    # Slice 0 at t=0, slice 4 at t=240s (5 slices × 60s gap)
    assert (plan.children[-1].scheduled_ts - plan.children[0].scheduled_ts).total_seconds() == 240.0


def test_twap_defaults_kick_in() -> None:
    parent = ParentOrder(symbol="MNQ", side="buy", total_qty=100.0, limit_price=500.0)
    plan = route(parent, "twap", now=_now())
    # Default num_slices=10
    assert len(plan.children) == 10


def test_twap_without_limit_uses_market() -> None:
    parent = ParentOrder(symbol="MNQ", side="buy", total_qty=50.0, num_slices=5)
    plan = route(parent, "twap", now=_now())
    assert all(c.order_type == "MARKET" for c in plan.children)


# ---------------------------------------------------------------------------
# Post-only
# ---------------------------------------------------------------------------


def test_post_only_requires_limit_price() -> None:
    parent = ParentOrder(symbol="MNQ", side="buy", total_qty=10.0)
    with pytest.raises(ValueError, match="requires an explicit limit_price"):
        route(parent, "post_only", now=_now(), best_bid=499.0, best_ask=500.0)


def test_post_only_accepts_passive_buy() -> None:
    parent = ParentOrder(symbol="MNQ", side="buy", total_qty=10.0, limit_price=499.0)
    plan = route(parent, "post_only", now=_now(), best_bid=499.0, best_ask=500.0)
    # 499 < best_ask=500 → doesn't cross → accepted
    assert len(plan.children) == 1
    assert plan.children[0].order_type == "POST_ONLY"


def test_post_only_skips_crossing_buy() -> None:
    parent = ParentOrder(symbol="MNQ", side="buy", total_qty=10.0, limit_price=500.5)
    plan = route(parent, "post_only", now=_now(), best_bid=499.0, best_ask=500.0)
    # 500.5 >= 500 (ask) → crosses → skip
    assert plan.children == []
    assert plan.remainder_qty == 10.0
    assert any("crossing" in n or "cross" in n for n in plan.notes)


def test_post_only_market_fallback() -> None:
    parent = ParentOrder(
        symbol="MNQ",
        side="buy",
        total_qty=10.0,
        limit_price=500.5,
        allow_market_fallback=True,
    )
    plan = route(parent, "post_only", now=_now(), best_bid=499.0, best_ask=500.0)
    assert len(plan.children) == 1
    assert plan.children[0].order_type == "MARKET"
    assert any("market fallback" in n for n in plan.notes)
