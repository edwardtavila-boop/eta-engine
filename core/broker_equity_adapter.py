"""
EVOLUTIONARY TRADING ALGO  //  core.broker_equity_adapter
==============================================
R1 closure (contract layer) -- formal protocol that any broker venue
adapter must satisfy to feed :class:`BrokerEquityReconciler` via
:class:`BrokerEquityPoller`.

Why this module exists
----------------------
The reconciler / poller pair was wired up in v0.1.59 against two concrete
adapters (``IBKRAdapter.get_net_liquidation`` and
``TastytradeVenue.get_net_liquidation``). Both happen to satisfy the same
shape, but the contract was implicit -- nothing in the type system said
"to be a broker equity source, you need a name and an async net-liq
reader." That implicit contract becomes a problem the moment we wire a
third venue (Tradovate when funding clears) or a paper-mode stub: there
is no compile-time / mypy-time / runtime check that the new thing fits.

This module formalises the contract three ways:

  1. **Structural typing** via :class:`BrokerEquityAdapter` -- a
     ``@runtime_checkable`` ``typing.Protocol``. Any object exposing
     ``name: str`` and ``async def get_net_liquidation() -> float | None``
     satisfies it without inheritance. Both the IBKR and Tastytrade
     venues (which subclass :class:`VenueBase`) already match this
     shape and need zero modification.

  2. **A null/stub implementation** -- :class:`NullBrokerEquityAdapter`
     -- that always returns ``None``. This is the canonical "broker
     source not wired" placeholder for paper mode, dry runs, and the
     dormant Tradovate adapter. Wiring this through the reconciler is
     equivalent to disabling drift detection (every reconcile becomes
     ``no_broker_data``) without having to special-case ``None`` every-
     where in the supervisor.

  3. **A factory helper** -- :func:`make_poller_for(adapter, ...)` --
     that takes any object satisfying the protocol and returns a
     ready-to-start :class:`BrokerEquityPoller` bound to that adapter's
     ``get_net_liquidation``. Centralising this construction means the
     supervisor wiring code stays a one-liner per broker.

Scope discipline
----------------
This is **scaffolding only**. v0.1.62 ships:

  * The protocol class.
  * The null adapter.
  * The factory helper.
  * Tests pinning protocol-satisfaction for IBKR / Tastytrade /
    NullBrokerEquityAdapter and verifying the factory builds a usable
    poller.

It does NOT change any runtime behaviour. The supervisor wiring (which
broker the reconciler is currently bound to, on what cadence, with what
tolerance) is unchanged. The point of v0.1.62 is to lock the contract
in TYPE-CHECKED form before v0.2.x flips drift detection on by default.

Closed in v0.1.64
-----------------
  * Routing-aware poller selection -- :class:`RouterBackedBrokerEquityAdapter`
    proxies to whichever futures venue ``router.choose_venue("MNQ")``
    currently picks, so failover from IBKR to Tastytrade transparently
    moves the drift probe with it. The supervisor now wires a single
    poller backed by this adapter instead of a statically-bound venue.

Non-goals (still deferred to v0.2.x)
-------------------------------------
Each item below has an explicit exit criterion -- the audit at
``scripts/_audit_deferral_criteria.py`` will reject any v0.2.x
deferral that does not. M1 / M2 below correspond to the residual
ledger in ``docs/red_team_d2_d3_review.md``.

  * M1 -- Multi-broker drift fan-out. Lands when:
    :class:`MultiAdapterEquitySource` grows a real fetch +
    reconciliation impl (vs the current placeholder), and
    ``test_multi_adapter_cross_broker_reconciles`` asserts the
    cross-check fires on a synthetic mismatch. Out of scope for
    Apex eval (single account at a time); reactivates when the
    operator runs multiple futures accounts simultaneously.
  * M2 -- KillVerdict synthesis on out-of-tolerance. Lands when:
    ``scripts/calibrate_broker_drift_tolerance.py`` emits a
    recommendation backed by 30+ days of live-paper data, AND
    ``configs/kill_switch.yaml`` grows a ``tier_a.broker_drift``
    entry that maps sustained drift -> ``KillVerdict``
    (PAUSE_NEW_ENTRIES or FLATTEN_TIER_A_PREEMPTIVE), AND
    ``test_run_eta_live`` grows an integration test asserting
    the KillVerdict fires. Reconciler stays observation-only
    until then per its v0.1.59 docstring.

Usage
-----
    from eta_engine.core.broker_equity_adapter import (
        BrokerEquityAdapter,
        NullBrokerEquityAdapter,
        make_poller_for,
    )
    from eta_engine.core.broker_equity_reconciler import (
        BrokerEquityReconciler,
    )
    from eta_engine.venues.tastytrade import TastytradeVenue

    tasty: BrokerEquityAdapter = TastytradeVenue()  # structural fit
    poller = make_poller_for(tasty, refresh_s=5.0, stale_after_s=30.0)
    await poller.start()
    rec = BrokerEquityReconciler(broker_equity_source=poller.current)

    # paper / dry-run path:
    null: BrokerEquityAdapter = NullBrokerEquityAdapter(name="paper")
    paper_poller = make_poller_for(null)  # returns no-data forever
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from eta_engine.core.broker_equity_poller import BrokerEquityPoller

if TYPE_CHECKING:
    from eta_engine.venues.router import SmartRouter

log = logging.getLogger(__name__)


class BrokerEquityNotAvailableError(RuntimeError):
    """Raised when live mode cannot resolve a real broker equity source.

    H6 closure (v0.1.65). The v0.1.64 ``_build_broker_equity_adapter``
    helper degraded silently to :class:`NullBrokerEquityAdapter` when
    both IBKR and Tastytrade creds were missing in live mode. The boot
    banner showed ``broker_equity : live-null-no-creds``, but a busy
    operator scanning startup output could miss it -- and the eval
    would then run with drift detection silently disabled. v0.1.65
    flips that: live mode refuses to boot when no real broker source
    resolves, unless the operator opts in via the environment variable
    ``APEX_ALLOW_LIVE_NO_DRIFT=1``. The exit path is loud (this
    exception) rather than quiet (a placeholder adapter).
    """


@runtime_checkable
class BrokerEquityAdapter(Protocol):
    """Structural contract for any broker that can report net liquidation.

    Any object exposing both attributes below satisfies this protocol --
    no inheritance required. The two production venues that already fit
    are :class:`eta_engine.venues.ibkr.IBKRAdapter` and
    :class:`eta_engine.venues.tastytrade.TastytradeVenue`.

    Attributes
    ----------
    name:
        Short identifier for log lines and metrics (e.g. ``"ibkr"``,
        ``"tastytrade"``, ``"tradovate"``, ``"paper"``).

    Methods
    -------
    get_net_liquidation:
        Async zero-arg reader returning broker-reported net-liq in USD,
        or ``None`` when the data is unavailable. Implementations MUST
        NOT raise -- transport / auth / parse failures should degrade
        to ``None`` so the reconciler can classify them as
        ``no_broker_data`` and the supervisor can keep running.

    Notes
    -----
    The protocol is intentionally narrow. Subclasses of
    :class:`eta_engine.venues.base.VenueBase` already provide many
    other capabilities (place_order, cancel_order, positions); those
    are not the equity-reconciler's concern. By keeping the protocol
    surface to two members we make it cheap for stub adapters
    (paper-mode, test fakes) to implement without dragging in the full
    venue surface.
    """

    name: str

    async def get_net_liquidation(self) -> float | None: ...


class NullBrokerEquityAdapter:
    """No-op adapter -- always returns ``None``.

    This is the canonical placeholder for:

      * Paper / dry-run mode (no real broker).
      * Brokers that are dormant (Tradovate while funding-blocked).
      * Test fixtures that want to wire the reconciler/poller without
        a real network call.

    Wiring this through :class:`BrokerEquityReconciler` is equivalent
    to disabling drift detection: every reconcile classifies as
    ``no_broker_data``, so :attr:`ReconcileStats.checks_no_data` grows
    while :attr:`ReconcileStats.checks_out_of_tolerance` stays at 0.

    Parameters
    ----------
    name:
        Identifier used in log lines. Defaults to ``"null"``.
    """

    def __init__(self, name: str = "null") -> None:
        self.name = name

    async def get_net_liquidation(self) -> float | None:
        return None


class RouterBackedBrokerEquityAdapter:
    """Adapter that proxies to whichever futures broker the router prefers right now.

    Closes the v0.1.62 deferred item *router-aware poller selection*. The
    v0.1.63 wiring binds a single :class:`BrokerEquityPoller` to one
    statically-chosen adapter. Under the broker dormancy mandate (IBKR
    primary, Tastytrade fallback) a mid-session router failover would
    leave the poller pointed at the now-circuit-tripped venue, silently
    degrading drift detection to ``no_broker_data`` for the duration of
    the failover window.

    This adapter resolves the active futures venue via
    :meth:`eta_engine.venues.router.SmartRouter.choose_venue` on every
    fetch. The reconciler / poller side keep their existing single-source
    contract; the router takes care of the substitution.

    Behaviour
    ---------
    * ``get_net_liquidation()`` consults ``router.choose_venue(probe_symbol)``
      to find the currently-active futures venue. If that venue exposes
      an async ``get_net_liquidation`` method (IBKR + Tastytrade do; the
      dormant Tradovate path is substituted upstream by
      :func:`_resolve_preferred_futures_venue`), it awaits the venue's
      reader. Any exception raised by the router or the venue (or a
      venue that lacks the method entirely) degrades to ``None``.
    * ``name`` is a stable identifier (default ``"router-active-futures"``)
      so the poller's log key does not flip every failover. The ``why``
      is recorded in the per-fetch debug log instead.

    Parameters
    ----------
    router:
        A :class:`eta_engine.venues.router.SmartRouter` instance. The
        adapter holds a strong reference -- callers should construct one
        router for the lifetime of the runtime and reuse it.
    probe_symbol:
        Symbol fed to ``router.choose_venue`` to resolve the active
        futures broker. Must be a futures root the router recognises;
        defaults to ``"MNQ"`` because that is the canonical Apex-eval
        symbol. Crypto / non-futures probes would resolve to crypto
        venues, which do not implement equity reconciliation.
    name:
        Identifier surfaced via the protocol. Defaults to
        ``"router-active-futures"``. Override to disambiguate when
        multiple router-backed adapters are wired (rare).

    Raises
    ------
    TypeError
        If ``router`` is not a :class:`SmartRouter` (caught at construct
        time -- a misconfigured supervisor should fail loud, not at the
        first poll).
    """

    def __init__(
        self,
        router: SmartRouter,
        *,
        probe_symbol: str = "MNQ",
        name: str = "router-active-futures",
    ) -> None:
        # Lazy import keeps this module's import graph free of the
        # router (which pulls in every venue's HTTP client). The runtime
        # check still rejects non-router objects.
        from eta_engine.venues.router import SmartRouter as _SmartRouter

        if not isinstance(router, _SmartRouter):
            msg = f"RouterBackedBrokerEquityAdapter requires a SmartRouter, got {type(router).__name__}"
            raise TypeError(msg)
        self._router = router
        self._probe_symbol = probe_symbol
        self.name = name

    @property
    def active_venue_name(self) -> str | None:
        """Best-effort name of the currently-active futures venue.

        Returns ``None`` if the router probe raises. Used by callers
        that want to log the active broker alongside drift events; the
        poller itself uses :attr:`name` (stable).
        """
        try:
            return self._router.choose_venue(self._probe_symbol).name
        except Exception:  # noqa: BLE001
            return None

    async def get_net_liquidation(self) -> float | None:
        """Read net-liquidation from whichever venue the router prefers now.

        Failure semantics: any exception from the router or the venue
        (network, auth, parse) is swallowed and returned as ``None`` so
        the reconciler classifies as ``no_broker_data``. A venue without
        a ``get_net_liquidation`` method also returns ``None`` (this
        path should not fire in practice -- both production futures
        venues expose the method -- but it keeps the adapter robust if
        the venue surface is ever pruned).
        """
        try:
            venue = self._router.choose_venue(self._probe_symbol)
        except Exception as exc:  # noqa: BLE001
            log.debug("router_backed_adapter: choose_venue raised: %s", exc)
            return None

        reader = getattr(venue, "get_net_liquidation", None)
        if reader is None or not callable(reader):
            log.debug(
                "router_backed_adapter: venue %r has no get_net_liquidation",
                getattr(venue, "name", "unknown"),
            )
            return None

        try:
            value = await reader()
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "router_backed_adapter: %s.get_net_liquidation raised: %s",
                getattr(venue, "name", "unknown"),
                exc,
            )
            return None
        return value


class SafeBrokerEquityAdapter:
    """Defensive wrapper that enforces the "MUST NOT raise" guarantee.

    H7 closure (v0.1.65). The :class:`BrokerEquityAdapter` Protocol's
    ``get_net_liquidation`` docstring promises that implementations
    "MUST NOT raise -- transport / auth / parse failures should
    degrade to ``None`` so the reconciler can classify them as
    ``no_broker_data``." That guarantee is by convention -- ``mypy``
    cannot enforce it, and a venue's ``get_net_liquidation`` raising
    deep inside an HTTP retry would propagate up through
    :class:`BrokerEquityPoller` and corrupt the runtime.

    This wrapper makes the contract enforceable at runtime. It takes
    any object satisfying the protocol and returns a wrapper that:

      * Forwards happy-path values unchanged.
      * Catches **every** exception from the wrapped adapter's
        ``get_net_liquidation`` and degrades to ``None`` (logged at
        DEBUG so the operator can grep without noise).
      * Forwards the wrapped adapter's ``name`` (or accepts an
        override).

    Use it whenever you do not fully trust the adapter's exception
    discipline -- in practice, that means "always", since adapter
    code paths span aiohttp, JSON parsing, and broker SDKs whose
    error surface is wide and undocumented.

    Parameters
    ----------
    adapter:
        Any object satisfying :class:`BrokerEquityAdapter`. Verified
        at construction.
    name:
        Optional name override. Defaults to ``f"safe({adapter.name})"``
        so a wrapped adapter is visually distinct in logs without the
        operator having to thread a custom name through every wiring
        site.

    Raises
    ------
    TypeError
        When ``adapter`` does not satisfy :class:`BrokerEquityAdapter`.
    """

    def __init__(
        self,
        adapter: BrokerEquityAdapter,
        *,
        name: str | None = None,
    ) -> None:
        if not isinstance(adapter, BrokerEquityAdapter):
            msg = f"SafeBrokerEquityAdapter requires a BrokerEquityAdapter, got {type(adapter).__name__}"
            raise TypeError(msg)
        self._adapter = adapter
        self.name = name if name is not None else f"safe({adapter.name})"

    async def get_net_liquidation(self) -> float | None:
        try:
            return await self._adapter.get_net_liquidation()
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "safe_broker_adapter: wrapped %s.get_net_liquidation raised: %s",
                getattr(self._adapter, "name", "unknown"),
                exc,
            )
            return None


def make_poller_for(
    adapter: BrokerEquityAdapter,
    *,
    refresh_s: float = 5.0,
    stale_after_s: float = 30.0,
    identical_warn_after: int = 0,
) -> BrokerEquityPoller:
    """Build a :class:`BrokerEquityPoller` bound to ``adapter``.

    Parameters
    ----------
    adapter:
        Any object satisfying :class:`BrokerEquityAdapter`. Verified at
        runtime via ``isinstance(adapter, BrokerEquityAdapter)``; raises
        :class:`TypeError` otherwise.
    refresh_s:
        Forwarded to :class:`BrokerEquityPoller`. Default 5.0s.
    stale_after_s:
        Forwarded to :class:`BrokerEquityPoller`. Default 30.0s.
    identical_warn_after:
        H4 partial closure (v0.1.69). Forwarded to
        :class:`BrokerEquityPoller`. Default 0 (disabled). When > 0,
        the poller logs a WARN once when N consecutive successful polls
        have returned the same value -- an early-warning signal that
        the broker may be serving a server-side cached snapshot. Use
        ``identical_warn_after = ceil(60 / refresh_s)`` (i.e. one
        minute of unchanged net-liq) as a starting point.

    Returns
    -------
    BrokerEquityPoller
        A constructed but **not yet started** poller. The caller is
        responsible for ``await poller.start()`` (and matching
        ``await poller.stop()`` on shutdown).

    Raises
    ------
    TypeError
        When ``adapter`` does not satisfy
        :class:`BrokerEquityAdapter`. The error message names the
        adapter's class to make miswiring easy to spot.

    Notes
    -----
    The runtime ``isinstance`` check is structural -- it succeeds for
    any object exposing ``name: str`` and an *attribute* called
    ``get_net_liquidation``. It does not (and cannot, per PEP 544)
    verify that ``get_net_liquidation`` is async / zero-arg / returns
    ``float | None``. Those guarantees are upheld by the adapter's
    own contract / tests. The intent of the runtime check is to catch
    obvious miswiring (passing a router, a config dict, a venue with a
    different surface) early, not to be a full type-checker.
    """
    if not isinstance(adapter, BrokerEquityAdapter):
        msg = (
            f"object of type {type(adapter).__name__} does not satisfy "
            "BrokerEquityAdapter (needs `name: str` and async "
            "`get_net_liquidation() -> float | None`)"
        )
        raise TypeError(msg)
    return BrokerEquityPoller(
        name=adapter.name,
        fetch_fn=adapter.get_net_liquidation,
        refresh_s=refresh_s,
        stale_after_s=stale_after_s,
        identical_warn_after=identical_warn_after,
    )


__all__ = [
    "BrokerEquityAdapter",
    "BrokerEquityNotAvailableError",
    "NullBrokerEquityAdapter",
    "RouterBackedBrokerEquityAdapter",
    "SafeBrokerEquityAdapter",
    "make_poller_for",
]
