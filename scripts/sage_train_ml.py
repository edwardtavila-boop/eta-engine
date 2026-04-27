"""Train + persist the sage ML school model (Wave-6, 2026-04-27).

Builds a (50-bar feature window) -> (50-bar-forward realized R label)
dataset from a bar source (parquet, CSV, or JSON), trains a
GradientBoostingClassifier (sklearn), and persists the trained pipeline
via joblib so the MLSchool inference path picks it up automatically
on next sage consult.

Usage::

    # CSV with open,high,low,close,volume,ts cols
    python scripts/sage_train_ml.py --csv mnq_5m.csv --out state/sage/ml_model.pkl

    # JSON list of bar dicts
    python scripts/sage_train_ml.py --bars state/bars/mnq_5m.json --out state/sage/ml_model.pkl

    # With custom forward-window + min sample threshold
    python scripts/sage_train_ml.py --csv mnq_5m.csv --forward-bars 30 --min-samples 200

The trained model expects the same 5-feature vector the MLSchool's
``_features()`` method computes:
    [recent_vol/baseline_vol, last_range/avg_range, last_vol_z,
     5-bar return %, 50-bar return %]

Labels:
    0 = SHORT winner (forward return < -0.001)
    1 = LONG winner  (forward return > +0.001)
    (rows where |forward return| < 0.001 are dropped; not actionable)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

logger = logging.getLogger("sage_train_ml")


def _load_bars_csv(path: Path) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                bars.append({
                    "open":   float(row["open"]),
                    "high":   float(row["high"]),
                    "low":    float(row["low"]),
                    "close":  float(row["close"]),
                    "volume": float(row.get("volume", 0)),
                    "ts":     row.get("ts") or row.get("timestamp") or "",
                })
            except (ValueError, KeyError) as exc:
                logger.debug("row skipped: %s", exc)
    return bars


def _load_bars_json(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _features_from_window(window: list[dict[str, Any]]) -> list[float]:
    """Same shape as MLSchool._features() so model is compatible."""
    closes = [float(b["close"]) for b in window]
    highs = [float(b["high"]) for b in window]
    lows = [float(b["low"]) for b in window]
    volumes = [float(b.get("volume", 0)) for b in window]
    rets = [(closes[i] - closes[i - 1]) / max(closes[i - 1], 1e-9)
            for i in range(1, len(closes))]
    recent_vol = sum(abs(r) for r in rets[-10:]) / 10
    baseline_vol = sum(abs(r) for r in rets) / len(rets)
    last_range = highs[-1] - lows[-1]
    avg_range = sum(h - lo for h, lo in zip(highs, lows, strict=True)) / len(highs)
    mean_vol = sum(volumes) / len(volumes)
    var_vol = sum((v - mean_vol) ** 2 for v in volumes) / len(volumes)
    sd_vol = max(var_vol ** 0.5, 1e-9)
    last_vol_z = (volumes[-1] - mean_vol) / sd_vol
    return [
        recent_vol / max(baseline_vol, 1e-9),
        last_range / max(avg_range, 1e-9),
        last_vol_z,
        sum(rets[-5:]) * 100,
        (closes[-1] - closes[0]) / max(closes[0], 1e-9) * 100,
    ]


def _build_dataset(
    bars: list[dict[str, Any]],
    *,
    window: int = 50,
    forward_bars: int = 50,
    min_abs_return: float = 0.001,
) -> tuple[list[list[float]], list[int]]:
    """Build (X, y) from rolling windows over the bar series.

    For each bar i in [window, len(bars) - forward_bars):
      * features = _features_from_window(bars[i-window:i])
      * label = 1 if (close[i+forward_bars] - close[i]) / close[i] > +min_abs_return
                0 if it is < -min_abs_return
                else skipped (not actionable)
    """
    X: list[list[float]] = []  # noqa: N806  -- sklearn idiom: capital X for feature matrix
    y: list[int] = []
    for i in range(window, len(bars) - forward_bars):
        try:
            feats = _features_from_window(bars[i - window : i])
        except (ZeroDivisionError, ValueError, KeyError):
            continue
        c_now = float(bars[i]["close"])
        c_fwd = float(bars[i + forward_bars]["close"])
        if c_now <= 0:
            continue
        fwd_ret = (c_fwd - c_now) / c_now
        if abs(fwd_ret) < min_abs_return:
            continue  # not actionable, skip
        label = 1 if fwd_ret > 0 else 0
        X.append(feats)
        y.append(label)
    return X, y


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", type=Path, help="CSV with open,high,low,close,volume cols")
    src.add_argument("--bars", type=Path, help="JSON list of bar dicts")
    p.add_argument("--out", type=Path,
                   default=Path("state/sage/ml_model.pkl"))
    p.add_argument("--window", type=int, default=50)
    p.add_argument("--forward-bars", type=int, default=50)
    p.add_argument("--min-abs-return", type=float, default=0.001)
    p.add_argument("--min-samples", type=int, default=200,
                   help="Minimum (X, y) samples needed to train")
    p.add_argument("--n-estimators", type=int, default=100)
    p.add_argument("--max-depth", type=int, default=4)
    p.add_argument("--learning-rate", type=float, default=0.05)
    p.add_argument("--test-split", type=float, default=0.20,
                   help="Holdout fraction for OOS accuracy report")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Lazy import sklearn + joblib so the script doesn't error at import
    # time when those deps aren't available.
    try:
        import joblib
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.model_selection import train_test_split
    except ImportError as exc:
        print(
            f"ERROR: scikit-learn + joblib are required to train.\n"
            f"  pip install scikit-learn joblib\n"
            f"  (raised: {exc})",
            file=sys.stderr,
        )
        return 1

    bars = _load_bars_csv(args.csv) if args.csv else _load_bars_json(args.bars)
    logger.info("loaded %d bars", len(bars))

    X, y = _build_dataset(  # noqa: N806  -- sklearn idiom: capital X for feature matrix
        bars,
        window=args.window,
        forward_bars=args.forward_bars,
        min_abs_return=args.min_abs_return,
    )
    logger.info("built %d samples (X, y); positive=%d negative=%d",
                len(X), sum(y), len(y) - sum(y))

    if len(X) < args.min_samples:
        logger.error("only %d samples -- need >= %d. Provide more bars or "
                     "lower --min-samples.", len(X), args.min_samples)
        return 1

    X_train, X_test, y_train, y_test = train_test_split(  # noqa: N806  -- sklearn idiom
        X, y, test_size=args.test_split, random_state=42, shuffle=False,
    )
    logger.info("train=%d test=%d", len(X_train), len(X_test))

    clf = GradientBoostingClassifier(
        n_estimators=args.n_estimators,
        max_depth=args.max_depth,
        learning_rate=args.learning_rate,
        random_state=42,
    )
    clf.fit(X_train, y_train)
    train_acc = clf.score(X_train, y_train)
    test_acc = clf.score(X_test, y_test)
    baseline = max(sum(y_test), len(y_test) - sum(y_test)) / len(y_test) if y_test else 0
    logger.info("train_acc=%.4f test_acc=%.4f baseline=%.4f",
                train_acc, test_acc, baseline)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(clf, args.out)
    logger.info("model persisted -> %s", args.out)

    # Print the metrics summary the operator can scan
    print()
    print("  === sage ML training summary ===")
    print(f"  bars loaded:    {len(bars)}")
    print(f"  samples built:  {len(X)} (pos={sum(y)}, neg={len(y) - sum(y)})")
    print(f"  train / test:   {len(X_train)} / {len(X_test)}")
    print(f"  train accuracy: {train_acc:.4f}")
    print(f"  test  accuracy: {test_acc:.4f}")
    print(f"  baseline:       {baseline:.4f}  (always-predict-majority)")
    print(f"  edge (vs baseline): {(test_acc - baseline) * 100:+.2f} pct points")
    print(f"  model -> {args.out}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
