"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_fear_greed_alternative
====================================================================
Pull the Crypto Fear & Greed Index from alternative.me.

API: https://api.alternative.me/fng/?limit=0  (free, JSON, no auth)

Per the user's BTC-driver write-up: "Sentiment, News & Hype: Media
coverage, regulatory news, celebrity tweets... Google Trends/search
interest often tracks retail FOMO/FUD."

The Fear & Greed Index is the most-cited single-number sentiment
proxy for crypto. Daily values 0-100:
  *  0-25  = Extreme Fear   (often local bottoms / accumulation)
  * 26-49  = Fear
  * 50-50  = Neutral
  * 51-74  = Greed
  * 75-100 = Extreme Greed  (often local tops / distribution)

Output schema (CSV):
    time,fear_greed
    1716508800,42
    ...

``fear_greed`` is the raw 0-100 score; the provider does any
transformation (e.g. centering / normalizing).

Usage::

    python -m eta_engine.scripts.fetch_fear_greed_alternative
    python -m eta_engine.scripts.fetch_fear_greed_alternative --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


_API_URL = "https://api.alternative.me/fng/?limit=0&format=json"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)


def _fetch_json(url: str, timeout: float = 30.0) -> dict | None:
    """GET JSON. Returns dict on success, None on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
        return None


def _parse(data: dict) -> list[tuple[datetime, int]]:
    """Extract (date, value) pairs from the alternative.me response.

    Schema:
        {"data": [{"value": "42", "timestamp": "1716508800", ...}, ...]}
    """
    rows: list[tuple[datetime, int]] = []
    items = data.get("data") or []
    for item in items:
        try:
            ts = datetime.fromtimestamp(int(item["timestamp"]), UTC)
            value = int(item["value"])
        except (KeyError, ValueError, TypeError):
            continue
        rows.append((ts, value))
    return rows


def _write_csv(out: Path, rows: list[tuple[datetime, int]]) -> int:
    if not rows:
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    by_ts: dict[int, int] = {int(ts.timestamp()): v for ts, v in rows}
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "fear_greed"])
        for ts in sorted(by_ts):
            w.writerow([ts, by_ts[ts]])
    return len(by_ts)


def fetch(out_path: Path, *, dry_run: bool = False) -> int:
    print("[fear-greed] fetching from alternative.me")
    data = _fetch_json(_API_URL)
    if data is None:
        print("[fear-greed] FAIL: API call returned no data")
        return 0
    rows = _parse(data)
    print(f"[fear-greed] parsed {len(rows)} day rows")
    if not rows:
        return 0
    if dry_run:
        for ts, val in rows[:5]:
            print(f"  {ts.date()}  {val}")
        print(f"  ... and {len(rows)-5} more")
        return len(rows)
    n = _write_csv(out_path, rows)
    last_ts, last_val = max(rows, key=lambda r: r[0])
    print(f"[fear-greed] wrote {n} rows to {out_path}; last={last_ts.date()} ({last_val})")
    return n


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out", type=Path,
        default=Path(r"C:\mnq_data\history\BTC_FEAR_GREED.csv"),
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    n = fetch(args.out, dry_run=args.dry_run)
    return 0 if n > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
