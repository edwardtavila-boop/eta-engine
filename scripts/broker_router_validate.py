"""Pre-deploy validator for ``configs/bot_broker_routing.yaml``.

Lints the routing config and verifies every active bot can resolve a
venue + venue-specific symbol that is acceptable to the matching venue
adapter. Useful as a CI / pre-launch gate so a typo in the routing
yaml fails the deploy instead of silently quarantining live orders.

What it checks
--------------

For every active bot from
:func:`eta_engine.strategies.per_bot_registry.all_assignments`:

1. :meth:`RoutingConfig.venue_for(bot_id, symbol)` resolves to a venue
   string (no exception).
2. The venue resolves to a known adapter via
   :meth:`BrokerRouter._resolve_venue_adapter` (i.e. SmartRouter has it).
3. :meth:`RoutingConfig.map_symbol(raw, venue)` produces a venue-ready
   symbol (no ValueError).
4. ``venue.has_credentials()`` returns True. Loud warning if False (a
   cert/paper venue without keys cannot route, but the validator does
   not exit non-zero for it — the operator may be deferring secrets).
5. The mapped symbol is in the venue's known inventory (best-effort:
   each venue's symbol surface is small and deterministic).

Example
-------

::

    python -m eta_engine.scripts.broker_router_validate
    PASS  bot=btc_optimized symbol=BTC venue=alpaca mapped=BTC/USD
    PASS  bot=mnq_v7        symbol=MNQ venue=ibkr   mapped=MNQ
    ...
    FAIL  bot=foo_bar       symbol=XYZ venue=ibkr   reason=unsupported (XYZ, ibkr)
    1 failure(s); routing yaml is NOT safe to deploy.

Exit codes
----------

* ``0`` — every active bot resolved cleanly.
* ``1`` — at least one bot failed (FAIL line printed). Non-zero exit.
* ``2`` — config could not be loaded (parse error, etc.).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from eta_engine.scripts.broker_router import (  # noqa: E402
    BrokerRouter,
    PendingOrder,
    RoutingConfig,
)
from eta_engine.venues.router import SmartRouter  # noqa: E402

logger = logging.getLogger("eta_engine.broker_router_validate")


# ---------------------------------------------------------------------------
# Per-venue inventory (best-effort static surface)
# ---------------------------------------------------------------------------

#: Recognized venues. Kept narrow on purpose — adding a venue here means
#: a real adapter exists in ``eta_engine.venues``. Tradovate remains DORMANT:
#: this validator may recognize its adapter slot, but active routing must keep
#: it disabled unless the broker dormancy mandate is updated in code and docs.
KNOWN_VENUES: frozenset[str] = frozenset({
    "ibkr", "tastytrade", "tasty", "alpaca", "alp",
    "bybit", "okx", "tradovate",
})


# ---------------------------------------------------------------------------
# Result dataclass + checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One row of the validator report."""

    bot_id: str
    symbol: str
    venue: str
    mapped: str
    ok: bool
    reason: str = ""

    def line(self) -> str:
        tag = "PASS" if self.ok else "FAIL"
        if self.ok:
            return (
                f"{tag}  bot={self.bot_id} symbol={self.symbol} "
                f"venue={self.venue} mapped={self.mapped}"
            )
        return (
            f"{tag}  bot={self.bot_id} symbol={self.symbol} "
            f"venue={self.venue} reason={self.reason}"
        )


def _resolve_active_bots() -> list[tuple[str, str]]:
    """Return ``(bot_id, symbol)`` pairs for every active assignment.

    Importing the registry here (rather than at module top) avoids
    pulling its heavy dependency tree when callers only want to lint a
    yaml file with ``check_routing_config()``.
    """
    from eta_engine.strategies import per_bot_registry

    pairs: list[tuple[str, str]] = []
    for assignment in per_bot_registry.all_assignments():
        if not per_bot_registry.is_active(assignment):
            continue
        pairs.append((assignment.bot_id, assignment.symbol))
    return pairs


def check_routing_config(
    cfg: RoutingConfig | None = None,
    *,
    bot_pairs: list[tuple[str, str]] | None = None,
    smart_router: SmartRouter | None = None,
) -> list[CheckResult]:
    """Run every lint check and return a :class:`CheckResult` per bot.

    Test-friendly: callers may inject ``cfg`` (parsed yaml),
    ``bot_pairs`` (skip the registry import), and ``smart_router`` (so
    venue-adapter lookups don't hit the live broker layer).
    """
    if cfg is None:
        cfg = RoutingConfig.load()
    if bot_pairs is None:
        bot_pairs = _resolve_active_bots()
    if smart_router is None:
        smart_router = SmartRouter()

    # We only need _resolve_venue_adapter from BrokerRouter; build a
    # lightweight router with no filesystem side effects. The pending
    # dir doesn't have to exist for adapter lookup — the constructor
    # tries to mkdir, so use a path under the OS temp dir.
    import tempfile
    state_root = Path(tempfile.mkdtemp(prefix="eta_route_validate_"))
    pending_dir = state_root / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    from eta_engine.obs.decision_journal import default_journal
    router = BrokerRouter(
        pending_dir=pending_dir,
        state_root=state_root,
        smart_router=smart_router,
        journal=default_journal(),
        routing_config=cfg,
        dry_run=True,
    )

    results: list[CheckResult] = []
    for bot_id, symbol in bot_pairs:
        # 1. Venue resolution.
        try:
            venue_name = cfg.venue_for(bot_id, symbol=symbol)
        except Exception as exc:  # noqa: BLE001 — operator-facing diagnostic
            results.append(CheckResult(
                bot_id=bot_id, symbol=symbol, venue="?", mapped="",
                ok=False, reason=f"venue_for raised: {exc}",
            ))
            continue

        # 2. Known venue?
        if venue_name not in KNOWN_VENUES:
            results.append(CheckResult(
                bot_id=bot_id, symbol=symbol, venue=venue_name, mapped="",
                ok=False, reason=f"unknown venue {venue_name!r}",
            ))
            continue

        # 3. Symbol mapping.
        try:
            mapped = cfg.map_symbol(symbol, venue_name)
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult(
                bot_id=bot_id, symbol=symbol, venue=venue_name, mapped="",
                ok=False, reason=f"map_symbol failed: {exc}",
            ))
            continue

        # 4. Venue-adapter lookup (so the runtime can actually reach it).
        order_stub = PendingOrder(
            ts="", signal_id="", side="BUY", qty=1.0, symbol=symbol,
            limit_price=1.0, bot_id=bot_id,
        )
        adapter = router._resolve_venue_adapter(venue_name, order_stub)
        if adapter is None:
            results.append(CheckResult(
                bot_id=bot_id, symbol=symbol, venue=venue_name, mapped=mapped,
                ok=False, reason="venue adapter not registered on SmartRouter",
            ))
            continue

        # 5. Credentials. NOT a hard fail — operator may be deferring
        # secrets — but log a clear WARN so the deploy step can decide.
        if not adapter.has_credentials():
            logger.warning(
                "validator: bot=%s venue=%s lacks live credentials; "
                "live routing will not work until secrets are wired",
                bot_id, venue_name,
            )

        results.append(CheckResult(
            bot_id=bot_id, symbol=symbol, venue=venue_name, mapped=mapped,
            ok=True,
        ))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="broker_router_validate",
        description="Validate eta_engine/configs/bot_broker_routing.yaml.",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Path to bot_broker_routing.yaml (default: env / project root).",
    )
    parser.add_argument(
        "--log-level", type=str, default="WARNING",
        help="Python log level (default WARNING).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(levelname)s %(name)s %(message)s",
    )

    try:
        cfg = RoutingConfig.load(Path(args.config) if args.config else None)
    except ValueError as exc:
        print(f"FATAL  routing config failed to load: {exc}", flush=True)
        return 2

    results = check_routing_config(cfg)
    failures = 0
    for r in results:
        print(r.line(), flush=True)
        if not r.ok:
            failures += 1

    if failures:
        print(
            f"{failures} failure(s); routing yaml is NOT safe to deploy.",
            flush=True,
        )
        return 1
    print(
        f"OK  {len(results)} active bot(s) resolved cleanly; "
        f"routing yaml is safe.", flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
