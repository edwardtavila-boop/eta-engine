"""Iteratively scroll TradingView chart back in time to force lazy-load more bars."""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from typing import Protocol

import websockets

CDP_URL = "http://localhost:9222"
MAX_ITERS = int(sys.argv[1]) if len(sys.argv) > 1 else 100
SCROLL_BARS = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
WAIT_MS = int(sys.argv[3]) if len(sys.argv) > 3 else 1500


class _CdpSocket(Protocol):
    async def send(self, message: str) -> object: ...

    async def recv(self) -> str: ...


def get_chart_target_ws() -> str:
    with urllib.request.urlopen(f"{CDP_URL}/json") as r:
        targets = json.loads(r.read())
    for t in targets:
        url = t.get("url", "")
        if "tradingview.com/chart" in url and t.get("type") == "page":
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("Chart tab not found")


async def cdp_eval(ws: _CdpSocket, expr: str, req_id: int) -> dict[str, object]:
    await ws.send(json.dumps({
        "id": req_id,
        "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
    }))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == req_id:
            if "error" in msg:
                raise RuntimeError(f"CDP error: {msg['error']}")
            r = msg["result"]["result"]
            if r.get("subtype") == "error":
                raise RuntimeError(f"JS error: {r.get('description')}")
            return r.get("value")


STATE_JS = """
(() => {
  const ms = window._exposed_chartWidgetCollection.activeChartWidget.value().model().mainSeries();
  const b = ms.bars();
  return {size: b.size(), res: ms.interval(), isLoading: ms.isLoading(),
          first: b.first().value[0], last: b.last().value[0]};
})()
"""

SCROLL_JS_TEMPLATE = """
(() => {
  const inner = window._exposed_chartWidgetCollection.activeChartWidget.value().model()._model();
  inner.timeScale().scrollToBar(%d);
  return 'ok';
})()
"""


async def main() -> None:
    ws_url = get_chart_target_ws()
    async with websockets.connect(ws_url, max_size=128 * 1024 * 1024) as ws:
        state = await cdp_eval(ws, STATE_JS, 1)
        print(f"start: size={state['size']} res={state['res']} first={state['first']} last={state['last']}")
        prev_size = state["size"]
        stuck = 0
        bar_offset = -SCROLL_BARS
        req_id = 2
        for i in range(MAX_ITERS):
            await cdp_eval(ws, SCROLL_JS_TEMPLATE % bar_offset, req_id)
            req_id += 1
            await asyncio.sleep(WAIT_MS / 1000)
            state = await cdp_eval(ws, STATE_JS, req_id)
            req_id += 1
            size = state["size"]
            print(f"iter {i+1}: offset={bar_offset} size={size} first_epoch={state['first']} (+{size - prev_size})")
            if size == prev_size:
                stuck += 1
                if stuck >= 3:
                    print("No more data loading. Stopping.")
                    break
            else:
                stuck = 0
            prev_size = size
            bar_offset -= SCROLL_BARS
        print(f"final: size={state['size']} first_epoch={state['first']}")


if __name__ == "__main__":
    asyncio.run(main())
