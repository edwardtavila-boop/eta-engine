"""EVOLUTIONARY TRADING ALGO  //  strategies.runtime_allowlist.

Cached runtime-allowlist derived from :mod:`strategies.oos_qualifier`.

Why this module exists
----------------------
v0.1.41 delivered :func:`qualify_strategies` which emits a
:class:`QualificationReport` containing ``passing_strategies`` --
strategies that cleared the DSR + degradation + min-trades gate on a
given asset. v0.1.40 already wired the policy router to accept a
custom eligibility dict via ``dispatch(... eligibility=...)``.

The missing piece was the wiring between the two:

  1. Somebody has to take the qualifier's ``passing_strategies`` and
     intersect it with :data:`DEFAULT_ELIGIBILITY` so live dispatch sees
     only the strategies that (a) are operationally supported on the
     asset AND (b) survived the most-recent OOS qualification run.
  2. That intersection has to be CACHED with a TTL so the router is
     not re-running a full walk-forward qualification on every bar --
     re-qualification is a periodic cadence, not a per-tick operation.
  3. On cache miss or expiry, the cache has to be able to re-run the
     qualifier and install the fresh allowlist atomically.

:class:`RuntimeAllowlistCache` is that intermediary. It is pure
in-process state -- no I/O, no threads -- so every branch is testable.
The clock is dependency-injected (default ``datetime.now(UTC)``) so
tests can advance time deterministically.

The output shape matches ``dispatch(eligibility=...)`` exactly: call
:meth:`RuntimeAllowlistCache.as_eligibility_map` to get a
``dict[str, tuple[StrategyId, ...]]`` you can hand straight to the
router with zero adaptation.

Pipeline in one line
--------------------

    dispatch(asset, bars, ctx, eligibility=cache.as_eligibility_map())

Call :meth:`ensure_fresh` on a cadence (e.g. once per hour, once per
session roll) and the router's eligibility table stays consistent with
the most-recent OOS verdict without blocking the hot path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from eta_engine.strategies.oos_qualifier import qualify_strategies
from eta_engine.strategies.policy_router import DEFAULT_ELIGIBILITY

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from eta_engine.strategies.models import Bar, StrategyId
    from eta_engine.strategies.oos_qualifier import QualificationReport

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "AllowlistEntry",
    "RuntimeAllowlistCache",
    "intersect_passing_with_base",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_TTL_SECONDS: float = 3600.0
"""Default allowlist freshness window: one hour.

Sized against the typical re-qualification cadence an operator can
afford to run -- a full walk-forward backtest across every
AI-Optimized strategy for a single asset is seconds-to-minutes, not
per-tick, so refreshing the verdict hourly gives plenty of headroom
without starving the cache."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AllowlistEntry:
    """A single per-asset allowlist snapshot.

    Parameters
    ----------
    asset:
        Uppercased ticker (e.g. ``"MNQ"``, ``"BTC"``).
    allowed:
        The intersection of base eligibility and the qualifier's
        passing strategies, preserving the base table's order.
    passing:
        The qualifier's raw ``passing_strategies`` for this asset --
        helpful for dashboards that want to see strategies that passed
        but were not in the base eligibility list.
    base_eligible:
        The base eligibility tuple used to compute this entry
        (captured so stale cache readers can diff against
        :data:`DEFAULT_ELIGIBILITY` mutations).
    report_asset:
        Asset name as reported by the qualifier (often matches
        ``asset`` but may differ in case or punctuation).
    refreshed_at_utc:
        ISO-8601 timestamp captured at entry-creation time. Independent
        of the cache's clock -- useful when entries are persisted or
        shipped across processes.
    """

    asset: str
    allowed: tuple[StrategyId, ...]
    passing: tuple[StrategyId, ...]
    base_eligible: tuple[StrategyId, ...]
    report_asset: str
    refreshed_at_utc: str

    def as_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "allowed": [s.value for s in self.allowed],
            "passing": [s.value for s in self.passing],
            "base_eligible": [s.value for s in self.base_eligible],
            "report_asset": self.report_asset,
            "refreshed_at_utc": self.refreshed_at_utc,
        }


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


def intersect_passing_with_base(
    report: QualificationReport,
    *,
    base_eligibility: Mapping[str, tuple[StrategyId, ...]] | None = None,
    now: datetime | None = None,
) -> AllowlistEntry:
    """Intersect ``report.passing_strategies`` with the base table.

    The intersection preserves the base table's declared ordering so
    the router's confidence-tie-breaker semantics remain stable across
    cache refreshes.

    Parameters
    ----------
    report:
        A :class:`QualificationReport` as produced by
        :func:`qualify_strategies`.
    base_eligibility:
        Mapping asset -> tuple of :class:`StrategyId`. Defaults to
        :data:`DEFAULT_ELIGIBILITY`.
    now:
        Optional wall-clock injection for deterministic tests; defaults
        to ``datetime.now(UTC)``.
    """
    base = base_eligibility if base_eligibility is not None else DEFAULT_ELIGIBILITY
    asset_u = report.asset.upper()
    base_eligible = base.get(asset_u, ())
    passing_set = set(report.passing_strategies)
    # preserve order of base_eligible
    allowed = tuple(s for s in base_eligible if s in passing_set)
    stamp = (now if now is not None else datetime.now(UTC)).isoformat()
    return AllowlistEntry(
        asset=asset_u,
        allowed=allowed,
        passing=tuple(report.passing_strategies),
        base_eligible=tuple(base_eligible),
        report_asset=report.asset,
        refreshed_at_utc=stamp,
    )


# ---------------------------------------------------------------------------
# TTL-backed cache
# ---------------------------------------------------------------------------


def _default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass
class RuntimeAllowlistCache:
    """In-process allowlist cache with per-entry TTL.

    Parameters
    ----------
    ttl_seconds:
        Seconds before an entry is considered stale. Defaults to
        :data:`DEFAULT_TTL_SECONDS`.
    base_eligibility:
        Base eligibility table. Defaults to :data:`DEFAULT_ELIGIBILITY`.
    clock:
        Callable returning the current UTC datetime. Dependency-
        injected so tests can advance time without mocking.
    """

    ttl_seconds: float = DEFAULT_TTL_SECONDS
    base_eligibility: Mapping[str, tuple[StrategyId, ...]] = field(
        default_factory=lambda: DEFAULT_ELIGIBILITY,
    )
    clock: Callable[[], datetime] = field(default_factory=lambda: _default_clock)
    _entries: dict[str, tuple[AllowlistEntry, datetime]] = field(
        default_factory=dict,
    )

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def update(self, report: QualificationReport) -> AllowlistEntry:
        """Install a fresh allowlist entry derived from ``report``."""
        now = self.clock()
        entry = intersect_passing_with_base(
            report,
            base_eligibility=self.base_eligibility,
            now=now,
        )
        self._entries[entry.asset] = (entry, now)
        return entry

    def invalidate(self, asset: str | None = None) -> None:
        """Drop cached entries.

        If ``asset`` is ``None``, the entire cache is cleared. Otherwise
        only that asset's entry (if any) is removed.
        """
        if asset is None:
            self._entries.clear()
        else:
            self._entries.pop(asset.upper(), None)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, asset: str) -> AllowlistEntry | None:
        """Return the fresh entry for ``asset`` or ``None`` if missing/stale."""
        asset_u = asset.upper()
        row = self._entries.get(asset_u)
        if row is None:
            return None
        entry, inserted = row
        if self._age_seconds(inserted) > self.ttl_seconds:
            return None
        return entry

    def is_stale(self, asset: str) -> bool:
        """``True`` if the asset is missing or past its TTL."""
        asset_u = asset.upper()
        row = self._entries.get(asset_u)
        if row is None:
            return True
        _, inserted = row
        return self._age_seconds(inserted) > self.ttl_seconds

    def assets(self) -> tuple[str, ...]:
        """Currently-tracked assets (including stale ones)."""
        return tuple(self._entries.keys())

    def as_eligibility_map(self) -> dict[str, tuple[StrategyId, ...]]:
        """Return a dict suitable for ``dispatch(eligibility=...)``.

        Only assets with fresh (non-stale) entries are included. The
        values are the intersected allowlist tuples, so routing
        consumers never see a strategy that has failed qualification
        on that asset.
        """
        out: dict[str, tuple[StrategyId, ...]] = {}
        for asset, (entry, inserted) in self._entries.items():
            if self._age_seconds(inserted) <= self.ttl_seconds:
                out[asset] = entry.allowed
        return out

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def ensure_fresh(
        self,
        asset: str,
        bars: list[Bar],
        *,
        qualifier: Callable[..., QualificationReport] | None = None,
        **qualifier_kwargs: object,
    ) -> AllowlistEntry:
        """Return a fresh entry, re-running the qualifier if needed.

        Parameters
        ----------
        asset:
            Asset symbol to ensure fresh for.
        bars:
            Bar tape passed through to the qualifier on refresh.
        qualifier:
            Optional override for the qualifier callable. Defaults to
            :func:`qualify_strategies`. Any extra kwargs are forwarded
            (useful for injecting a custom gate, harness_config, or
            registry in tests).
        """
        fresh = self.get(asset)
        if fresh is not None:
            return fresh
        fn = qualifier if qualifier is not None else qualify_strategies
        report = fn(bars, asset, **qualifier_kwargs)
        return self.update(report)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _age_seconds(self, inserted: datetime) -> float:
        return (self.clock() - inserted).total_seconds()
