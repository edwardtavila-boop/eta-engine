"""EVOLUTIONARY TRADING ALGO  //  strategies.allowlist_scheduler.

Cadence-driven refresh loop on top of :mod:`strategies.runtime_allowlist`.

Context
-------
v0.1.42 delivered :class:`RuntimeAllowlistCache` -- a TTL-backed
allowlist that converts a :class:`QualificationReport` into the exact
eligibility dict ``dispatch(eligibility=...)`` consumes. But the cache
only knows how to ``update`` (install a fresh entry) and ``get`` (read
the freshest entry). Somebody has to call ``cache.ensure_fresh`` on a
cadence, and that somebody has to be cheap enough to run on every
bar in the live bot controller's tick loop.

:class:`AllowlistScheduler` is that cheap bar-by-bar tick. It owns

  1. A :class:`RefreshTrigger` -- the declarative cadence (every N
     bars, every S seconds, minimum warmup bars before the first
     refresh).
  2. Per-asset "last refresh" bookkeeping -- both wall-clock time and
     bar-count high-water mark, so the scheduler can fire on the
     earlier of the two triggers.
  3. A one-shot :meth:`tick` call the bot controller invokes on every
     bar with ``(asset, bars)``. The scheduler checks the trigger
     predicate and either re-runs the qualifier + updates the cache
     (returning the new :class:`AllowlistEntry`) or returns ``None``.

The first successful refresh anchors the per-asset bookkeeping: later
ticks compare against it to decide whether enough bars/seconds have
accumulated to justify another refresh.

Why split from the cache?
-------------------------
- The cache alone is inert: it does not know when to refresh, only
  whether its entries are fresh.
- The scheduler is the policy layer that says "refresh now" or "skip";
  the cache is the storage layer.
- Splitting keeps the cache's invariants (TTL, order preservation)
  independent of the scheduler's cadence rules.

Minimal live-bot usage
----------------------

    trigger = RefreshTrigger(every_n_bars=500, min_bars_before_first=120)
    cache   = RuntimeAllowlistCache(ttl_seconds=3600.0)
    sched   = AllowlistScheduler(cache=cache, trigger=trigger)

    # per-bar tick on the controller:
    sched.tick("MNQ", bars)

    # per-bar dispatch also on the controller:
    dispatch("MNQ", bars, ctx, eligibility=cache.as_eligibility_map())

On a cold start the scheduler waits until ``len(bars) >=
min_bars_before_first`` before firing its first refresh so the
qualifier's walk-forward windows have enough warmup to be meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from eta_engine.strategies.oos_qualifier import qualify_strategies

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.strategies.models import Bar
    from eta_engine.strategies.oos_qualifier import QualificationReport
    from eta_engine.strategies.runtime_allowlist import (
        AllowlistEntry,
        RuntimeAllowlistCache,
    )

__all__ = [
    "AllowlistScheduler",
    "RefreshTrigger",
]


# ---------------------------------------------------------------------------
# RefreshTrigger
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefreshTrigger:
    """Declarative refresh cadence for the allowlist scheduler.

    Parameters
    ----------
    every_n_bars:
        Trigger a refresh whenever the bar count has grown by at
        least this many bars since the last refresh. ``None`` disables
        the bar-count trigger.
    every_seconds:
        Trigger a refresh whenever at least this many wall-clock
        seconds have elapsed since the last refresh. ``None`` disables
        the time trigger.
    min_bars_before_first:
        The very first refresh is suppressed until the bar tape is at
        least this long. This keeps the qualifier's walk-forward
        windows meaningful on a cold start.

    At least one of ``every_n_bars`` / ``every_seconds`` must be set.
    """

    every_n_bars: int | None = None
    every_seconds: float | None = None
    min_bars_before_first: int = 50

    def __post_init__(self) -> None:
        if self.every_n_bars is None and self.every_seconds is None:
            msg = "RefreshTrigger requires at least one of every_n_bars / every_seconds to be set"
            raise ValueError(msg)
        if self.every_n_bars is not None and self.every_n_bars < 1:
            msg = "every_n_bars must be >= 1"
            raise ValueError(msg)
        if self.every_seconds is not None and self.every_seconds <= 0.0:
            msg = "every_seconds must be > 0"
            raise ValueError(msg)
        if self.min_bars_before_first < 0:
            msg = "min_bars_before_first must be >= 0"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# AllowlistScheduler
# ---------------------------------------------------------------------------


def _default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass
class AllowlistScheduler:
    """Bar-by-bar orchestrator for :class:`RuntimeAllowlistCache`.

    Parameters
    ----------
    cache:
        The allowlist cache to refresh.
    trigger:
        The refresh cadence.
    clock:
        Callable returning the current UTC datetime. Dependency-
        injected for deterministic tests.
    """

    cache: RuntimeAllowlistCache
    trigger: RefreshTrigger
    clock: Callable[[], datetime] = field(default_factory=lambda: _default_clock)
    _last_refresh_time: dict[str, datetime] = field(default_factory=dict)
    _last_refresh_bar_count: dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Primary tick
    # ------------------------------------------------------------------

    def tick(
        self,
        asset: str,
        bars: list[Bar],
        *,
        qualifier: Callable[..., QualificationReport] | None = None,
        **qualifier_kwargs: object,
    ) -> AllowlistEntry | None:
        """Per-bar entry point.

        Returns the newly-installed :class:`AllowlistEntry` if the
        trigger fired and a refresh happened this call. Returns
        ``None`` when the trigger did not fire (cheap no-op path).

        The bot controller should call this on every bar regardless of
        whether a refresh is expected; the scheduler internally decides
        whether to do work.
        """
        asset_u = asset.upper()
        bar_count = len(bars)
        # Warmup guard: the first refresh needs enough bars that the
        # qualifier's walk-forward windows are meaningful.
        if bar_count < self.trigger.min_bars_before_first:
            return None
        if not self._should_refresh(asset_u, bar_count):
            return None
        return self._do_refresh(
            asset_u,
            bars,
            bar_count,
            qualifier,
            **qualifier_kwargs,
        )

    # ------------------------------------------------------------------
    # Force paths
    # ------------------------------------------------------------------

    def force_refresh(
        self,
        asset: str,
        bars: list[Bar],
        *,
        qualifier: Callable[..., QualificationReport] | None = None,
        **qualifier_kwargs: object,
    ) -> AllowlistEntry:
        """Refresh NOW, ignoring the trigger (useful on operator event)."""
        asset_u = asset.upper()
        bar_count = len(bars)
        return self._do_refresh(
            asset_u,
            bars,
            bar_count,
            qualifier,
            **qualifier_kwargs,
        )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def last_refresh_at(self, asset: str) -> datetime | None:
        return self._last_refresh_time.get(asset.upper())

    def last_refresh_bar_count(self, asset: str) -> int | None:
        return self._last_refresh_bar_count.get(asset.upper())

    def tracked_assets(self) -> tuple[str, ...]:
        return tuple(self._last_refresh_time.keys())

    def reset(self, asset: str | None = None) -> None:
        """Clear scheduler bookkeeping (does NOT touch the cache).

        Use this when the upstream data source rolled (e.g. a session
        boundary) and the scheduler should treat the next tick as a
        cold start.
        """
        if asset is None:
            self._last_refresh_time.clear()
            self._last_refresh_bar_count.clear()
        else:
            a = asset.upper()
            self._last_refresh_time.pop(a, None)
            self._last_refresh_bar_count.pop(a, None)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _should_refresh(self, asset: str, bar_count: int) -> bool:
        # First refresh always fires once warmup has cleared.
        last_time = self._last_refresh_time.get(asset)
        last_count = self._last_refresh_bar_count.get(asset)
        if last_time is None or last_count is None:
            return True

        now = self.clock()

        # Time-based trigger
        if self.trigger.every_seconds is not None:
            elapsed = (now - last_time).total_seconds()
            if elapsed >= self.trigger.every_seconds:
                return True

        # Bar-count trigger
        if self.trigger.every_n_bars is not None:
            grew = bar_count - last_count
            if grew >= self.trigger.every_n_bars:
                return True

        return False

    def _do_refresh(
        self,
        asset: str,
        bars: list[Bar],
        bar_count: int,
        qualifier: Callable[..., QualificationReport] | None,
        **qualifier_kwargs: object,
    ) -> AllowlistEntry:
        fn = qualifier if qualifier is not None else qualify_strategies
        report = fn(bars, asset, **qualifier_kwargs)
        entry = self.cache.update(report)
        self._last_refresh_time[asset] = self.clock()
        self._last_refresh_bar_count[asset] = bar_count
        return entry
