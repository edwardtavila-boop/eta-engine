"""
EVOLUTIONARY TRADING ALGO  //  data.tradingview.parsers
=======================================================
Pure parsers for the four TradingView data shapes.

Kept free of any Playwright / network dependency so they can be unit-
tested with literal fixture strings. Each parser is total: it returns
``None`` (or an empty dict) on unknown shapes rather than raising.

Shapes supported
----------------

* :func:`parse_quote_frame` -- a TradingView v3 ``socket.io`` text frame
  carrying a ``qsd`` (quote streaming data) or ``timescale_update``
  payload. Returns one or more bar/tick records.

* :func:`parse_indicator_tooltip` -- the inner-text of a chart legend
  entry, like ``RSI (14, close)  62.43``. Returns
  ``{indicator, params, value}``.

* :func:`parse_watchlist_row` -- the dict shape Playwright emits when
  evaluating ``[...document.querySelectorAll('.tv-screener-table__row')]``.

* :func:`parse_alert_row` -- a row from the ``/alerts/`` page.
"""

from __future__ import annotations

import json
import re
from typing import Any

# ---------------------------------------------------------------------------
# Quote-frame (websocket) parser
# ---------------------------------------------------------------------------

# TradingView socket.io text frames are framed: "~m~<len>~m~<json>".
# A single TCP message can contain multiple frames concatenated.
_FRAME_RE = re.compile(r"~m~(\d+)~m~", re.DOTALL)


def _split_frames(payload: str) -> list[str]:
    """Split a TradingView socket.io text payload into JSON sub-frames."""
    out: list[str] = []
    cursor = 0
    while cursor < len(payload):
        m = _FRAME_RE.match(payload, cursor)
        if not m:
            break
        length = int(m.group(1))
        start = m.end()
        end = start + length
        if end > len(payload):
            break
        out.append(payload[start:end])
        cursor = end
    return out


def parse_quote_frame(payload: str | bytes) -> list[dict[str, Any]]:
    """Extract bar/tick records from a raw TradingView ws frame string.

    Returns a list (possibly empty). Each record has::

        {
          "kind":     "tick" | "bar",
          "symbol":   "<exchange>:<ticker>",
          "ts":       <epoch seconds float>,
          "price":    <float, last>,    # tick only
          "o":        <float>,          # bar only
          "h":        <float>,          # bar only
          "l":        <float>,          # bar only
          "c":        <float>,          # bar only
          "v":        <float>,          # bar only (may be 0)
        }

    Unknown frame shapes yield no records (rather than raising) -- the
    caller is expected to ignore.
    """
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return []

    if not payload or not payload.startswith("~m~"):
        return []

    out: list[dict[str, Any]] = []
    for frame in _split_frames(payload):
        if not frame.startswith("{"):
            # Heartbeats look like ``~h~12`` -- ignore.
            continue
        try:
            obj = json.loads(frame)
        except json.JSONDecodeError:
            continue

        method = obj.get("m")
        params = obj.get("p") or []
        if method == "qsd" and len(params) >= 2:
            # qsd ~ quote streaming data:
            # ``["sessionid", {"n": "BINANCE:BTCUSDT", "s": "ok",
            #                  "v": {"lp": 50000.0, "lp_time": 1714000000}}]``
            quote = params[1] or {}
            v = quote.get("v") or {}
            sym = quote.get("n") or ""
            price = v.get("lp")
            ts = v.get("lp_time") or v.get("update_mode_seconds")
            if sym and price is not None:
                out.append(
                    {
                        "kind": "tick",
                        "symbol": sym,
                        "ts": float(ts) if ts is not None else 0.0,
                        "price": float(price),
                    },
                )
        elif method == "timescale_update" and len(params) >= 2:
            # ``["chart_id", {"sds_1": {"s": [{"i":0,"v":[1714,o,h,l,c,v]}]}}]``
            payload2 = params[1] or {}
            for series in payload2.values():
                if not isinstance(series, dict):
                    continue
                series_name = series.get("ns", {}).get("d", "")
                bars = series.get("s") or []
                for entry in bars:
                    arr = entry.get("v") or []
                    if len(arr) < 6:
                        continue
                    ts, op, hi, lo, cl, vol = arr[:6]
                    out.append(
                        {
                            "kind": "bar",
                            "symbol": series_name or params[0],
                            "ts": float(ts),
                            "o": float(op),
                            "h": float(hi),
                            "l": float(lo),
                            "c": float(cl),
                            "v": float(vol),
                        },
                    )
    return out


# ---------------------------------------------------------------------------
# Indicator legend tooltip
# ---------------------------------------------------------------------------

# Examples:
#   "RSI (14, close)  62.43"
#   "MACD (12, 26, 9)  0.45  0.32  0.13"
#   "Volume MA  120.5K"
_INDICATOR_RE = re.compile(
    r"""
    ^\s*
    (?P<name>[A-Za-z][A-Za-z0-9 _.+-]*?)              # indicator name
    \s*
    (?:\((?P<params>[^)]*)\))?                        # optional (params)
    \s+
    (?P<values>-?\d[\d., \s\-+kKmMbB%]+)              # one or more numbers
    \s*$
    """,
    re.VERBOSE,
)


def _parse_si_number(token: str) -> float | None:
    """Parse a TradingView legend number token (handles K/M/B suffix and %)."""
    t = token.strip().replace(",", "")
    if not t:
        return None
    pct = t.endswith("%")
    if pct:
        t = t[:-1]
    mult = 1.0
    if t and t[-1] in "kKmMbB":
        mult = {"k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "b": 1e9, "B": 1e9}[t[-1]]
        t = t[:-1]
    try:
        v = float(t) * mult
    except ValueError:
        return None
    return v / 100.0 if pct else v


def parse_indicator_tooltip(text: str) -> dict[str, Any] | None:
    """Parse one indicator legend row into ``{indicator, params, value, all}``.

    Returns ``None`` when the input doesn't match the expected shape.
    Multi-output indicators (MACD = signal+macd+hist) put the *primary*
    value in ``value`` and the full sequence in ``all``.
    """
    if not text or not isinstance(text, str):
        return None
    m = _INDICATOR_RE.match(text.strip())
    if not m:
        return None
    name = m.group("name").strip()
    params_str = (m.group("params") or "").strip() or None
    raw = re.split(r"\s+", m.group("values").strip())
    nums: list[float] = []
    for tok in raw:
        v = _parse_si_number(tok)
        if v is not None:
            nums.append(v)
    if not nums:
        return None
    return {
        "indicator": name,
        "params": params_str,
        "value": nums[0],
        "all": nums,
    }


# ---------------------------------------------------------------------------
# Watchlist row
# ---------------------------------------------------------------------------


def parse_watchlist_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a raw scraped watchlist row.

    Input shape (as produced by Playwright querying the DOM)::

        {"symbol": "BINANCE:BTCUSDT", "last": "50,123.4",
         "chg":    "+0.42%",          "vol":  "1.2B"}

    Returns::

        {"symbol": str, "last": float, "chg_pct": float, "vol": float}

    or ``None`` when ``symbol`` is missing.
    """
    if not isinstance(row, dict):
        return None
    sym = (row.get("symbol") or "").strip()
    if not sym:
        return None
    out: dict[str, Any] = {"symbol": sym}
    last = _parse_si_number(str(row.get("last") or ""))
    chg_pct = _parse_si_number(str(row.get("chg") or ""))
    vol = _parse_si_number(str(row.get("vol") or ""))
    out["last"] = last if last is not None else 0.0
    # TradingView "chg" is already a percentage; we store it directly so
    # ``chg_pct`` is the displayed percent (NOT divided by 100 again).
    out["chg_pct"] = (
        (chg_pct * 100.0) if chg_pct is not None and abs(chg_pct) < 1 else (chg_pct if chg_pct is not None else 0.0)
    )
    out["vol"] = vol if vol is not None else 0.0
    return out


# ---------------------------------------------------------------------------
# Alert row
# ---------------------------------------------------------------------------


def parse_alert_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a row from the /alerts/ page.

    Input shape (rough)::

        {"name": "BTCUSDT > 60000", "symbol": "BINANCE:BTCUSDT",
         "condition": ">", "value": "60000", "active": True,
         "fired_at": "2026-04-20T12:00:00Z"  // optional
        }

    Returns ``{symbol, condition, value, active, name, fired_at}`` or
    ``None`` when essential fields are missing.
    """
    if not isinstance(row, dict):
        return None
    sym = (row.get("symbol") or "").strip()
    name = (row.get("name") or "").strip()
    if not sym and not name:
        return None
    cond = (row.get("condition") or "").strip() or None
    raw_val = row.get("value")
    value: float | None = None
    if raw_val is not None:
        value = _parse_si_number(str(raw_val))
    active = row.get("active")
    if not isinstance(active, bool):
        active = True
    fired_at = row.get("fired_at") or None
    return {
        "symbol": sym or None,
        "name": name,
        "condition": cond,
        "value": value,
        "active": bool(active),
        "fired_at": fired_at,
    }
