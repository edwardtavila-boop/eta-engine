"""
EVOLUTIONARY TRADING ALGO  //  scripts.capture_depth_snapshots
==============================================================
Phase-1 capture: order book depth snapshots from IBKR Pro.

Why this exists
---------------
Per docs/IBKR_PRO_DATA_INVENTORY.md, ETA has ZERO order-book
history because no current code calls ``reqMktDepth``. Every day
uncaptured is irreplaceable book history lost. This script is the
twin of ``capture_tick_stream.py`` -- both are Phase 1 of the L2
upgrade path.

What it captures
----------------
For each subscribed symbol, opens a ``reqMktDepth`` subscription
that streams DOM updates. Once per second the script reads the
current top-N bids/asks from the IBKR-managed local book and
appends a snapshot to a per-symbol/per-day JSONL file.

Storage
-------
  C:\\EvolutionaryTradingAlgo\\mnq_data\\depth\\<SYMBOL>_<YYYYMMDD>.jsonl

One JSON object per line, schema:
  {
    "ts": "2026-05-08T14:32:11.000000+00:00",
    "epoch_s": 1746719531.0,
    "symbol": "MNQ1",
    "bids": [
      {"price": 29014.50, "size": 12, "mm": "CME"},
      {"price": 29014.25, "size": 8,  "mm": "CME"},
      ...
    ],
    "asks": [
      {"price": 29014.75, "size": 5,  "mm": "CME"},
      ...
    ],
    "spread": 0.25,
    "mid": 29014.625
  }

Subscription verification
-------------------------
``reqMktDepth`` requires the **CME Depth of Book** subscription (or
equivalent per exchange). If the IBKR account lacks it, the book
arrives empty / very thin / 15-min stale. The script logs CRITICAL
when first snapshot has fewer than 3 levels per side or when the
book is stuck for >30s without updates.

Run
---
::

    # default: pinned-bot symbol set at top-5 levels, 1s cadence
    python -m eta_engine.scripts.capture_depth_snapshots

    # custom symbols, 10-level book, 500ms cadence
    python -m eta_engine.scripts.capture_depth_snapshots \\
        --symbols MNQ NQ --depth-rows 10 --snapshot-interval-ms 500
"""

from __future__ import annotations

# ruff: noqa: ANN401, SIM105, BLE001
# Standalone capture script: ib_insync returns Any everywhere (no
# upstream type stubs), and we deliberately swallow OS errors on
# best-effort file close. BLE001 -- broad `except Exception` is the
# correct choice when wrapping every external callback so one bad
# symbol does not kill the whole capture loop.
import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

log = logging.getLogger("capture_depth_snapshots")

_DEFAULT_HOST: str = "127.0.0.1"
_DEFAULT_PORT: int = 4002
# 2026-05-13: bumped from 32 to 132. The supervisor + its helpers
# routinely occupy clientIds 30-50 via parallel IB connections; clientId
# 32 collided with PID 8792 (supervisor child) in production, surfacing
# as "Error 326: client id is already in use" and rc=1 on every
# scheduled-task firing. Capture tasks now use the 130+ range which the
# supervisor doesn't touch. _connect retries with a randomized clientId
# in 200-300 if the chosen one is somehow still in use.
_DEFAULT_CLIENT_ID: int = 132
_CONNECT_TIMEOUT_S: float = 20.0

_DEFAULT_SYMBOLS: tuple[str, ...] = (
    "MNQ",
    "NQ",
    "M2K",
    "6E",
    "MCL",
    "MYM",
    "NG",
    "MBT",
)

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

DEPTH_ROOT = Path(os.environ.get("ETA_DEPTH_ROOT", str(ROOT.parent / "mnq_data" / "depth")))

# Snapshot health thresholds.
_MIN_LEVELS_PER_SIDE = 3
_BOOK_STUCK_THRESHOLD_S = 30.0


class DepthWriter:
    """Per-symbol JSONL writer with daily rotation."""

    def __init__(self, symbol: str, root: Path) -> None:
        self.symbol = symbol
        self.root = root
        self._fh: Any = None
        self._current_date: str | None = None
        self._lock = threading.Lock()

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
            self._fh = open(self._path_for(date_str), "a", encoding="utf-8")  # noqa: SIM115
            self._current_date = date_str

    def write(self, record: dict[str, Any]) -> None:
        with self._lock:
            ts = datetime.fromisoformat(record["ts"])
            self._ensure_fh(ts)
            try:
                self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")
                self._fh.flush()
            except OSError as exc:
                log.exception("depth write failed for %s: %s", self.symbol, exc)

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass


class DepthSnapshotCapture:
    """Manage IBKR market-depth subscriptions + periodic snapshots."""

    def __init__(
        self,
        *,
        symbols: list[str],
        host: str,
        port: int,
        client_id: int,
        depth_rows: int,
        snapshot_interval_ms: int,
    ) -> None:
        self.symbols = symbols
        self.host = host
        self.port = port
        self.client_id = client_id
        self.depth_rows = depth_rows
        self.snapshot_interval_s = snapshot_interval_ms / 1000.0
        self.writers: dict[str, DepthWriter] = {s: DepthWriter(s, DEPTH_ROOT) for s in symbols}
        self._tickers: dict[str, Any] = {}
        self._last_update_ts: dict[str, float] = {}
        self._snapshot_count: dict[str, int] = {}
        self._stuck_flagged: set[str] = set()
        self._ib: Any = None
        self._stop = threading.Event()

    def connect(self) -> None:
        """Connect to IBKR gateway with clientId-collision retry.

        2026-05-13: the supervisor + its helpers occupy IB clientIds in
        the 30-50 range AND zombie connections from previous runs can
        keep arbitrary clientIds reserved until TWS times them out
        (typically 5-10 min). The first connect() may surface this as a
        TimeoutError because the IBKR Error 326 is async-emitted via
        the wrapper, NOT raised through the Python connect() call.
        Retry up to 3 times with a fresh random clientId in 200-999
        before giving up — that band is far enough from both the GUI
        reserved range (1-31) and the supervisor's operating range
        (30-50) that a fresh-random collision is extremely unlikely.
        """
        import random  # noqa: PLC0415

        from ib_insync import IB  # noqa: PLC0415

        attempts: list[int] = [self.client_id]
        # 3 fallback random IDs in case the preferred one is busy.
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
                # Best-effort cleanup of the half-open IB instance.
                try:
                    ib.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                continue
            # Success
            self.client_id = cid
            ib.reqMarketDataType(1)  # realtime
            self._ib = ib
            log.info(
                "connected ib_insync host=%s port=%s clientId=%s depth_rows=%d cadence_ms=%d",
                self.host,
                self.port,
                self.client_id,
                self.depth_rows,
                int(self.snapshot_interval_s * 1000),
            )
            return
        # All attempts failed — re-raise the last exception.
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
        details = self._ib.reqContractDetails(contract)
        if not details:
            raise RuntimeError(f"{sym}: no contract details returned by IBKR")
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
                ticker = self._ib.reqMktDepth(
                    contract,
                    numRows=self.depth_rows,
                    isSmartDepth=False,
                )
            except Exception:
                log.exception("reqMktDepth failed for %s; skipping", sym)
                continue
            self._tickers[sym] = ticker
            ticker.updateEvent += lambda t, s=sym: self._on_book_update(s, t)
            log.info("subscribed %s -> %s.%s (depth=%d)", sym, contract.exchange, contract.localSymbol, self.depth_rows)

    def _on_book_update(self, sym: str, _ticker: Any) -> None:
        self._last_update_ts[sym] = time.monotonic()

    def _snapshot(self, sym: str) -> dict[str, Any] | None:
        ticker = self._tickers.get(sym)
        if ticker is None:
            return None
        ts = datetime.now(tz=UTC)
        bids = []
        for lvl in (getattr(ticker, "domBids", None) or [])[: self.depth_rows]:
            bids.append(
                {
                    "price": float(lvl.price),
                    "size": int(lvl.size),
                    "mm": getattr(lvl, "marketMaker", "") or "",
                }
            )
        asks = []
        for lvl in (getattr(ticker, "domAsks", None) or [])[: self.depth_rows]:
            asks.append(
                {
                    "price": float(lvl.price),
                    "size": int(lvl.size),
                    "mm": getattr(lvl, "marketMaker", "") or "",
                }
            )
        spread = None
        mid = None
        if bids and asks:
            spread = asks[0]["price"] - bids[0]["price"]
            mid = (asks[0]["price"] + bids[0]["price"]) / 2.0
        return {
            "ts": ts.isoformat(),
            "epoch_s": ts.timestamp(),
            "symbol": sym,
            "bids": bids,
            "asks": asks,
            "spread": spread,
            "mid": mid,
        }

    def _check_health(self, sym: str, snap: dict[str, Any]) -> None:
        # Subscription / book-quality verification
        if sym in self._stuck_flagged:
            return
        bid_n = len(snap.get("bids") or [])
        ask_n = len(snap.get("asks") or [])
        if bid_n < _MIN_LEVELS_PER_SIDE or ask_n < _MIN_LEVELS_PER_SIDE:
            log.warning(
                "%s book has only %d bids / %d asks at first snapshot -- "
                "possible missing exchange Depth subscription. Check IBKR "
                "Account Mgmt -> Market Data Subscriptions for the relevant "
                "exchange's 'Depth of Book' product.",
                sym,
                bid_n,
                ask_n,
            )
            self._stuck_flagged.add(sym)
        # Book-stuck check: no updates in N seconds
        last = self._last_update_ts.get(sym)
        if last is not None and (time.monotonic() - last) > _BOOK_STUCK_THRESHOLD_S:
            log.warning(
                "%s book has not updated in %.1fs -- feed may be stuck or session closed.",
                sym,
                time.monotonic() - last,
            )
            self._stuck_flagged.add(sym)

    def snapshot_loop(self) -> None:
        log.info("entering snapshot loop at %.2fs cadence", self.snapshot_interval_s)
        while not self._stop.is_set():
            for sym in self.symbols:
                snap = self._snapshot(sym)
                if snap is None:
                    continue
                self._check_health(sym, snap)
                self.writers[sym].write(snap)
                self._snapshot_count[sym] = self._snapshot_count.get(sym, 0) + 1
            if self._ib is not None and self._ib.isConnected():
                self._ib.sleep(self.snapshot_interval_s)
            else:
                self._stop.wait(self.snapshot_interval_s)
        log.info("snapshot loop exited; final counts: %s", self._snapshot_count)

    def stop(self) -> None:
        self._stop.set()
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()

    def close_writers(self) -> None:
        for w in self.writers.values():
            w.close()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )


def _run_capture(args: argparse.Namespace) -> int:
    capture = DepthSnapshotCapture(
        symbols=list(args.symbols),
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        depth_rows=args.depth_rows,
        snapshot_interval_ms=args.snapshot_interval_ms,
    )

    def _shutdown(_signum: int, _frame: Any) -> None:
        log.info("shutdown signal received")
        capture.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        capture.connect()
        capture.subscribe()
        capture.snapshot_loop()
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt")
    except ConnectionRefusedError:
        # IBKR Gateway not reachable. Common during cold-start ordering
        # (broker task hasn't started yet). Exit 0 so the scheduled-task
        # alarm doesn't fire; the next run will pick up the connection.
        log.warning(
            "IBKR gateway %s:%s refused connection; exiting cleanly until gateway is up",
            args.host, args.port,
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        # 2026-05-13: distinguish "subscription missing" (operator hasn't
        # paid for CME Depth of Book) from a real bug. Subscription gaps
        # surface as repeated "no contract details" or RuntimeError for
        # specific symbols — those are an OPS BACKLOG item, not a crash.
        msg = str(exc).lower()
        if (
            "depth subscription" in msg
            or "market data subscription" in msg
            or "no contract details" in msg
            or "no non-expired" in msg
        ):
            log.warning(
                "capture_depth: data subscription likely missing (%s). "
                "This is an ops backlog item — not a real failure. "
                "See IBKR Account Mgmt > Market Data Subscriptions for "
                "CME Depth of Book. Exiting 0.",
                exc,
            )
            return 0
        log.exception("capture loop crashed")
        return 1
    finally:
        capture.close_writers()
    return 0


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument("--depth-rows", type=int, default=5, help="depth-of-book rows per side (default 5)")
    parser.add_argument("--snapshot-interval-ms", type=int, default=1000, help="ms between snapshots (default 1000)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    return _run_capture(args)


if __name__ == "__main__":
    sys.exit(main())
