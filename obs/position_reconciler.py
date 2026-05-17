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

from eta_engine.scripts import workspace_roots

logger = logging.getLogger("position_reconciler")

ROOT = Path(__file__).resolve().parents[1]

#: Canonical workspace state root. Position truth currently lands in a
#: few compatible formats:
#:
#: * legacy bot state: ``<state_root>/bots/<bot_name>/positions.json``
#: * supervisor per-bot state: ``<state_root>/bots/<bot_id>/open_position.json``
#: * supervisor aggregate belief: ``<state_root>/supervisor_open_positions.json``
#:
#: This reconciler reads all three so the router/gates stay aligned with the
#: active Jarvis supervisor lane instead of only the older BaseBot layout.
DEFAULT_STATE_ROOT: Path = workspace_roots.ETA_RUNTIME_STATE_DIR

#: Glob pattern that picks up every legacy BaseBot positions file under the
#: state root. ``*`` matches ``self.config.name`` (no recursion -- the
#: per-bot directory is always exactly one level deep).
BOT_POSITIONS_GLOB: str = "bots/*/positions.json"
SUPERVISOR_OPEN_POSITION_GLOB: str = "bots/*/open_position.json"
SUPERVISOR_STATE_FILE: str = "supervisor_open_positions.json"


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

    **Supported state contracts:**

    * ``BaseBot.persist_positions`` writes
      ``<state_root>/bots/<bot.config.name>/positions.json`` after every
      fill (see ``eta_engine/bots/base_bot.py``).
    * ``jarvis_strategy_supervisor`` writes
      ``<state_root>/bots/<bot_id>/open_position.json`` for each active
      bot, plus the aggregate heartbeat
      ``<state_root>/supervisor_open_positions.json``.

    This function merges all compatible sources into a single
    ``{symbol: {bot_name: signed_qty}}`` map so the router/correlation
    gates see the active supervisor truth instead of only the legacy bot
    layout.

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
    legacy_files = sorted(root.glob(BOT_POSITIONS_GLOB))
    supervisor_open_position_files = sorted(root.glob(SUPERVISOR_OPEN_POSITION_GLOB))
    supervisor_state_path = root / SUPERVISOR_STATE_FILE

    if not legacy_files and not supervisor_open_position_files and not supervisor_state_path.exists():
        if os.environ.get("ETA_RECONCILE_ALLOW_EMPTY_STATE") == "1":
            logger.info(
                "no bot position state files under %s; ETA_RECONCILE_ALLOW_EMPTY_STATE=1 honored (first-boot case)",
                root / "bots",
            )
            return {}
        raise RuntimeError(
            f"no bot position state files found under {root / 'bots'!s} "
            f"(globs '{BOT_POSITIONS_GLOB}' / '{SUPERVISOR_OPEN_POSITION_GLOB}') "
            f"and no aggregate state at {supervisor_state_path!s}; a wiped state dir silently "
            "looks 'all reconciled'. Set ETA_RECONCILE_ALLOW_EMPTY_STATE=1 "
            "for first-boot, or ETA_RECONCILE_DISABLED=1 to skip entirely."
        )

    out: dict[str, dict[str, float]] = {}

    def _record(symbol: object, bot_name: object, qty: float) -> None:
        if abs(qty) <= 0.0:
            return
        symbol_key = str(symbol or "").upper().strip()
        bot_key = str(bot_name or "").strip()
        if not symbol_key or not bot_key:
            return
        out.setdefault(symbol_key, {})[bot_key] = qty

    def _signed_qty(qty: object, side: object | None = None) -> float:
        qty_value = float(qty or 0.0)
        if qty_value == 0.0:
            return 0.0
        if qty_value < 0.0:
            return qty_value
        side_norm = str(side or "").strip().upper()
        if side_norm in {"SELL", "SHORT"}:
            return -abs(qty_value)
        return abs(qty_value)

    for path in legacy_files:
        # Bot name is the parent-directory name -- mirrors the layout
        # written by ``BaseBot._positions_path``.
        bot_name = path.parent.name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "skipping corrupt bot-positions file for bot=%s (%s): %s",
                bot_name,
                path,
                exc,
            )
            continue
        # Prefer the embedded bot_name when present (defensive against
        # someone renaming the directory); fall back to dir name.
        recorded_name = str(payload.get("bot_name") or bot_name)
        for entry in payload.get("positions", []) or []:
            try:
                symbol = str(entry["symbol"]).upper()
                qty = _signed_qty(entry.get("qty", 0.0), entry.get("side"))
            except (KeyError, TypeError, ValueError):
                continue
            _record(symbol, recorded_name, qty)

    if supervisor_state_path.exists():
        try:
            payload = json.loads(supervisor_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "skipping unreadable supervisor aggregate positions file (%s): %s",
                supervisor_state_path,
                exc,
            )
        else:
            for entry in payload.get("positions", []) or []:
                try:
                    symbol = str(entry["symbol"]).upper()
                    qty = _signed_qty(entry.get("qty", 0.0), entry.get("side"))
                except (KeyError, TypeError, ValueError):
                    continue
                bot_name = entry.get("bot_id") or entry.get("bot_name")
                _record(symbol, bot_name, qty)

    for path in supervisor_open_position_files:
        bot_name = path.parent.name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "skipping unreadable supervisor open-position file for bot=%s (%s): %s",
                bot_name,
                path,
                exc,
            )
            continue
        try:
            symbol = str(payload["symbol"]).upper()
            qty = _signed_qty(payload.get("qty", 0.0), payload.get("side"))
        except (KeyError, TypeError, ValueError):
            continue
        _record(symbol, bot_name, qty)
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
            logger.warning("get_positions failed for venue=%s: %s", getattr(venue, "name", "?"), exc)
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
        dispatcher.send(
            "position_drift",
            {
                "diff_count": len(diffs),
                "summary": [
                    {"bot": d.bot, "symbol": d.symbol, "bot_qty": d.bot_qty, "broker_qty": d.broker_qty} for d in diffs
                ],
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("position_drift alert dispatch failed (non-fatal): %s", exc)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
        logger.info(
            "reconcile: %d bot-position keys, %d broker-position keys, NO DIFF",
            sum(len(v) for v in bot_pos.values()),
            sum(len(v) for v in broker_pos.values()),
        )
        return 0

    logger.warning("reconcile: %d drift(s) detected", len(diffs))
    for d in diffs:
        logger.warning(
            "  %s/%s: bot=%.4f broker=%.4f drift=%.4f", d.bot, d.symbol, d.bot_qty, d.broker_qty, d.abs_drift
        )

    if not args.dry_run:
        fire_drift_alert(diffs, alerts_yaml=args.alerts_yaml)

    return 1  # exit non-zero so Task Scheduler registers an "incident"


if __name__ == "__main__":
    sys.exit(main())
