"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_ibkr_crypto_bars
==============================================================
IBKR-native crypto bar fetcher. Sibling of ``fetch_btc_bars.py``.

Why this exists
---------------
Per the standing operator data-source policy
(``memory/eta_data_source_policy.md``), every crypto strategy
promoted via the Coinbase-spot baseline must be re-fetched from
IBKR-native CME-aligned bars and drift-checked before real-money
activation. This is the fetcher the policy points at.

Outputs are written to the workspace ``data/crypto/ibkr/history`` root,
deliberately a sibling of the Coinbase root, NOT the same root.
Both feeds stay side-by-side so:

  1. The data library can lookup whichever the audit asks for.
  2. ``obs.drift_monitor.assess_drift`` can pair the two
     baselines and quantify the IS/OOS-style drift between
     research data (Coinbase) and live data (IBKR/CME-linked).
  3. We never silently swap roots mid-research — promotion is
     a deliberate gate, not a default.

Pre-flight requirements
-----------------------
1. **IBKR Client Portal Gateway running** at the configured
   ``IBKR_CP_BASE_URL`` (default ``https://127.0.0.1:5000/v1/api``).
   Start it with ``./bin/run.sh root/conf.yaml`` from the
   clientportal.gw download.
2. **Authenticated session** — visit ``https://127.0.0.1:5000``
   in a browser, log in with your IBKR credentials. The session
   is then available to this script via the localhost gateway.
3. **CME Crypto market-data subscription** active on the
   account (~$10/mo). Without it, the historical endpoint
   returns empty payloads with no error code.

Usage::

    # Fetch 1h BTC bars covering the last 12 months
    python -m eta_engine.scripts.fetch_ibkr_crypto_bars \\
        --symbol BTC --timeframe 1h --months 12

    # Daily ETH bars for an explicit window
    python -m eta_engine.scripts.fetch_ibkr_crypto_bars \\
        --symbol ETH --timeframe 1d \\
        --start 2025-01-01 --end 2026-04-27

The Client Portal historical endpoint caps each request at 1000
bars; the script chunks transparently.

After fetching, run the drift comparison::

    python -m eta_engine.scripts.compare_coinbase_vs_ibkr \\
        --symbol BTC --timeframe 1h

(comparison script lives separately so this fetcher stays focused
on its single responsibility — pulling bars).
"""

from __future__ import annotations

import argparse
import csv
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import CRYPTO_IBKR_HISTORY_ROOT as IBKR_HISTORY_ROOT  # noqa: E402

# Default Client Portal Gateway URL. Override via --base-url or the
# IBKR_CP_BASE_URL env var. The gateway serves a self-signed cert on
# localhost so we wrap requests in an SSL context that doesn't verify.
_DEFAULT_BASE_URL = "https://127.0.0.1:5000/v1/api"

# Conids — these mirror ``venues.ibkr._DEFAULT_CONIDS`` so the fetcher
# and live trading routing agree about which contract IBKR exposes for
# each symbol. PAXOS spot at IBKR is the closest analogue to CME-linked
# crypto exposure for retail accounts; CME-listed BTC futures use a
# different conid that requires the futures market-data bundle.
_SYMBOL_TO_CONID: dict[str, int] = {
    "BTC": 764777976,   # PAXOS BTCUSD spot
    "ETH": 764777977,   # PAXOS ETHUSD spot
}

# IBKR Client Portal supports these bar sizes for /iserver/marketdata/history.
# Map our unified timeframe vocabulary to IBKR's. ``D`` -> ``1d`` per the
# Client Portal docs.
_TF_TO_IBKR_BAR: dict[str, str] = {
    "1m":  "1min",
    "5m":  "5min",
    "15m": "15min",
    "30m": "30min",
    "1h":  "1h",
    "4h":  "4h",
    "1d":  "1d",
    "D":   "1d",
}

# Approximate bar count per chunk that the Client Portal returns. Empirical
# limit is ~1000 bars per call; we ask for a slightly conservative window
# size so chunks always come back complete.
_CHUNK_BAR_LIMIT = 900

# Bar duration in seconds for chunk math. 1d := 86400s.
_BAR_SECONDS: dict[str, int] = {
    "1m":  60,
    "5m":  300,
    "15m": 900,
    "30m": 1800,
    "1h":  3600,
    "4h":  14400,
    "1d":  86400,
    "D":   86400,
}


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ChunkRequest:
    conid: int
    bar: str
    period: str  # e.g. "30d" — IBKR's notation for "last N units"
    end_ms: int  # epoch ms; IBKR returns bars STRICTLY before this


def _make_ctx() -> ssl.SSLContext:
    """SSL context that accepts the Client Portal Gateway's self-signed cert.

    The gateway runs on localhost; cert verification adds nothing
    security-wise here and just blocks the request.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch_chunk(
    base_url: str, req: _ChunkRequest, *, timeout: float = 15.0,
) -> list[dict]:
    """Hit ``/iserver/marketdata/history`` once. Returns the raw bar list.

    Returns ``[]`` on any error. Distinguishing "no auth" from "no
    market-data sub" from "bad conid" requires reading the gateway
    log; the script just reports zero rows so the operator notices.
    """
    qs = (
        f"?conid={req.conid}"
        f"&period={req.period}"
        f"&bar={req.bar}"
        f"&startTime={req.end_ms}"
    )
    url = f"{base_url}/iserver/marketdata/history{qs}"
    request = urllib.request.Request(  # noqa: S310 -- localhost gateway
        url,
        headers={"User-Agent": "eta-engine/fetch_ibkr_crypto_bars"},
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 -- localhost gateway
            request, timeout=timeout, context=_make_ctx(),
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read()[:200] if hasattr(exc, "read") else b""
        print(f"  HTTPError {exc.code}: {body!r}")
        return []
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"  URLError: {exc}")
        return []
    except json.JSONDecodeError as exc:
        print(f"  JSONDecodeError: {exc}")
        return []
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return data


# ---------------------------------------------------------------------------
# Window math
# ---------------------------------------------------------------------------


def _period_for_chunk(bar: str) -> str:
    """How many units (minutes/hours/days) of history one chunk asks for.

    Client Portal expects period like ``30d`` / ``2h`` / ``1m`` (1 month).
    We size each chunk so the response is well below the ~1000-bar limit.
    """
    if bar.endswith("min"):
        unit = bar[:-3]
        n = int(unit) if unit else 1
        # N-min bar; chunk covers _CHUNK_BAR_LIMIT bars in minutes.
        minutes = _CHUNK_BAR_LIMIT * n
        # IBKR period notation: "Nh" (max 168), "Nd" (max 365), etc.
        if minutes >= 60:
            return f"{minutes // 60}h"
        return f"{minutes}min"
    if bar == "1h":
        return f"{_CHUNK_BAR_LIMIT}h"
    if bar == "4h":
        return f"{(_CHUNK_BAR_LIMIT * 4)}h"
    if bar == "1d":
        return f"{_CHUNK_BAR_LIMIT}d"
    return f"{_CHUNK_BAR_LIMIT}d"


# ---------------------------------------------------------------------------
# Top-level fetch
# ---------------------------------------------------------------------------


def fetch_bars(
    *, symbol: str, timeframe: str, start: datetime, end: datetime,
    base_url: str = _DEFAULT_BASE_URL,
) -> list[dict]:
    """Pull all bars in [start, end). Stitches IBKR's chunked responses.

    Returns a list of bar dicts in the IBKR shape: ``{t, o, h, l, c, v}``
    where ``t`` is epoch ms.
    """
    conid = _SYMBOL_TO_CONID.get(symbol.upper())
    if conid is None:
        raise ValueError(
            f"unknown symbol {symbol!r}; supported: {sorted(_SYMBOL_TO_CONID)}",
        )
    bar = _TF_TO_IBKR_BAR.get(timeframe)
    if bar is None:
        raise ValueError(
            f"unknown timeframe {timeframe!r}; supported: {sorted(_TF_TO_IBKR_BAR)}",
        )

    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)

    period = _period_for_chunk(bar)
    bar_secs = _BAR_SECONDS[timeframe]

    out: list[dict] = []
    cursor = end
    # Walk backwards from `end` -> `start`, since IBKR's `startTime`
    # parameter is actually the END of the requested window (badly
    # named in the API). Each chunk returns the most-recent N bars
    # ending at `startTime`.
    while cursor > start:
        end_ms = int(cursor.timestamp() * 1000)
        req = _ChunkRequest(
            conid=conid, bar=bar, period=period, end_ms=end_ms,
        )
        print(
            f"  fetching {symbol}/{timeframe} <= {cursor.isoformat()} ...",
        )
        rows = _fetch_chunk(base_url, req)
        if not rows:
            # Either we've reached the start of data, or the gateway
            # is unauthenticated / unsubscribed. Either way, stop.
            break
        out.extend(rows)
        # Move cursor to the earliest bar's timestamp - 1 bar so the
        # next chunk fetches strictly older bars without overlap.
        oldest = min(rows, key=lambda r: r.get("t", 0))
        oldest_ms = int(oldest.get("t", 0))
        if oldest_ms == 0:
            break
        prev_cursor = cursor
        cursor = datetime.fromtimestamp(oldest_ms / 1000, tz=UTC) - timedelta(
            seconds=bar_secs,
        )
        if cursor >= prev_cursor:
            # Defensive: gateway returned bars that don't move the cursor
            # backward. Avoid an infinite loop.
            break
        # Polite delay; Client Portal isn't documented to rate-limit
        # historical-data requests but lets be friendly.
        time.sleep(0.2)

    # Sort ascending and dedupe by timestamp.
    out.sort(key=lambda r: r.get("t", 0))
    seen: set[int] = set()
    deduped: list[dict] = []
    for r in out:
        ts = int(r.get("t", 0))
        if ts in seen or ts == 0:
            continue
        # Filter to the requested window — IBKR sometimes returns a
        # bar whose timestamp is just before `start`.
        if ts < int(start.timestamp() * 1000):
            continue
        if ts >= int(end.timestamp() * 1000):
            continue
        seen.add(ts)
        deduped.append(r)
    return deduped


# ---------------------------------------------------------------------------
# CSV write — match the history schema used by data.library
# ---------------------------------------------------------------------------


def _filename(symbol: str, timeframe: str) -> str:
    """Match the existing history-schema filename convention.

    The library's filename parser uses ``D`` / ``W`` for daily/weekly
    (no leading digit), and ``\\d+[smh]`` for sub-day timeframes.
    """
    tf_for_filename = {"1d": "D", "1w": "W"}.get(timeframe.lower(), timeframe)
    return f"{symbol.upper()}_{tf_for_filename}.csv"


def write_csv(path: Path, rows: list[dict]) -> None:
    """IBKR returns dicts with epoch-ms timestamps. Convert to seconds and
    write the history schema: ``time, open, high, low, close, volume``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for r in rows:
            ts_s = int(r.get("t", 0)) // 1000
            w.writerow(
                [
                    ts_s,
                    r.get("o", 0.0),
                    r.get("h", 0.0),
                    r.get("l", 0.0),
                    r.get("c", 0.0),
                    r.get("v", 0.0),
                ],
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(prog="fetch_ibkr_crypto_bars")
    p.add_argument(
        "--symbol", default="BTC", choices=sorted(_SYMBOL_TO_CONID),
        help="crypto symbol; conid resolved from venues.ibkr defaults",
    )
    p.add_argument(
        "--timeframe", default="1h", choices=sorted(_TF_TO_IBKR_BAR),
        help="bar size (matches the data.library naming convention)",
    )
    p.add_argument(
        "--months", type=int, default=12,
        help="lookback in months (mutually exclusive with --start/--end)",
    )
    p.add_argument("--start", help="ISO date YYYY-MM-DD")
    p.add_argument("--end", help="ISO date YYYY-MM-DD; default = today")
    p.add_argument(
        "--root", type=Path, default=IBKR_HISTORY_ROOT,
        help="output directory (default: canonical ETA IBKR crypto history root)",
    )
    p.add_argument(
        "--base-url", default=_DEFAULT_BASE_URL,
        help="Client Portal gateway URL",
    )
    args = p.parse_args()

    if args.start:
        start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    else:
        start = datetime.now(UTC) - timedelta(days=30 * args.months)
    end = (
        datetime.fromisoformat(args.end).replace(tzinfo=UTC)
        if args.end else datetime.now(UTC)
    )

    print(
        f"[fetch_ibkr_crypto_bars] {args.symbol}/{args.timeframe} "
        f"{start.date()} -> {end.date()} -> {args.root}",
    )
    print(
        f"  base_url={args.base_url}  "
        f"conid={_SYMBOL_TO_CONID[args.symbol]} "
        f"bar={_TF_TO_IBKR_BAR[args.timeframe]}",
    )

    rows = fetch_bars(
        symbol=args.symbol, timeframe=args.timeframe,
        start=start, end=end, base_url=args.base_url,
    )
    if not rows:
        print(
            "[fetch_ibkr_crypto_bars] zero rows fetched.\n"
            "  Likely causes:\n"
            "    1. Client Portal Gateway not running at base_url\n"
            "    2. Session not authenticated (visit base_url in browser)\n"
            "    3. CME Crypto market-data subscription not active\n"
            "  Verify /v1/api/iserver/auth/status returns authenticated=true.",
        )
        return 1

    out_path = args.root / _filename(args.symbol, args.timeframe)
    write_csv(out_path, rows)
    print(
        f"[fetch_ibkr_crypto_bars] wrote {len(rows)} rows to {out_path}\n"
        "Next: re-run the registry walk-forward at the promoted config "
        "and call obs.drift_monitor.assess_drift to compare vs the "
        "Coinbase baseline. See eta_data_source_policy.md.",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
