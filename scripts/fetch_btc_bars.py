"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_btc_bars
======================================================
Fetcher entry point for CME-style crypto bars (BTC, MBT, ETH, MET).
Writes CSVs into ``C:\\crypto_data\\`` so the data library picks
them up automatically (the next call to
``announce_data_library`` will reflect the new datasets and the
audit will flip BTC bots from BLOCKED → RUNNABLE).

Status — 2026-04-27 (placeholder)
---------------------------------
This file is intentionally a thin wrapper. The actual fetch logic
must live in one of:

* ``scripts/btc_paper_lane.py`` — already has Coinbase + Binance
  REST clients but writes to runtime state, not CSV.
* ``scripts/btc_paper_trade.py`` — same.
* ``scripts/dual_data_collector.py`` — already writes CSVs but
  for MNQ.

The CME-friendly path (per operator directive 2026-04-27, "for
crypto we are doing cme if it make any difference to cost factors"):

* CME BTC futures (BTC, MBT) — cash-settled to CF BRR.
* CME ETH futures (ETH, MET) — cash-settled to CF ETHUSD_RR.
* No funding column; CSV shape matches the MNQ "main" or
  "history" formats already handled by ``data.library``.

Sources to wire (in priority order):

1. **CME data direct** (Globex bars via the broker's market-data
   API — IBKR / Tastytrade both expose this).
2. **Coinbase / Binance spot** — proxies for CME BTC; can be
   used to produce a basis indicator. NOT a replacement: spot is
   24/7 while CME has session breaks, so the bar timestamps don't
   align without resampling.
3. **TradingView API via tradingview-mcp** — already used by
   ``scripts/data_pipeline/pull_tv_bars.py`` for MNQ; the pattern
   would extend to BTC1!/MBT1!/ETH1!/MET1! tickers.

Until one of those is wired, this script is a no-op that prints
the wiring plan and exits 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

CRYPTO_DATA_ROOT = Path(r"C:\crypto_data")


def main() -> int:
    print("[fetch_btc_bars] CME crypto bars fetcher — not yet wired")
    print()
    print(f"Target output root: {CRYPTO_DATA_ROOT}")
    print()
    print("Required fetchers (pick one, write CSVs in 'main' or 'history' shape):")
    print("  1. IBKR / Tastytrade Globex market-data API → CME BTC/MBT/ETH/MET bars")
    print("  2. tradingview-mcp ticker fetch (BTC1!, MBT1!, ETH1!, MET1!)")
    print("  3. dual_data_collector.py extension (already writes MNQ CSVs)")
    print()
    print("Schema expectations (must match data.library auto-detection):")
    print("  history: time(epoch_s), open, high, low, close, volume")
    print("  main:    timestamp_utc, epoch_s, open, high, low, close, volume, session")
    print()
    print("Once the fetcher writes CSVs to C:\\crypto_data\\:")
    print("  1. Add Path(r'C:\\crypto_data') to data.library.DEFAULT_ROOTS")
    print("  2. Re-run: python -m eta_engine.scripts.announce_data_library")
    print("  3. The audit will flip btc_hybrid / eth_perp / sol_perp from")
    print("     BLOCKED -> RUNNABLE (assuming bars match BotRequirements)")
    print()
    print("This script will exit 1 until the fetcher is wired.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
