"""EVOLUTIONARY TRADING ALGO  //  strategies.live_adapter.

Factory for building a fully-wired live :class:`RouterAdapter`.

Context
-------
By v0.1.44 every piece of the OOS-governed trading path existed:

  * :mod:`strategies.oos_qualifier` -- walk-forward + DSR verdict.
  * :mod:`strategies.runtime_allowlist` -- TTL cache sitting on top.
  * :mod:`strategies.allowlist_scheduler` -- per-bar cadence trigger.
  * :class:`strategies.engine_adapter.RouterAdapter` -- accepts an
    optional ``allowlist_scheduler`` and ticks it before ``dispatch``.

What was still manual: constructing the full stack for a given asset.
Every live bot that wanted the OOS-governed loop had to import four
modules, pick sensible TTL / cadence / warmup numbers, and wire them
together. That boilerplate was error-prone (the TTL has to be sized
against the trigger cadence, the trigger has to respect the
qualifier's walk-forward warmup, etc.) and there was no canonical
"live defaults" table.

:func:`build_live_adapter` is that canonical factory. Pass it the
asset symbol, optionally override any knob, and get back a
:class:`RouterAdapter` with the scheduler + cache already wired.
Drop it into the bot's ``strategy_adapter`` field and the
OOS-qualification loop governs the bot on every bar.

Defaults
--------
Chosen for the live 1m / 5m live trading tapes the EVOLUTIONARY TRADING ALGO
fleet runs on:

  * ``buffer_bars``           = 300   -- ``mtf_trend_following``
    needs a 200-period MA plus a lookback window; 300 gives headroom
    without holding a full day.
  * ``ttl_seconds``           = 7200  -- 2x the wall-clock trigger
    below so the cache is always "fresh" between scheduler ticks
    (never degrades to DEFAULT_ELIGIBILITY just because a tick was
    skipped).
  * ``refresh_every_n_bars``  = 288   -- 24h of 5m bars, i.e.
    "refresh once per trading day even on quiet tapes".
  * ``refresh_every_seconds`` = 3600  -- 1h wall-clock, for fast
    tapes where 288 bars pile up in minutes.
  * ``min_bars_before_first`` = 200   -- the qualifier's
    walk-forward windows need this much warmup to be meaningful.

Every knob is overridable. The factory is a pure in-process
constructor -- no I/O, no threads, safe to call from a bot's
``start()`` coroutine.

Minimal usage
-------------
::

    adapter = build_live_adapter("MNQ")
    bot = MnqBot(strategy_adapter=adapter)

Or, via the bot's built-in auto-wire kwarg (v0.1.45+):
::

    bot = MnqBot(auto_wire_ai_strategies=True)
    # bot.start() invokes build_live_adapter("MNQ") internally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eta_engine.strategies.allowlist_scheduler import (
    AllowlistScheduler,
    RefreshTrigger,
)
from eta_engine.strategies.engine_adapter import (
    DEFAULT_BUFFER_BARS,
    RouterAdapter,
)
from eta_engine.strategies.runtime_allowlist import RuntimeAllowlistCache

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from eta_engine.strategies.decision_sink import RouterDecisionSink
    from eta_engine.strategies.models import StrategyId

__all__ = [
    "DEFAULT_LIVE_MIN_BARS_BEFORE_FIRST",
    "DEFAULT_LIVE_REFRESH_EVERY_N_BARS",
    "DEFAULT_LIVE_REFRESH_EVERY_SECONDS",
    "DEFAULT_LIVE_TTL_SECONDS",
    "build_live_adapter",
]


# ---------------------------------------------------------------------------
# Live defaults
# ---------------------------------------------------------------------------


DEFAULT_LIVE_TTL_SECONDS: float = 7200.0
"""Default TTL for the runtime allowlist in live mode.

Sized at 2x the default wall-clock refresh trigger
(:data:`DEFAULT_LIVE_REFRESH_EVERY_SECONDS`) so the cache never
goes stale between scheduler ticks on a normally-ticking bot. If
the scheduler stops ticking (catastrophic qualifier failure) the
cache degrades to empty after this much time, which in turn means
:meth:`RouterAdapter._effective_eligibility` falls back to the
static override (or ``None`` -> ``DEFAULT_ELIGIBILITY``)."""


DEFAULT_LIVE_REFRESH_EVERY_N_BARS: int = 288
"""Default bar-count trigger for the live refresh scheduler.

288 bars = 24 hours of 5-minute bars, i.e. roughly once per trading
day. On faster tapes the :data:`DEFAULT_LIVE_REFRESH_EVERY_SECONDS`
trigger will usually fire first; this bar-count trigger is the
backstop for slow / low-volume tapes."""


DEFAULT_LIVE_REFRESH_EVERY_SECONDS: float = 3600.0
"""Default wall-clock trigger for the live refresh scheduler.

1 hour. Fires earlier than the bar-count trigger on fast tapes;
keeps the allowlist snappy in live markets while still giving the
qualifier a full hour between runs (the DSR computation is
bounded but not free)."""


DEFAULT_LIVE_MIN_BARS_BEFORE_FIRST: int = 200
"""Default warmup bars before the scheduler fires its first refresh.

Matches the qualifier's walk-forward requirements -- 200 bars is
the minimum that lets the DSR estimator produce meaningful
statistics on any AI-Optimized strategy. Below this the scheduler
returns ``None`` and the router falls back to the static
eligibility (or DEFAULT_ELIGIBILITY) until the tape catches up."""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_live_adapter(  # noqa: PLR0913 -- factory with many optional knobs
    asset: str,
    *,
    buffer_bars: int = DEFAULT_BUFFER_BARS,
    ttl_seconds: float = DEFAULT_LIVE_TTL_SECONDS,
    refresh_every_n_bars: int | None = DEFAULT_LIVE_REFRESH_EVERY_N_BARS,
    refresh_every_seconds: float | None = DEFAULT_LIVE_REFRESH_EVERY_SECONDS,
    min_bars_before_first: int = DEFAULT_LIVE_MIN_BARS_BEFORE_FIRST,
    base_eligibility: Mapping[str, tuple[StrategyId, ...]] | None = None,
    eligibility_override: dict[str, tuple[StrategyId, ...]] | None = None,
    decision_sink: RouterDecisionSink | None = None,
    scheduler_kwargs: Mapping[str, object] | None = None,
    clock: Callable[[], datetime] | None = None,
    kill_switch_active: bool = False,
    session_allows_entries: bool = True,
) -> RouterAdapter:
    """Construct a fully-wired live :class:`RouterAdapter`.

    The returned adapter has:

      * a :class:`RuntimeAllowlistCache` with ``ttl_seconds`` and
        (optionally) a custom ``base_eligibility`` table;
      * an :class:`AllowlistScheduler` wrapping that cache with a
        :class:`RefreshTrigger` built from the ``refresh_*`` knobs;
      * ``allowlist_scheduler`` wired into the
        :class:`RouterAdapter` so ``push_bar`` ticks the scheduler
        before :func:`dispatch`.

    Parameters
    ----------
    asset:
        Ticker symbol (e.g. ``"MNQ"``). Upper-cased internally.
    buffer_bars:
        Size of the adapter's rolling bar buffer (must be >= 2).
    ttl_seconds:
        Allowlist cache freshness window.
    refresh_every_n_bars:
        Bar-count refresh trigger (``None`` disables).
    refresh_every_seconds:
        Wall-clock refresh trigger seconds (``None`` disables).
    min_bars_before_first:
        Warmup guard for the scheduler's first refresh.
    base_eligibility:
        Base eligibility table passed to the cache (defaults to
        :data:`strategies.policy_router.DEFAULT_ELIGIBILITY`).
    eligibility_override:
        Static eligibility dict on the :class:`RouterAdapter` -- if
        supplied, its per-asset entries override the scheduler's
        cache map on conflict (explicit operator choice wins).
    decision_sink:
        Optional :class:`RouterDecisionSink` for the decision
        journal. See v0.1.38.
    scheduler_kwargs:
        Forwarded verbatim to
        :meth:`AllowlistScheduler.tick` on every bar -- typical
        keys are ``gate``, ``n_windows``, ``is_fraction``.
    clock:
        Optional clock override. Injected into BOTH the cache and
        the scheduler so their notions of "now" stay aligned.
    kill_switch_active:
        Initial :attr:`RouterAdapter.kill_switch_active` value. The
        live bot will keep this in sync with its own state on every
        tick.
    session_allows_entries:
        Initial :attr:`RouterAdapter.session_allows_entries` value.

    Returns
    -------
    A fresh :class:`RouterAdapter` with the scheduler + cache wired
    in. The buffer is empty (:meth:`RouterAdapter.seed` can preload
    historical bars before going live).
    """
    cache_kwargs: dict[str, object] = {"ttl_seconds": ttl_seconds}
    if base_eligibility is not None:
        cache_kwargs["base_eligibility"] = base_eligibility
    if clock is not None:
        cache_kwargs["clock"] = clock
    cache = RuntimeAllowlistCache(**cache_kwargs)  # type: ignore[arg-type]

    trigger = RefreshTrigger(
        every_n_bars=refresh_every_n_bars,
        every_seconds=refresh_every_seconds,
        min_bars_before_first=min_bars_before_first,
    )

    scheduler_init: dict[str, object] = {"cache": cache, "trigger": trigger}
    if clock is not None:
        scheduler_init["clock"] = clock
    scheduler = AllowlistScheduler(**scheduler_init)  # type: ignore[arg-type]

    return RouterAdapter(
        asset=asset,
        max_bars=buffer_bars,
        eligibility=eligibility_override,
        kill_switch_active=kill_switch_active,
        session_allows_entries=session_allows_entries,
        decision_sink=decision_sink,
        allowlist_scheduler=scheduler,
        scheduler_kwargs=scheduler_kwargs,
    )
