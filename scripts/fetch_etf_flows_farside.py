"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_etf_flows_farside
================================================================
Pull daily BTC spot-ETF net-flow totals from Farside Investors.

Farside Investors publishes daily aggregate Bitcoin spot-ETF flows
(IBIT + FBTC + ARKB + BITB + BTCO + EZBC + BRRR + HODL + BTCW +
GBTC + others) at https://farside.co.uk/bitcoin-etf-flow-all-data/.

Per the user's BTC-driver write-up (2026-04-27): "Spot Bitcoin ETFs
(especially BlackRock's IBIT) have been massive buyers, absorbing
billions in inflows and often outpacing new miner supply."

Output schema (CSV):
    time,net_flow_usd_m
    1716508800,256.4
    ...

``net_flow_usd_m`` is the day's aggregate net flow across all
spot-BTC ETFs in USD millions. Positive = inflow, negative = outflow.

Usage::

    python -m eta_engine.scripts.fetch_etf_flows_farside [--out PATH]
    python -m eta_engine.scripts.fetch_etf_flows_farside --dry-run

The script tries multiple Farside endpoints in priority order and
parses the HTML table when their CSV-export URL changes (which it
has historically). Failure is non-fatal — script returns 2 and the
provider falls back to no-op when the file is missing or stale.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


# Farside endpoints in priority order. The HTML-table URL is the
# stable one; the CSV export URL has changed historically.
_ENDPOINTS: tuple[str, ...] = (
    "https://farside.co.uk/bitcoin-etf-flow-all-data/",
    "https://farside.co.uk/btc-etf-flow-all-data/",
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)


def _fetch_html(url: str, timeout: float = 30.0) -> str | None:
    """Fetch a URL with a browser-style UA. Returns text on 2xx, None on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return None


def _parse_farside_html(html: str) -> list[tuple[datetime, float]]:
    """Extract (date, total_flow_usd_m) pairs from a Farside HTML page.

    Farside's table has a header row with each ETF ticker and a final
    "Total" column. We detect rows by looking for date-shaped cells
    (DD MMM YYYY or YYYY-MM-DD) and grabbing the LAST numeric cell
    in that row as the total.
    """
    rows: list[tuple[datetime, float]] = []
    # Very permissive row regex — any <tr>...</tr>
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.IGNORECASE | re.DOTALL):
        cells = re.findall(
            r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.IGNORECASE | re.DOTALL,
        )
        if not cells:
            continue
        # Strip HTML tags + whitespace from each cell
        clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if not clean:
            continue
        # Try to parse the FIRST cell as a date
        dt = _parse_farside_date(clean[0])
        if dt is None:
            continue
        # The TOTAL is conventionally the last numeric cell in the row.
        total = _parse_farside_number(clean[-1])
        if total is None:
            continue
        rows.append((dt, total))
    return rows


def _parse_farside_date(s: str) -> datetime | None:
    """Farside dates: '02 May 2024' or '2024-05-02'."""
    s = s.strip()
    for fmt in ("%d %b %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            d = datetime.strptime(s, fmt).replace(tzinfo=UTC)
            return d
        except ValueError:
            continue
    return None


def _parse_farside_number(s: str) -> float | None:
    """Parse '256.4' / '(123.4)' / '-' / '' from a Farside total cell.

    Parens denote negative (outflow); '-' / '' means no data that day.
    """
    s = s.strip().replace(",", "")
    if not s or s == "-":
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
        neg = True
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _write_csv(out: Path, rows: list[tuple[datetime, float]]) -> int:
    """Sort + de-dupe by timestamp, write standard schema."""
    if not rows:
        return 0
    by_ts: dict[int, float] = {}
    for ts, val in rows:
        by_ts[int(ts.timestamp())] = val
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "net_flow_usd_m"])
        for ts in sorted(by_ts):
            w.writerow([ts, by_ts[ts]])
    return len(by_ts)


def fetch(out_path: Path, *, dry_run: bool = False) -> int:
    """Fetch Farside ETF flows. Returns row count on success, 0 on failure."""
    print("[etf-flows] fetching from Farside Investors")
    html: str | None = None
    for url in _ENDPOINTS:
        print(f"[etf-flows] try {url}")
        html = _fetch_html(url)
        if html and "<table" in html.lower():
            break
    if not html:
        print("[etf-flows] FAIL: no Farside endpoint returned HTML")
        return 0
    rows = _parse_farside_html(html)
    print(f"[etf-flows] parsed {len(rows)} day rows")
    if not rows:
        return 0
    if dry_run:
        for ts, val in rows[-5:]:
            print(f"  {ts.date()}  {val:+.1f} M USD")
        print("[dry-run] CSV not written")
        return len(rows)
    n = _write_csv(out_path, rows)
    last_dt, last_val = rows[-1]
    print(
        f"[etf-flows] wrote {n} rows to {out_path}; "
        f"last={last_dt.date()} ({last_val:+.1f} M USD)"
    )
    return n


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out", type=Path,
        default=Path(r"C:\mnq_data\history\BTC_ETF_FLOWS.csv"),
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    n = fetch(args.out, dry_run=args.dry_run)
    return 0 if n > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
