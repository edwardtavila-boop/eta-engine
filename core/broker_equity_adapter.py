"""
APEX PREDATOR  //  core.broker_equity_adapter
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

Non-goals (deferred to v0.2.x)
-------------------------------
  * Routing-aware poller selection (e.g. "use Tastytrade's poller when
    the active futures broker is Tastytrade, IBKR's when it's IBKR").
    Today the supervisor explicitly picks one. v0.2.x will lift that
    into a router-driven selector.
  * Multi-broker drift fan-out (poll BOTH IBKR and Tastytrade and
    cross-check). Not needed for Apex eval (single account at a time).
    Sketched for completeness in :class:`MultiAdapterEquitySource` but
    NOT shipped here -- defer until we genuinely run multi-broker.
  * KillVerdict synthesis on out-of-tolerance. Reconciler stays
    observation-only per its v0.1.59 docstring.

Usage
-----
    from apex_predator.core.broker_equity_adapter import (
        BrokerEquityAdapter,
        NullBrokerEquityAdapter,
        make_poller_for,
    )
    from apex_predator.core.broker_equity_reconciler import (
        BrokerEquityReconciler,
    )
    from apex_predator.venues.tastytrade import TastytradeVenue

    tasty: BrokerEquityAdapter = TastytradeVenue()  # structural fit
    poller = make_poller_for(tasty, refresh_s=5.0, stale_after_s=30.0)
    await poller.start()
    rec = BrokerEquityReconciler(broker_equity_source=poller.current)

    # paper / dry-run path:
    null: BrokerEquityAdapter = NullBrokerEquityAdapter(name="paper")
    paper_poller = make_poller_for(null)  # returns no-data forever
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from apex_predator.core.broker_equity_poller import BrokerEquityPoller


@runtime_checkable
class BrokerEquityAdapter(Protocol):
    """Structural contract for any broker that can report net liquidation.

    Any object exposing both attributes below satisfies this protocol --
    no inheritance required. The two production venues that already fit
    are :class:`apex_predator.venues.ibkr.IBKRAdapter` and
    :class:`apex_predator.venues.tastytrade.TastytradeVenue`.

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
    :class:`apex_predator.venues.base.VenueBase` already provide many
    other capabilities (place_order, cancel_order, positions); those
    are not the equity-reconciler's concern. By keeping the protocol
    surface to two members we make it cheap for stub adapters
    (paper-mode, test fakes) to implement without dragging in the full
    venue surface.
    """

    name: str

    async def get_net_liquidation(self) -> float | None:
        ...


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


def make_poller_for(
    adapter: BrokerEquityAdapter,
    *,
    refresh_s: float = 5.0,
    stale_after_s: float = 30.0,
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
    )


__all__ = [
    "BrokerEquityAdapter",
    "NullBrokerEquityAdapter",
    "make_poller_for",
]
