"""
EVOLUTIONARY TRADING ALGO  //  scripts.fetch_lth_proxy
========================================================
Compute a long-term-holder (LTH) supply proxy from existing BTC
daily OHLCV data — no external on-chain feed required.

Background
----------
Per the user's BTC-driver write-up: "Network Fundamentals: Rising
on-chain activity, long-term holder accumulation, and declining
exchange reserves signal strong hands not selling easily."

Proper LTH supply (% of circulating BTC held >155 days) lives at
Glassnode (paid) and CoinMetrics (mostly paid). For our framework
we ship a *price-derived proxy* that captures the same underlying
phase — accumulation vs distribution — using ONLY the BTC daily
close series we already have on disk (1800 bars = 5 years).

The proxy
---------
**Mayer Multiple percentile**: ratio of current price to its
200-day SMA, ranked over a rolling 365-day window.

* Mayer Multiple < ~0.8  (current < 80% of 200-day MA)
  => historical accumulation phase (LTH buying, exchange reserves
     fall). We score this **positive** for longs, negative for shorts.
* Mayer Multiple > ~2.4  (current > 240% of 200-day MA)
  => distribution phase (LTH selling into euphoria). Score
     **negative** for longs.
* Middle band → score ~0 (neutral).

Output (CSV in our standard schema):
    time,lth_proxy
    1716508800,0.32

``lth_proxy`` is in [-1, +1]:
   +1 = strong accumulation phase (deep below 200 SMA)
    0 = neutral
   -1 = strong distribution phase (well above 200 SMA)

Usage::

    python -m eta_engine.scripts.fetch_lth_proxy
    python -m eta_engine.scripts.fetch_lth_proxy --dry-run

This is a PROXY. It correlates with real LTH supply but isn't a
substitute. Wire a true on-chain feed (Glassnode/CoinMetrics) to
the same provider hook when paid access is available.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


_DEFAULT_INPUT = Path(r"C:\mnq_data\history\BTC_D.csv")
_DEFAULT_OUTPUT = Path(r"C:\mnq_data\history\BTC_LTH_PROXY.csv")
_SMA_PERIOD = 200          # Mayer Multiple denominator
_PCT_LOOKBACK_DAYS = 365   # rolling-percentile window


def _read_btc_daily(path: Path) -> list[tuple[datetime, float]]:
    """Read BTC daily OHLCV CSV, return [(ts, close), ...]."""
    if not path.exists():
        return []
    out: list[tuple[datetime, float]] = []
    with path.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                ts = datetime.fromtimestamp(int(row["time"]), UTC)
                close = float(row["close"])
            except (ValueError, KeyError, TypeError):
                continue
            out.append((ts, close))
    out.sort(key=lambda x: x[0])
    return out


def _compute_proxy(
    series: list[tuple[datetime, float]],
    *,
    sma_period: int = _SMA_PERIOD,
    pct_lookback: int = _PCT_LOOKBACK_DAYS,
) -> list[tuple[datetime, float]]:
    """Compute the LTH-proxy series.

    For each day i with i >= sma_period + pct_lookback:
      * mm[i]  = close[i] / mean(close[i-sma_period+1 ... i])
      * pct    = rank of mm[i] within the last pct_lookback Mayer
                 Multiples (0.0 = lowest, 1.0 = highest)
      * proxy  = 1.0 - 2.0 * pct        (so accumulation -> +1)

    Bars before warmup are skipped.
    """
    if not series:
        return []
    closes = [c for _, c in series]
    timestamps = [t for t, _ in series]
    mayer: list[float | None] = [None] * len(series)
    for i in range(sma_period - 1, len(series)):
        sma = sum(closes[i - sma_period + 1 : i + 1]) / sma_period
        if sma > 0:
            mayer[i] = closes[i] / sma
    out: list[tuple[datetime, float]] = []
    for i in range(sma_period - 1 + pct_lookback, len(series)):
        window = [
            mayer[j] for j in range(i - pct_lookback + 1, i + 1)
            if mayer[j] is not None
        ]
        cur = mayer[i]
        if not window or cur is None:
            continue
        sorted_w = sorted(window)
        try:
            rank = sorted_w.index(cur)
        except ValueError:
            continue
        pct = rank / max(len(sorted_w) - 1, 1)
        proxy = 1.0 - 2.0 * pct  # 0 -> +1, 1 -> -1
        proxy = max(-1.0, min(1.0, proxy))
        out.append((timestamps[i], proxy))
    return out


def _write_csv(out: Path, rows: list[tuple[datetime, float]]) -> int:
    if not rows:
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    by_ts = {int(ts.timestamp()): v for ts, v in rows}
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "lth_proxy"])
        for ts in sorted(by_ts):
            w.writerow([ts, f"{by_ts[ts]:.4f}"])
    return len(by_ts)


def compute_and_write(
    btc_daily_csv: Path, out_path: Path, *, dry_run: bool = False,
) -> int:
    print(f"[lth-proxy] reading {btc_daily_csv}")
    series = _read_btc_daily(btc_daily_csv)
    if not series:
        print("[lth-proxy] FAIL: no BTC daily data on disk")
        return 0
    print(f"[lth-proxy] {len(series)} daily bars")
    rows = _compute_proxy(series)
    print(f"[lth-proxy] {len(rows)} proxy values after warmup")
    if not rows:
        return 0
    if dry_run:
        for ts, v in rows[-5:]:
            print(f"  {ts.date()}  {v:+.3f}")
        return len(rows)
    n = _write_csv(out_path, rows)
    last_ts, last_v = rows[-1]
    print(
        f"[lth-proxy] wrote {n} rows to {out_path}; "
        f"last={last_ts.date()} (proxy={last_v:+.3f})"
    )
    return n


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--in", dest="in_path", type=Path, default=_DEFAULT_INPUT)
    p.add_argument("--out", type=Path, default=_DEFAULT_OUTPUT)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    n = compute_and_write(args.in_path, args.out, dry_run=args.dry_run)
    return 0 if n > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
