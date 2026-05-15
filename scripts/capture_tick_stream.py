"""
EVOLUTIONARY TRADING ALGO  //  scripts.capture_tick_stream
==========================================================
Phase-1 capture: tick-by-tick trade stream from IBKR Pro to disk.

Why this exists
---------------
Per docs/IBKR_PRO_DATA_INVENTORY.md, ETA currently consumes OHLCV
bars only. The operator unlocked Level 2 + CME real-time on
2026-05-08. This script is Phase 1 of the L2 upgrade path: capture
ticks NOW so we have an irreplaceable trade-by-trade history when
the buy/sell-split bar builder lands in Phase 2 and the footprint
strategies land in Phase 3.

What it captures
----------------
For each subscribed symbol, opens a ``reqTickByTickData`` stream
with ``tickType='Last'`` and appends every trade tick (price, size,
exchange, conditions) to a per-symbol/per-day JSONL file.

Storage
-------
  C:\\EvolutionaryTradingAlgo\\mnq_data\\ticks\\<SYMBOL>_<YYYYMMDD>.jsonl

One JSON object per line, schema:
  {
    "ts": "2026-05-08T14:32:11.123456+00:00",
    "epoch_s": 1746719531.123456,
    "symbol": "MNQ1",
    "price": 29014.75,
    "size": 2,
    "exchange": "CME",
    "conditions": [4, 12],         # IBKR tick attribute flags
    "past_limit": false,
    "unreported": false
  }

Subscription verification
-------------------------
Before subscribing, calls ``ib.reqMarketDataType(1)`` (1 = real-time).
If the IBKR account lacks the relevant exchange subscription, IBKR
silently downgrades to delayed (type 3) and the tick callbacks
arrive 15 minutes late. We probe this by checking the first tick's
timestamp against now(); anything > 60 seconds stale gets logged
as CRITICAL and the symbol gets dropped from the capture set (no
silent delayed data).

Run
---
::

    # capture the current pinned-bot symbol set
    python -m eta_engine.scripts.capture_tick_stream

    # capture a custom set
    python -m eta_engine.scripts.capture_tick_stream \\
        --symbols MNQ NQ M2K 6E MCL MYM NG MBT

    # capture with explicit ports / client id
    python -m eta_engine.scripts.capture_tick_stream \\
        --port 4002 --client-id 31

Notes
-----
This is a long-running process. Expected to run 24x7 on the VPS via
its own scheduled task (`ETA-CaptureTicks`). Disk writes are
appended; rotation happens at UTC date rollover. Each symbol
buffers in-memory and flushes every 100 ticks or 1 second
(whichever comes first), so kill -9 loses at most ~1 second of
data per symbol.
"""

from __future__ import annotations

# ruff: noqa: ANN401, SIM105, BLE001
# Standalone capture script: ib_insync returns Any everywhere (no
# upstream type stubs), and we deliberately swallow OS errors on
# best-effort file close to avoid masking the real exception in the
# logging path. BLE001 -- broad `except Exception` is the correct
# choice when wrapping every external callback (subscription
# resolve, tick parse) in defensive try/except so one bad symbol
# does not kill the whole capture loop.
import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

log = logging.getLogger("capture_tick_stream")

_DEFAULT_HOST: str = "127.0.0.1"
_DEFAULT_PORT: int = 4002
# 2026-05-13: bumped from 31 to 131. clientId 31 collided with the
# supervisor + its helpers (PIDs 2416/7720/8792 hold connections to
# IBKR gateway), surfacing as "Error 326: client id is already in use"
# and rc=1 on scheduled-task firings. The 130+ range stays clear of
# the supervisor's operating clientIds.
_DEFAULT_CLIENT_ID: int = 131
_CONNECT_TIMEOUT_S: float = 20.0

# Core always-on set. The VPS hit IBKR Error 10190 when the daemon requested
# the larger 8-symbol tick-by-tick set, so the default stays inside the five
# symbols that were confirmed to stream. Wider runs remain available via
# ``--symbols`` after more slots/data are provisioned.
_DEFAULT_SYMBOLS: tuple[str, ...] = (
    "MNQ",
    "NQ",
    "M2K",
    "6E",
    "MCL",
)

# Symbol -> (root, exchange, currency). Mirrors fetch_tws_historical_bars.
_FUTURES_MAP: dict[str, tuple[str, str, str]] = {
    "MNQ": ("MNQ", "CME", "USD"),
    "NQ": ("NQ", "CME", "USD"),
    "ES": ("ES", "CME", "USD"),
    "MES": ("MES", "CME", "USD"),
    "RTY": ("RTY", "CME", "USD"),
    "M2K": ("M2K", "CME", "USD"),
    "MBT": ("MBT", "CME", "USD"),
    "MET": ("MET", "CME", "USD"),
    "NG": ("NG", "NYMEX", "USD"),
    "CL": ("CL", "NYMEX", "USD"),
    "MCL": ("MCL", "NYMEX", "USD"),
    "GC": ("GC", "COMEX", "USD"),
    "MGC": ("MGC", "COMEX", "USD"),
    "ZN": ("ZN", "CBOT", "USD"),
    "6E": ("EUR", "CME", "USD"),
    "YM": ("YM", "CBOT", "USD"),
    "MYM": ("MYM", "CBOT", "USD"),
}

# Where ticks land. The default stays inside the canonical workspace; the env
# override is for isolated tests or explicit operator-run sandboxes only.
TICK_ROOT = Path(os.environ.get("ETA_TICK_ROOT", str(ROOT.parent / "mnq_data" / "ticks")))
STATE_DIR = ROOT.parent / "var" / "eta_engine" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
TICK_STATUS_FILE = STATE_DIR / "capture_tick_status.json"

# Flush buffer params.
_FLUSH_EVERY_TICKS = 100
_FLUSH_EVERY_SECONDS = 1.0
# CRITICAL if first tick arrives more than this seconds stale.
_DELAYED_DATA_THRESHOLD_S = 60.0
_STATUS_WRITE_INTERVAL_S = 5.0


def _tick_ops_blocker(error_code: int, message: str) -> dict[str, Any] | None:
    """Return operator-actionable blocker metadata for known IBKR async errors."""
    lowered = message.lower()
    if error_code == 10189 and "different ip address" in lowered:
        return {
            "code": 10189,
            "slug": "different_ip_trading_session",
            "summary": (
                "Tick-by-tick data blocked because another trading TWS session is connected "
                "from a different IP address."
            ),
            "operator_action": (
                "Close the competing trading TWS/Gateway session or route trading back to the VPS-only session, "
                "then restart ETA-CaptureTicks."
            ),
        }
    return None


def _write_tick_status(payload: dict[str, Any], *, path: Path = TICK_STATUS_FILE) -> None:
    """Best-effort status surface for operator tooling and capture health."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    except OSError:
        log.debug("could not write tick status to %s", path)


class TickWriter:
    """Per-symbol buffered JSONL writer with daily rotation."""

    def __init__(self, symbol: str, root: Path) -> None:
        self.symbol = symbol
        self.root = root
        self._buf: list[str] = []
        self._buf_lock = threading.Lock()
        self._last_flush = time.monotonic()
        self._current_date: str | None = None
        self._fh: Any = None

    def _path_for(self, date_str: str) -> Path:
        return self.root / f"{self.symbol}_{date_str}.jsonl"

    def _ensure_fh(self, ts: datetime) -> None:
        date_str = ts.strftime("%Y%m%d")
        if self._current_date != date_str:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
            self.root.mkdir(parents=True, exist_ok=True)
            self._fh = open(self._path_for(date_str), "a", encoding="utf-8")  # noqa: SIM115 -- long-lived handle
            self._current_date = date_str

    def append(self, record: dict[str, Any]) -> None:
        # wave-25q post-review: read len(self._buf) INSIDE the lock so a
        # concurrent appender can't make the count-trigger stale. Without
        # this, two threads both appending near the threshold could each
        # see len < trigger and never flush until the time gate fires.
        with self._buf_lock:
            self._buf.append(json.dumps(record, separators=(",", ":")))
            buf_len = len(self._buf)
        now = time.monotonic()
        if buf_len >= _FLUSH_EVERY_TICKS or (now - self._last_flush) >= _FLUSH_EVERY_SECONDS:
            self.flush()

    def flush(self) -> None:
        with self._buf_lock:
            if not self._buf:
                self._last_flush = time.monotonic()
                return
            batch = self._buf
            self._buf = []
        # We just synthesize a timestamp from now() if the batch is empty
        # of dt info -- should never happen, but keep the rotation safe.
        ts = datetime.now(tz=UTC)
        self._ensure_fh(ts)
        try:
            self._fh.write("\n".join(batch) + "\n")
            self._fh.flush()
        except OSError as exc:
            log.exception("tick write failed for %s: %s", self.symbol, exc)
        self._last_flush = time.monotonic()

    def close(self) -> None:
        self.flush()
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass


class TickStreamCapture:
    """Manage IBKR tick subscriptions + writers."""

    def __init__(self, *, symbols: list[str], host: str, port: int, client_id: int) -> None:
        self.symbols = symbols
        self.host = host
        self.port = port
        self.client_id = client_id
        self.writers: dict[str, TickWriter] = {s: TickWriter(s, TICK_ROOT) for s in symbols}
        self._stale_flagged: set[str] = set()
        self._counts: dict[str, int] = defaultdict(int)
        self._tick_offsets: dict[str, int] = defaultdict(int)
        self._async_errors: list[dict[str, Any]] = []
        self._blocked_reason: dict[str, Any] | None = None
        self._last_status_write = 0.0
        self._ib: Any = None
        self._stop = threading.Event()

    def connect(self) -> None:
        """Connect with multi-retry clientId-collision handling.

        IBKR Error 326 (clientId in use) is emitted async via the wrapper,
        not raised through ib.connect() — the Python call surfaces it as
        a TimeoutError when the server then closes the connection. So we
        retry up to 3 times with fresh random IDs in 200-999 rather than
        sniffing exception messages.
        """
        import random  # noqa: PLC0415

        from ib_insync import IB  # noqa: PLC0415

        attempts: list[int] = [self.client_id]
        attempts.extend(random.randint(200, 999) for _ in range(3))

        last_exc: Exception | None = None
        for cid in attempts:
            ib = IB()
            try:
                ib.connect(
                    self.host,
                    self.port,
                    clientId=cid,
                    timeout=_CONNECT_TIMEOUT_S,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.warning(
                    "connect attempt clientId=%d failed (%s); trying next id",
                    cid, exc,
                )
                try:
                    ib.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                continue
            self.client_id = cid
            ib.reqMarketDataType(1)
            self._ib = ib
            try:
                ib.errorEvent += self._on_ib_error
            except Exception:
                pass
            log.info(
                "connected ib_insync host=%s port=%s clientId=%s realtime requested",
                self.host,
                self.port,
                self.client_id,
            )
            self._write_status("CONNECTED", force=True)
            return
        raise RuntimeError(
            f"could not connect to IBKR gateway after {len(attempts)} clientId attempts: {last_exc}",
        )

    def _resolve(self, sym: str) -> Any:
        from ib_insync import Future  # noqa: PLC0415

        spec = _FUTURES_MAP.get(sym.upper().strip())
        if spec is None:
            raise ValueError(f"unknown symbol {sym!r}; add to _FUTURES_MAP")
        root, exchange, currency = spec
        contract = Future(symbol=root, exchange=exchange, currency=currency, includeExpired=False)
        qualified = self._ib.qualifyContracts(contract)
        if qualified:
            return qualified[0]
        # Fallback for ambiguous front-month picks.
        details = self._ib.reqContractDetails(contract)
        if not details:
            raise RuntimeError(f"{sym}: no contract details returned by IBKR")
        # Pick the nearest non-expired contract.
        today = datetime.now(tz=UTC).strftime("%Y%m%d")
        candidates = sorted(
            (d.contract for d in details if d.contract.lastTradeDateOrContractMonth >= today),
            key=lambda c: c.lastTradeDateOrContractMonth,
        )
        if not candidates:
            raise RuntimeError(f"{sym}: no non-expired contracts found")
        return candidates[0]

    def subscribe(self) -> None:
        for sym in self.symbols:
            try:
                contract = self._resolve(sym)
            except Exception:
                log.exception("resolve failed for %s; skipping", sym)
                continue
            try:
                ticks = self._ib.reqTickByTickData(contract, "Last", 0, False)
            except Exception:
                log.exception("reqTickByTickData failed for %s; skipping", sym)
                continue
            ticks.updateEvent += lambda t, s=sym: self._on_tick(s, t)
            log.info("subscribed %s -> %s.%s", sym, contract.exchange, contract.localSymbol)
        self._write_status("SUBSCRIBED", force=True)

    def _on_ib_error(self, reqId, errorCode, errorString, contract_arg=None) -> None:  # noqa: ANN001, ARG002, N803
        code = int(errorCode or 0)
        message = str(errorString or "").strip()
        entry = {
            "ts": datetime.now(tz=UTC).isoformat(),
            "code": code,
            "message": message[:240],
            "req_id": int(reqId) if reqId is not None else -1,
        }
        self._async_errors.append(entry)
        blocker = _tick_ops_blocker(code, message)
        if blocker is None or self._blocked_reason is not None:
            return
        self._blocked_reason = blocker | {"ibkr_error": entry}
        log.error("tick capture blocked by IBKR error %s: %s", code, message)
        self._write_status("BLOCKED", note=blocker["summary"], force=True)
        self.stop()

    def _on_tick(self, sym: str, ticker: Any) -> None:
        all_trades = list(getattr(ticker, "tickByTicks", []) or [])
        start = self._tick_offsets.get(sym, 0)
        if start > len(all_trades):
            start = 0
        self._tick_offsets[sym] = len(all_trades)
        for trade in all_trades[start:]:
            ts: datetime = trade.time
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            # Subscription verification: first tick must arrive within
            # _DELAYED_DATA_THRESHOLD_S of "now"; otherwise IBKR has
            # downgraded us to delayed-data silently.
            if sym not in self._stale_flagged:
                age_s = (datetime.now(tz=UTC) - ts).total_seconds()
                if age_s > _DELAYED_DATA_THRESHOLD_S:
                    log.critical(
                        "DELAYED DATA for %s: first tick is %.1fs stale "
                        "-- IBKR subscription for this exchange is NOT "
                        "realtime. Operator action: enable the exchange "
                        "subscription in Account Mgmt; until then ETA "
                        "is making decisions on 15-min stale prices.",
                        sym,
                        age_s,
                    )
                    self._stale_flagged.add(sym)
                else:
                    self._stale_flagged.add(sym)  # mark as verified-fresh
                    log.info("VERIFIED REALTIME %s (first tick %.2fs old)", sym, age_s)
            self._counts[sym] += 1
            record = {
                "ts": ts.isoformat(),
                "epoch_s": ts.timestamp(),
                "symbol": sym,
                "price": float(trade.price),
                "size": int(trade.size),
                "exchange": getattr(trade, "exchange", ""),
                "conditions": list(getattr(trade, "specialConditions", "") or ""),
                "past_limit": bool(getattr(trade, "pastLimit", False)),
                "unreported": bool(getattr(trade, "unreported", False)),
            }
            self.writers[sym].append(record)
        if start < len(all_trades):
            self._write_status("RUNNING")

    def run(self) -> None:
        log.info("entering tick capture loop")
        while not self._stop.is_set():
            if self._ib is None or not self._ib.isConnected():
                if self._blocked_reason is None:
                    log.warning("IBKR tick capture connection dropped; exiting loop")
                    self._write_status("DISCONNECTED", note="IBKR connection dropped", force=True)
                return
            self._write_status("RUNNING")
            self._ib.sleep(1.0)

    def stop(self) -> None:
        self._stop.set()
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()

    def close_writers(self) -> None:
        for w in self.writers.values():
            w.close()

    def stats(self) -> dict[str, int]:
        return dict(self._counts)

    @property
    def blocked_reason(self) -> dict[str, Any] | None:
        return self._blocked_reason

    def _write_status(self, status: str, *, note: str | None = None, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_status_write) < _STATUS_WRITE_INTERVAL_S:
            return
        self._last_status_write = now
        payload = {
            "ts": datetime.now(tz=UTC).isoformat(),
            "status": status,
            "host": self.host,
            "port": self.port,
            "client_id": self.client_id,
            "symbols": self.symbols,
            "counts": self.stats(),
            "blocked_reason": self._blocked_reason,
            "recent_ibkr_errors": self._async_errors[-5:],
            "connected": bool(self._ib is not None and self._ib.isConnected()),
        }
        if note:
            payload["note"] = note
        _write_tick_status(payload)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )


def _run_capture(args: argparse.Namespace) -> int:
    """Drive the capture lifecycle; isolated for test instrumentation.

    2026-05-13: split out of ``main()`` so the graceful-exit branches
    (ConnectionRefusedError + subscription-gap RuntimeError) can be
    exercised by pytest without spawning a subprocess.
    """
    capture = TickStreamCapture(
        symbols=list(args.symbols),
        host=args.host,
        port=args.port,
        client_id=args.client_id,
    )

    def _shutdown(_signum: int, _frame: Any) -> None:
        log.info("shutdown signal received; flushing writers and disconnecting")
        capture.stop()
        capture.close_writers()
        log.info("final counts: %s", capture.stats())

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        _write_tick_status(
            {
                "ts": datetime.now(tz=UTC).isoformat(),
                "status": "STARTING",
                "host": args.host,
                "port": args.port,
                "client_id": args.client_id,
                "symbols": list(args.symbols),
            }
        )
        capture.connect()
        capture.subscribe()
        capture.run()
        if capture.blocked_reason is not None:
            log.warning(
                "capture_tick blocked by operator-session issue: %s",
                capture.blocked_reason.get("summary", "unknown IBKR blocker"),
            )
            return 0
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt")
    except ConnectionRefusedError:
        # IBKR Gateway not reachable (broker hasn't started yet, or is
        # being restarted). Exit 0 so the scheduled-task alarm doesn't
        # fire; the next cycle will reconnect.
        log.warning(
            "IBKR gateway %s:%s refused connection; exiting cleanly",
            args.host, args.port,
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        # 2026-05-13: subscription gaps (CME Top-of-Book / Real-Time
        # ticks) surface as "no contract details" / RuntimeError for
        # specific symbols. Treat as ops backlog (no paid feed), not a
        # crash — return 0 so schtasks shows clean state until the
        # operator adds the subscription.
        msg = str(exc).lower()
        if (
            "market data subscription" in msg
            or "no contract details" in msg
            or "no non-expired" in msg
        ):
            log.warning(
                "capture_tick: market data subscription likely missing (%s). "
                "Ops backlog — not a real failure. Exiting 0.",
                exc,
            )
            return 0
        log.exception("capture loop crashed")
        return 1
    finally:
        capture.close_writers()
        final_status = "BLOCKED" if capture.blocked_reason is not None else ("STOPPED" if capture.stats() else "IDLE")
        final_note = capture.blocked_reason.get("summary") if capture.blocked_reason else None
        capture._write_status(final_status, note=final_note, force=True)
        log.info("final counts: %s", capture.stats())
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint — parses args, configures logging, delegates to
    ``_run_capture`` for the actual capture lifecycle."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=list(_DEFAULT_SYMBOLS),
        help=f"futures roots to subscribe (default: {' '.join(_DEFAULT_SYMBOLS)})",
    )
    parser.add_argument("--host", default=_DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--client-id", type=int, default=_DEFAULT_CLIENT_ID)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    return _run_capture(args)


if __name__ == "__main__":
    sys.exit(main())
