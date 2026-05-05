"""Position-reconciliation watcher (Tier-1 #2, 2026-04-27).

A bot's internal-state position can drift from the broker's actual
position. Causes include partial fills, broker-side cancellations,
manual interventions, or reconnects that miss a fill notification.
When the bot's view of "I am 2 contracts long" disagrees with the
broker's "you are 0 contracts", every subsequent risk + size decision
is wrong.

This module exposes a watcher that:
  1. loads each bot's persisted positions from disk
  2. polls each registered broker for current positions
  3. compares the two and fires a Resend ``position_drift`` alert when
     the diff exceeds tolerance

Run as a 30-second scheduled task once the per-broker pollers land.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("position_reconciler")

ROOT = Path(__file__).resolve().parents[1]

#: Canonical workspace state root. Bots persist their open positions to
#: ``<state_root>/bots/<bot_name>/positions.json`` from
#: ``BaseBot.persist_positions``; this module aggregates them.
DEFAULT_STATE_ROOT: Path = Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state")

#: Glob pattern that picks up every per-bot positions file under the
#: state root. ``*`` matches ``self.config.name`` (no recursion -- the
#: per-bot directory is always exactly one level deep).
BOT_POSITIONS_GLOB: str = "bots/*/positions.json"


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


def fetch_bot_positions(
    state_root: Path | None = None,
) -> dict[str, dict[str, float]]:
    """Aggregate every bot's persisted positions into ``{symbol: {bot_name: qty}}``.

    This is the bot half of reconciliation. The broker half lives in
    :func:`fetch_broker_positions`. The diff between the two is what
    :func:`diff_positions` operates on.

    **Contract (2026-05-04):** ``BaseBot.persist_positions`` writes
    ``<state_root>/bots/<bot.config.name>/positions.json`` after every
    fill (see ``eta_engine/bots/base_bot.py``). This function globs
    those files and aggregates per-bot signed quantities by symbol.

    Behavior:

    * One file per bot, valid JSON: contributes to the aggregate.
    * One file per bot, corrupt JSON: log WARNING, skip that bot,
      continue. A single bad file does not poison reconciliation.
    * No files at all under ``<state_root>/bots/``: fail-loud with
      :class:`RuntimeError`. An empty bots directory is suspicious --
      a wiped state dir would silently look "all reconciled" and mask
      the exact crash-recovery gap this watcher exists to catch.

    Operator escape hatches:

    * ``ETA_RECONCILE_DISABLED=1`` -- return ``{}`` with a WARNING.
    * ``ETA_RECONCILE_ALLOW_EMPTY_STATE=1`` -- legitimate first-boot
      case where no bot has written yet; return ``{}`` silently.
    """
    if os.environ.get("ETA_RECONCILE_DISABLED") == "1":
        logger.warning(
            "position reconciliation DISABLED via ETA_RECONCILE_DISABLED=1; "
            "bot-vs-broker drift will NOT be detected. "
            "Operator must verify positions manually."
        )
        return {}

    root = Path(state_root) if state_root is not None else DEFAULT_STATE_ROOT
    files = sorted(root.glob(BOT_POSITIONS_GLOB))

    if not files:
        if os.environ.get("ETA_RECONCILE_ALLOW_EMPTY_STATE") == "1":
            logger.info(
                "no bot positions files under %s; "
                "ETA_RECONCILE_ALLOW_EMPTY_STATE=1 honored (first-boot case)",
                root / "bots",
            )
            return {}
        raise RuntimeError(
            f"no per-bot positions files found under {root / 'bots'!s} "
            f"(glob '{BOT_POSITIONS_GLOB}'); a wiped state dir silently "
            "looks 'all reconciled'. Set ETA_RECONCILE_ALLOW_EMPTY_STATE=1 "
            "for first-boot, or ETA_RECONCILE_DISABLED=1 to skip entirely."
        )

    out: dict[str, dict[str, float]] = {}
    for path in files:
        # Bot name is the parent-directory name -- mirrors the layout
        # written by ``BaseBot._positions_path``.
        bot_name = path.parent.name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "skipping corrupt bot-positions file for bot=%s (%s): %s",
                bot_name, path, exc,
            )
            continue
        # Prefer the embedded bot_name when present (defensive against
        # someone renaming the directory); fall back to dir name.
        recorded_name = str(payload.get("bot_name") or bot_name)
        for entry in payload.get("positions", []) or []:
            try:
                symbol = str(entry["symbol"]).upper()
                qty = float(entry.get("qty", 0.0))
            except (KeyError, TypeError, ValueError):
                continue
            if not symbol:
                continue
            out.setdefault(symbol, {})[recorded_name] = qty
    return out


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
