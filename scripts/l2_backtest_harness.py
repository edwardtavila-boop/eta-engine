"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_backtest_harness
==========================================================
Phase-5 of the IBKR Pro upgrade path: replay tick + depth history
through a strategy to evaluate L2-aware edges before paper-soak.

Why this exists
---------------
Per docs/IBKR_PRO_DATA_INVENTORY.md Phase 5:
> L2 backtest harness — replay depth snapshots through Phase 3
> strategies for honest pre-live evaluation.

The existing ``strategy_creation_harness.py`` consumes BAR data
(open/high/low/close).  L2-aware strategies need to see ticks
and depth snapshots in chronological order, with the strategy
state machine receiving each event as it would in production.

This harness:
1. Reads tick + depth files for a symbol over a date range
2. Merges them into a single chronological event stream
3. Feeds events to a strategy (book_imbalance, spread_regime,
   l2_overlay-augmented strategies)
4. Tracks simulated PnL per signal — using PESSIMISTIC fills
   (stop fills 1 tick worse than stop level, target fills only
   if low <= target on a SHORT or high >= target on a LONG and
   not after stop is hit in the same bar)
5. Applies spread_regime_filter so backtest mirrors live behavior
   (was missing — backtest used to trade through wide-spread
   periods that the live strategy would skip)
6. Walk-forward split (train 70% / test 30%) with min-N gate so
   sharpe_proxy on a tiny sample doesn't drive promotion
7. Reports an L2-aware verdict consistent with the 5-light gate
   that the bar-based harness uses

Run
---
::

    # Backtest book_imbalance on MNQ for last 7 days of captures
    python -m eta_engine.scripts.l2_backtest_harness \\
        --strategy book_imbalance --symbol MNQ --days 7

    # Backtest with custom config (--json reports machine-readable
    # so the supercharge orchestrator can ingest verdicts):
    python -m eta_engine.scripts.l2_backtest_harness \\
        --strategy book_imbalance --symbol MNQ --days 7 \\
        --entry-threshold 2.0 --consecutive-snaps 5 --json

    # Skip walk-forward split (single-pass mode) — only when you have
    # < 30 trades AND want diagnostic output before tuning.
    python -m eta_engine.scripts.l2_backtest_harness --no-walk-forward
"""
# ruff: noqa: ANN001, ANN202
# Internal helpers are deliberately untyped on the entry-signal arg
# (different strategies emit different signal classes) and the
# context-manager return.
from __future__ import annotations

import argparse
import gzip
import json
import math
import random
import sys
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TICKS_DIR = ROOT.parent / "mnq_data" / "ticks"
DEPTH_DIR = ROOT.parent / "mnq_data" / "depth"
L2_BACKTEST_LOG = LOG_DIR / "l2_backtest_runs.jsonl"
# D-fix 2026-05-11: every (threshold, k, …) config tried gets appended to
# this log so we can compute Deflated Sharpe (Bailey/Lopez de Prado)
# retrospectively and detect multi-config overfitting.
CONFIG_SEARCH_LOG = LOG_DIR / "l2_harness_config_search.jsonl"


# B4: Symbol → (point_value_usd, tick_size, default_atr_pts) lookup.
# point_value: dollars per 1.0 price point on the ROUND-TRIP contract.
# tick_size:   smallest price increment.
# default_atr: realistic 1-min ATR for the symbol (used when realized
#              ATR computation is unavailable or has too few snaps).
# Verified vs CME / NYMEX / COMEX product specs as of 2026-05.
SYMBOL_SPECS: dict[str, dict[str, float]] = {
    # CME equity-index futures
    "MNQ":  {"point_value": 2.0,    "tick_size": 0.25,    "default_atr": 2.0},   # CME Micro Nasdaq
    "NQ":   {"point_value": 20.0,   "tick_size": 0.25,    "default_atr": 2.0},   # CME E-mini Nasdaq
    "MES":  {"point_value": 5.0,    "tick_size": 0.25,    "default_atr": 1.5},   # CME Micro S&P
    "ES":   {"point_value": 50.0,   "tick_size": 0.25,    "default_atr": 1.5},   # CME E-mini S&P
    "M2K":  {"point_value": 5.0,    "tick_size": 0.10,    "default_atr": 1.0},   # CME Micro Russell 2000
    "RTY":  {"point_value": 50.0,   "tick_size": 0.10,    "default_atr": 1.0},   # CME E-mini Russell 2000
    "MYM":  {"point_value": 0.50,   "tick_size": 1.0,     "default_atr": 25.0},  # CBOT Micro Dow
    "YM":   {"point_value": 5.0,    "tick_size": 1.0,     "default_atr": 25.0},  # CBOT E-mini Dow
    # COMEX metals
    "MGC":  {"point_value": 10.0,   "tick_size": 0.10,    "default_atr": 0.8},   # COMEX Micro Gold
    "GC":   {"point_value": 100.0,  "tick_size": 0.10,    "default_atr": 0.8},   # COMEX Gold
    "SIL":  {"point_value": 5000.0, "tick_size": 0.005,   "default_atr": 0.10},  # COMEX Micro Silver
    "SI":   {"point_value": 5000.0, "tick_size": 0.005,   "default_atr": 0.10},  # COMEX Silver
    "HG":   {"point_value": 25000.0, "tick_size": 0.0005, "default_atr": 0.010}, # COMEX Copper
    # NYMEX energy
    "MCL":  {"point_value": 100.0,  "tick_size": 0.01,    "default_atr": 0.15},  # NYMEX Micro Crude
    "CL":   {"point_value": 1000.0, "tick_size": 0.01,    "default_atr": 0.15},  # NYMEX Crude
    "QM":   {"point_value": 500.0,  "tick_size": 0.025,   "default_atr": 0.15},  # NYMEX E-mini Crude
    "NG":   {"point_value": 10000.0, "tick_size": 0.001,  "default_atr": 0.020}, # NYMEX Natural Gas
    "RB":   {"point_value": 42000.0, "tick_size": 0.0001, "default_atr": 0.005}, # NYMEX RBOB Gasoline
    "HO":   {"point_value": 42000.0, "tick_size": 0.0001, "default_atr": 0.005}, # NYMEX Heating Oil
    # CME FX
    "M6E":  {"point_value": 12.50,  "tick_size": 0.0001,  "default_atr": 0.0010},
    "6E":   {"point_value": 125000.0, "tick_size": 0.00005, "default_atr": 0.0010},
    "M6B":  {"point_value": 6.25,   "tick_size": 0.0001,  "default_atr": 0.0015},  # CME Micro GBP/USD
    "6B":   {"point_value": 62500.0, "tick_size": 0.0001, "default_atr": 0.0015},
    "M6J":  {"point_value": 1.25,   "tick_size": 0.0000005, "default_atr": 0.0000050},
    "6J":   {"point_value": 12500.0, "tick_size": 0.0000005, "default_atr": 0.0000050},
    "6A":   {"point_value": 100000.0, "tick_size": 0.0001, "default_atr": 0.0015},
    "6C":   {"point_value": 100000.0, "tick_size": 0.0001, "default_atr": 0.0015},
    # CME interest-rate
    "ZN":   {"point_value": 1000.0, "tick_size": 0.015625, "default_atr": 0.10},  # CBOT 10-yr T-Note
    "ZB":   {"point_value": 1000.0, "tick_size": 0.03125, "default_atr": 0.25},   # CBOT 30-yr T-Bond
    # CME crypto
    "MBT":  {"point_value": 0.10,   "tick_size": 5.0,     "default_atr": 200.0},  # CME Micro Bitcoin
    "BTC":  {"point_value": 5.0,    "tick_size": 5.0,     "default_atr": 200.0},  # CME Bitcoin
    "MET":  {"point_value": 0.10,   "tick_size": 0.50,    "default_atr": 15.0},   # CME Micro Ether
}
# Round-trip commission per contract in USD.  Approximate IBKR Pro
# rates incl exchange/clearing/regulatory fees.  Conservative.
COMMISSION_PER_RT_USD = 0.85


def get_spec(symbol: str) -> dict[str, float]:
    """Return SYMBOL_SPECS entry, raising on unknown so callers can't
    silently use the wrong point_value.  Strips trailing '1' (front-month
    suffix used by some capture scripts: MNQ1 → MNQ)."""
    base = symbol.rstrip("1") if symbol.endswith("1") and len(symbol) > 1 else symbol
    if base not in SYMBOL_SPECS:
        raise ValueError(
            f"Unknown SYMBOL_SPECS for {symbol!r}. "
            f"Add it to SYMBOL_SPECS in l2_backtest_harness.py."
        )
    return SYMBOL_SPECS[base]


@dataclass
class L2Trade:
    """One round-trip — entry + exit + PnL."""
    side: str             # "LONG" | "SHORT"
    entry_ts: str
    entry_price: float
    stop: float
    target: float
    exit_ts: str
    exit_price: float
    exit_reason: str      # "TARGET" | "STOP" | "EOD" | "TIMEOUT"
    pnl_points: float
    pnl_dollars: float          # gross (before commission)
    pnl_dollars_net: float      # after round-trip commission
    confidence: float
    signal_id: str = ""


@dataclass
class L2BacktestResult:
    """Per-symbol per-strategy backtest summary."""
    strategy: str
    symbol: str
    days: int
    n_snapshots: int
    n_signals: int
    n_trades: int
    n_wins: int
    win_rate: float
    total_pnl_points: float
    total_pnl_dollars: float        # gross
    total_pnl_dollars_net: float    # after commission
    avg_pnl_per_trade: float
    sharpe_proxy: float    # mean / std of per-trade R, NOT annualized
    sharpe_proxy_valid: bool       # False when n_trades < min_n_for_sharpe
    min_n_for_sharpe: int = 30
    point_value_usd: float = 2.0
    commission_per_rt_usd: float = COMMISSION_PER_RT_USD
    n_skipped_regime_pause: int = 0  # signals dropped by spread_regime
    walk_forward: dict | None = None  # train/test split summary
    # D-fix 2026-05-11: bootstrap CIs on win_rate + sharpe_proxy (1000
    # resamples) provide honest uncertainty bounds.  None when n_trades<5.
    win_rate_ci_95: tuple[float, float] | None = None
    sharpe_ci_95: tuple[float, float] | None = None
    bootstrap_n_resamples: int = 1000
    # D-fix: deflated sharpe correction for multi-config selection.
    # When >1 config has been tried against the same data window,
    # the operator's MAX-selected sharpe overstates true edge.
    deflated_sharpe: float | None = None
    n_configs_searched: int = 1
    trades: list[L2Trade] = field(default_factory=list)


def bootstrap_ci(values: list[float], *, n_resamples: int = 1000,
                 confidence: float = 0.95,
                 seed: int | None = None) -> tuple[float, float] | None:
    """Bootstrap confidence interval on the mean of ``values``.

    Returns (lower, upper) bounds at the given confidence level, or
    None when len(values) < 5 (sample too small for meaningful CI).

    Pure-Python implementation — no numpy dependency.  Uses 1000
    resamples by default (quant-review recommendation).
    """
    if len(values) < 5:
        return None
    rng = random.Random(seed)
    n = len(values)
    means: list[float] = []
    for _ in range(n_resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    alpha = (1.0 - confidence) / 2.0
    lo_idx = int(alpha * n_resamples)
    hi_idx = int((1.0 - alpha) * n_resamples) - 1
    lo_idx = max(0, min(n_resamples - 1, lo_idx))
    hi_idx = max(0, min(n_resamples - 1, hi_idx))
    return (round(means[lo_idx], 4), round(means[hi_idx], 4))


def bootstrap_sharpe_ci(per_trade_returns: list[float],
                        *, n_resamples: int = 1000,
                        confidence: float = 0.95,
                        seed: int | None = None) -> tuple[float, float] | None:
    """Bootstrap CI on the sharpe_proxy (m/std) of the per-trade R
    series.  Same resampling scheme as bootstrap_ci but computes
    sharpe per resample, not just mean."""
    if len(per_trade_returns) < 5:
        return None
    rng = random.Random(seed)
    n = len(per_trade_returns)
    sharpes: list[float] = []
    for _ in range(n_resamples):
        sample = [per_trade_returns[rng.randrange(n)] for _ in range(n)]
        m = sum(sample) / n
        var = sum((x - m) ** 2 for x in sample) / max(n - 1, 1)
        std = var ** 0.5
        sharpes.append(m / std if std > 0 else 0.0)
    sharpes.sort()
    alpha = (1.0 - confidence) / 2.0
    lo_idx = int(alpha * n_resamples)
    hi_idx = int((1.0 - alpha) * n_resamples) - 1
    lo_idx = max(0, min(n_resamples - 1, lo_idx))
    hi_idx = max(0, min(n_resamples - 1, hi_idx))
    return (round(sharpes[lo_idx], 4), round(sharpes[hi_idx], 4))


def deflated_sharpe_ratio(observed_sharpe: float, n_trials: int,
                          n_trades: int) -> float:
    """Bailey/Lopez de Prado deflated Sharpe correction.

    When the operator selects the BEST sharpe across N tried
    configurations, the observed sharpe overstates true edge.
    Deflated SR adjusts for selection bias.

    Reference: Bailey & Lopez de Prado, "The Deflated Sharpe Ratio:
    Correcting for Selection Bias, Backtest Overfitting, and
    Non-Normality" (2014).

    Simplified formula (assumes near-normal returns, no skew/kurt
    correction): DSR = SR_observed * sqrt(1 - sigma_SR^2 * Z(1-1/N))

    Where Z(1-1/N) is the inverse normal CDF at quantile (1 - 1/N).

    For practical use this returns a CONSERVATIVE estimate.  When
    n_trials=1, returns observed_sharpe unchanged.
    """
    if n_trials <= 1 or n_trades < 5:
        return observed_sharpe
    # Approx inverse normal CDF at p = 1 - 1/N using Beasley-Springer-Moro
    # approximation (good for our range; no scipy dependency).
    p = 1.0 - 1.0 / n_trials
    z = _norm_ppf(p)
    # Variance of the sharpe estimator under the null (zero edge):
    # sigma_SR^2 ≈ (1 + 0.5 * SR^2) / (T - 1) for T trades
    sigma_sr_sq = (1.0 + 0.5 * observed_sharpe ** 2) / max(n_trades - 1, 1)
    # Conservative deflation: subtract z * sigma_sr from observed
    deflation = z * math.sqrt(sigma_sr_sq)
    return round(observed_sharpe - deflation, 4)


def _norm_ppf(p: float) -> float:
    """Beasley-Springer-Moro inverse normal CDF.  Returns z such that
    P(Z <= z) = p for standard normal Z.  No scipy dependency."""
    # Coefficients for Beasley-Springer-Moro algorithm
    a = [-39.69683028665376, 220.9460984245205, -275.9285104469687,
         138.3577518672690, -30.66479806614716, 2.506628277459239]
    b = [-54.47609879822406, 161.5858368580409, -155.6989798598866,
         66.80131188771972, -13.28068155288572]
    c = [-0.007784894002430293, -0.3223964580411365, -2.400758277161838,
         -2.549732539343734, 4.374664141464968, 2.938163982698783]
    d = [0.007784695709041462, 0.3224671290700398, 2.445134137142996,
         3.754408661907416]
    p_low = 0.02425
    p_high = 1 - p_low
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)


def log_config_search(*, strategy: str, symbol: str, days: int,
                       config: dict, n_trades: int, sharpe_proxy: float,
                       sharpe_proxy_valid: bool,
                       win_rate: float,
                       total_pnl_dollars_net: float) -> None:
    """Append a one-line record to CONFIG_SEARCH_LOG.  Called once per
    harness invocation.  Retrospective analysis (e.g. deflated sharpe
    across N configs tried) reads this log.
    """
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "strategy": strategy,
        "symbol": symbol,
        "days": days,
        "config": config,
        "n_trades": n_trades,
        "sharpe_proxy": sharpe_proxy,
        "sharpe_proxy_valid": sharpe_proxy_valid,
        "win_rate": win_rate,
        "total_pnl_dollars_net": total_pnl_dollars_net,
    }
    try:
        with CONFIG_SEARCH_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: could not append config_search to {CONFIG_SEARCH_LOG}: {e}",
              file=sys.stderr)


def count_prior_configs_searched(strategy: str, symbol: str,
                                   *, since_days: int = 30) -> int:
    """Count how many DISTINCT (config, days) tuples have been tried
    against this strategy/symbol in the last ``since_days`` days.
    Returns 1 when log is empty (no prior search).  Used by deflated
    sharpe to correct for multi-config selection bias."""
    if not CONFIG_SEARCH_LOG.exists():
        return 1
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    seen: set[str] = set()
    try:
        with CONFIG_SEARCH_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("strategy") != strategy or rec.get("symbol") != symbol:
                    continue
                ts = rec.get("ts")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt < cutoff:
                    continue
                cfg = rec.get("config", {})
                # Key by sorted config items + days for de-dup
                key = json.dumps(cfg, sort_keys=True) + f"|days={rec.get('days')}"
                seen.add(key)
    except OSError:
        return 1
    return max(1, len(seen))


def _open_jsonl_maybe_gz(path: Path):
    """Return a context manager opening either .jsonl or .jsonl.gz.

    Caller is responsible for closing — typically used with a
    finally block.  See _iter_depth_snapshots for the canonical
    pattern; new callers should prefer using a `with` block via
    `contextlib.closing` if they don't need the conditional logic.
    """
    if path.exists():
        return path.open("r", encoding="utf-8")
    gz = path.with_suffix(path.suffix + ".gz")
    if gz.exists():
        return gzip.open(gz, "rt", encoding="utf-8")
    raise FileNotFoundError(f"neither {path} nor {gz} exists")


def _iter_depth_snapshots(symbol: str, start_date: datetime,
                          days: int) -> list[dict]:
    """Concatenate depth files for symbol over the date range, in
    chronological order."""
    snaps: list[dict] = []
    for offset in range(days):
        d = start_date + timedelta(days=offset)
        path = DEPTH_DIR / f"{symbol}_{d.strftime('%Y%m%d')}.jsonl"
        try:
            f = _open_jsonl_maybe_gz(path)
        except FileNotFoundError:
            continue
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    snaps.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            f.close()
    snaps.sort(key=lambda s: s.get("epoch_s", 0))
    return snaps


def _realized_atr_points(snapshots: list[dict], lookback: int = 20,
                          default: float = 1.0) -> float:
    """Compute realized 'ATR' (mean range) over the trailing N snaps
    using mid as the price reference (MNQ depth has no high/low fields).
    Returns ``default`` when not enough snaps to compute.

    This is admittedly a poor proxy for true bar-ATR — but better than
    the previous hardcoded ``atr=1.0`` because it scales with realized
    snapshot-to-snapshot volatility for the actual symbol/regime.
    """
    if len(snapshots) < lookback:
        return default
    mids = [float(s.get("mid", 0.0)) for s in snapshots[-lookback:]]
    if not mids or any(m == 0 for m in mids):
        return default
    rng = max(mids) - min(mids)
    return max(rng, default * 0.25)  # floor at 0.25 of default to avoid div-by-zero stops


def _simulate_exit_pessimistic(entry_signal, future_snaps: list[dict],
                                point_value: float = 2.0,
                                tick_size: float = 0.25,
                                max_bars: int = 60) -> L2Trade:
    """I1: Walk forward up to max_bars snapshots, exiting at target/stop/EOD
    using a PESSIMISTIC fill model:

      - STOP: fills one tick WORSE than stop (LONG: stop - tick;
        SHORT: stop + tick) — represents real slippage on stop-market
        orders.  When BOTH stop and target are touched in the same
        snap window, STOP wins (conservative tie-break).
      - TARGET: fills at target (limit order — assume queue position
        but no improvement).
      - TIMEOUT: fills at last seen mid (no slippage applied for
        timeout because operator could have used market or limit).
    """
    is_long = entry_signal.side.upper() in {"LONG", "BUY"}
    exit_reason = "TIMEOUT"
    exit_price = entry_signal.entry_price
    exit_ts = entry_signal.snapshot_ts
    for snap in future_snaps[:max_bars]:
        mid = float(snap.get("mid", 0.0))
        # We use the snap's spread to bracket the high/low of this tick
        # window (mid ± spread/2 is a rough proxy when no OHLC is in
        # the depth schema).  This is a coarse approximation but better
        # than treating mid as the only price visited in the window.
        spread = float(snap.get("spread", 0.0))
        snap_high = mid + spread / 2
        snap_low = mid - spread / 2
        snap_ts = str(snap.get("ts", ""))

        if is_long:
            stop_hit = snap_low <= entry_signal.stop
            target_hit = snap_high >= entry_signal.target
            if stop_hit and target_hit:
                # Tie-break: STOP wins (conservative)
                exit_reason = "STOP"
                exit_price = entry_signal.stop - tick_size
                exit_ts = snap_ts
                break
            if stop_hit:
                exit_reason = "STOP"
                exit_price = entry_signal.stop - tick_size
                exit_ts = snap_ts
                break
            if target_hit:
                exit_reason = "TARGET"
                exit_price = entry_signal.target
                exit_ts = snap_ts
                break
        else:
            stop_hit = snap_high >= entry_signal.stop
            target_hit = snap_low <= entry_signal.target
            if stop_hit and target_hit:
                exit_reason = "STOP"
                exit_price = entry_signal.stop + tick_size
                exit_ts = snap_ts
                break
            if stop_hit:
                exit_reason = "STOP"
                exit_price = entry_signal.stop + tick_size
                exit_ts = snap_ts
                break
            if target_hit:
                exit_reason = "TARGET"
                exit_price = entry_signal.target
                exit_ts = snap_ts
                break
        # Update exit_price/ts to last seen for TIMEOUT fallback
        exit_price = mid
        exit_ts = snap_ts

    pnl_points = (exit_price - entry_signal.entry_price) if is_long \
                 else (entry_signal.entry_price - exit_price)
    pnl_dollars = pnl_points * point_value
    pnl_dollars_net = pnl_dollars - COMMISSION_PER_RT_USD
    return L2Trade(
        side=entry_signal.side,
        entry_ts=str(entry_signal.snapshot_ts),
        entry_price=entry_signal.entry_price,
        stop=entry_signal.stop,
        target=entry_signal.target,
        exit_ts=exit_ts, exit_price=exit_price, exit_reason=exit_reason,
        pnl_points=round(pnl_points, 4),
        pnl_dollars=round(pnl_dollars, 2),
        pnl_dollars_net=round(pnl_dollars_net, 2),
        confidence=entry_signal.confidence,
        signal_id=getattr(entry_signal, "signal_id", ""),
    )


def _summarize(strategy: str, symbol: str, days: int,
                n_snapshots: int, trades: list[L2Trade],
                n_signals: int, n_skipped_regime: int,
                point_value: float,
                walk_forward: dict | None,
                min_n_for_sharpe: int = 30,
                n_configs_searched: int = 1,
                bootstrap_seed: int | None = 42) -> L2BacktestResult:
    n_trades = len(trades)
    n_wins = sum(1 for t in trades if t.pnl_points > 0)
    win_rate = n_wins / n_trades if n_trades else 0.0
    total_pts = sum(t.pnl_points for t in trades)
    total_dollars = sum(t.pnl_dollars for t in trades)
    total_net = sum(t.pnl_dollars_net for t in trades)
    avg = total_pts / n_trades if n_trades else 0.0
    per_trade_returns = [t.pnl_points for t in trades]
    if n_trades >= 2:
        # Sharpe-proxy on per-trade R returns (not annualized)
        m = avg
        var = sum((t.pnl_points - m) ** 2 for t in trades) / max(n_trades - 1, 1)
        std = var ** 0.5
        sharpe = m / std if std > 0 else 0.0
    else:
        sharpe = 0.0
    # D-fix: bootstrap CIs on win_rate + sharpe_proxy.
    # win_rate is a Bernoulli mean — bootstrap on the 0/1 series.
    win_indicators = [1.0 if t.pnl_points > 0 else 0.0 for t in trades]
    win_rate_ci = bootstrap_ci(win_indicators, seed=bootstrap_seed) if n_trades >= 5 else None
    sharpe_ci = bootstrap_sharpe_ci(per_trade_returns, seed=bootstrap_seed) if n_trades >= 5 else None
    # D-fix: deflated sharpe correction when multiple configs have been tried
    dsr = deflated_sharpe_ratio(sharpe, n_configs_searched, n_trades) \
            if n_trades >= 5 and n_configs_searched > 1 else None
    return L2BacktestResult(
        strategy=strategy, symbol=symbol, days=days,
        n_snapshots=n_snapshots, n_signals=n_signals,
        n_trades=n_trades, n_wins=n_wins, win_rate=round(win_rate, 3),
        total_pnl_points=round(total_pts, 4),
        total_pnl_dollars=round(total_dollars, 2),
        total_pnl_dollars_net=round(total_net, 2),
        avg_pnl_per_trade=round(avg, 4),
        sharpe_proxy=round(sharpe, 3),
        win_rate_ci_95=win_rate_ci,
        sharpe_ci_95=sharpe_ci,
        deflated_sharpe=dsr,
        n_configs_searched=n_configs_searched,
        sharpe_proxy_valid=(n_trades >= min_n_for_sharpe),
        min_n_for_sharpe=min_n_for_sharpe,
        point_value_usd=point_value,
        n_skipped_regime_pause=n_skipped_regime,
        walk_forward=walk_forward,
        trades=trades,
    )


def _replay_book_imbalance(snaps: list[dict], cfg, symbol: str,
                            *, apply_regime_filter: bool = True,
                            atr_lookback: int = 20) -> tuple[list, list[L2Trade], int]:
    """Inner replay loop, factored out so walk-forward can reuse."""
    from eta_engine.strategies.book_imbalance_strategy import (
        BookImbalanceState,
        evaluate_snapshot,
    )
    from eta_engine.strategies.spread_regime_filter import (
        SpreadRegimeConfig,
        SpreadRegimeState,
        update_spread_regime,
    )
    spec = get_spec(symbol)
    state = BookImbalanceState()
    regime_cfg = SpreadRegimeConfig()
    regime_state = SpreadRegimeState()

    rolling: deque = deque(maxlen=atr_lookback)

    signals: list = []
    trades: list[L2Trade] = []
    n_skipped_regime = 0
    for i, snap in enumerate(snaps):
        rolling.append(snap)
        regime = update_spread_regime(snap, regime_cfg, regime_state) if apply_regime_filter else None
        if regime is not None and regime["verdict"] in {"PAUSE", "STALE"}:
            n_skipped_regime += 1
            continue
        # I10: realized ATR replaces hardcoded 1.0
        atr = _realized_atr_points(list(rolling), lookback=atr_lookback,
                                    default=spec["default_atr"])
        sig = evaluate_snapshot(snap, cfg, state, atr=atr, symbol=symbol)
        if sig is not None:
            signals.append(sig)
            future = snaps[i + 1:]
            trades.append(_simulate_exit_pessimistic(
                sig, future,
                point_value=spec["point_value"],
                tick_size=spec["tick_size"],
            ))
    return signals, trades, n_skipped_regime


def run_book_imbalance(symbol: str, days: int, *,
                       entry_threshold: float, consecutive_snaps: int,
                       n_levels: int, atr_stop_mult: float,
                       rr_target: float,
                       walk_forward: bool = True,
                       min_n_for_sharpe: int = 30,
                       apply_regime_filter: bool = True,
                       log_config_search_flag: bool = True) -> L2BacktestResult:
    """Replay depth history through book_imbalance_strategy.

    I9: walk_forward=True splits snapshots 70/30 (chronological);
        first 70% replays for in-sample, last 30% for OOS.  The
        operator can promote ONLY when OOS sharpe_proxy_valid AND
        OOS sharpe >= 0.5 AND OOS n_trades >= min_n_for_sharpe.

    D-fix: every config gets logged to CONFIG_SEARCH_LOG so deflated
    sharpe can be computed retrospectively across many invocations.
    """
    from eta_engine.strategies.book_imbalance_strategy import BookImbalanceConfig
    cfg = BookImbalanceConfig(
        n_levels=n_levels,
        entry_threshold=entry_threshold,
        consecutive_snaps=consecutive_snaps,
        atr_stop_mult=atr_stop_mult,
        rr_target=rr_target,
    )
    # D-fix: count prior config invocations against same strategy/symbol
    n_configs_searched = count_prior_configs_searched(
        "book_imbalance", symbol) if log_config_search_flag else 1
    spec = get_spec(symbol)
    # Scan dates [now - (days-1), ..., now] inclusive of today.
    # Bug fix 2026-05-11: prior version started at `now - days` and
    # walked `days` offsets, missing today's data entirely.
    start = datetime.now(UTC) - timedelta(days=max(days - 1, 0))
    snaps = _iter_depth_snapshots(symbol, start, days)

    walk_summary: dict | None = None
    if walk_forward and len(snaps) >= 100:
        # 70/30 chronological split — train is in-sample (used for
        # tuning if anyone tunes against the digest), test is OOS.
        split_idx = int(len(snaps) * 0.70)
        train_snaps = snaps[:split_idx]
        test_snaps = snaps[split_idx:]
        train_sig, train_trades, train_skipped = _replay_book_imbalance(
            train_snaps, cfg, symbol,
            apply_regime_filter=apply_regime_filter)
        test_sig, test_trades, test_skipped = _replay_book_imbalance(
            test_snaps, cfg, symbol,
            apply_regime_filter=apply_regime_filter)
        # Build sub-summaries for the walk_summary dict
        train_res = _summarize(
            "book_imbalance", symbol, days,
            n_snapshots=len(train_snaps), trades=train_trades,
            n_signals=len(train_sig),
            n_skipped_regime=train_skipped,
            point_value=spec["point_value"],
            walk_forward=None,
            min_n_for_sharpe=min_n_for_sharpe,
        )
        test_res = _summarize(
            "book_imbalance", symbol, days,
            n_snapshots=len(test_snaps), trades=test_trades,
            n_signals=len(test_sig),
            n_skipped_regime=test_skipped,
            point_value=spec["point_value"],
            walk_forward=None,
            min_n_for_sharpe=min_n_for_sharpe,
        )
        walk_summary = {
            "split": "70/30 chronological",
            "train": {"n_snaps": train_res.n_snapshots,
                       "n_trades": train_res.n_trades,
                       "win_rate": train_res.win_rate,
                       "sharpe_proxy": train_res.sharpe_proxy,
                       "sharpe_proxy_valid": train_res.sharpe_proxy_valid,
                       "total_pnl_dollars_net": train_res.total_pnl_dollars_net},
            "test": {"n_snaps": test_res.n_snapshots,
                      "n_trades": test_res.n_trades,
                      "win_rate": test_res.win_rate,
                      "sharpe_proxy": test_res.sharpe_proxy,
                      "sharpe_proxy_valid": test_res.sharpe_proxy_valid,
                      "total_pnl_dollars_net": test_res.total_pnl_dollars_net},
            "promotion_gate": {
                "rule": "OOS sharpe_proxy_valid AND OOS sharpe >= 0.5 AND OOS n_trades >= min_n",
                "passes": (test_res.sharpe_proxy_valid
                            and test_res.sharpe_proxy >= 0.5
                            and test_res.n_trades >= min_n_for_sharpe),
            },
        }

    # Always also run the full-window replay for the headline numbers
    signals, trades, n_skipped_regime = _replay_book_imbalance(
        snaps, cfg, symbol,
        apply_regime_filter=apply_regime_filter)

    result = _summarize("book_imbalance", symbol, days,
                         n_snapshots=len(snaps), trades=trades,
                         n_signals=len(signals),
                         n_skipped_regime=n_skipped_regime,
                         point_value=spec["point_value"],
                         walk_forward=walk_summary,
                         min_n_for_sharpe=min_n_for_sharpe,
                         n_configs_searched=n_configs_searched)
    # D-fix: append this config to the audit log so the NEXT invocation
    # against the same strategy+symbol counts it for deflation
    if log_config_search_flag:
        log_config_search(
            strategy="book_imbalance", symbol=symbol, days=days,
            config={"entry_threshold": entry_threshold,
                     "consecutive_snaps": consecutive_snaps,
                     "n_levels": n_levels,
                     "atr_stop_mult": atr_stop_mult,
                     "rr_target": rr_target,
                     "apply_regime_filter": apply_regime_filter},
            n_trades=result.n_trades,
            sharpe_proxy=result.sharpe_proxy,
            sharpe_proxy_valid=result.sharpe_proxy_valid,
            win_rate=result.win_rate,
            total_pnl_dollars_net=result.total_pnl_dollars_net,
        )
    return result


def _replay_microprice(snaps: list[dict], cfg, symbol: str,
                        *, apply_regime_filter: bool = True,
                        atr_lookback: int = 20) -> tuple[list, list[L2Trade], int]:
    """Replay microprice_drift through the depth stream.  Uses the
    last snap's mid as the trade_price proxy (until real tick stream
    is wired alongside)."""
    from eta_engine.strategies.microprice_drift_strategy import (
        MicropriceState,
        evaluate_snapshot,
        update_trade_price,
    )
    from eta_engine.strategies.spread_regime_filter import (
        SpreadRegimeConfig,
        SpreadRegimeState,
        update_spread_regime,
    )
    spec = get_spec(symbol)
    state = MicropriceState()
    regime_cfg = SpreadRegimeConfig()
    regime_state = SpreadRegimeState()
    rolling: deque = deque(maxlen=atr_lookback)
    signals: list = []
    trades: list[L2Trade] = []
    n_skipped_regime = 0
    for i, snap in enumerate(snaps):
        rolling.append(snap)
        # Use prior mid as trade-print proxy
        if i > 0:
            update_trade_price(state, float(snaps[i - 1].get("mid", 0.0)))
        regime = update_spread_regime(snap, regime_cfg, regime_state) if apply_regime_filter else None
        if regime is not None and regime["verdict"] in {"PAUSE", "STALE"}:
            n_skipped_regime += 1
            continue
        atr = _realized_atr_points(list(rolling), lookback=atr_lookback,
                                    default=spec["default_atr"])
        sig = evaluate_snapshot(snap, cfg, state, atr=atr, symbol=symbol)
        if sig is not None:
            signals.append(sig)
            future = snaps[i + 1:]
            trades.append(_simulate_exit_pessimistic(
                sig, future,
                point_value=spec["point_value"],
                tick_size=spec["tick_size"],
            ))
    return signals, trades, n_skipped_regime


def _replay_aggressor_flow_from_l1_bars(bars: list[dict], cfg,
                                          symbol: str) -> tuple[list, list[L2Trade], int]:
    """Replay aggressor_flow over L1 bars produced by bar_builder_l1.

    Uses point_value/tick_size from SYMBOL_SPECS.  Note: aggressor flow
    is bar-based, not snap-based; trades are simulated against the next
    N bars using a coarse close-only proxy because depth snaps may not
    align with bar boundaries.  The spread_regime_filter does not apply
    here (no per-snap regime classification on bar data)."""
    from eta_engine.strategies.aggressor_flow_strategy import (
        AggressorFlowState,
        evaluate_bar,
    )
    spec = get_spec(symbol)
    state = AggressorFlowState()
    signals: list = []
    trades: list[L2Trade] = []
    # ATR from bars (high-low range over a 20-bar lookback)
    for i, bar in enumerate(bars):
        recent = bars[max(0, i - 20):i + 1]
        if len(recent) >= 2:
            atr = sum(float(b.get("high", 0)) - float(b.get("low", 0))
                       for b in recent) / len(recent)
        else:
            atr = spec["default_atr"]
        sig = evaluate_bar(bar, cfg, state, atr=max(atr, spec["default_atr"] * 0.25),
                            symbol=symbol)
        if sig is not None:
            signals.append(sig)
            # Build a snap-shaped exit-window from subsequent bars
            future_snaps = [{
                "mid": float(b.get("close", 0)),
                "spread": (float(b.get("high", 0)) - float(b.get("low", 0))) / 4,
                "ts": b.get("timestamp_utc", ""),
            } for b in bars[i + 1:]]
            trades.append(_simulate_exit_pessimistic(
                sig, future_snaps,
                point_value=spec["point_value"],
                tick_size=spec["tick_size"],
            ))
    return signals, trades, 0  # no regime filter applied


def run_microprice_drift(symbol: str, days: int, *,
                          drift_threshold_ticks: float = 2.0,
                          consecutive_snaps: int = 3,
                          atr_stop_mult: float = 1.5,
                          rr_target: float = 2.0,
                          walk_forward: bool = True,
                          min_n_for_sharpe: int = 30,
                          apply_regime_filter: bool = True,
                          log_config_search_flag: bool = True) -> L2BacktestResult:
    """Replay depth history through microprice_drift_strategy."""
    from eta_engine.strategies.microprice_drift_strategy import MicropriceConfig
    cfg = MicropriceConfig(
        drift_threshold_ticks=drift_threshold_ticks,
        consecutive_snaps=consecutive_snaps,
        atr_stop_mult=atr_stop_mult,
        rr_target=rr_target,
    )
    spec = get_spec(symbol)
    n_configs_searched = count_prior_configs_searched(
        "microprice_drift", symbol) if log_config_search_flag else 1
    start = datetime.now(UTC) - timedelta(days=max(days - 1, 0))
    snaps = _iter_depth_snapshots(symbol, start, days)

    walk_summary: dict | None = None
    if walk_forward and len(snaps) >= 100:
        split_idx = int(len(snaps) * 0.70)
        train_snaps = snaps[:split_idx]
        test_snaps = snaps[split_idx:]
        _, train_trades, train_skipped = _replay_microprice(
            train_snaps, cfg, symbol,
            apply_regime_filter=apply_regime_filter)
        _, test_trades, test_skipped = _replay_microprice(
            test_snaps, cfg, symbol,
            apply_regime_filter=apply_regime_filter)
        train_res = _summarize(
            "microprice_drift", symbol, days,
            n_snapshots=len(train_snaps), trades=train_trades,
            n_signals=len(train_trades), n_skipped_regime=train_skipped,
            point_value=spec["point_value"], walk_forward=None,
            min_n_for_sharpe=min_n_for_sharpe)
        test_res = _summarize(
            "microprice_drift", symbol, days,
            n_snapshots=len(test_snaps), trades=test_trades,
            n_signals=len(test_trades), n_skipped_regime=test_skipped,
            point_value=spec["point_value"], walk_forward=None,
            min_n_for_sharpe=min_n_for_sharpe)
        walk_summary = {
            "split": "70/30 chronological",
            "train": {"n_snaps": train_res.n_snapshots,
                       "n_trades": train_res.n_trades,
                       "win_rate": train_res.win_rate,
                       "sharpe_proxy": train_res.sharpe_proxy,
                       "sharpe_proxy_valid": train_res.sharpe_proxy_valid,
                       "total_pnl_dollars_net": train_res.total_pnl_dollars_net},
            "test": {"n_snaps": test_res.n_snapshots,
                      "n_trades": test_res.n_trades,
                      "win_rate": test_res.win_rate,
                      "sharpe_proxy": test_res.sharpe_proxy,
                      "sharpe_proxy_valid": test_res.sharpe_proxy_valid,
                      "total_pnl_dollars_net": test_res.total_pnl_dollars_net},
            "promotion_gate": {
                "rule": "OOS sharpe_proxy_valid AND OOS sharpe >= 0.5 AND OOS n_trades >= min_n",
                "passes": (test_res.sharpe_proxy_valid
                            and test_res.sharpe_proxy >= 0.5
                            and test_res.n_trades >= min_n_for_sharpe),
            },
        }

    signals, trades, n_skipped = _replay_microprice(
        snaps, cfg, symbol, apply_regime_filter=apply_regime_filter)
    result = _summarize("microprice_drift", symbol, days,
                         n_snapshots=len(snaps), trades=trades,
                         n_signals=len(signals),
                         n_skipped_regime=n_skipped,
                         point_value=spec["point_value"],
                         walk_forward=walk_summary,
                         min_n_for_sharpe=min_n_for_sharpe,
                         n_configs_searched=n_configs_searched)
    if log_config_search_flag:
        log_config_search(
            strategy="microprice_drift", symbol=symbol, days=days,
            config={"drift_threshold_ticks": drift_threshold_ticks,
                     "consecutive_snaps": consecutive_snaps,
                     "atr_stop_mult": atr_stop_mult,
                     "rr_target": rr_target},
            n_trades=result.n_trades,
            sharpe_proxy=result.sharpe_proxy,
            sharpe_proxy_valid=result.sharpe_proxy_valid,
            win_rate=result.win_rate,
            total_pnl_dollars_net=result.total_pnl_dollars_net,
        )
    return result


def _load_l1_bars(symbol: str, timeframe: str = "5m") -> list[dict]:
    """Load L1 bars produced by bar_builder_l1.  Returns empty list
    when the file doesn't exist (graceful pre-data behavior)."""
    import csv
    bars_path = ROOT.parent / "mnq_data" / "history_l1" / f"{symbol}_{timeframe}_l1.csv"
    if not bars_path.exists():
        return []
    bars: list[dict] = []
    try:
        with bars_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Cast numeric fields
                bars.append({
                    "timestamp_utc": row.get("timestamp_utc", ""),
                    "epoch_s": float(row.get("epoch_s", 0)),
                    "open": float(row.get("open", 0)),
                    "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)),
                    "close": float(row.get("close", 0)),
                    "volume_total": float(row.get("volume_total", 0)),
                    "volume_buy": float(row.get("volume_buy", 0)),
                    "volume_sell": float(row.get("volume_sell", 0)),
                    "n_trades": int(row.get("n_trades", 0)),
                })
    except OSError:
        return []
    return bars


def run_aggressor_flow(symbol: str, days: int, *,
                        window_bars: int = 10,
                        entry_threshold: float = 0.35,
                        consecutive_bars: int = 2,
                        atr_stop_mult: float = 1.0,
                        rr_target: float = 2.0,
                        timeframe: str = "5m",
                        log_config_search_flag: bool = True) -> L2BacktestResult:
    """Replay aggressor_flow over L1 bars.  Walk-forward is not
    applicable here because bar data comes pre-segmented; the harness
    just runs full-window."""
    from eta_engine.strategies.aggressor_flow_strategy import AggressorFlowConfig
    cfg = AggressorFlowConfig(
        window_bars=window_bars,
        entry_threshold=entry_threshold,
        consecutive_bars=consecutive_bars,
        atr_stop_mult=atr_stop_mult,
        rr_target=rr_target,
    )
    spec = get_spec(symbol)
    bars = _load_l1_bars(symbol, timeframe)
    # Filter to last `days` worth of bars by timestamp
    cutoff = (datetime.now(UTC) - timedelta(days=days)).timestamp()
    bars = [b for b in bars if b.get("epoch_s", 0) >= cutoff]
    n_configs_searched = count_prior_configs_searched(
        "aggressor_flow", symbol) if log_config_search_flag else 1
    signals, trades, n_skipped = _replay_aggressor_flow_from_l1_bars(
        bars, cfg, symbol)
    result = _summarize("aggressor_flow", symbol, days,
                         n_snapshots=len(bars),  # bars in this case
                         trades=trades,
                         n_signals=len(signals),
                         n_skipped_regime=n_skipped,
                         point_value=spec["point_value"],
                         walk_forward=None,
                         n_configs_searched=n_configs_searched)
    if log_config_search_flag:
        log_config_search(
            strategy="aggressor_flow", symbol=symbol, days=days,
            config={"window_bars": window_bars,
                     "entry_threshold": entry_threshold,
                     "consecutive_bars": consecutive_bars,
                     "atr_stop_mult": atr_stop_mult,
                     "rr_target": rr_target,
                     "timeframe": timeframe},
            n_trades=result.n_trades,
            sharpe_proxy=result.sharpe_proxy,
            sharpe_proxy_valid=result.sharpe_proxy_valid,
            win_rate=result.win_rate,
            total_pnl_dollars_net=result.total_pnl_dollars_net,
        )
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy",
                    choices=["book_imbalance", "microprice_drift", "aggressor_flow"],
                    default="book_imbalance")
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--entry-threshold", type=float, default=1.75)
    ap.add_argument("--consecutive-snaps", type=int, default=3)
    ap.add_argument("--n-levels", type=int, default=3)
    ap.add_argument("--atr-stop-mult", type=float, default=1.0)
    ap.add_argument("--rr-target", type=float, default=2.0)
    ap.add_argument("--no-walk-forward", action="store_true",
                    help="Disable train/test split (single-pass mode)")
    ap.add_argument("--no-regime-filter", action="store_true",
                    help="Disable spread_regime_filter (NOT recommended)")
    ap.add_argument("--min-n", type=int, default=30,
                    help="Minimum n_trades for sharpe_proxy_valid (default 30)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.strategy == "microprice_drift":
        result = run_microprice_drift(
            args.symbol, args.days,
            drift_threshold_ticks=args.entry_threshold * 2,  # different scale
            consecutive_snaps=args.consecutive_snaps,
            atr_stop_mult=args.atr_stop_mult,
            rr_target=args.rr_target,
            walk_forward=not args.no_walk_forward,
            min_n_for_sharpe=args.min_n,
            apply_regime_filter=not args.no_regime_filter,
        )
    elif args.strategy == "aggressor_flow":
        result = run_aggressor_flow(
            args.symbol, args.days,
            window_bars=10,
            entry_threshold=args.entry_threshold * 0.2,  # ratio scale
            consecutive_bars=args.consecutive_snaps,
            atr_stop_mult=args.atr_stop_mult,
            rr_target=args.rr_target,
        )
    else:  # book_imbalance (default)
        result = run_book_imbalance(
            args.symbol, args.days,
            entry_threshold=args.entry_threshold,
            consecutive_snaps=args.consecutive_snaps,
            n_levels=args.n_levels,
            atr_stop_mult=args.atr_stop_mult,
            rr_target=args.rr_target,
            walk_forward=not args.no_walk_forward,
            min_n_for_sharpe=args.min_n,
            apply_regime_filter=not args.no_regime_filter,
        )

    # Persist to L2 backtest log
    digest = {
        "ts": datetime.now(UTC).isoformat(),
        "strategy": result.strategy,
        "symbol": result.symbol,
        "days": result.days,
        "n_snapshots": result.n_snapshots,
        "n_signals": result.n_signals,
        "n_trades": result.n_trades,
        "n_skipped_regime_pause": result.n_skipped_regime_pause,
        "win_rate": result.win_rate,
        "total_pnl_dollars": result.total_pnl_dollars,
        "total_pnl_dollars_net": result.total_pnl_dollars_net,
        "sharpe_proxy": result.sharpe_proxy,
        "sharpe_proxy_valid": result.sharpe_proxy_valid,
        "min_n_for_sharpe": result.min_n_for_sharpe,
        "point_value_usd": result.point_value_usd,
        "commission_per_rt_usd": result.commission_per_rt_usd,
        "walk_forward": result.walk_forward,
        "config": {
            "entry_threshold": args.entry_threshold,
            "consecutive_snaps": args.consecutive_snaps,
            "n_levels": args.n_levels,
            "atr_stop_mult": args.atr_stop_mult,
            "rr_target": args.rr_target,
            "regime_filter": not args.no_regime_filter,
            "walk_forward": not args.no_walk_forward,
        },
    }
    try:
        with L2_BACKTEST_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(digest, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: could not write digest to {L2_BACKTEST_LOG}: {e}",
              file=sys.stderr)

    if args.json:
        out = asdict(result)
        out["trades"] = [asdict(t) for t in result.trades]
        print(json.dumps(out, indent=2))
    else:
        print(f"\nL2 backtest: {result.strategy} on {result.symbol} "
              f"over {result.days}d  (point_value=${result.point_value_usd}/pt)")
        print(f"  snapshots scanned : {result.n_snapshots:,}")
        print(f"  signals emitted   : {result.n_signals}")
        print(f"  skipped (regime)  : {result.n_skipped_regime_pause}")
        print(f"  trades simulated  : {result.n_trades}")
        print(f"  wins              : {result.n_wins}  ({result.win_rate*100:.1f}%)")
        print(f"  total P&L gross   : {result.total_pnl_points:+.2f} pts  "
              f"(${result.total_pnl_dollars:+.2f})")
        print(f"  total P&L net     : ${result.total_pnl_dollars_net:+.2f}  "
              f"(after ${COMMISSION_PER_RT_USD:.2f}/RT commission)")
        print(f"  avg / trade       : {result.avg_pnl_per_trade:+.4f} pts")
        sharpe_label = f"{result.sharpe_proxy:+.3f}"
        if not result.sharpe_proxy_valid:
            sharpe_label += f"  [INSUFFICIENT_SAMPLE: n_trades<{result.min_n_for_sharpe}]"
        if result.sharpe_ci_95:
            sharpe_label += f"  95% CI=[{result.sharpe_ci_95[0]:+.3f}, {result.sharpe_ci_95[1]:+.3f}]"
        print(f"  sharpe-proxy      : {sharpe_label}")
        if result.win_rate_ci_95:
            print(f"  win_rate 95% CI   : [{result.win_rate_ci_95[0]:.3f}, {result.win_rate_ci_95[1]:.3f}]")
        if result.deflated_sharpe is not None:
            print(f"  deflated sharpe   : {result.deflated_sharpe:+.3f}  "
                  f"(after correcting for n_configs_searched={result.n_configs_searched})")
        elif result.n_configs_searched > 1:
            print(f"  configs searched  : {result.n_configs_searched}  "
                  f"(deflation skipped: insufficient sample)")
        if result.walk_forward:
            wf = result.walk_forward
            print(f"  walk-forward      : {wf['split']}")
            print(f"    train  n_trades={wf['train']['n_trades']}  "
                  f"win={wf['train']['win_rate']*100:.1f}%  "
                  f"sharpe={wf['train']['sharpe_proxy']:+.3f}  "
                  f"net=${wf['train']['total_pnl_dollars_net']:+.2f}")
            print(f"    test   n_trades={wf['test']['n_trades']}  "
                  f"win={wf['test']['win_rate']*100:.1f}%  "
                  f"sharpe={wf['test']['sharpe_proxy']:+.3f}  "
                  f"net=${wf['test']['total_pnl_dollars_net']:+.2f}")
            gate = wf['promotion_gate']
            print(f"  promotion gate    : {'PASS' if gate['passes'] else 'FAIL'}  "
                  f"({gate['rule']})")
        if result.n_snapshots == 0:
            print()
            print("  NOTE: no depth snapshots found — start Phase-1 capture")
            print("        daemons on the VPS and wait for data to accumulate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
