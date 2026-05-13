"""
pull_tv_bars.py
===============
Multi-symbol / multi-timeframe orchestrator on top of extract_mnq.py.

For each (symbol, timeframe) pair:
    1. swap the running TradingView Desktop chart via CDP,
    2. wait for bars to load,
    3. (optional) scroll back N pages to force lazy-load of more history,
    4. dump OHLCV + session tags to  mnq_{sym}_{tf}.csv.

Requires TradingView Desktop running with --remote-debugging-port=9222
(same contract as extract_mnq.py). Run once per session or on a cron.

Usage:
    python pull_tv_bars.py                        # default matrix
    python pull_tv_bars.py --symbols MNQ1!,NQ1! --timeframes 5,1
    python pull_tv_bars.py --scroll 20            # 20 lazy-load passes

The symbol list is intentionally biased toward day-trading confluence:

    MNQ1!  primary instrument
    NQ1!   big sibling (lead-lag; MNQ mean-reverts to NQ)
    ES1!   S&P cross-index confluence
    RTY1!  small-cap breadth check
    DXY    dollar tailwind / headwind
    TICK   NYSE up/down tick breadth (RTH only)
    VIX    vol regime filter

Missing symbols are skipped with a warning instead of aborting the run.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import websockets
from extract_mnq import EXTRACT_JS, session_flag  # reuse the JS payload


class _WebSocketLike(Protocol):
    async def send(self, message: str) -> object: ...
    async def recv(self) -> str: ...


CDP_URL = "http://localhost:9222"
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = WORKSPACE_ROOT / "mnq_data"

DEFAULT_SYMBOLS = ["MNQ1!", "NQ1!", "ES1!", "RTY1!", "DXY", "TICK", "VIX"]
DEFAULT_TIMEFRAMES = ["5", "1"]  # minutes; "1S" for 1-second
SCROLL_BARS_PER_PASS = 2000
SCROLL_WAIT_MS = 1200


def get_chart_ws() -> str:
    with urllib.request.urlopen(f"{CDP_URL}/json") as r:
        targets = json.loads(r.read())
    for t in targets:
        if t.get("type") == "page" and "tradingview.com/chart" in t.get("url", ""):
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("TradingView chart tab not found on CDP port 9222")


async def _eval(ws: _WebSocketLike, expr: str, rid: int) -> object:
    await ws.send(
        json.dumps(
            {
                "id": rid,
                "method": "Runtime.evaluate",
                "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
            }
        )
    )
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == rid:
            if "error" in msg:
                raise RuntimeError(msg["error"])
            r = msg["result"]["result"]
            if r.get("subtype") == "error":
                raise RuntimeError(r.get("description"))
            return r.get("value")


SET_SYMBOL_JS = """
(() => {
  const cw = window._exposed_chartWidgetCollection.activeChartWidget.value();
  cw.setSymbol(%r);
  return 'ok';
})()
"""

SET_RESOLUTION_JS = """
(() => {
  const cw = window._exposed_chartWidgetCollection.activeChartWidget.value();
  cw.setResolution(%r);
  return 'ok';
})()
"""

STATE_JS = """
(() => {
  const ms = window._exposed_chartWidgetCollection.activeChartWidget.value().model().mainSeries();
  const b = ms.bars();
  return {size: b.size(), res: ms.interval(), loading: ms.isLoading(),
          symbol: ms.symbol(),
          first: b.size() ? b.first().value[0] : null,
          last:  b.size() ? b.last().value[0]  : null};
})()
"""

SCROLL_JS = """
(() => {
  const inner = window._exposed_chartWidgetCollection.activeChartWidget.value().model()._model();
  inner.timeScale().scrollToBar(%d);
  return 'ok';
})()
"""


async def _wait_for_load(
    ws: _WebSocketLike,
    rid: int,
    target_symbol: str,
    timeout_s: float = 20.0,
) -> tuple[int, object]:
    """Poll state until loading=False and symbol matches (TV is async)."""
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = await _eval(ws, STATE_JS, rid)
        rid += 1
        if not st.get("loading") and str(st.get("symbol", "")).upper().endswith(target_symbol.upper().rstrip("!")):
            return rid, st
        await asyncio.sleep(0.3)
    return rid, await _eval(ws, STATE_JS, rid)


async def _scroll_back(ws: _WebSocketLike, rid: int, passes: int) -> int:
    offset = -SCROLL_BARS_PER_PASS
    prev = None
    stuck = 0
    for _i in range(passes):
        await _eval(ws, SCROLL_JS % offset, rid)
        rid += 1
        await asyncio.sleep(SCROLL_WAIT_MS / 1000)
        st = await _eval(ws, STATE_JS, rid)
        rid += 1
        size = st["size"]
        if prev is not None and size == prev:
            stuck += 1
            if stuck >= 3:
                break
        else:
            stuck = 0
        prev = size
        offset -= SCROLL_BARS_PER_PASS
    return rid


def _write_csv(data: dict, symbol: str, tf_label: str) -> Path:
    safe_sym = symbol.replace("!", "").replace("/", "_").lower()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"mnq_{safe_sym}_{tf_label}.csv"
    lines = (data.get("csv") or "").split("\n")
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_utc", "epoch_s", "open", "high", "low", "close", "volume", "session"])
        for line in lines:
            parts = line.split(",")
            if len(parts) != 6:
                continue
            t = int(parts[0])
            iso = datetime.fromtimestamp(t, tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            w.writerow([iso, t, parts[1], parts[2], parts[3], parts[4], parts[5], session_flag(t)])
    return out


async def _pull_one(ws: _WebSocketLike, rid: int, symbol: str, tf: str, scroll: int) -> int:
    try:
        await _eval(ws, SET_SYMBOL_JS % symbol, rid)
        rid += 1
        rid, _ = await _wait_for_load(ws, rid, symbol)
        await _eval(ws, SET_RESOLUTION_JS % tf, rid)
        rid += 1
        await asyncio.sleep(1.0)  # let resolution swap settle
        if scroll > 0:
            rid = await _scroll_back(ws, rid, scroll)
        data = await _eval(ws, EXTRACT_JS, rid)
        rid += 1
        if not data or not data.get("count"):
            print(f"  [skip] {symbol} @ {tf}: no bars returned")
            return rid
        out = _write_csv(data, symbol, tf)
        print(f"  [ok]   {symbol} @ {tf}: {data['count']} bars -> {out.name} ({out.stat().st_size:,} bytes)")
    except Exception as e:
        print(f"  [fail] {symbol} @ {tf}: {e}")
    return rid


async def main_async(symbols: list[str], timeframes: list[str], scroll: int) -> None:
    ws_url = get_chart_ws()
    print(f"Connecting to: {ws_url}")
    async with websockets.connect(ws_url, max_size=128 * 1024 * 1024) as ws:
        rid = 1
        for sym in symbols:
            for tf in timeframes:
                print(f"\n--> {sym} @ {tf} (scroll={scroll})")
                rid = await _pull_one(ws, rid, sym, tf, scroll)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--symbols", default=",".join(DEFAULT_SYMBOLS), help=f"comma-separated list; default {DEFAULT_SYMBOLS}"
    )
    ap.add_argument(
        "--timeframes", default=",".join(DEFAULT_TIMEFRAMES), help="comma-separated TV resolutions (e.g. 1,5,1S)"
    )
    ap.add_argument("--scroll", type=int, default=0, help="scroll-back passes per (sym,tf); 0=disabled")
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    asyncio.run(main_async(symbols, timeframes, args.scroll))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
