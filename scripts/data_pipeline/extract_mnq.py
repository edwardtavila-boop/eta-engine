"""Extract MNQ bars from a running TradingView Desktop instance via CDP.

Uses raw Chrome DevTools Protocol over websockets - bypasses MCP token limits.
"""

from __future__ import annotations

import asyncio
import csv
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import websockets

CDP_URL = "http://localhost:9222"
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = WORKSPACE_ROOT / "mnq_data"


# --- RTH/ETH session classification for CME MNQ ---
# RTH (equity hours) = 09:30-16:00 America/New_York, Mon-Fri
# ETH = Globex electronic trading outside RTH, Sun 17:00 CT through Fri 16:00 CT
# Weekly halt = Fri 16:00 CT - Sun 17:00 CT (roughly 46 hours)
# Daily maintenance halt for NQ futures = 16:00-17:00 CT Mon-Thu (1 hour)
# For simplicity and robustness, we compute from UTC using fixed DST-aware rules.


def _is_us_dst(dt_utc: datetime) -> bool:
    """US DST: 2nd Sun Mar 07:00 UTC -> 1st Sun Nov 06:00 UTC (approx)."""
    y = dt_utc.year
    # second Sunday of March
    d = datetime(y, 3, 8, 7, 0, tzinfo=UTC)
    while d.weekday() != 6:
        d = d.replace(day=d.day + 1)
    dst_start = d
    # first Sunday of November
    d = datetime(y, 11, 1, 6, 0, tzinfo=UTC)
    while d.weekday() != 6:
        d = d.replace(day=d.day + 1)
    dst_end = d
    return dst_start <= dt_utc < dst_end


def session_flag(epoch_s: int) -> str:
    """Return 'RTH' / 'ETH' / 'CLOSED'."""
    dt = datetime.fromtimestamp(epoch_s, tz=UTC)
    dst = _is_us_dst(dt)
    # ET offset: -4 in DST, -5 otherwise
    et_offset = -4 if dst else -5
    ct_offset = -5 if dst else -6

    # minutes since midnight UTC
    mins_utc = dt.hour * 60 + dt.minute
    wday_utc = dt.weekday()  # Mon=0..Sun=6

    # Convert to ET wallclock
    et_mins = mins_utc + et_offset * 60
    et_wday = wday_utc
    if et_mins < 0:
        et_mins += 1440
        et_wday = (et_wday - 1) % 7
    elif et_mins >= 1440:
        et_mins -= 1440
        et_wday = (et_wday + 1) % 7

    # CT wallclock
    ct_mins = mins_utc + ct_offset * 60
    ct_wday = wday_utc
    if ct_mins < 0:
        ct_mins += 1440
        ct_wday = (ct_wday - 1) % 7
    elif ct_mins >= 1440:
        ct_mins -= 1440
        ct_wday = (ct_wday + 1) % 7

    # Weekly halt: Fri 16:00 CT -> Sun 17:00 CT
    if ct_wday == 4 and ct_mins >= 16 * 60:  # Fri after 16:00 CT
        return "CLOSED"
    if ct_wday == 5:  # Saturday
        return "CLOSED"
    if ct_wday == 6 and ct_mins < 17 * 60:  # Sun before 17:00 CT
        return "CLOSED"

    # Daily maintenance halt: 16:00-17:00 CT Mon-Thu
    if ct_wday in (0, 1, 2, 3) and 16 * 60 <= ct_mins < 17 * 60:
        return "CLOSED"

    # RTH: 09:30-16:00 ET Mon-Fri
    if et_wday in (0, 1, 2, 3, 4) and 9 * 60 + 30 <= et_mins < 16 * 60:
        return "RTH"

    return "ETH"


# --- CDP client ---


def get_chart_target_ws() -> str:
    with urllib.request.urlopen(f"{CDP_URL}/json") as r:
        targets = json.loads(r.read())
    for t in targets:
        url = t.get("url", "")
        if "tradingview.com/chart" in url and t.get("type") == "page":
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("Chart tab not found")


EXTRACT_JS = r"""
(() => {
  const ms = window._exposed_chartWidgetCollection.activeChartWidget.value().model().mainSeries();
  const b = ms.bars();
  const first = b.firstIndex();
  const last = b.lastIndex();
  const rows = new Array(last - first + 1);
  let n = 0;
  for (let i = first; i <= last; i++) {
    const v = b.valueAt(i);
    if (v) {
      rows[n++] = v[0] + ',' + v[1] + ',' + v[2] + ',' + v[3] + ',' + v[4] + ',' + v[5];
    }
  }
  rows.length = n;
  return {
    count: n,
    resolution: ms.interval(),
    symbol: ms.symbol(),
    csv: rows.join('\n')
  };
})()
"""


async def cdp_eval(ws_url: str, expression: str) -> dict:
    # Bump websocket max message to 128 MB for large CSV payloads
    async with websockets.connect(ws_url, max_size=128 * 1024 * 1024) as ws:
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                }
            )
        )
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(f"CDP error: {msg['error']}")
                result = msg["result"]["result"]
                if result.get("subtype") == "error":
                    raise RuntimeError(f"JS error: {result.get('description')}")
                return result.get("value")


def write_csv(data: dict, timeframe_label: str) -> Path:
    csv_lines = data["csv"].split("\n") if data["csv"] else []
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"mnq_{timeframe_label}.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_utc", "epoch_s", "open", "high", "low", "close", "volume", "session"])
        for line in csv_lines:
            parts = line.split(",")
            if len(parts) != 6:
                continue
            t = int(parts[0])
            iso = datetime.fromtimestamp(t, tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            sess = session_flag(t)
            w.writerow([iso, t, parts[1], parts[2], parts[3], parts[4], parts[5], sess])
    return out


async def main() -> None:
    timeframe = sys.argv[1] if len(sys.argv) > 1 else "5m"
    label = timeframe
    ws_url = get_chart_target_ws()
    print(f"Connecting to: {ws_url}")
    data = await cdp_eval(ws_url, EXTRACT_JS)
    print(f"Extracted {data['count']} bars | resolution={data['resolution']} | symbol={data['symbol']}")
    out = write_csv(data, label)
    print(f"Wrote: {out}  ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
