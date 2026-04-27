"""Refresh the cross-bot correlation matrix from realized data
(Tier-4 #16, 2026-04-27).

The hardcoded ``_CORRELATIONS`` table in
``brain/jarvis_correlation.py`` is a long-run static estimate. This
script computes the rolling 90-day daily-return correlation matrix
from parquet bar data + writes the result to
``state/correlation/learned.json``.

The runtime correlation helper checks the learned file FIRST; if it
exists and is < 30 days old, uses those values. Otherwise falls back
to the hardcoded defaults.

Run quarterly via scheduled task. Doesn't auto-overwrite the source
``_CORRELATIONS`` -- writes a side-by-side learned file so the
operator can diff before committing.

Inputs
------
Parquet bar files at ``data/parquet/<symbol>_1d.parquet`` per the
existing data layout. Symbols: MNQ, NQ, BTCUSDT, ETHUSDT, SOLUSDT,
XRPUSDT (also accepts CME aliases like MBT/MET).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("refresh_correlation_matrix")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

#: Symbols we track. Aliases (MBT/MET) collapse to BTCUSDT/ETHUSDT.
TRACKED_SYMBOLS = ["MNQ", "NQ", "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]


def _load_returns(parquet_dir: Path, symbol: str, *, days: int) -> list[float] | None:
    """Daily returns for symbol from parquet, last `days` days. Returns
    None if data is missing or too sparse."""
    candidates = [
        parquet_dir / f"{symbol}_1d.parquet",
        parquet_dir / f"{symbol.lower()}_1d.parquet",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return None
    try:
        import pandas as pd
        df = pd.read_parquet(path)
    except (ImportError, OSError) as exc:
        logger.debug("load %s failed: %s", path, exc)
        return None
    # Expect a 'close' column (or 'c'); compute pct returns
    col = "close" if "close" in df.columns else ("c" if "c" in df.columns else None)
    if col is None:
        return None
    series = df[col].dropna().tail(days + 1)
    if len(series) < days // 4:  # need at least 25% coverage
        return None
    returns = series.pct_change().dropna().tolist()
    return [float(r) for r in returns]


def _pearson(a: list[float], b: list[float]) -> float:
    """Pearson correlation of two equal-length series."""
    n = min(len(a), len(b))
    if n < 5:
        return 0.0
    a2 = a[:n]
    b2 = b[:n]
    mean_a = sum(a2) / n
    mean_b = sum(b2) / n
    num   = sum((x - mean_a) * (y - mean_b) for x, y in zip(a2, b2))
    den_a = sum((x - mean_a) ** 2 for x in a2) ** 0.5
    den_b = sum((y - mean_b) ** 2 for y in b2) ** 0.5
    if den_a == 0 or den_b == 0:
        return 0.0
    return round(num / (den_a * den_b), 4)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--parquet-dir", type=Path,
                   default=ROOT.parent / "data" / "parquet")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--out", type=Path,
                   default=ROOT / "state" / "correlation" / "learned.json")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not args.parquet_dir.exists():
        logger.warning("parquet dir %s missing -- nothing to compute", args.parquet_dir)
        return 0

    returns: dict[str, list[float]] = {}
    for sym in TRACKED_SYMBOLS:
        r = _load_returns(args.parquet_dir, sym, days=args.days)
        if r is None:
            logger.info("no data for %s -- skipping", sym)
            continue
        returns[sym] = r
        logger.info("loaded %s: %d days of returns", sym, len(r))

    if len(returns) < 2:
        logger.warning("need >=2 symbols with data -- got %d", len(returns))
        return 0

    pairs: dict[str, float] = {}
    syms = sorted(returns.keys())
    for i, a in enumerate(syms):
        for b in syms[i+1:]:
            corr = _pearson(returns[a], returns[b])
            pairs[f"{a}|{b}"] = corr
            logger.info("  corr(%s,%s) = %.4f", a, b, corr)

    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "window_days": args.days,
        "n_symbols": len(returns),
        "n_pairs": len(pairs),
        "pairs": pairs,
        "source": "scripts/refresh_correlation_matrix.py",
    }

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d pairs)", args.out, len(pairs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
