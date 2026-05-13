"""Live slippage tracker (Tier-1 #6, 2026-04-27).

Captures the diff between EXPECTED fill price (at order-submit time)
and REALIZED fill price (when the venue confirms). Without this, a
strategy's theoretical edge can be silently erased by execution and
nobody notices until the equity curve diverges from the backtest.

Usage at the order-routing layer::

    from eta_engine.obs.slippage_tracker import record_expected, record_realized

    # On submit:
    record_expected(order_id=oid, symbol=sym, side=side,
                    expected_price=expected, ts=time.time())

    # On fill:
    record_realized(order_id=oid, realized_price=fill_price, ts=time.time())
    # ...the tracker writes a slippage event to state/slippage/events.jsonl

Daily roll-up via ``daily_summary()``. Operator inspects + alerts when
realized slippage exceeds backtest assumption.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
EVENTS_PATH = ROOT / "state" / "slippage" / "events.jsonl"
PENDING_PATH = ROOT / "state" / "slippage" / "pending.json"


@dataclass
class SlippageEvent:
    order_id: str
    symbol: str
    side: str
    expected_price: float
    realized_price: float
    slippage_abs: float  # signed: positive = paid more (worse for buyer)
    slippage_bps: float  # in basis points of expected_price
    submit_ts: float
    fill_ts: float
    latency_ms: float


_lock = threading.Lock()


def _load_pending() -> dict[str, dict]:
    if not PENDING_PATH.exists():
        return {}
    try:
        return json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_pending(d: dict) -> None:
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(d), encoding="utf-8")


def record_expected(
    *,
    order_id: str,
    symbol: str,
    side: str,
    expected_price: float,
    ts: float,
) -> None:
    """Stash the expected fill price + submit timestamp. Resolved when
    record_realized() fires for the same order_id."""
    with _lock:
        pending = _load_pending()
        pending[order_id] = {
            "symbol": symbol,
            "side": side,
            "expected_price": float(expected_price),
            "submit_ts": float(ts),
        }
        _save_pending(pending)


def record_realized(
    *,
    order_id: str,
    realized_price: float,
    ts: float,
) -> SlippageEvent | None:
    """Resolve the slippage diff. Returns the event if the matching
    expected was found; None otherwise (out-of-order or unknown order).
    """
    with _lock:
        pending = _load_pending()
        match = pending.pop(order_id, None)
        if match is None:
            logger.debug("no pending expected for order_id=%s", order_id)
            return None

        expected = float(match["expected_price"])
        side = match["side"]
        # Slippage convention: positive = worse for the trader.
        # BUY: realized > expected = bad. SELL: realized < expected = bad.
        slippage_abs = realized_price - expected if side.lower() == "buy" else expected - realized_price
        slippage_bps = (slippage_abs / expected * 10_000) if expected else 0.0
        submit_ts = float(match["submit_ts"])
        latency_ms = max(0.0, (float(ts) - submit_ts) * 1000.0)

        event = SlippageEvent(
            order_id=order_id,
            symbol=match["symbol"],
            side=side,
            expected_price=expected,
            realized_price=float(realized_price),
            slippage_abs=round(slippage_abs, 6),
            slippage_bps=round(slippage_bps, 2),
            submit_ts=submit_ts,
            fill_ts=float(ts),
            latency_ms=round(latency_ms, 1),
        )

        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(event), default=str) + "\n")
        _save_pending(pending)
        return event


def daily_summary(*, since_hours: float = 24.0) -> dict:
    """Aggregate the last N hours of slippage events.

    Useful for the daily kaizen report and the live preflight gate
    (high realized slippage = signal that backtest assumptions need
    revisiting).
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=since_hours)).timestamp()
    events: list[dict] = []
    if EVENTS_PATH.exists():
        try:
            for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("fill_ts", 0) < cutoff:
                    continue
                events.append(rec)
        except OSError:
            pass

    if not events:
        return {
            "n": 0,
            "since_hours": since_hours,
            "mean_slippage_bps": 0.0,
            "p95_slippage_bps": 0.0,
            "mean_latency_ms": 0.0,
            "p95_latency_ms": 0.0,
        }

    bps = [e.get("slippage_bps", 0.0) for e in events]
    latencies = [e.get("latency_ms", 0.0) for e in events]
    bps_sorted = sorted(bps)
    lat_sorted = sorted(latencies)
    p95_idx = int(0.95 * (len(events) - 1))
    return {
        "n": len(events),
        "since_hours": since_hours,
        "mean_slippage_bps": round(sum(bps) / len(bps), 2),
        "p95_slippage_bps": round(bps_sorted[p95_idx], 2),
        "max_slippage_bps": round(max(bps), 2),
        "mean_latency_ms": round(sum(latencies) / len(latencies), 1),
        "p95_latency_ms": round(lat_sorted[p95_idx], 1),
        "max_latency_ms": round(max(latencies), 1),
        "by_symbol": _group_by_symbol(events),
    }


def _group_by_symbol(events: list[dict]) -> dict[str, dict]:
    by_sym: dict[str, list[float]] = {}
    for e in events:
        sym = e.get("symbol", "?")
        by_sym.setdefault(sym, []).append(e.get("slippage_bps", 0.0))
    return {
        sym: {
            "n": len(bps_list),
            "mean_bps": round(sum(bps_list) / len(bps_list), 2),
        }
        for sym, bps_list in by_sym.items()
    }
