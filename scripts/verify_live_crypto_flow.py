"""Live-fill verification harness for Alpaca paper crypto routing.

Polls Alpaca paper ``/v2/orders?status=all&limit=20`` on a fixed cadence
and reports any orders observed during the watch window. Exits 0 when at
least one new crypto order appears, non-zero otherwise — suitable for
post-deploy CI smoke or hand-run on the VPS to confirm the supervisor +
broker_router + Alpaca path is wired end-to-end.

Why not reuse the ``broker_router`` heartbeat? The router heartbeat
proves the *router process* is alive; the question this harness answers
is the harder one: did a strategy decision land at the broker as a real
order? Polling Alpaca's order log is the only fully-honest signal — it
crosses the JARVIS → supervisor → broker_router → AlpacaVenue chain in
the same direction live trading uses, so a passing run gives high
confidence the wiring is intact.

Usage
-----
    python -m eta_engine.scripts.verify_live_crypto_flow --watch-seconds 300

    # Belt-and-braces: fail if the configured base_url is not paper.
    python -m eta_engine.scripts.verify_live_crypto_flow --alpaca-paper

Exit codes
----------
* 0   -- at least one new crypto order observed within the window.
* 2   -- credentials missing or live-probe failed (config/env issue).
* 3   -- watched the full window, no crypto orders appeared.
* 4   -- ``--alpaca-paper`` requested but config does not target paper.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.venues.alpaca import (  # noqa: E402
    AlpacaConfig,
    AlpacaVenue,
    _alpaca_crypto_base,
)
from eta_engine.venues.base import ConnectionStatus  # noqa: E402

logger = logging.getLogger("verify_live_crypto_flow")

DEFAULT_WATCH_SECONDS = 300
DEFAULT_POLL_INTERVAL_S = 5.0
DEFAULT_ORDER_LIMIT = 20


def _is_paper_url(base_url: str) -> bool:
    return "paper" in (base_url or "").lower()


async def _list_recent_orders(
    venue: AlpacaVenue,
    *,
    limit: int = DEFAULT_ORDER_LIMIT,
) -> list[dict[str, Any]]:
    """Pull the last ``limit`` orders regardless of status.

    Returns an empty list on any transport / auth failure — the calling
    loop continues polling so a transient blip doesn't fail the whole
    verification run. The hard failure path is only triggered when the
    harness's pre-flight ``connect()`` already reported the venue as
    not READY.
    """
    path = f"/v2/orders?status=all&limit={int(limit)}&direction=desc"
    payload = await venue._get(path)  # noqa: SLF001 — internal helper, intentional
    if isinstance(payload, list):
        return payload
    return []


def _format_order_row(order: dict[str, Any]) -> str:
    """One-line summary suitable for live console tail."""
    ts = order.get("submitted_at") or order.get("created_at") or "?"
    sym = order.get("symbol", "?")
    side = order.get("side", "?")
    qty = order.get("qty", "?")
    status = order.get("status", "?")
    avg_price = order.get("filled_avg_price") or "—"
    order_id = order.get("id", "?")
    return f"  ts={ts} sym={sym} side={side} qty={qty} status={status} filled_avg_price={avg_price} id={order_id}"


async def watch(
    *,
    watch_seconds: int = DEFAULT_WATCH_SECONDS,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    require_paper: bool = False,
) -> int:
    """Main poll loop. Returns the process exit code."""
    config = AlpacaConfig.from_env()
    if require_paper and not _is_paper_url(config.base_url):
        logger.error(
            "verify_live_crypto_flow: --alpaca-paper required but base_url=%s is not a paper host. Refusing to run.",
            config.base_url,
        )
        return 4

    venue = AlpacaVenue(config=config)

    # Pre-flight live probe so the operator gets a fast, deterministic
    # rejection if the keys / host / account state are wrong. This
    # mirrors the supervisor's startup behaviour so the same failure
    # modes look identical to the operator across surfaces.
    report = await venue.connect()
    logger.info(
        "alpaca connect(): status=%s creds_present=%s endpoint=%s probe=%s",
        report.status.value,
        report.creds_present,
        report.details.get("endpoint"),
        report.details.get("probe"),
    )
    if report.status not in {ConnectionStatus.READY, ConnectionStatus.DEGRADED}:
        logger.error(
            "verify_live_crypto_flow: alpaca venue is %s — cannot proceed. details=%s error=%s",
            report.status.value,
            report.details,
            report.error,
        )
        return 2

    # Orders observed BEFORE we start watching. Only orders appearing
    # after this baseline count toward the live-fill check, so a stale
    # historical fill from days ago can't mask a dead pipeline.
    baseline_orders = await _list_recent_orders(venue)
    baseline_ids = {str(o.get("id")) for o in baseline_orders if o.get("id")}
    logger.info(
        "baseline: %d existing orders (will not count); watch_seconds=%d poll_interval_s=%.1f",
        len(baseline_orders),
        watch_seconds,
        poll_interval_s,
    )
    if baseline_orders:
        # Show the most recent order so the operator sees we're live on
        # the right account.
        logger.info("most-recent prior order:\n%s", _format_order_row(baseline_orders[0]))

    deadline = time.monotonic() + max(1, int(watch_seconds))
    first_fill_ts: str | None = None
    seen_new_ids: set[str] = set()
    seen_new_crypto_ids: set[str] = set()
    poll_count = 0

    while time.monotonic() < deadline:
        poll_count += 1
        orders = await _list_recent_orders(venue)
        for order in orders:
            oid = str(order.get("id") or "")
            if not oid or oid in baseline_ids or oid in seen_new_ids:
                continue
            seen_new_ids.add(oid)
            sym = str(order.get("symbol", ""))
            is_crypto = bool(_alpaca_crypto_base(sym))
            tag = "CRYPTO" if is_crypto else "EQUITY"
            now_iso = datetime.now(UTC).isoformat()
            print(  # noqa: T201 — operator-facing harness output
                f"[{now_iso}] NEW {tag} ORDER\n{_format_order_row(order)}",
                flush=True,
            )
            if is_crypto:
                seen_new_crypto_ids.add(oid)
                if first_fill_ts is None and order.get("filled_avg_price"):
                    first_fill_ts = order.get("filled_at") or now_iso
                    print(  # noqa: T201
                        f"[{now_iso}] FIRST CRYPTO FILL OBSERVED at filled_avg_price={order.get('filled_avg_price')}",
                        flush=True,
                    )
        # Lightweight progress heartbeat every ~30s so the operator can
        # tell the harness is still working.
        if poll_count % 6 == 0:
            remaining = max(0, int(deadline - time.monotonic()))
            print(  # noqa: T201
                f"[poll #{poll_count}] new_orders={len(seen_new_ids)} "
                f"new_crypto={len(seen_new_crypto_ids)} remaining_s={remaining}",
                flush=True,
            )
        await asyncio.sleep(poll_interval_s)

    print(  # noqa: T201
        f"\n=== verify_live_crypto_flow summary ===\n"
        f"  watch_seconds={watch_seconds}\n"
        f"  polls={poll_count}\n"
        f"  new_orders_total={len(seen_new_ids)}\n"
        f"  new_crypto_orders={len(seen_new_crypto_ids)}\n"
        f"  first_crypto_fill_ts={first_fill_ts or 'none'}\n",
        flush=True,
    )
    if seen_new_crypto_ids:
        return 0
    return 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--watch-seconds",
        type=int,
        default=DEFAULT_WATCH_SECONDS,
        help="How long to poll Alpaca for new crypto orders (default 300s).",
    )
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=DEFAULT_POLL_INTERVAL_S,
        help="Seconds between polls (default 5).",
    )
    parser.add_argument(
        "--alpaca-paper",
        action="store_true",
        help="Refuse to run unless ALPACA_BASE_URL targets a paper host.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Reduce log volume (errors only).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        return asyncio.run(
            watch(
                watch_seconds=args.watch_seconds,
                poll_interval_s=args.poll_interval_s,
                require_paper=args.alpaca_paper,
            )
        )
    except KeyboardInterrupt:
        logger.warning("interrupted; exiting non-zero")
        return 130


if __name__ == "__main__":
    sys.exit(main())
