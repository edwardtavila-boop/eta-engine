"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_xrp_news_history
==============================================================
Free-API regulatory/news sentiment proxy for XRP.

Why this exists
---------------
The xrp_perp registry row was DEACTIVATED 2026-04-27 with the
explicit gate:

    "Reactivate once: (1) a news/regulatory feed is wired into
    the data library (see BotRequirements:xrp_perp), and (2) a
    feature class consumes it..."

This script satisfies (1) — at the **structural** level. It populates
``XRPSENT_D.csv`` under the workspace ``data/crypto/sentiment`` root with
a daily mention-count time series so:

  * ``data.audit`` flips XRP's sentiment requirement from
    MISSING -> AVAILABLE.
  * A future feature class can read the file via
    ``data.library.get(symbol="XRPSENT", timeframe="D")``.

What's covered
--------------
Free, no-key sources only:

* **SEC EDGAR full-text search** — ``efts.sec.gov/LATEST/search-
  index?q=...``. Returns filings mentioning "ripple" or "xrp".
  Cadence: daily aggregation of total filings touching the
  search query.

What's not covered
------------------
* Sentiment polarity (positive vs negative). This script only
  counts mentions; classifying tone needs an LLM/NLP layer.
* Twitter/X regulatory sentiment — those APIs are no longer free.
* Breaking-news primary sources — the operator can layer a
  proper news API (Reuters, WSJ) on top of this CSV when XRP
  reactivation is on the table.

Reactivation gate (per registry rationale)
------------------------------------------
Beyond this fetcher, the second gate item is a feature class
that **consumes** the file (e.g. ``SECHeadlineFeature`` returning
a time-decay signal around recent rulings). That's a separate
PR; this script just unblocks the data-side gate.

Usage::

    python -m eta_engine.scripts.fetch_xrp_news_history --days 365

After running, audit XRP coverage::

    python -c "from eta_engine.data.audit import audit_bot; \\
               r = audit_bot('xrp_perp'); \\
               print(f'missing_critical: {len(r.missing_critical)}')"
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import CRYPTO_SENTIMENT_ROOT as SENTIMENT_ROOT  # noqa: E402

# SEC EDGAR full-text search endpoint. Public, no auth required, but
# requires a custom User-Agent identifying the requester (per SEC's
# fair-access policy).
_EDGAR_FTS = "https://efts.sec.gov/LATEST/search-index"
_USER_AGENT = "eta-engine fetch_xrp_news_history (edward.t.avila@gmail.com)"


def _http_json(url: str, *, timeout: float = 10.0) -> dict | None:
    request = urllib.request.Request(  # noqa: S310 -- public SEC endpoint
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 -- public SEC endpoint
            request, timeout=timeout,
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"  HTTP failed: {exc}")
        return None
    except json.JSONDecodeError as exc:
        print(f"  JSON decode failed: {exc}")
        return None
    return payload if isinstance(payload, dict) else None


def _query_edgar(query: str, dates: tuple[date, date]) -> list[dict]:
    """Return the SEC EDGAR full-text search hits for ``query``.

    SEC FTS uses a ``forms``/``dateRange``/``q`` shape. We aggregate
    hits over the full window in one call (FTS caps at ~100 hits per
    page; this is enough for the sparse "xrp" / "ripple" universe).
    """
    qs = urllib.parse.urlencode(
        {
            "q": f'"{query}"',
            "dateRange": "custom",
            "startdt": dates[0].isoformat(),
            "enddt": dates[1].isoformat(),
        },
    )
    url = f"{_EDGAR_FTS}?{qs}"
    payload = _http_json(url)
    if not payload:
        return []
    hits = (payload.get("hits") or {}).get("hits") or []
    return hits if isinstance(hits, list) else []


def _build_daily_series(  # noqa: PLR0912 -- straight-line aggregation over one date window
    queries: list[str], days: int,
) -> dict[date, dict[str, int]]:
    end_d = datetime.now(UTC).date()
    start_d = end_d - timedelta(days=days)
    series: dict[date, dict[str, int]] = {}

    for q in queries:
        print(f"  EDGAR query: {q!r} {start_d} -> {end_d}")
        hits = _query_edgar(q, (start_d, end_d))
        if not hits:
            continue
        # Each hit has {_source: {file_date: "YYYY-MM-DD", ...}}.
        # Count by file_date so the daily column is dense.
        counts: Counter[date] = Counter()
        for h in hits:
            if not isinstance(h, dict):
                continue
            src = h.get("_source")
            if not isinstance(src, dict):
                continue
            fd = src.get("file_date") or src.get("filed")
            if not isinstance(fd, str):
                continue
            try:
                d = datetime.fromisoformat(fd).date()
            except ValueError:
                continue
            counts[d] += 1
        col = f"sec_mentions_{q.replace(' ', '_').lower()}"
        for d, n in counts.items():
            series.setdefault(d, {})[col] = int(n)
        time.sleep(0.5)  # SEC fair-access cadence
    return series


def _filename(symbol: str) -> str:
    return f"{symbol.upper()}SENT_D.csv"


def write_csv(  # noqa: PLR0913 - explicit args keep CSV shape intentional
    *, path: Path, series: dict[date, dict[str, int]],
    queries: list[str], days: int, end_d: date,
) -> None:
    """Write XRPSENT_D.csv with one row per day, including zero-count days.

    The data.library bar parser reads ``time, open, high, low, close,
    volume`` only. We use the total-mention count as ``close`` so
    feature consumers can read it as a generic time series via
    ``DataLibrary.load_bars``. Per-query columns are appended for the
    eventual feature class to consume directly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [f"sec_mentions_{q.replace(' ', '_').lower()}" for q in queries]
    end_d_actual = end_d
    start_d = end_d_actual - timedelta(days=days)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume", *cols])
        cursor = start_d
        while cursor <= end_d_actual:
            day_data = series.get(cursor, {})
            total = sum(day_data.get(c, 0) for c in cols)
            ts = int(datetime(
                cursor.year, cursor.month, cursor.day, tzinfo=UTC,
            ).timestamp())
            row = [
                ts,
                float(total),  # open
                float(total),  # high
                float(total),  # low
                float(total),  # close
                float(total),  # volume = total mention count
                *[day_data.get(c, 0) for c in cols],
            ]
            w.writerow(row)
            cursor += timedelta(days=1)


def main() -> int:
    p = argparse.ArgumentParser(prog="fetch_xrp_news_history")
    p.add_argument("--days", type=int, default=365)
    p.add_argument(
        "--root", type=Path, default=SENTIMENT_ROOT,
        help="output directory (default: canonical ETA crypto sentiment root)",
    )
    p.add_argument(
        "--queries", nargs="+", default=["ripple", "XRP"],
        help="EDGAR full-text search terms (whitespace separates terms)",
    )
    args = p.parse_args()

    print(
        f"[fetch_xrp_news_history] last {args.days}d, "
        f"queries={args.queries} -> {args.root}",
    )
    series = _build_daily_series(args.queries, args.days)
    end_d = datetime.now(UTC).date()
    out = args.root / _filename("XRP")
    write_csv(
        path=out,
        series=series,
        queries=args.queries,
        days=args.days,
        end_d=end_d,
    )
    n_active = sum(
        1 for d in series if any(series[d].get(c) for c in series[d])
    )
    print(
        f"  wrote {args.days + 1} day rows to {out}  "
        f"(non-zero days = {n_active})",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
