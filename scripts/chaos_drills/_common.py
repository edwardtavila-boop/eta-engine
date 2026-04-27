"""EVOLUTIONARY TRADING ALGO // scripts.chaos_drills._common.

Shared result-shape helper for every chaos drill in this package.

Keeps the emitted dict identical to what :mod:`scripts.chaos_drill`
already expects (``drill``, ``passed``, ``details``, ``observed``,
``ts``), so drills from this package are fully compatible with the
existing runner and JSON report path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

__all__ = ["drill_result"]


def drill_result(
    name: str,
    *,
    passed: bool,
    details: str,
    observed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the standard chaos-drill result dict."""
    return {
        "drill": name,
        "passed": passed,
        "details": details,
        "observed": observed or {},
        "ts": datetime.now(UTC).isoformat(),
    }
