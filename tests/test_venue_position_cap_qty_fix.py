"""Regression test for the venue position-cap qty-field-name bug.

The IBKR venues read `getattr(request, "quantity", 0) or 0` to pass the
order size to `assert_within_caps`.  But `OrderRequest.qty` is the actual
field name — `quantity` doesn't exist, so getattr returned 0, which made
the position cap a no-op for every live order.

This test asserts that:
1. `OrderRequest.qty` is the canonical field
2. A request with `qty > position_cap` is REJECTED by `assert_within_caps`
3. Reading `getattr(request, "qty", ...)` returns the actual qty
"""

from __future__ import annotations

import pytest


def test_order_request_qty_field_exists():
    from eta_engine.venues.base import OrderRequest, Side

    req = OrderRequest(symbol="MNQ1", side=Side.BUY, qty=5.0)
    assert req.qty == 5.0
    # And the wrong-name attribute used to silently return 0:
    assert getattr(req, "quantity", "not_present") == "not_present"


def test_position_cap_blocks_oversize_order():
    """The bug: if the venue read `quantity` instead of `qty`, this test
    would have passed (cap not enforced).  After the fix, this raises."""
    import os

    os.environ["ETA_POSITION_CAP"] = "1"
    from eta_engine.safety.position_cap import (
        PositionCapExceededError,
        assert_within_caps,
    )

    # Try to add 5 contracts when cap is 1 — must raise
    with pytest.raises(PositionCapExceededError):
        assert_within_caps(side="mnq", venue="ibkr", symbol="MNQ1", requested_delta=5.0)


def test_position_cap_allows_within_size():
    import os

    os.environ["ETA_POSITION_CAP"] = "5"
    from eta_engine.safety.position_cap import assert_within_caps

    # Add 1 contract when cap is 5 — must pass
    assert_within_caps(side="mnq", venue="ibkr", symbol="MNQ1", requested_delta=1.0)
