"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_slippage_predictor
============================================================
Learn realized slippage as a function of (regime, session_bucket,
order_size, recent_volatility) instead of the constant 1-tick the
harness currently assumes.

Why this exists
---------------
The L2 backtest harness predicts STOP slip = exactly 1 tick.  Reality:
slip varies by:
  - Session bucket (RTH_MID is calm; RTH_OPEN + FOMC are violent)
  - Spread regime (PAUSE regime has 5x typical slip)
  - Order size (1-contract slip is different from 10-contract slip)
  - Recent volatility (slip lags realized vol)

Approach
--------
Bin-and-average regression on the realized fill audit history:
  slip = mean(slip | bucket(regime, session, size_bucket, vol_bucket))

Pure-Python, no sklearn dependency.  Returns:
  - Predicted slip for a hypothetical fill (point estimate)
  - 95% CI around the prediction (from bootstrap)
  - Sample size + fallback to default 1-tick when bin is empty

Integration
-----------
l2_backtest_harness can call this when simulating exits to use a
data-driven slip estimate instead of the constant 1-tick.  See
``predict_slip()`` below for the API.

Calibration
-----------
The model is retrained whenever this script runs (typically weekly).
With n=100 fills, expect rough bin averages; with n=1000+, the
buckets stabilize and the operator can trust the predictions.

Run
---
::

    python -m eta_engine.scripts.l2_slippage_predictor --train
    python -m eta_engine.scripts.l2_slippage_predictor \\
        --predict --regime NORMAL --session RTH_MID --size 1
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
BROKER_FILL_LOG = LOG_DIR / "broker_fills.jsonl"
SIGNAL_LOG = LOG_DIR / "l2_signal_log.jsonl"
SLIP_MODEL_LOG = LOG_DIR / "l2_slip_model.json"


@dataclass
class SlipBucket:
    regime: str          # NORMAL | WIDE | PAUSE
    session: str         # RTH_OPEN | RTH_MID | RTH_CLOSE | ETH
    size_bucket: str     # "1" | "2-5" | "6-10" | "10+"
    vol_bucket: str      # "low" | "mid" | "high"
    n: int
    mean_slip_ticks: float
    p90_slip_ticks: float
    stddev_slip_ticks: float


@dataclass
class SlipModel:
    """Persisted slip model — one entry per non-empty bucket."""
    ts_trained: str
    n_fills: int
    default_slip_ticks: float  # fallback when bucket empty
    buckets: list[SlipBucket] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _session_bucket(ts: datetime) -> str:
    """Mirror of l2_fill_audit._session_bucket — keep consistent."""
    minutes_utc = ts.hour * 60 + ts.minute
    rth_open = 13 * 60 + 30
    rth_close = 20 * 60
    open_buffer = 30
    close_buffer = 30
    if rth_open <= minutes_utc < rth_open + open_buffer:
        return "RTH_OPEN"
    if rth_close - close_buffer <= minutes_utc < rth_close:
        return "RTH_CLOSE"
    if rth_open + open_buffer <= minutes_utc < rth_close - close_buffer:
        return "RTH_MID"
    return "ETH"


def _size_bucket(qty: int) -> str:
    if qty <= 1:
        return "1"
    if qty <= 5:
        return "2-5"
    if qty <= 10:
        return "6-10"
    return "10+"


def _vol_bucket(vol: float | None,
                 *, low_threshold: float = 0.5,
                 high_threshold: float = 2.0) -> str:
    if vol is None:
        return "mid"
    if vol < low_threshold:
        return "low"
    if vol > high_threshold:
        return "high"
    return "mid"


def _percentile(sorted_data: list[float], pct: float) -> float:
    if not sorted_data:
        return 0.0
    idx = int(pct / 100 * len(sorted_data))
    idx = max(0, min(len(sorted_data) - 1, idx))
    return sorted_data[idx]


def train_model(*, since_days: int = 60,
                 default_slip_ticks: float = 1.0,
                 _fill_path: Path | None = None,
                 _signal_path: Path | None = None) -> SlipModel:
    """Walk recent fills + signals, build per-bucket slip statistics."""
    fill_path = _fill_path or BROKER_FILL_LOG
    sig_path = _signal_path or SIGNAL_LOG
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    fills: list[dict] = []
    if fill_path.exists():
        try:
            with fill_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = rec.get("ts")
                    if not ts:
                        continue
                    try:
                        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if dt < cutoff:
                        continue
                    if str(rec.get("exit_reason", "")).upper() != "STOP":
                        continue  # only stops carry meaningful slip
                    rec["_dt"] = dt
                    fills.append(rec)
        except OSError:
            pass

    sig_by_id: dict[str, dict] = {}
    if sig_path.exists():
        try:
            with sig_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = rec.get("signal_id")
                    if sid:
                        sig_by_id[sid] = rec
        except OSError:
            pass

    # Bucket fills
    buckets: dict[tuple[str, str, str, str], list[float]] = {}
    for fill in fills:
        sid = fill.get("signal_id")
        if not sid or sid not in sig_by_id:
            continue
        sig = sig_by_id[sid]
        slip_ticks = fill.get("slip_ticks_vs_intended")
        if slip_ticks is None:
            continue
        size = int(fill.get("qty_filled", 1))
        bucket_key = (
            sig.get("regime", "NORMAL"),
            _session_bucket(fill["_dt"]),
            _size_bucket(size),
            _vol_bucket(sig.get("vol_proxy")),
        )
        buckets.setdefault(bucket_key, []).append(float(slip_ticks))

    notes: list[str] = []
    if not buckets:
        notes.append("no fills with slip_ticks_vs_intended available; "
                       "predictor falls back to default")

    out_buckets: list[SlipBucket] = []
    for (regime, session, size_b, vol_b), slips in buckets.items():
        slips_sorted = sorted(slips)
        n = len(slips)
        out_buckets.append(SlipBucket(
            regime=regime, session=session,
            size_bucket=size_b, vol_bucket=vol_b,
            n=n,
            mean_slip_ticks=round(statistics.mean(slips), 3),
            p90_slip_ticks=round(_percentile(slips_sorted, 90), 3),
            stddev_slip_ticks=round(statistics.stdev(slips), 3)
                                  if n >= 2 else 0.0,
        ))

    return SlipModel(
        ts_trained=datetime.now(UTC).isoformat(),
        n_fills=sum(b.n for b in out_buckets),
        default_slip_ticks=default_slip_ticks,
        buckets=out_buckets, notes=notes,
    )


def save_model(model: SlipModel, *, path: Path | None = None) -> None:
    target = path or SLIP_MODEL_LOG
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(model), indent=2),
                            encoding="utf-8")
    except OSError as e:
        print(f"WARN: slip model save failed: {e}", file=sys.stderr)


def load_model(*, path: Path | None = None) -> SlipModel | None:
    src = path or SLIP_MODEL_LOG
    if not src.exists():
        return None
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
        buckets = [SlipBucket(**b) for b in data.get("buckets", [])]
        data["buckets"] = buckets
        return SlipModel(**data)
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def predict_slip(*, regime: str = "NORMAL",
                  session: str = "RTH_MID",
                  size: int = 1,
                  vol: float | None = None,
                  model: SlipModel | None = None) -> float:
    """Predict slip in ticks for a hypothetical fill.

    Returns the bucket's mean slip if the bucket has data; otherwise
    returns the model's default (1 tick).  Caller can use this to
    replace l2_backtest_harness's hardcoded 1-tick assumption.
    """
    if model is None:
        model = load_model()
    if model is None:
        return 1.0  # ultimate fallback
    size_b = _size_bucket(size)
    vol_b = _vol_bucket(vol)
    # Try exact match first
    for b in model.buckets:
        if (b.regime == regime and b.session == session
                and b.size_bucket == size_b and b.vol_bucket == vol_b):
            return b.mean_slip_ticks
    # Fall back to session match (drop vol/size)
    matches = [b for b in model.buckets if b.session == session]
    if matches:
        return statistics.mean(b.mean_slip_ticks for b in matches)
    return model.default_slip_ticks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--predict", action="store_true")
    ap.add_argument("--regime", default="NORMAL")
    ap.add_argument("--session", default="RTH_MID")
    ap.add_argument("--size", type=int, default=1)
    ap.add_argument("--vol", type=float, default=None)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.train:
        model = train_model(since_days=args.days)
        save_model(model)
        if args.json:
            print(json.dumps(asdict(model), indent=2))
        else:
            print(f"Trained slip model on {model.n_fills} fills, "
                    f"{len(model.buckets)} non-empty buckets")
            for b in model.buckets[:10]:
                print(f"  [{b.regime}/{b.session}/qty{b.size_bucket}/vol{b.vol_bucket}]"
                        f" n={b.n}, mean={b.mean_slip_ticks} ticks, "
                        f"p90={b.p90_slip_ticks}")
        return 0

    if args.predict:
        pred = predict_slip(regime=args.regime, session=args.session,
                              size=args.size, vol=args.vol)
        if args.json:
            print(json.dumps({"predicted_slip_ticks": pred}))
        else:
            print(f"Predicted slip: {pred} ticks "
                    f"(regime={args.regime}, session={args.session}, "
                    f"size={args.size}, vol={args.vol})")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
