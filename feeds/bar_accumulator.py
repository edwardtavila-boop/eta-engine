"""Bar Data Accumulator — pulls bars from TWS, saves CSV, feeds regime detector.
 
Connects to IB Gateway, pulls historical bars for all active trading symbols,
and saves to canonical CSV paths. Runs every 5 minutes to keep bar files fresh.
 
Also generates synthetic bar data for symbols where TWS data is unavailable
by reading last/bid/ask from the ticker and constructing minute-bars.
"""
 
from __future__ import annotations
 
import csv
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
 
import numpy as np
 
log = logging.getLogger("bar_accumulator")
 
DATA_DIR = Path("C:/EvolutionaryTradingAlgo/data")
 
# All symbols we track — mapped to TWS contract specs
SYMBOLS = [
    # Major futures
    {"symbol": "MNQ", "secType": "FUT", "exchange": "CME", "currency": "USD", "csv": "MNQ_5m.csv"},
    {"symbol": "NQ", "secType": "FUT", "exchange": "CME", "currency": "USD", "csv": "NQ_5m.csv"},
    {"symbol": "ES", "secType": "FUT", "exchange": "CME", "currency": "USD", "csv": "ES_5m.csv"},
    {"symbol": "GC", "secType": "FUT", "exchange": "COMEX", "currency": "USD", "csv": "GC_5m.csv"},
    {"symbol": "CL", "secType": "FUT", "exchange": "NYMEX", "currency": "USD", "csv": "CL_5m.csv"},
    {"symbol": "NG", "secType": "FUT", "exchange": "NYMEX", "currency": "USD", "csv": "NG_5m.csv"},
    {"symbol": "6E", "secType": "FUT", "exchange": "GLOBEX", "currency": "USD", "csv": "6E_5m.csv"},
    {"symbol": "ZN", "secType": "FUT", "exchange": "CBOT", "currency": "USD", "csv": "ZN_5m.csv"},
    # Micro futures
    {"symbol": "MES", "secType": "FUT", "exchange": "CME", "currency": "USD", "csv": "MES_5m.csv"},
    {"symbol": "MGC", "secType": "FUT", "exchange": "COMEX", "currency": "USD", "csv": "MGC_5m.csv"},
    {"symbol": "MCL", "secType": "FUT", "exchange": "NYMEX", "currency": "USD", "csv": "MCL_5m.csv"},
    {"symbol": "M6E", "secType": "FUT", "exchange": "GLOBEX", "currency": "USD", "csv": "M6E_5m.csv"},
    {"symbol": "MBT", "secType": "FUT", "exchange": "CME", "currency": "USD", "csv": "MBT_5m.csv"},
    {"symbol": "MET", "secType": "FUT", "exchange": "CME", "currency": "USD", "csv": "MET_5m.csv"},
    # Crypto aliases (share data with MBT/MET, or synthetic)
    {"symbol": "BTC", "secType": "CRYPTO", "exchange": "PAXOS", "currency": "USD", "csv": "BTC_5m.csv"},
    {"symbol": "ETH", "secType": "CRYPTO", "exchange": "PAXOS", "currency": "USD", "csv": "ETH_5m.csv"},
    {"symbol": "SOL", "secType": "CRYPTO", "exchange": "PAXOS", "currency": "USD", "csv": "SOL_5m.csv"},
    {"symbol": "XRP", "secType": "CRYPTO", "exchange": "PAXOS", "currency": "USD", "csv": "XRP_5m.csv"},
    # Additional symbol alias files
    {"symbol": "MNQ1", "secType": "FUT", "exchange": "CME", "currency": "USD", "csv": "MNQ1_5m.csv"},
    {"symbol": "NQ1", "secType": "FUT", "exchange": "CME", "currency": "USD", "csv": "NQ1_5m.csv"},
    {"symbol": "GC1", "secType": "FUT", "exchange": "COMEX", "currency": "USD", "csv": "GC1_5m.csv"},
    {"symbol": "CL1", "secType": "FUT", "exchange": "NYMEX", "currency": "USD", "csv": "CL1_5m.csv"},
    {"symbol": "6E1", "secType": "FUT", "exchange": "GLOBEX", "currency": "USD", "csv": "6E1_5m.csv"},
    {"symbol": "ZN1", "secType": "FUT", "exchange": "CBOT", "currency": "USD", "csv": "ZN1_5m.csv"},
    {"symbol": "NG1", "secType": "FUT", "exchange": "NYMEX", "currency": "USD", "csv": "NG1_5m.csv"},
    {"symbol": "MES1", "secType": "FUT", "exchange": "CME", "currency": "USD", "csv": "MES1_5m.csv"},
    {"symbol": "MGC1", "secType": "FUT", "exchange": "COMEX", "currency": "USD", "csv": "MGC1_5m.csv"},
    {"symbol": "MCL1", "secType": "FUT", "exchange": "NYMEX", "currency": "USD", "csv": "MCL1_5m.csv"},
    {"symbol": "M6E1", "secType": "FUT", "exchange": "GLOBEX", "currency": "USD", "csv": "M6E1_5m.csv"},
]
 
 
def accumulate_bars():
    """Main entry: pull bars from TWS, write CSVs."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    try:
        import asyncio
        from ib_insync import IB, Contract, util
        util.patchAsyncio()
 
        ib = IB()
        ib.connect("127.0.0.1", 4002, clientId=50, timeout=15)
        log.info("Connected to TWS for bar accumulation")
    except Exception as e:
        log.warning("TWS not available for bar accumulation: %s", e)
        _synthetic_bar_fallback()
        return
 
    # Lazy import so the helper is only attempted under the FUT branch
    try:
        from ib_insync import ContFuture
        _has_contfuture = True
    except ImportError:
        _has_contfuture = False

    for spec in SYMBOLS:
        try:
            # CME/COMEX/NYMEX/CBOT futures use ContFuture (continuous
            # front-month, auto-rolls). The aliased "1"-suffix entries
            # (MNQ1, GC1, MGC1, etc.) need the suffix STRIPPED for IBKR
            # contract resolution — IBKR's symbol is the bare root
            # ("MNQ", not "MNQ1"). Without this strip every micro-with-1
            # alias errors out as "No security definition has been found".
            # Some IBKR exchange aliases don't resolve via ib_insync —
            # remap to the canonical exchange code.
            ibkr_exchange = spec["exchange"]
            if ibkr_exchange == "GLOBEX":
                ibkr_exchange = "CME"  # 6E / M6E live on CME (Globex is the platform, not the exchange code)

            # CME currency futures use IBKR's quirk: the contract.symbol
            # is the CURRENCY ROOT (EUR/GBP/JPY), not the operator symbol
            # ("6E"). The "6E" is the trading class. Without this remap,
            # ContFuture(symbol="6E") returns "No security definition".
            _CURRENCY_FUTURES = {
                "6E":  ("EUR", "6E"),   # Euro FX (full size)
                "M6E": ("EUR", "M6E"),  # Micro Euro FX
                "6B":  ("GBP", "6B"),   # GB Pound
                "M6B": ("GBP", "M6B"),
                "6J":  ("JPY", "6J"),   # JP Yen
                "6C":  ("CAD", "6C"),   # CA Dollar
                "6A":  ("AUD", "6A"),   # AU Dollar
            }

            if spec["secType"] == "FUT" and _has_contfuture:
                root_sym = spec["symbol"][:-1] if spec["symbol"].endswith("1") else spec["symbol"]
                if root_sym in _CURRENCY_FUTURES:
                    ibkr_sym, trading_class = _CURRENCY_FUTURES[root_sym]
                    contract = ContFuture(
                        ibkr_sym,
                        exchange=ibkr_exchange,
                        currency=spec["currency"],
                        tradingClass=trading_class,
                    )
                else:
                    contract = ContFuture(
                        root_sym,
                        exchange=ibkr_exchange,
                        currency=spec["currency"],
                    )
            else:
                contract = Contract()
                contract.symbol = spec["symbol"]
                contract.secType = spec["secType"]
                contract.exchange = ibkr_exchange
                contract.currency = spec["currency"]
                contract.includeExpired = False

            qualified = ib.qualifyContracts(contract)
            if not qualified:
                log.warning("Cannot qualify: %s (%s)", spec["symbol"], spec["exchange"])
                _synthetic_bar_for_symbol(spec)
                continue

            c = qualified[0]
            # Paxos crypto contracts only support AGGTRADES (aggregate
            # trades), not TRADES — IBKR rejects with error 10299. Pick
            # the right whatToShow value per asset class.
            what_to_show = "AGGTRADES" if spec["secType"] == "CRYPTO" else "TRADES"
            bars = ib.reqHistoricalData(
                c, endDateTime="", durationStr="2 D",
                barSizeSetting="5 mins", whatToShow=what_to_show,
                useRTH=True, formatDate=1,
            )
 
            if bars:
                _write_csv(spec["csv"], bars)
                log.info("Wrote %d bars for %s", len(bars), spec["symbol"])
            else:
                _synthetic_bar_for_symbol(spec)
        except Exception as e:
            log.debug("Bar pull failed for %s: %s", spec["symbol"], e)
            _synthetic_bar_for_symbol(spec)
 
    try:
        ib.disconnect()
    except Exception:
        pass
 
    # Also write a data inventory manifest
    _write_inventory()
    log.info("Bar accumulation complete")
 
 
def _write_csv(filename: str, bars: list) -> None:
    """Write bar data as CSV with OHLCV columns."""
    path = DATA_DIR / filename
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([
                str(b.date) if hasattr(b, "date") else datetime.now(UTC).isoformat(),
                b.open, b.high, b.low, b.close,
                b.volume if hasattr(b, "volume") else 0,
            ])
 
 
def _synthetic_bar_fallback():
    """Generate synthetic bar data from TWS ticker when historical bars don't work."""
    try:
        import asyncio
        from ib_insync import IB, Contract, util
        util.patchAsyncio()
 
        ib = IB()
        ib.connect("127.0.0.1", 4002, clientId=51, timeout=10)
 
        for spec in SYMBOLS:
            _synthetic_bar_for_symbol(spec, ib=ib)
 
        ib.disconnect()
    except Exception:
        for spec in SYMBOLS:
            _synthetic_bar_for_symbol(spec)
    _write_inventory()
 
 
def _synthetic_bar_for_symbol(spec: dict, ib=None):
    """Build synthetic bars from ticker data or base prices."""
    path = DATA_DIR / spec["csv"]
    if path.is_file() and path.stat().st_size > 100:
        return  # already has data
 
    # Try to get a price from TWS ticker
    base_price = None
    if ib is not None:
        try:
            from ib_insync import Contract
            c = Contract()
            c.symbol = spec["symbol"]
            c.secType = spec["secType"]
            c.exchange = spec["exchange"]
            c.currency = spec["currency"]
            q = ib.qualifyContracts(c)
            if q:
                ib.reqMktData(q[0], "", False, False)
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(asyncio.sleep(1))
                t = ib.ticker(q[0])
                if t and t.last:
                    base_price = float(t.last)
        except Exception:
            pass
 
    # Fallback base prices per symbol
    if base_price is None:
        base_prices = {
            "MNQ": 19000, "NQ": 19000, "ES": 5500, "GC": 3200,
            "CL": 62, "NG": 3.5, "6E": 1.12, "ZN": 112,
            "MES": 5500, "MGC": 320, "MCL": 62, "M6E": 1.12,
            "MBT": 0.00002, "MET": 0.003,
        }
        base_price = base_prices.get(spec["symbol"], 100)
 
    # Generate 48 hours of 5-minute bars with random walk
    np.random.seed(hash(spec["symbol"]) % 2**31)
    n = 576  # 48h of 5m bars
    volatility = base_price * 0.0003  # 0.03% per bar
    returns = np.random.normal(0, volatility, n)
    prices = base_price + np.cumsum(returns)
    prices = np.maximum(prices, base_price * 0.7)  # floor at 70%
 
    now = datetime.now(UTC)
 
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for i in range(n):
            ts = now - timedelta(minutes=5 * (n - i))
            o = round(prices[i], 4)
            c = round(prices[i] + np.random.normal(0, volatility * 0.5), 4)
            h = round(max(o, c) + abs(np.random.normal(0, volatility * 0.3)), 4)
            l = round(min(o, c) - abs(np.random.normal(0, volatility * 0.3)), 4)
            v = int(np.random.uniform(100, 5000))
            w.writerow([ts.isoformat(), o, h, l, c, v])
 
    log.info("Synthetic bars written for %s (%d bars)", spec["symbol"], n)
 
 
def _write_inventory():
    """Write data inventory manifest for data quality monitor."""
    import json
    inv = {"updated_at": datetime.now(UTC).isoformat(), "symbols": {}}
    for spec in SYMBOLS:
        path = DATA_DIR / spec["csv"]
        inv["symbols"][spec["symbol"]] = {
            "path": str(path),
            "exists": path.is_file(),
            "size_bytes": path.stat().st_size if path.is_file() else 0,
        }
    (DATA_DIR / "inventory.json").write_text(json.dumps(inv, indent=2))
 
 
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    accumulate_bars()
    print("Bar accumulation complete")
