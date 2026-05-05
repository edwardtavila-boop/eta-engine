"""Broker router: consumes pending_order.json files written by the strategy supervisor.

Dispatches each pending order through the gate chain, then submits via SmartRouter.
Owns long-lived venue connections so all bots share one IBKR session.

State layout under var/eta_engine/state/router/:
  pending/    -- not used; we read from docs/btc_live/broker_fleet/
  processing/ -- in-flight orders (atomic-rename lock)
  blocked/    -- gate-denied orders (audit)
  archive/<YYYY-MM-DD>/ -- terminal states (filled/rejected/etc.)
  quarantine/ -- malformed JSON
  failed/     -- venue submission errors after retries
  fill_results/ -- sidecar JSONs per submitted order
  broker_router_heartbeat.json -- liveness signal

Honors env vars:
  ETA_BROKER_ROUTER_INTERVAL_S (default 5)
  ETA_BROKER_ROUTER_PENDING_DIR (default <repo>/eta_engine/docs/btc_live/broker_fleet)
  ETA_BROKER_ROUTER_STATE_ROOT (default C:/EvolutionaryTradingAlgo/var/eta_engine/state/router)
  ETA_BROKER_ROUTER_DRY_RUN (default 0)
  ETA_BROKER_ROUTER_MAX_RETRIES (default 3)
  + ETA_LIVE_MODE, ETA_GATE_BOOTSTRAP, ETA_IDEMPOTENCY_STORE etc. passed
    through to underlying systems.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import traceback
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from eta_engine.obs.decision_journal import (  # noqa: E402
    Actor,
    DecisionJournal,
    Outcome,
    default_journal,
)
from eta_engine.scripts.workspace_roots import (  # noqa: E402
    ETA_RUNTIME_STATE_DIR,
)
from eta_engine.venues.base import (  # noqa: E402
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    VenueBase,
)
from eta_engine.venues.router import SmartRouter  # noqa: E402

logger = logging.getLogger("eta_engine.broker_router")

# ---------------------------------------------------------------------------
# Defaults & symbol mapping
# ---------------------------------------------------------------------------

#: Default pending-order directory. Mirrors the supervisor write path
#: in jarvis_strategy_supervisor.py:826
#: (``ROOT / "docs" / "btc_live" / "broker_fleet"`` where ROOT is
#: ``eta_engine/``). Operators may override with ETA_BROKER_ROUTER_PENDING_DIR.
DEFAULT_PENDING_DIR = ROOT / "docs" / "btc_live" / "broker_fleet"

#: Default router state root, anchored under canonical workspace state.
DEFAULT_STATE_ROOT = ETA_RUNTIME_STATE_DIR / "router"

DEFAULT_INTERVAL_S = 5.0
DEFAULT_MAX_RETRIES = 3

#: Translate the supervisor's raw symbol token to the form the target
#: venue expects. Keys are (raw_symbol, venue_name); when the venue is
#: unknown, the IBKR mapping is used (since IBKR is the M2 default).
_SYMBOL_TABLE: dict[tuple[str, str], str] = {
    ("BTC", "ibkr"): "BTCUSD",
    ("ETH", "ibkr"): "ETHUSD",
    ("SOL", "ibkr"): "SOLUSD",
    ("XRP", "ibkr"): "XRPUSD",
    ("BTC", "tastytrade"): "BTCUSDT",
    ("ETH", "tastytrade"): "ETHUSDT",
    ("SOL", "tastytrade"): "SOLUSDT",
    ("XRP", "tastytrade"): "XRPUSDT",
    # Crypto-native symbols pass through unchanged for non-US-person flows.
    ("BTCUSDT", "bybit"): "BTCUSDT",
    ("ETHUSDT", "bybit"): "ETHUSDT",
    ("SOLUSDT", "bybit"): "SOLUSDT",
    ("XRPUSDT", "bybit"): "XRPUSDT",
}

#: Recognized futures roots that don't need symbol normalization.
_FUTURES_ROOTS = ("MNQ", "NQ", "ES", "MES", "RTY", "MBT", "MET")


def normalize_symbol(raw_symbol: str, target_venue: str) -> str:
    """Translate a supervisor's raw symbol to the venue's expected form.

    Supervisor writes ``BTC``/``ETH``/``SOL``/``XRP`` (raw crypto roots) or
    futures forms like ``MNQ1`` (with month suffix). Venues expect
    ``BTCUSD`` (IBKR PAXOS), ``BTCUSDT`` (Tastytrade/Bybit), or futures
    roots stripped of any month suffix.

    Args:
        raw_symbol: Symbol string from the pending-order JSON.
        target_venue: Lowercase venue name (e.g. ``"ibkr"``).

    Returns:
        The venue-ready symbol string.

    Raises:
        ValueError: If the (symbol, venue) pair is unsupported.
    """
    up = raw_symbol.strip().upper()
    venue = target_venue.strip().lower()

    # Futures pass-through: strip trailing month-coded suffix when present
    # ("MNQ1" -> "MNQ", "MBTH26" -> "MBT").
    for root in _FUTURES_ROOTS:
        if up == root:
            return root
        if up.startswith(root):
            suffix = up[len(root):]
            # Bare-digit month index ("MNQ1") or CME month-code ("MBTH26").
            if suffix.isdigit() or (
                len(suffix) >= 2
                and suffix[0] in "FGHJKMNQUVXZ"
                and suffix[1:].isdigit()
            ):
                return root

    key = (up, venue)
    if key in _SYMBOL_TABLE:
        return _SYMBOL_TABLE[key]
    # Pass-through for already-normalized forms.
    if up.endswith(("USD", "USDT", "USDC")):
        return up
    msg = f"unsupported (symbol, venue) pair: ({raw_symbol!r}, {target_venue!r})"
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Pending-order parsing
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PendingOrder:
    """One row of the supervisor pending-order JSONL contract."""

    ts: str
    signal_id: str
    side: str
    qty: float
    symbol: str
    limit_price: float
    bot_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_pending_file(path: Path) -> PendingOrder:
    """Parse one ``<bot_id>.pending_order.json`` file.

    Bot id is taken from the filename stem (everything before the first ``.``).

    Raises:
        ValueError: when JSON is malformed or any required field is missing.
    """
    name = path.name
    if not name.endswith(".pending_order.json"):
        raise ValueError(f"unexpected filename pattern: {name!r}")
    bot_id = name[: -len(".pending_order.json")]
    if not bot_id:
        raise ValueError(f"empty bot_id in filename: {name!r}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"json read failed: {exc}") from exc

    required = ("ts", "signal_id", "side", "qty", "symbol", "limit_price")
    missing = [k for k in required if k not in payload]
    if missing:
        raise ValueError(f"missing fields {missing} in {name!r}")

    side = str(payload["side"]).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"invalid side {side!r}")

    try:
        qty = float(payload["qty"])
        limit_price = float(payload["limit_price"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"non-numeric qty/limit_price: {exc}") from exc

    if qty <= 0.0:
        raise ValueError(f"non-positive qty {qty}")

    return PendingOrder(
        ts=str(payload["ts"]),
        signal_id=str(payload["signal_id"]),
        side=side,
        qty=qty,
        symbol=str(payload["symbol"]),
        limit_price=limit_price,
        bot_id=bot_id,
    )


# ---------------------------------------------------------------------------
# BrokerRouter
# ---------------------------------------------------------------------------


class BrokerRouter:
    """Long-running consumer for supervisor-emitted pending orders.

    One instance owns the SmartRouter (which owns venue sessions) and the
    decision journal. Safe to run as a single process; if multiple
    instances ever race on the same pending dir, the atomic-rename lock
    in :meth:`_process_pending_file` prevents double-submission.
    """

    def __init__(
        self,
        pending_dir: Path,
        state_root: Path,
        smart_router: SmartRouter,
        journal: DecisionJournal,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        dry_run: bool = False,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self.pending_dir = Path(pending_dir)
        self.state_root = Path(state_root)
        self.smart_router = smart_router
        self.journal = journal
        self.interval_s = max(0.5, float(interval_s))
        self.dry_run = bool(dry_run)
        self.max_retries = max(1, int(max_retries))

        self.processing_dir = self.state_root / "processing"
        self.blocked_dir = self.state_root / "blocked"
        self.archive_dir = self.state_root / "archive"
        self.quarantine_dir = self.state_root / "quarantine"
        self.failed_dir = self.state_root / "failed"
        self.fill_results_dir = self.state_root / "fill_results"
        self.heartbeat_path = self.state_root / "broker_router_heartbeat.json"

        for d in (
            self.processing_dir,
            self.blocked_dir,
            self.archive_dir,
            self.quarantine_dir,
            self.failed_dir,
            self.fill_results_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        self._stopped = False
        self._retry_counts: dict[str, int] = {}
        # Bounded recent-event ring for heartbeat reporting.
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=32)
        self._counts: dict[str, int] = {
            "parsed": 0,
            "blocked": 0,
            "submitted": 0,
            "filled": 0,
            "rejected": 0,
            "failed": 0,
            "quarantined": 0,
        }

    # -- lifecycle ----------------------------------------------------------

    def request_stop(self) -> None:
        """Signal the run loop to drain and exit on next iteration boundary."""
        self._stopped = True

    async def run(self) -> None:
        """Main poll loop. Stops on SIGINT/SIGTERM or :meth:`request_stop`."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, AttributeError):
                loop.add_signal_handler(sig, self.request_stop)

        logger.info(
            "broker_router starting pending=%s state=%s dry_run=%s interval_s=%.1f",
            self.pending_dir, self.state_root, self.dry_run, self.interval_s,
        )
        while not self._stopped:
            await self._tick()
            if self._stopped:
                break
            try:
                await asyncio.sleep(self.interval_s)
            except asyncio.CancelledError:
                break
        logger.info("broker_router stopped")

    async def run_once(self) -> None:
        """Single-pass scan + heartbeat. Used by ``--once`` and tests."""
        await self._tick()

    async def _tick(self) -> None:
        """One poll: scan for *.pending_order.json, dispatch each, heartbeat."""
        try:
            paths = sorted(self.pending_dir.glob("*.pending_order.json"))
        except OSError as exc:
            logger.warning("pending dir scan failed: %s", exc)
            paths = []
        for path in paths:
            if self._stopped:
                break
            try:
                await self._process_pending_file(path)
            except Exception:  # noqa: BLE001 -- one bad file must not kill the loop
                logger.error(
                    "unhandled exception processing %s:\n%s",
                    path, traceback.format_exc(),
                )
        self._emit_heartbeat()

    # -- per-file lifecycle -------------------------------------------------

    async def _process_pending_file(self, path: Path) -> None:
        """Move-to-processing, parse, gate, route, submit, archive."""
        # 1. Atomic-move to processing/. If this raises (another worker grabbed
        # the file, or the supervisor is mid-write) skip and let the next tick
        # try again.
        if self.dry_run:
            target = path  # leave the file in place
        else:
            target = self.processing_dir / path.name
            try:
                self._atomic_move(path, target)
            except OSError as exc:
                logger.info("skip (move failed, likely raced): %s (%s)", path.name, exc)
                return

        # 2. Parse.
        try:
            order = parse_pending_file(target)
        except ValueError as exc:
            self._counts["quarantined"] += 1
            self._record_event(target.name, "quarantined", str(exc))
            if not self.dry_run:
                quarantine_target = self.quarantine_dir / target.name
                with contextlib.suppress(OSError):
                    self._atomic_move(target, quarantine_target)
            self._safe_journal(
                actor=Actor.STRATEGY_ROUTER,
                intent="pending_order_quarantined",
                rationale=f"parse failed: {exc}",
                outcome=Outcome.NOTED,
                links=[f"file:{target.name}"],
                metadata={"path": str(target), "error": str(exc)},
            )
            return
        self._counts["parsed"] += 1

        # 3. Gate-chain evaluation. Lazy-import to keep this module testable
        # without the firm/eta_engine submodule on sys.path.
        gate_results = await self._evaluate_gates(order)
        gate_checks_summary = [
            ("+" if r["allow"] else "-") + r["gate"] for r in gate_results
        ]
        denied = next((r for r in gate_results if not r["allow"]), None)
        if denied is not None:
            self._counts["blocked"] += 1
            self._record_event(target.name, "blocked", denied["gate"])
            block_meta = {
                "denied_gate": denied["gate"],
                "reason": denied["reason"],
                "context": denied["context"],
                "all_gates": gate_results,
                "order": order.to_dict(),
            }
            if not self.dry_run:
                self._write_sidecar(
                    self.blocked_dir / f"{order.signal_id}_block.json",
                    block_meta,
                )
                blocked_target = self.blocked_dir / target.name
                with contextlib.suppress(OSError):
                    self._atomic_move(target, blocked_target)
            self._safe_journal(
                actor=Actor.STRATEGY_ROUTER,
                intent="pending_order_blocked",
                rationale=f"gate={denied['gate']} reason={denied['reason']}",
                gate_checks=gate_checks_summary,
                outcome=Outcome.BLOCKED,
                links=[f"signal:{order.signal_id}", f"bot:{order.bot_id}"],
                metadata=block_meta,
            )
            return

        # 4. Pick venue.
        try:
            venue = self.smart_router.choose_venue(
                order.symbol, order.qty, urgency="normal",
            )
        except Exception as exc:  # noqa: BLE001
            self._handle_routing_error(order, target, f"choose_venue failed: {exc}")
            return

        # 5. Normalize symbol for the chosen venue.
        try:
            venue_symbol = normalize_symbol(order.symbol, venue.name)
        except ValueError as exc:
            self._handle_routing_error(order, target, f"normalize_symbol failed: {exc}")
            return

        # 6. Build OrderRequest.
        side_enum = Side.BUY if order.side == "BUY" else Side.SELL
        request = OrderRequest(
            symbol=venue_symbol,
            side=side_enum,
            qty=order.qty,
            order_type=OrderType.LIMIT,
            price=order.limit_price,
            client_order_id=order.signal_id,
            bot_id=order.bot_id,
        )

        # 7. Dry-run short-circuit: log, do not submit, do not move.
        if self.dry_run:
            logger.info(
                "[dry_run] would submit signal=%s bot=%s venue=%s symbol=%s "
                "side=%s qty=%s limit=%s",
                order.signal_id, order.bot_id, venue.name, venue_symbol,
                order.side, order.qty, order.limit_price,
            )
            self._safe_journal(
                actor=Actor.STRATEGY_ROUTER,
                intent="pending_order_dry_run",
                rationale="dry_run=True; no venue submission",
                gate_checks=gate_checks_summary,
                outcome=Outcome.NOTED,
                links=[f"signal:{order.signal_id}", f"bot:{order.bot_id}"],
                metadata={"venue": venue.name, "venue_symbol": venue_symbol},
            )
            return

        # 8. Submit. Venue handles its own idempotency / fleet / cap gates.
        await self._submit_and_finalize(
            order, target, venue, request, gate_checks_summary,
        )

    async def _submit_and_finalize(
        self,
        order: PendingOrder,
        target: Path,
        venue: VenueBase,
        request: OrderRequest,
        gate_checks_summary: list[str],
    ) -> None:
        """Send the order, classify the result, archive or fail."""
        self._counts["submitted"] += 1
        try:
            result = await venue.place_order(request)
        except Exception as exc:  # noqa: BLE001
            self._handle_routing_error(
                order, target, f"venue.place_order raised: {exc}",
            )
            return

        sidecar_payload = {
            "signal_id": order.signal_id,
            "bot_id": order.bot_id,
            "venue": venue.name,
            "request": json.loads(request.model_dump_json()),
            "result": json.loads(result.model_dump_json()),
            "ts": datetime.now(UTC).isoformat(),
        }
        self._write_sidecar(
            self.fill_results_dir / f"{order.signal_id}_result.json",
            sidecar_payload,
        )

        if result.status is OrderStatus.REJECTED:
            self._counts["rejected"] += 1
            self._retry_counts[order.signal_id] = (
                self._retry_counts.get(order.signal_id, 0) + 1
            )
            attempts = self._retry_counts[order.signal_id]
            if attempts >= self.max_retries:
                self._counts["failed"] += 1
                self._record_event(target.name, "failed", "max_retries")
                fail_target = self.failed_dir / target.name
                with contextlib.suppress(OSError):
                    self._atomic_move(target, fail_target)
                self._safe_journal(
                    actor=Actor.STRATEGY_ROUTER,
                    intent="pending_order_failed",
                    rationale=(
                        f"venue={venue.name} rejected {attempts} times; "
                        f"order_id={result.order_id}"
                    ),
                    gate_checks=gate_checks_summary,
                    outcome=Outcome.FAILED,
                    links=[
                        f"signal:{order.signal_id}",
                        f"bot:{order.bot_id}",
                        f"order:{result.order_id}",
                    ],
                    metadata=sidecar_payload,
                )
                self._retry_counts.pop(order.signal_id, None)
            else:
                # Leave the file in processing/ for the next tick to retry.
                logger.info(
                    "rejected attempt=%d/%d signal=%s; will retry",
                    attempts, self.max_retries, order.signal_id,
                )
                self._record_event(target.name, "rejected_retry", str(attempts))
                self._safe_journal(
                    actor=Actor.STRATEGY_ROUTER,
                    intent="pending_order_rejected_retry",
                    rationale=(
                        f"venue={venue.name} rejected attempt={attempts}/"
                        f"{self.max_retries}"
                    ),
                    gate_checks=gate_checks_summary,
                    outcome=Outcome.NOTED,
                    links=[
                        f"signal:{order.signal_id}",
                        f"bot:{order.bot_id}",
                        f"order:{result.order_id}",
                    ],
                    metadata=sidecar_payload,
                )
            return

        # FILLED / PARTIAL / OPEN -> archive as terminal.
        self._counts["filled"] += 1
        self._record_event(target.name, "executed", result.status.value)
        self._retry_counts.pop(order.signal_id, None)
        archive_dated = self.archive_dir / datetime.now(UTC).strftime("%Y-%m-%d")
        archive_dated.mkdir(parents=True, exist_ok=True)
        archive_target = archive_dated / target.name
        with contextlib.suppress(OSError):
            self._atomic_move(target, archive_target)
        self._safe_journal(
            actor=Actor.STRATEGY_ROUTER,
            intent="pending_order_executed",
            rationale=(
                f"venue={venue.name} status={result.status.value} "
                f"filled={result.filled_qty} avg_price={result.avg_price}"
            ),
            gate_checks=gate_checks_summary,
            outcome=Outcome.EXECUTED,
            links=[
                f"signal:{order.signal_id}",
                f"bot:{order.bot_id}",
                f"order:{result.order_id}",
            ],
            metadata=sidecar_payload,
        )

    def _handle_routing_error(
        self, order: PendingOrder, target: Path, reason: str,
    ) -> None:
        """Move to failed/, journal, increment counters."""
        self._counts["failed"] += 1
        self._record_event(target.name, "routing_error", reason)
        if not self.dry_run:
            with contextlib.suppress(OSError):
                self._atomic_move(target, self.failed_dir / target.name)
        self._safe_journal(
            actor=Actor.STRATEGY_ROUTER,
            intent="pending_order_routing_error",
            rationale=reason,
            outcome=Outcome.FAILED,
            links=[f"signal:{order.signal_id}", f"bot:{order.bot_id}"],
            metadata={"reason": reason, "order": order.to_dict()},
        )

    # -- gate chain ---------------------------------------------------------

    async def _evaluate_gates(self, order: PendingOrder) -> list[dict[str, Any]]:
        """Run the firm/eta_engine default gate chain. Returns list of dicts.

        Each dict has keys ``gate``, ``allow``, ``reason``, ``context``.
        Lazy-imports the gate chain so this module loads even when the
        firm/eta_engine submodule isn't on sys.path.
        """
        # Inject firm/eta_engine/src so ``mnq.risk.gate_chain`` resolves.
        firm_src = ROOT.parent / "firm" / "eta_engine" / "src"
        if firm_src.is_dir() and str(firm_src) not in sys.path:
            sys.path.insert(0, str(firm_src))

        try:
            from mnq.risk.gate_chain import build_default_chain  # type: ignore[import-not-found]
        except ImportError as exc:
            logger.warning(
                "gate chain import failed (%s); allowing order through "
                "with a NOTED journal entry. Operator must investigate.",
                exc,
            )
            return [{
                "gate": "import_error",
                "allow": True,
                "reason": f"gate_chain unavailable: {exc}",
                "context": {},
            }]

        # Pull current bot positions for the correlation gate. Tolerate
        # the empty-state first-boot case via the documented env var.
        try:
            from eta_engine.obs.position_reconciler import fetch_bot_positions
            agg = fetch_bot_positions()
        except RuntimeError as exc:
            if os.environ.get("ETA_RECONCILE_ALLOW_EMPTY_STATE") == "1":
                logger.info("empty bot-positions tolerated: %s", exc)
                agg = {}
            else:
                logger.warning("fetch_bot_positions failed: %s", exc)
                agg = {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_bot_positions errored: %s", exc)
            agg = {}

        # Collapse {symbol: {bot: qty}} into {symbol: int(net_qty)} for the
        # correlation gate's expected shape.
        open_positions: dict[str, int] = {}
        for symbol, by_bot in agg.items():
            net = sum(by_bot.values())
            if abs(net) > 0.0:
                open_positions[symbol] = int(round(net))

        try:
            chain = build_default_chain(
                open_positions=open_positions,
                new_symbol=order.symbol,
                new_qty=int(round(order.qty)) or 1,
            )
            allow, results = chain.evaluate()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "gate chain evaluation raised %s; treating as DENY (fail-closed)",
                exc,
            )
            return [{
                "gate": "chain_error",
                "allow": False,
                "reason": f"chain raised: {exc}",
                "context": {},
            }]

        return [
            {
                "gate": r.gate,
                "allow": r.allow,
                "reason": r.reason,
                "context": dict(r.context) if r.context else {},
            }
            for r in results
        ]

    # -- IO helpers ---------------------------------------------------------

    def _atomic_move(self, src: Path, dst: Path) -> None:
        """Rename with parent-mkdir; raises OSError on collision/race."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)

    def _write_sidecar(self, path: Path, payload: dict[str, Any]) -> None:
        """Write a small JSON sidecar; failures are logged not raised."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("sidecar write failed %s: %s", path, exc)

    def _emit_heartbeat(self) -> None:
        """Write a small heartbeat snapshot for monitoring."""
        snap = {
            "ts": datetime.now(UTC).isoformat(),
            "pending_dir": str(self.pending_dir),
            "state_root": str(self.state_root),
            "dry_run": self.dry_run,
            "interval_s": self.interval_s,
            "max_retries": self.max_retries,
            "counts": dict(self._counts),
            "recent_events": list(self._recent_events),
        }
        self._write_sidecar(self.heartbeat_path, snap)

    def _record_event(self, filename: str, kind: str, detail: str) -> None:
        self._recent_events.append({
            "ts": datetime.now(UTC).isoformat(),
            "file": filename,
            "kind": kind,
            "detail": detail,
        })

    def _safe_journal(
        self,
        *,
        actor: Actor,
        intent: str,
        rationale: str = "",
        gate_checks: list[str] | None = None,
        outcome: Outcome = Outcome.NOTED,
        links: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append to the journal; failures are logged, not raised."""
        try:
            self.journal.record(
                actor=actor,
                intent=intent,
                rationale=rationale,
                gate_checks=gate_checks or [],
                outcome=outcome,
                links=links or [],
                metadata=metadata or {},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("journal append failed (intent=%s): %s", intent, exc)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _resolve_pending_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    env = os.environ.get("ETA_BROKER_ROUTER_PENDING_DIR")
    if env:
        return Path(env)
    return DEFAULT_PENDING_DIR


def _resolve_state_root(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    env = os.environ.get("ETA_BROKER_ROUTER_STATE_ROOT")
    if env:
        return Path(env)
    return DEFAULT_STATE_ROOT


def _resolve_interval(arg: float | None) -> float:
    if arg is not None:
        return float(arg)
    env = os.environ.get("ETA_BROKER_ROUTER_INTERVAL_S")
    if env:
        try:
            return float(env)
        except ValueError:
            logger.warning("invalid ETA_BROKER_ROUTER_INTERVAL_S=%r; using default", env)
    return DEFAULT_INTERVAL_S


def _resolve_dry_run(arg: bool) -> bool:
    if arg:
        return True
    return os.environ.get("ETA_BROKER_ROUTER_DRY_RUN", "").strip() in ("1", "true", "yes")


def _resolve_max_retries(arg: int | None) -> int:
    if arg is not None:
        return int(arg)
    env = os.environ.get("ETA_BROKER_ROUTER_MAX_RETRIES")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return DEFAULT_MAX_RETRIES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="broker_router",
        description=__doc__.split("\n", 1)[0],
    )
    parser.add_argument("--interval", type=float, default=None,
                        help="Poll interval seconds (default 5).")
    parser.add_argument("--pending-dir", type=str, default=None,
                        help="Where the supervisor writes *.pending_order.json files.")
    parser.add_argument("--state-root", type=str, default=None,
                        help="Router state root for processing/blocked/archive.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and gate-check, but do not submit or move files.")
    parser.add_argument("--once", action="store_true",
                        help="Single pass, then exit.")
    parser.add_argument("--max-retries", type=int, default=None,
                        help="Max venue rejections before moving to failed/.")
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    pending_dir = _resolve_pending_dir(args.pending_dir)
    state_root = _resolve_state_root(args.state_root)
    interval_s = _resolve_interval(args.interval)
    dry_run = _resolve_dry_run(args.dry_run)
    max_retries = _resolve_max_retries(args.max_retries)

    smart_router = SmartRouter()
    journal = default_journal()
    router = BrokerRouter(
        pending_dir=pending_dir,
        state_root=state_root,
        smart_router=smart_router,
        journal=journal,
        interval_s=interval_s,
        dry_run=dry_run,
        max_retries=max_retries,
    )
    if args.once:
        asyncio.run(router.run_once())
    else:
        asyncio.run(router.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
