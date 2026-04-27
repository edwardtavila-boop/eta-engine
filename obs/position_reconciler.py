"""Position-reconciliation watcher (Tier-1 #2, 2026-04-27).

A bot's internal-state position can drift from the broker's actual
position. Causes include partial fills, broker-side cancellations,
manual interventions, or reconnects that miss a fill notification.
When the bot's view of "I am 2 contracts long" disagrees with the
broker's "you are 0 contracts", every subsequent risk + size decision
is wrong.

This module exposes a watcher that:
  1. polls each registered broker for current positions
  2. compares against the bot's internal state file
  3. fires a Resend ``position_drift`` alert when the diff exceeds tolerance

SCAFFOLD: the broker-handshake half is venue-specific (IBKR Client
Portal API has its own auth flow, Tastytrade uses sessionToken, etc.).
We commit the framework + the reconciliation diff logic + tests; the
per-broker pollers are TODO-marked.

Run as a 30-second scheduled task once the per-broker pollers land.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("position_reconciler")

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class PositionDiff:
    bot: str
    symbol: str
    bot_qty: float
    broker_qty: float
    @property
    def abs_drift(self) -> float:
        return abs(self.bot_qty - self.broker_qty)


def diff_positions(
    bot_state: dict[str, dict[str, float]],
    broker_state: dict[str, dict[str, float]],
    *,
    tolerance: float = 0.0001,
) -> list[PositionDiff]:
    """Compare bot-internal positions to broker-reported positions.

    Both inputs shaped: {symbol: {bot_name: qty}}. Returns one
    ``PositionDiff`` for every (bot, symbol) where |bot - broker| > tolerance.
    """
    diffs: list[PositionDiff] = []
    all_keys: set[tuple[str, str]] = set()
    for sym, bots in bot_state.items():
        for b in bots:
            all_keys.add((b, sym))
    for sym, bots in broker_state.items():
        for b in bots:
            all_keys.add((b, sym))
    for bot_name, sym in sorted(all_keys):
        bq = bot_state.get(sym, {}).get(bot_name, 0.0)
        rq = broker_state.get(sym, {}).get(bot_name, 0.0)
        if abs(bq - rq) > tolerance:
            diffs.append(PositionDiff(bot=bot_name, symbol=sym, bot_qty=bq, broker_qty=rq))
    return diffs


def fetch_bot_positions() -> dict[str, dict[str, float]]:
    """TODO: load each bot's internal state and aggregate positions.

    Each bot writes its state to ``var/<bot_name>/state.json`` per the
    BaseBot.persist() pattern. Until that aggregator lands, this returns
    an empty mapping (which means: no diff vs broker -> no false alarms).
    """
    return {}


async def _fetch_broker_positions_async() -> dict[str, dict[str, float]]:
    """Query IBKR + Tastytrade for current positions, asyncio version.

    Returns ``{symbol: {venue_name: qty}}``. When a venue has no creds
    populated, ``get_positions()`` returns ``[]`` -- the missing data
    silently falls out of the diff (no false-positive alerts).

    Each position dict from the venue layer is normalized to:
      * symbol  (string, e.g. "MNQ" or "BTCUSDT")
      * qty     (float, signed: + long, - short)
    """
    out: dict[str, dict[str, float]] = {}
    venues: list[Any] = []
    try:
        from eta_engine.venues.ibkr import IbkrClientPortalVenue
        venues.append(IbkrClientPortalVenue())
    except Exception as exc:  # noqa: BLE001
        logger.debug("IBKR venue unavailable: %s", exc)
    try:
        from eta_engine.venues.tastytrade import TastytradeVenue
        venues.append(TastytradeVenue())
    except Exception as exc:  # noqa: BLE001
        logger.debug("Tastytrade venue unavailable: %s", exc)

    for venue in venues:
        try:
            positions = await venue.get_positions()
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_positions failed for venue=%s: %s",
                           getattr(venue, "name", "?"), exc)
            continue
        for pos in positions or []:
            sym = str(pos.get("symbol") or pos.get("ticker") or "").upper()
            if not sym:
                continue
            try:
                qty = float(pos.get("qty") or pos.get("position") or 0.0)
            except (TypeError, ValueError):
                continue
            out.setdefault(sym, {})[getattr(venue, "name", "venue")] = qty
    return out


def fetch_broker_positions() -> dict[str, dict[str, float]]:
    """Synchronous wrapper for venue position polling. Returns
    ``{symbol: {venue_name: qty}}``.

    Empty dict means: no broker reported any position. That's the safe
    default -- the diff against bot state will then either:
      (a) match (both empty) -> no alert
      (b) bot says +N, broker says 0 -> drift alert (which is correct;
          we don't have any data from the broker but the bot thinks it
          does, so the operator should be told)
    """
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Already inside an event loop; can't nest. Run in a thread.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(asyncio.run, _fetch_broker_positions_async()).result(timeout=30)
    except RuntimeError:
        pass
    return asyncio.run(_fetch_broker_positions_async())


def fire_drift_alert(diffs: list[PositionDiff], *, alerts_yaml: Path) -> None:
    try:
        import yaml
        from eta_engine.obs.alert_dispatcher import AlertDispatcher
        cfg = yaml.safe_load(alerts_yaml.read_text(encoding="utf-8"))
        dispatcher = AlertDispatcher(cfg)
        dispatcher.send("position_drift", {
            "diff_count": len(diffs),
            "summary": [
                {"bot": d.bot, "symbol": d.symbol, "bot_qty": d.bot_qty, "broker_qty": d.broker_qty}
                for d in diffs
            ],
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("position_drift alert dispatch failed (non-fatal): %s", exc)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tolerance", type=float, default=0.0001)
    p.add_argument("--alerts-yaml", type=Path, default=ROOT / "configs" / "alerts.yaml")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    bot_pos = fetch_bot_positions()
    broker_pos = fetch_broker_positions()
    diffs = diff_positions(bot_pos, broker_pos, tolerance=args.tolerance)

    if not diffs:
        logger.info("reconcile: %d bot-position keys, %d broker-position keys, NO DIFF",
                    sum(len(v) for v in bot_pos.values()),
                    sum(len(v) for v in broker_pos.values()))
        return 0

    logger.warning("reconcile: %d drift(s) detected", len(diffs))
    for d in diffs:
        logger.warning("  %s/%s: bot=%.4f broker=%.4f drift=%.4f",
                       d.bot, d.symbol, d.bot_qty, d.broker_qty, d.abs_drift)

    if not args.dry_run:
        fire_drift_alert(diffs, alerts_yaml=args.alerts_yaml)

    return 1  # exit non-zero so Task Scheduler registers an "incident"


if __name__ == "__main__":
    sys.exit(main())
