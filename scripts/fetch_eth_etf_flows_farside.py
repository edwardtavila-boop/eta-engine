"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_eth_etf_flows_farside
===================================================================
Pull daily ETH spot-ETF net-flow totals from Farside Investors.

ETH spot ETFs launched July 2024 in the US (ETHA, ETHE, FETH, etc.).
Farside aggregates the daily flows the same way they do for BTC.

This unlocks the +1.32-Sharpe ETF flow filter pattern that worked
on BTC and applies it to ETH — gives ETH a path past the +1.97
plain-sage ceiling toward a BTC-class +5+ result.

Source: https://farside.co.uk/ethereum-etf-flow-all-data/

Output schema matches the BTC ETF flow file:
    time,net_flow_usd_m

Usage::

    python -m eta_engine.scripts.fetch_eth_etf_flows_farside
    python -m eta_engine.scripts.fetch_eth_etf_flows_farside --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

# Reuse the BTC ETF fetcher's parsing — Farside's table layout is
# identical across BTC and ETH endpoints.
from eta_engine.scripts.fetch_etf_flows_farside import (  # noqa: E402
    _fetch_html,
    _parse_farside_html,
    _write_csv,
)
from eta_engine.scripts.workspace_roots import MNQ_HISTORY_ROOT  # noqa: E402

_ENDPOINTS: tuple[str, ...] = (
    "https://farside.co.uk/ethereum-etf-flow-all-data/",
    "https://farside.co.uk/eth-etf-flow-all-data/",
)


def fetch(out_path: Path, *, dry_run: bool = False) -> int:
    print("[eth-etf-flows] fetching from Farside Investors")
    html: str | None = None
    for url in _ENDPOINTS:
        print(f"[eth-etf-flows] try {url}")
        html = _fetch_html(url)
        if html and "<table" in html.lower():
            break
    if not html:
        print("[eth-etf-flows] FAIL: no Farside endpoint returned HTML")
        return 0
    rows = _parse_farside_html(html)
    print(f"[eth-etf-flows] parsed {len(rows)} day rows")
    if not rows:
        return 0
    if dry_run:
        for ts, val in rows[-5:]:
            print(f"  {ts.date()}  {val:+.1f} M USD")
        print("[dry-run] CSV not written")
        return len(rows)
    n = _write_csv(out_path, rows)
    last_dt, last_val = rows[-1]
    print(f"[eth-etf-flows] wrote {n} rows to {out_path}; last={last_dt.date()} ({last_val:+.1f} M USD)")
    return n


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out",
        type=Path,
        default=MNQ_HISTORY_ROOT / "ETH_ETF_FLOWS.csv",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    n = fetch(args.out, dry_run=args.dry_run)
    return 0 if n > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
