"""
BTC broker-paper lane runner.
==============================

Each of the four BTC workers (``btc-{grid,directional}-{tastytrade,ibkr}``)
runs a :class:`PaperLaneRunner` to drive real paper-order lifecycle
against its broker, replacing the prior heartbeat-only behavior.

A lane runner:
  * builds the broker adapter once at startup (TastytradeVenue or
    IbkrClientPortalVenue);
  * maintains a per-lane "probe" order state: on the first tick after
    the broker is ready, submits a tiny limit-BUY intentionally priced
    far from market so it rests on the book without filling;
  * on every subsequent tick, polls the broker with
    ``get_order_status`` and reflects the current state
    (Routed -> Live -> Cancelled/Replaced) into the worker's heartbeat;
  * writes one line per status transition to
    ``docs/btc_live/broker_fleet/btc_paper_trades.jsonl``;
  * cancels the probe order on clean shutdown.

Auto-submission is gated by the ``BTC_PAPER_LANE_AUTO_SUBMIT`` env
var (default: ``0``). The operator has to opt in before any real
broker POST happens. Without the opt-in the runner only reconciles
existing orders recorded in the ledger — making this module safe to
import from the supervisor without accidentally sending anything.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from eta_engine.bots.base_bot import Fill
from eta_engine.venues.base import (
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
)
from eta_engine.venues.ibkr import (
    IbkrClientPortalConfig,
    IbkrClientPortalVenue,
)
from eta_engine.venues.tastytrade import (
    TastytradeConfig,
    TastytradeVenue,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = logging.getLogger(__name__)

# Env var that enables auto-submission of probe orders. Anything truthy
# ("1", "true", "yes") arms the worker; missing / "0" keeps it in
# reconcile-only mode.
_AUTO_SUBMIT_ENV = "BTC_PAPER_LANE_AUTO_SUBMIT"

# Probe price as a fraction of a synthetic anchor. A 0.7 means the
# BUY sits 30% below the anchor — deep enough that it never fills on
# broker-paper, close enough that the broker accepts it as a valid
# price. The anchor defaults to $90,000 for BTCUSD; override with
# ``BTC_PAPER_LANE_ANCHOR_PRICE``.
_DEFAULT_ANCHOR = 90_000.0
_PROBE_PRICE_FRAC_BY_LANE: dict[str, float] = {
    "grid": 0.70,  # grid lane: far BUY, rests as passive liquidity
    "directional": 0.90,  # directional lane: closer BUY, still won't fill
}

_DEFAULT_PROBE_QTY = 1  # 1 unit of the native symbol (1 BTC on Paxos)


class _BrokerAdapter(Protocol):
    """Minimum surface every BTC lane adapter needs."""

    async def place_order(self, request: OrderRequest) -> OrderResult: ...
    async def get_order_status(
        self,
        symbol: str,
        order_id: str,
    ) -> OrderResult | None: ...
    async def cancel_order(self, symbol: str, order_id: str) -> bool: ...


@dataclass
class LaneState:
    """Persisted lane state. One file per worker."""

    worker_id: str
    broker: str
    lane: str
    symbol: str = "BTCUSD"
    anchor_price: float = _DEFAULT_ANCHOR
    probe_qty: int = _DEFAULT_PROBE_QTY
    active_order_id: str | None = None
    active_order_status: str = "NONE"
    active_order_filled_qty: float = 0.0
    active_order_avg_price: float = 0.0
    last_reconcile_utc: str = ""
    last_event: str = ""
    last_event_utc: str = ""
    submitted_orders: int = 0
    reconciled_orders: int = 0
    terminal_orders: int = 0
    notes: list[str] = field(default_factory=list)


class PaperLaneRunner:
    """Drive one lane's paper-order lifecycle against a broker adapter.

    Injecting the adapter (instead of having the runner build one) keeps
    this class trivially testable.
    """

    def __init__(
        self,
        *,
        worker_id: str,
        broker: str,
        lane: str,
        symbol: str = "BTCUSD",
        adapter: _BrokerAdapter | None = None,
        state_dir: Path,
        ledger_path: Path,
        fills_ledger_path: Path | None = None,
        on_terminal_fill: Callable[[Fill], None] | None = None,
        anchor_price: float | None = None,
        probe_qty: int | None = None,
        auto_submit: bool | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        env_map = dict(env if env is not None else os.environ)
        self.worker_id = worker_id
        self.broker = broker.lower().strip()
        self.lane = lane.lower().strip()
        if self.lane not in _PROBE_PRICE_FRAC_BY_LANE:
            msg = f"unsupported lane: {lane}"
            raise ValueError(msg)
        self.symbol = symbol.upper().strip() or "BTCUSD"
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_file = self.state_dir / f"{worker_id}.lane.json"
        anchor = (
            anchor_price
            if anchor_price is not None
            else _float_env(
                env_map,
                "BTC_PAPER_LANE_ANCHOR_PRICE",
                _DEFAULT_ANCHOR,
            )
        )
        qty = (
            probe_qty
            if probe_qty is not None
            else _int_env(
                env_map,
                "BTC_PAPER_LANE_PROBE_QTY",
                _DEFAULT_PROBE_QTY,
            )
        )
        submit = (
            auto_submit
            if auto_submit is not None
            else _bool_env(
                env_map,
                _AUTO_SUBMIT_ENV,
                default=False,
            )
        )
        self._auto_submit = submit
        self.state = self._load_state(worker_id, broker, self.lane, self.symbol, anchor, qty)
        self.adapter: _BrokerAdapter = adapter if adapter is not None else self._build_adapter()
        # v0.1.59: fill-observation hooks. The lane always writes a
        # line to ``fills_ledger_path`` when an order goes terminal
        # (FILLED / PARTIAL). If the caller also passes
        # ``on_terminal_fill``, that callable is invoked with the
        # resolved :class:`Fill` so a bot can call ``record_fill``
        # without polling the ledger itself.
        self._fills_ledger_path = (
            Path(fills_ledger_path)
            if fills_ledger_path is not None
            else self.ledger_path.parent / "btc_paper_fills.jsonl"
        )
        self._fills_ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._on_terminal_fill = on_terminal_fill

    # ------------------------------------------------------------------
    # Adapter construction (per broker)
    # ------------------------------------------------------------------

    def _build_adapter(self) -> _BrokerAdapter:
        if self.broker == "tastytrade":
            return TastytradeVenue(TastytradeConfig.from_env())
        if self.broker == "ibkr":
            return IbkrClientPortalVenue(IbkrClientPortalConfig.from_env())
        msg = f"unsupported broker: {self.broker}"
        raise ValueError(msg)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_state(
        self,
        worker_id: str,
        broker: str,
        lane: str,
        symbol: str,
        anchor_price: float,
        probe_qty: int,
    ) -> LaneState:
        if not self._state_file.exists():
            return LaneState(
                worker_id=worker_id,
                broker=broker,
                lane=lane,
                symbol=symbol,
                anchor_price=anchor_price,
                probe_qty=probe_qty,
            )
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return LaneState(
                worker_id=worker_id,
                broker=broker,
                lane=lane,
                symbol=symbol,
                anchor_price=anchor_price,
                probe_qty=probe_qty,
            )
        return LaneState(
            worker_id=raw.get("worker_id", worker_id),
            broker=raw.get("broker", broker),
            lane=raw.get("lane", lane),
            symbol=raw.get("symbol", symbol),
            anchor_price=float(raw.get("anchor_price") or anchor_price),
            probe_qty=int(raw.get("probe_qty") or probe_qty),
            active_order_id=raw.get("active_order_id"),
            active_order_status=str(raw.get("active_order_status") or "NONE"),
            active_order_filled_qty=float(raw.get("active_order_filled_qty") or 0.0),
            active_order_avg_price=float(raw.get("active_order_avg_price") or 0.0),
            last_reconcile_utc=str(raw.get("last_reconcile_utc") or ""),
            last_event=str(raw.get("last_event") or ""),
            last_event_utc=str(raw.get("last_event_utc") or ""),
            submitted_orders=int(raw.get("submitted_orders") or 0),
            reconciled_orders=int(raw.get("reconciled_orders") or 0),
            terminal_orders=int(raw.get("terminal_orders") or 0),
            notes=list(raw.get("notes") or []),
        )

    def _persist(self) -> None:
        payload = asdict(self.state)
        tmp = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._state_file)

    def _append_ledger(self, row: dict[str, Any]) -> None:
        try:
            with self.ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        except OSError as exc:
            logger.warning("lane ledger write failed %s: %s", self.worker_id, exc)

    # ------------------------------------------------------------------
    # Core tick
    # ------------------------------------------------------------------

    async def tick(self) -> dict[str, Any]:
        """Run one lane iteration. Returns a state snapshot for the heartbeat.

        Behavior:
          * If no active order AND auto-submit is on, submit a probe.
          * If there is an active order, poll and record transitions.
          * Otherwise no-op and just refresh the reconcile timestamp.
        """
        self.state.last_reconcile_utc = _utc_now()
        if self.state.active_order_id is None:
            if self._auto_submit:
                await self._submit_probe()
        else:
            await self._reconcile_active()
        self._persist()
        return self.snapshot()

    async def _submit_probe(self) -> None:
        frac = _PROBE_PRICE_FRAC_BY_LANE[self.lane]
        price = round(self.state.anchor_price * frac, 2)
        client_order_id = f"{self.worker_id}-{uuid.uuid4().hex[:8]}"
        req = OrderRequest(
            symbol=self.symbol,
            side=Side.BUY,
            qty=float(self.state.probe_qty),
            order_type=OrderType.LIMIT,
            price=price,
            client_order_id=client_order_id,
        )
        try:
            result = await self.adapter.place_order(req)
        except Exception as exc:  # noqa: BLE001 -- transport errors must not crash the worker
            self.state.last_event = f"submit_error:{type(exc).__name__}"
            self.state.last_event_utc = _utc_now()
            logger.warning("lane submit failed %s: %s", self.worker_id, exc)
            return
        self.state.active_order_id = result.order_id
        self.state.active_order_status = result.status.value
        self.state.active_order_filled_qty = result.filled_qty
        self.state.active_order_avg_price = result.avg_price
        self.state.submitted_orders += 1
        self.state.last_event = f"submitted:{result.status.value}"
        self.state.last_event_utc = _utc_now()
        self._append_ledger(
            {
                "ts_utc": self.state.last_event_utc,
                "worker_id": self.worker_id,
                "broker": self.broker,
                "lane": self.lane,
                "symbol": self.symbol,
                "order_id": result.order_id,
                "order_status": result.status.value,
                "side": "BUY",
                "qty": float(self.state.probe_qty),
                "entry_price": price,
                "status": "OPEN" if result.status is OrderStatus.OPEN else result.status.value,
                "updated_at_utc": self.state.last_event_utc,
                "event": "submit",
                "note": "paper-lane probe submitted",
            }
        )

    async def _reconcile_active(self) -> None:
        oid = self.state.active_order_id
        if oid is None:
            return
        try:
            result = await self.adapter.get_order_status(self.symbol, oid)
        except Exception as exc:  # noqa: BLE001
            self.state.last_event = f"reconcile_error:{type(exc).__name__}"
            self.state.last_event_utc = _utc_now()
            logger.debug("lane reconcile failed %s: %s", self.worker_id, exc)
            return
        self.state.reconciled_orders += 1
        if result is None:
            self.state.last_event = "reconcile_missing"
            self.state.last_event_utc = _utc_now()
            return
        old_status = self.state.active_order_status
        new_status = result.status.value
        self.state.active_order_status = new_status
        self.state.active_order_filled_qty = result.filled_qty
        self.state.active_order_avg_price = result.avg_price
        if new_status != old_status:
            self.state.last_event = f"transition:{old_status}->{new_status}"
            self.state.last_event_utc = _utc_now()
            self._append_ledger(
                {
                    "ts_utc": self.state.last_event_utc,
                    "worker_id": self.worker_id,
                    "broker": self.broker,
                    "lane": self.lane,
                    "symbol": self.symbol,
                    "order_id": oid,
                    "order_status": new_status,
                    "filled_qty": result.filled_qty,
                    "avg_price": result.avg_price,
                    "status": new_status,
                    "updated_at_utc": self.state.last_event_utc,
                    "event": "transition",
                    "prior_status": old_status,
                }
            )
        # Terminal state -> clear so the next tick can submit a new probe
        # (subject to auto_submit gate).
        if result.status in {OrderStatus.FILLED, OrderStatus.REJECTED}:
            self.state.terminal_orders += 1
            # v0.1.59: fire fill-observation hooks BEFORE we clear the
            # active-order fields so callers still see the qty/price.
            if result.status is OrderStatus.FILLED and result.filled_qty > 0.0:
                fill = Fill(
                    symbol=self.symbol,
                    side="BUY",  # probes are always BUY; bot-driven lanes pass their own side
                    price=result.avg_price or 0.0,
                    size=result.filled_qty,
                    fee=getattr(result, "fees", 0.0) or 0.0,
                    realized_pnl=0.0,
                )
                self._emit_fill(fill, order_id=result.order_id)
            self.state.active_order_id = None
            self.state.active_order_status = "NONE"
            self.state.active_order_filled_qty = 0.0
            self.state.active_order_avg_price = 0.0

    def _emit_fill(self, fill: Fill, *, order_id: str) -> None:
        """Persist a Fill to the fills ledger + invoke on_terminal_fill.

        Both steps are best-effort: a broken ledger disk or a raising
        bot callback must never crash the lane tick. The fills ledger
        is distinct from the status-transition ledger so bots can
        consume fill events without having to filter status noise.
        """
        record = {
            "ts_utc": _utc_now(),
            "worker_id": self.worker_id,
            "broker": self.broker,
            "lane": self.lane,
            "symbol": self.symbol,
            "order_id": order_id,
            "side": fill.side,
            "size": fill.size,
            "price": fill.price,
            "fee": fill.fee,
        }
        try:
            with self._fills_ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as exc:
            logger.warning("fills ledger write failed %s: %s", self.worker_id, exc)
        if self._on_terminal_fill is not None:
            try:
                self._on_terminal_fill(fill)
            except Exception as exc:  # noqa: BLE001 - callback must never kill the lane
                logger.warning(
                    "on_terminal_fill raised %s for %s: %s",
                    type(exc).__name__,
                    self.worker_id,
                    exc,
                )

    async def cancel_active(self) -> bool:
        """Cancel the current probe order. Used on clean shutdown."""
        oid = self.state.active_order_id
        if oid is None:
            return True
        try:
            ok = await self.adapter.cancel_order(self.symbol, oid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("lane cancel failed %s: %s", self.worker_id, exc)
            return False
        if ok:
            self.state.last_event = "cancelled"
            self.state.last_event_utc = _utc_now()
            self._append_ledger(
                {
                    "ts_utc": self.state.last_event_utc,
                    "worker_id": self.worker_id,
                    "broker": self.broker,
                    "lane": self.lane,
                    "symbol": self.symbol,
                    "order_id": oid,
                    "order_status": "CANCELLED",
                    "status": "CANCELLED",
                    "updated_at_utc": self.state.last_event_utc,
                    "event": "cancel",
                }
            )
            self.state.active_order_id = None
            self.state.active_order_status = "NONE"
            self._persist()
        return ok

    # ------------------------------------------------------------------
    # Heartbeat surface
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return a heartbeat-friendly snapshot of the lane state."""
        return {
            "worker_id": self.state.worker_id,
            "broker": self.state.broker,
            "lane": self.state.lane,
            "symbol": self.state.symbol,
            "auto_submit_armed": self._auto_submit,
            "active_order_id": self.state.active_order_id,
            "active_order_status": self.state.active_order_status,
            "active_order_filled_qty": self.state.active_order_filled_qty,
            "active_order_avg_price": self.state.active_order_avg_price,
            "anchor_price": self.state.anchor_price,
            "probe_qty": self.state.probe_qty,
            "last_reconcile_utc": self.state.last_reconcile_utc,
            "last_event": self.state.last_event,
            "last_event_utc": self.state.last_event_utc,
            "submitted_orders": self.state.submitted_orders,
            "reconciled_orders": self.state.reconciled_orders,
            "terminal_orders": self.state.terminal_orders,
            "execution_state": (
                "ACTIVE"
                if self.state.active_order_id is not None
                else ("ARMED" if self._auto_submit else "RECONCILE_ONLY")
            ),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _bool_env(env: Mapping[str, str], key: str, *, default: bool) -> bool:
    raw = str(env.get(key, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def _float_env(env: Mapping[str, str], key: str, default: float) -> float:
    raw = str(env.get(key, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(env: Mapping[str, str], key: str, default: int) -> int:
    raw = str(env.get(key, "") or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


async def run_one_tick(runner: PaperLaneRunner) -> dict[str, Any]:
    """Convenience wrapper used by the worker loop."""
    return await runner.tick()


async def shutdown(runner: PaperLaneRunner) -> None:
    """Cancel any active probe. Used by the fleet's ``--stop``."""
    with contextlib.suppress(Exception):
        await runner.cancel_active()


__all__ = [
    "LaneState",
    "PaperLaneRunner",
    "run_one_tick",
    "shutdown",
]
