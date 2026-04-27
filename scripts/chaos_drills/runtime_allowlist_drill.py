"""EVOLUTIONARY TRADING ALGO // scripts.chaos_drills.runtime_allowlist_drill.

Drill: validate the runtime-allowlist refresh cycle.

What this drill asserts
-----------------------
:class:`strategies.runtime_allowlist.RuntimeAllowlistCache` is the TTL'd
in-process intermediary between the walk-forward OOS qualifier and the
live policy router. A silent regression could:

* Leak a stale allowlist past the TTL (router routes to strategies the
  qualifier has since demoted).
* Drop a fresh entry too early (router starves on `None` eligibility).
* Silently mutate base-eligibility ordering (breaks the confidence
  tie-breaker semantics documented in :mod:`strategies.policy_router`).
* Fail to clear on ``invalidate(asset)`` (poisoned cache survives a
  manual flush).

The drill wires a mutable :class:`_FakeClock` into the cache, installs
a hand-built :class:`QualificationReport`, and exercises the four
branches:

1. Fresh ``get(asset)`` inside TTL -- returns the installed entry.
2. ``get`` after advancing clock past TTL -- returns ``None`` (stale).
3. ``invalidate(asset)`` -- drops the entry; subsequent ``get`` is
   ``None`` even when the clock is rewound inside TTL.
4. The intersected ``entry.allowed`` tuple preserves the base-
   eligibility ordering from :data:`DEFAULT_ELIGIBILITY`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from eta_engine.scripts.chaos_drills._common import drill_result
from eta_engine.strategies.oos_qualifier import (
    DEFAULT_QUALIFICATION_GATE,
    QualificationReport,
    StrategyQualification,
)
from eta_engine.strategies.policy_router import DEFAULT_ELIGIBILITY
from eta_engine.strategies.runtime_allowlist import RuntimeAllowlistCache

if TYPE_CHECKING:
    from pathlib import Path

    from eta_engine.strategies.models import StrategyId

__all__ = ["drill_runtime_allowlist"]


@dataclass
class _FakeClock:
    """Mutable clock stub whose ``now()`` advances on demand."""

    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


def _passing_qualification(sid: StrategyId) -> StrategyQualification:
    """Build a passing StrategyQualification for ``sid`` on MNQ."""
    return StrategyQualification(
        strategy=sid,
        asset="MNQ",
        n_windows=4,
        avg_is_sharpe=2.1,
        avg_oos_sharpe=1.8,
        avg_degradation_pct=0.10,
        dsr=0.72,
        n_trades_is_total=240,
        n_trades_oos_total=120,
        passes_gate=True,
        fail_reasons=(),
    )


def drill_runtime_allowlist(sandbox: Path) -> dict[str, Any]:  # noqa: ARG001
    """Exercise TTL freshness, invalidate(), and ordering guarantees."""
    # Install a deterministic clock so TTL boundaries are exact.
    clock = _FakeClock(current=datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC))
    ttl = 3600.0
    cache = RuntimeAllowlistCache(ttl_seconds=ttl, clock=clock.now)

    # Pick the first two base-eligible MNQ strategies so intersection is
    # non-empty and we can verify ordering preservation.
    mnq_base = DEFAULT_ELIGIBILITY["MNQ"]
    if len(mnq_base) < 2:
        return drill_result(
            "runtime_allowlist",
            passed=False,
            details=f"MNQ base eligibility has <2 entries: {mnq_base}",
        )
    # Feed the qualifier report out of base-declared order to prove
    # intersect_passing_with_base() re-imposes the base ordering.
    passing_ids = (mnq_base[1], mnq_base[0])
    report = QualificationReport(
        asset="MNQ",
        gate=DEFAULT_QUALIFICATION_GATE,
        n_windows_requested=4,
        n_windows_executed=4,
        per_window=(),
        qualifications=tuple(_passing_qualification(sid) for sid in passing_ids),
    )

    # --- Step 1: install + fresh get --------------------------------------
    installed = cache.update(report)
    got_fresh = cache.get("MNQ")
    if got_fresh is None:
        return drill_result(
            "runtime_allowlist",
            passed=False,
            details="cache.get('MNQ') returned None immediately after update()",
        )
    if got_fresh.allowed != installed.allowed:
        return drill_result(
            "runtime_allowlist",
            passed=False,
            details=(f"get() returned a different entry than update(): {got_fresh.allowed} vs {installed.allowed}"),
        )

    # intersect_passing_with_base must preserve base-declared ordering.
    expected_order = tuple(s for s in mnq_base if s in set(passing_ids))
    if got_fresh.allowed != expected_order:
        return drill_result(
            "runtime_allowlist",
            passed=False,
            details=(f"allowed ordering {got_fresh.allowed} did not match base ordering {expected_order}"),
        )

    # Eligibility map snapshot (fresh branch) must round-trip.
    elig_map_fresh = cache.as_eligibility_map()
    if elig_map_fresh.get("MNQ") != expected_order:
        return drill_result(
            "runtime_allowlist",
            passed=False,
            details=(
                f"as_eligibility_map fresh branch returned {elig_map_fresh.get('MNQ')!r} (expected {expected_order!r})"
            ),
        )

    # --- Step 2: advance past TTL, entry must be stale --------------------
    clock.advance(ttl + 1.0)
    got_stale = cache.get("MNQ")
    if got_stale is not None:
        return drill_result(
            "runtime_allowlist",
            passed=False,
            details=f"get('MNQ') returned non-None after TTL expiry: {got_stale!r}",
        )
    if not cache.is_stale("MNQ"):
        return drill_result(
            "runtime_allowlist",
            passed=False,
            details="is_stale('MNQ') returned False after TTL expiry",
        )
    elig_map_stale = cache.as_eligibility_map()
    if "MNQ" in elig_map_stale:
        return drill_result(
            "runtime_allowlist",
            passed=False,
            details=f"stale entry leaked into as_eligibility_map: {elig_map_stale}",
        )

    # --- Step 3: refresh, then invalidate must drop the entry --------------
    cache.update(report)  # installed at current (post-expiry) clock
    if cache.get("MNQ") is None:
        return drill_result(
            "runtime_allowlist",
            passed=False,
            details="cache.get('MNQ') returned None after re-update",
        )
    cache.invalidate("MNQ")
    if cache.get("MNQ") is not None:
        return drill_result(
            "runtime_allowlist",
            passed=False,
            details="invalidate('MNQ') did not drop the entry",
        )
    if "MNQ" in cache.assets():
        return drill_result(
            "runtime_allowlist",
            passed=False,
            details=f"assets() still contains MNQ after invalidate: {cache.assets()}",
        )

    return drill_result(
        "runtime_allowlist",
        passed=True,
        details=(
            "fresh get returned installed entry; TTL expiry blanked get + "
            "as_eligibility_map; invalidate dropped the entry; base ordering preserved"
        ),
        observed={
            "ttl_seconds": ttl,
            "allowed": [s.value for s in got_fresh.allowed],
            "expected_order": [s.value for s in expected_order],
            "mnq_base_len": len(mnq_base),
        },
    )
