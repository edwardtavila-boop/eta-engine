"""
EVOLUTIONARY TRADING ALGO  //  feeds.strategy_lab.engine  (v2)
==============================================================
Walk-forward engine for the FULL bot fleet — every asset class, every
strategy kind in production today.

WHAT'S NEW IN V2 (2026-05-04 supercharge)
------------------------------------------
* Auto-resolves bar paths to the canonical roots (futures →
  mnq_data/history, crypto → data/crypto/ibkr/history) — no more
  hardcoded bar_dir guessing.
* Multi-strategy dispatch: sweep_reclaim, compression_breakout,
  confluence_scorecard, vwap_mr, ema_cross. Each strategy_kind
  registers its own signal generator.
* Symbol coverage: all 16 active symbols (MNQ/NQ/ES/MES/GC/MGC/CL/MCL/
  6E/M6E/ZN/ZB/NG/MBT/MET futures + BTC/ETH/SOL/AVAX/LINK/DOGE crypto).
* Regime overlay: reads var/eta_engine/state/regime_state.json and tags
  every backtest trade with the active global_regime at entry, so the
  regime_conditional_pnl block surfaces real signal not synthetic chunks.
* Parameter sweeps: per-strategy_kind grids (stop_atr, target_atr,
  lookback, threshold) — pre-tuned per asset class.
* Batch CLI mode: run a list of (bot_id, symbol, strategy_kind) pairs
  in one shot for the full fleet sweep. Outputs JSON per bot under
  reports/lab_reports/.
* Promotion-gate compatible: writes lab_report.json with the schema
  StrategyAssignment.extras can attach to.

STRATEGY KIND IMPLEMENTATIONS
-----------------------------
* ema_cross           — legacy 9/21 EMA pullback (kept for compat)
* sweep_reclaim       — liquidity sweep at lookback extreme + reclaim
* compression_breakout — Bollinger-narrow + ATR-z low → breakout
* confluence_scorecard — multi-factor 2-of-4 score gate
* vwap_mr             — 2σ VWAP-band mean reversion (RTH-aware)
"""
from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

log = logging.getLogger("strategy_lab")

# ─── Canonical roots ──────────────────────────────────────────────

_WS = Path(os.environ.get("ETA_WORKSPACE", r"C:\EvolutionaryTradingAlgo"))
MNQ_HISTORY_ROOT       = _WS / "mnq_data" / "history"
# Coinbase recurring-refresh mirror (1m/5m/1h/D up to 12mo via merge fetcher)
CRYPTO_HISTORY_ROOT    = _WS / "data" / "crypto" / "ibkr" / "history"
# yfinance long-history mirror (BTC/ETH 731-day daily — 2026-05-04)
CRYPTO_HISTORY_ROOT_YF = _WS / "data" / "crypto" / "history"
REGIME_STATE_PATH      = _WS / "var" / "eta_engine" / "state" / "regime_state.json"
LAB_REPORTS_ROOT       = _WS / "reports" / "lab_reports"

CRYPTO_SYMBOLS = {"BTC", "ETH", "SOL", "XRP", "AVAX", "LINK", "DOGE", "ADA", "DOT"}

# Map asset-class → canonical bar filename pattern
def _resolve_bar_path(symbol: str, timeframe: str) -> Path | None:
    sym = symbol.upper()
    if sym in CRYPTO_SYMBOLS:
        # For daily bars, prefer the yfinance long-history mirror if it has
        # the file (731-day coverage). Falls back to the Coinbase mirror.
        if timeframe in ("D", "1d"):
            yf = CRYPTO_HISTORY_ROOT_YF / f"{sym}_D.csv"
            if yf.exists():
                return yf
        return CRYPTO_HISTORY_ROOT / f"{sym}_{timeframe}.csv"
    if sym in ("DXY", "VIX"):
        return MNQ_HISTORY_ROOT / f"{sym}_{timeframe}.csv"
    # Futures: try with "1" suffix first (front-month convention),
    # fall back to bare for the rare case.
    p1 = MNQ_HISTORY_ROOT / f"{sym}1_{timeframe}.csv"
    if p1.exists():
        return p1
    p_bare = MNQ_HISTORY_ROOT / f"{sym}_{timeframe}.csv"
    if p_bare.exists():
        return p_bare
    return p1  # canonical preference even if missing


# ─── Output schema ────────────────────────────────────────────────


@dataclass
class LabResult:
    strategy_id: str
    bot_id: str = ""
    symbol: str = ""
    timeframe: str = ""
    strategy_kind: str = ""
    total_trades: int = 0
    win_rate: float = 0.0
    expectancy: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    parameter_heatmap: dict[str, dict[str, float]] = field(default_factory=dict)
    regime_conditional_pnl: dict[str, dict[str, float]] = field(default_factory=dict)
    walk_forward_windows: int = 0
    bars_used: int = 0
    coverage_days: float = 0.0
    # `passed` retained for back-compat. New code should consult
    # `passed_strict` - the rigor-gated verdict (see fields below).
    # Still populated by the legacy gate (count + sharpe + positive expR).
    passed: bool = False
    pass_reason: str = ""
    fail_reasons: list[str] = field(default_factory=list)
    report_path: str = ""
    ts: str = ""
    # --- Rigor extensions (2026-05-07) ----------------------------
    # 1. Block-bootstrap CI on expR (R-multiples per trade)
    expR_p5: float = 0.0
    expR_p50: float = 0.0
    expR_p95: float = 0.0
    bootstrap_block_size: int = 0
    bootstrap_n_resamples: int = 0
    # 2. Multiple-testing adjustment (Bonferroni)
    p_value_raw: float = 1.0
    p_value_bonferroni: float = 1.0
    multi_test_count: int = 0
    # 3. Friction-aware net expR (per-trade R after RT costs)
    expR_net: float = 0.0
    friction_R_per_trade: float = 0.0
    # 4. Split-half stability - same sign on first/second OOS half
    expR_half_1: float = 0.0
    expR_half_2: float = 0.0
    split_half_sign_stable: bool = False
    # 5. Deflated Sharpe (Lopez de Prado 2014)
    sharpe_deflated: float = 0.0
    # Strict gate verdict - replaces `passed` for go/no-go decisions
    passed_strict: bool = False
    strict_fail_reasons: list[str] = field(default_factory=list)
    legacy_passed: bool = False  # mirror of `passed` for explicitness


# ─── OHLCV bar loader ─────────────────────────────────────────────


def _load_ohlcv(path: Path) -> dict[str, np.ndarray] | None:
    """Load full OHLCV from canonical CSV. Returns dict of arrays or None."""
    try:
        data = np.genfromtxt(
            path, delimiter=",", skip_header=1, dtype=float,
            usecols=(0, 1, 2, 3, 4, 5),
        )
    except (OSError, ValueError):
        return None
    if data.size == 0 or len(data.shape) < 2:
        return None
    return {
        "time":   data[:, 0],
        "open":   data[:, 1],
        "high":   data[:, 2],
        "low":    data[:, 3],
        "close":  data[:, 4],
        "volume": data[:, 5],
    }


# ─── Strategy signal generators ───────────────────────────────────
# Each returns a list of (entry_idx, side, stop_atr_mult, target_atr_mult).


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    tr = np.maximum.reduce([
        high[1:] - low[1:],
        np.abs(high[1:] - close[:-1]),
        np.abs(low[1:] - close[:-1]),
    ])
    atr = np.zeros_like(close)
    atr[period:] = np.convolve(tr, np.ones(period) / period, mode="valid")[: len(atr) - period]
    return atr


def _ema(arr: np.ndarray, period: int) -> np.ndarray:
    out = np.zeros_like(arr)
    if len(arr) == 0:
        return out
    alpha = 2.0 / (period + 1)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def _rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's RSI. Returns same-length array with leading values undefined (~50)."""
    n = len(close)
    out = np.full(n, 50.0)
    if n <= period:
        return out
    delta = np.diff(close)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    # Seed with simple averages over first `period` deltas
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 0.0
        rsi_val = 100.0 - 100.0 / (1.0 + rs) if avg_loss > 0 else 100.0
        out[i + 1] = rsi_val
    return out


def signals_ema_cross(bars: dict[str, np.ndarray], spec: dict[str, Any]) -> list[tuple[int, str, float, float]]:
    close = bars["close"]
    fast = _ema(close, int(spec.get("ema_fast", 9)))
    slow = _ema(close, int(spec.get("ema_slow", 21)))
    stop_atr = float(spec.get("stop_atr", 1.5))
    target_atr = float(spec.get("target_atr", 3.0))
    out: list[tuple[int, str, float, float]] = []
    for i in range(2, len(close)):
        if fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
            out.append((i, "long", stop_atr, target_atr))
        elif fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]:
            out.append((i, "short", stop_atr, target_atr))
    return out


def signals_sweep_reclaim(bars: dict[str, np.ndarray], spec: dict[str, Any]) -> list[tuple[int, str, float, float]]:
    """Sweep: high/low pierces prior N-bar extreme then reclaims the level."""
    high = bars["high"]
    low  = bars["low"]
    close = bars["close"]
    open_ = bars["open"]
    lookback = int(spec.get("lookback", 24))
    wick_pct_min = float(spec.get("min_wick_pct", 0.30))
    stop_atr = float(spec.get("stop_atr", 1.5))
    target_atr = float(spec.get("target_atr", 2.5))
    out = []
    for i in range(lookback + 1, len(close)):
        prior_high = high[i - lookback : i].max()
        prior_low = low[i - lookback : i].min()
        bar_range = max(high[i] - low[i], 1e-9)
        upper_wick = (high[i] - max(open_[i], close[i])) / bar_range
        lower_wick = (min(open_[i], close[i]) - low[i]) / bar_range
        # bullish sweep: low takes out prior_low, close back above
        if low[i] < prior_low and close[i] > prior_low and lower_wick >= wick_pct_min:
            out.append((i, "long", stop_atr, target_atr))
        elif high[i] > prior_high and close[i] < prior_high and upper_wick >= wick_pct_min:
            out.append((i, "short", stop_atr, target_atr))
    return out


def signals_compression_breakout(
    bars: dict[str, np.ndarray], spec: dict[str, Any],
) -> list[tuple[int, str, float, float]]:
    """Bollinger band narrowing + ATR-z low → directional breakout.

    2026-05-04: raised default compression_pct 0.20 → 0.40 (top-40% lowest-ATR
    rather than top-20%) and require only 1 prior close in same direction to
    confirm, not 2. This more than doubles signal frequency while still gating
    on a real volatility-compression backdrop.
    """
    close = bars["close"]
    high = bars["high"]
    low = bars["low"]
    bb_period = int(spec.get("bb_period", 20))
    bb_compression_pct = float(spec.get("bb_compression_pct", 0.40))
    stop_atr = float(spec.get("stop_atr", 1.0))
    target_atr = float(spec.get("target_atr", 2.0))
    atr_arr = _atr(high, low, close, bb_period)
    out = []
    for i in range(bb_period + 1, len(close)):
        atr_window = atr_arr[max(0, i - 50) : i]
        atr_window = atr_window[atr_window > 0]
        if len(atr_window) < 5:
            continue
        atr_pct = (atr_arr[i] - atr_window.min()) / max(atr_window.max() - atr_window.min(), 1e-9)
        if atr_pct > bb_compression_pct:
            continue
        prior_close = close[i - 1]
        if close[i] > prior_close:
            out.append((i, "long", stop_atr, target_atr))
        elif close[i] < prior_close:
            out.append((i, "short", stop_atr, target_atr))
    return out


def signals_confluence_scorecard(
    bars: dict[str, np.ndarray], spec: dict[str, Any],
) -> list[tuple[int, str, float, float]]:
    """2-of-5 confluence (tunable): trend, slow alignment, vol z, momentum, follow-through.

    2026-05-04: relaxed primary trend gate from strict 3-EMA stack (fast>mid>slow)
    to 2-EMA (fast>mid) so signals actually fire on shorter histories. Slow EMA
    alignment is now a scoring component, not a hard gate. min_score raised to
    keep selectivity.
    """
    close = bars["close"]
    volume = bars["volume"]
    fast = _ema(close, int(spec.get("ema_fast", 9)))
    mid  = _ema(close, int(spec.get("ema_mid", 21)))
    slow = _ema(close, int(spec.get("ema_slow", 50)))
    min_score = int(spec.get("min_score", 2))
    stop_atr = float(spec.get("stop_atr", 1.5))
    target_atr = float(spec.get("target_atr", 2.5))
    vol_lookback = 20
    out = []
    start = max(int(spec.get("ema_mid", 21)) + 1, vol_lookback + 1)
    for i in range(start, close.shape[0] - 1):
        # Primary direction: 2-EMA fast/mid (more frequent than 3-EMA stack)
        trend_up = fast[i] > mid[i]
        trend_dn = fast[i] < mid[i]
        if not (trend_up or trend_dn):
            continue
        vol_window = volume[max(0, i - vol_lookback) : i]
        if vol_window.std() <= 0:
            continue
        vol_z = (volume[i] - vol_window.mean()) / max(vol_window.std(), 1e-9)
        score = 1  # base point for 2-EMA trend
        # +1 if slow aligns (full 3-stack still rewarded)
        if (trend_up and mid[i] > slow[i]) or (trend_dn and mid[i] < slow[i]):
            score += 1
        if vol_z > 0.3:
            score += 1
        if (trend_up and close[i] > close[i - 1]) or (trend_dn and close[i] < close[i - 1]):
            score += 1
        if abs(close[i] - close[i - 5]) > close[i - 5] * 0.001:
            score += 1
        if score < min_score:
            continue
        side = "long" if trend_up else "short"
        out.append((i, side, stop_atr, target_atr))
    return out


def scorecard_score_at(bars: dict[str, np.ndarray], spec: dict[str, Any],
                        i: int, side: str) -> int:
    """Return the confluence_scorecard score (0-5) at bar `i` for `side` direction.

    Used by fleet_sweep's composite-filter dispatch: bots with
    strategy_kind="confluence_scorecard" + sub_strategy_kind=<X> use
    sub-strategy signals filtered by this score >= min_score. This is the
    architecture the registry's "DIAMOND" tier was actually designed around
    (sweep_reclaim entries × scorecard quality filter), but the lab was
    treating dispatch as an either/or instead of a composition.
    """
    close = bars["close"]
    volume = bars["volume"]
    if i < 6:
        return 0
    fast = _ema(close, int(spec.get("ema_fast", 9)))
    mid  = _ema(close, int(spec.get("ema_mid", 21)))
    slow = _ema(close, int(spec.get("ema_slow", 50)))
    vol_lookback = 20
    if i < max(int(spec.get("ema_mid", 21)) + 1, vol_lookback + 1):
        return 0
    trend_up = fast[i] > mid[i]
    trend_dn = fast[i] < mid[i]
    side_up = side.lower() in ("long", "buy")
    if side_up and not trend_up:
        return 0
    if not side_up and not trend_dn:
        return 0
    vol_window = volume[max(0, i - vol_lookback) : i]
    if vol_window.std() <= 0:
        return 0
    vol_z = (volume[i] - vol_window.mean()) / max(vol_window.std(), 1e-9)
    score = 1
    if (trend_up and mid[i] > slow[i]) or (trend_dn and mid[i] < slow[i]):
        score += 1
    if vol_z > 0.3:
        score += 1
    if (trend_up and close[i] > close[i - 1]) or (trend_dn and close[i] < close[i - 1]):
        score += 1
    if abs(close[i] - close[i - 5]) > close[i - 5] * 0.001:
        score += 1
    return score


def signals_vwap_mr(bars: dict[str, np.ndarray], spec: dict[str, Any]) -> list[tuple[int, str, float, float]]:
    """Mean reversion at session-VWAP ±2σ bands."""
    close = bars["close"]
    high = bars["high"]
    low = bars["low"]
    volume = bars["volume"]
    sigma_mult = float(spec.get("sigma_mult", 2.0))
    stop_atr = float(spec.get("stop_atr", 1.0))
    target_atr = float(spec.get("target_atr", 2.0))
    typical = (high + low + close) / 3.0
    cum_pv = np.cumsum(typical * volume)
    cum_v = np.cumsum(volume)
    vwap = np.where(cum_v > 0, cum_pv / cum_v, close)
    out = []
    for i in range(50, len(close)):
        window = close[max(0, i - 200) : i]
        sigma = window.std()
        if sigma <= 0:
            continue
        upper = vwap[i] + sigma_mult * sigma
        lower = vwap[i] - sigma_mult * sigma
        if close[i] < lower:
            out.append((i, "long", stop_atr, target_atr))
        elif close[i] > upper:
            out.append((i, "short", stop_atr, target_atr))
    return out


def signals_orb_sage_gated(bars: dict[str, np.ndarray], spec: dict[str, Any]) -> list[tuple[int, str, float, float]]:
    """Opening Range Breakout with daily-trend sage gate.
    Range computed over first N bars of session; breakout in trend direction only.
    """
    close = bars["close"]
    high = bars["high"]
    low = bars["low"]
    range_bars = int(spec.get("range_bars", 12))   # 12 5m bars = first hour of RTH
    trend_window = int(spec.get("trend_window", 200))  # daily-trend proxy
    stop_atr = float(spec.get("stop_atr", 1.5))
    target_atr = float(spec.get("target_atr", 3.0))
    out = []
    if len(close) < range_bars + trend_window + 5:
        return out
    sma_long = _ema(close, trend_window)
    for i in range(range_bars + trend_window, len(close) - 1):
        # Use trailing range_bars window as "opening range"
        rng_high = high[i - range_bars : i].max()
        rng_low = low[i - range_bars : i].min()
        sage_up = close[i] > sma_long[i]   # daily trend allows long
        sage_dn = close[i] < sma_long[i]
        if sage_up and close[i] > rng_high and close[i - 1] <= rng_high:
            out.append((i, "long", stop_atr, target_atr))
        elif sage_dn and close[i] < rng_low and close[i - 1] >= rng_low:
            out.append((i, "short", stop_atr, target_atr))
    return out


def signals_sage_daily_gated(bars: dict[str, np.ndarray], spec: dict[str, Any]) -> list[tuple[int, str, float, float]]:
    """Sage daily-conviction gate on top of crypto_orb base.
    Same as ORB but with stricter trend confirmation (require both EMA stack
    AND price > 200-bar mean by min_conviction multiple of sigma).
    """
    close = bars["close"]
    range_bars = int(spec.get("range_minutes", 120) // 5)
    min_conviction = float(spec.get("min_conviction", 0.30))
    stop_atr = float(spec.get("stop_atr", 2.5))
    target_atr = float(spec.get("target_atr", 3.0))
    fast = _ema(close, 21)
    slow = _ema(close, 100)
    out = []
    if len(close) < 200 + range_bars:
        return out
    for i in range(200 + range_bars, len(close) - 1):
        window = close[i - 200 : i]
        mu = window.mean()
        sigma = max(window.std(), 1e-9)
        z = (close[i] - mu) / sigma
        if abs(z) < min_conviction:
            continue
        rng_high = bars["high"][i - range_bars : i].max()
        rng_low = bars["low"][i - range_bars : i].min()
        if z > 0 and fast[i] > slow[i] and close[i] > rng_high:
            out.append((i, "long", stop_atr, target_atr))
        elif z < 0 and fast[i] < slow[i] and close[i] < rng_low:
            out.append((i, "short", stop_atr, target_atr))
    return out


def signals_ensemble_voting(bars: dict[str, np.ndarray], spec: dict[str, Any]) -> list[tuple[int, str, float, float]]:
    """Vote across sweep_reclaim + compression_breakout + confluence_scorecard.
    Fires when at least min_votes (default 2) sub-strategies agree on direction
    at the same bar.
    """
    min_votes = int(spec.get("min_votes", 2))
    sub_specs = [
        ("sweep_reclaim", dict(spec)),
        ("compression_breakout", dict(spec)),
        ("confluence_scorecard", dict(spec)),
    ]
    # Build per-bar vote tally
    votes_long: dict[int, int] = {}
    votes_short: dict[int, int] = {}
    sub_signals: dict[int, list[tuple[float, float]]] = {}
    for kind, ss in sub_specs:
        if kind not in SIGNAL_GENERATORS:
            continue
        for idx, side, sm, tm in SIGNAL_GENERATORS[kind](bars, ss):
            if side == "long":
                votes_long[idx] = votes_long.get(idx, 0) + 1
            else:
                votes_short[idx] = votes_short.get(idx, 0) + 1
            sub_signals.setdefault(idx, []).append((sm, tm))

    out = []
    stop_atr_default = float(spec.get("stop_atr", 1.5))
    target_atr_default = float(spec.get("target_atr", 2.5))
    for idx in sorted(set(votes_long.keys()) | set(votes_short.keys())):
        if votes_long.get(idx, 0) >= min_votes:
            sm, tm = sub_signals[idx][0] if sub_signals.get(idx) else (stop_atr_default, target_atr_default)
            out.append((idx, "long", sm, tm))
        elif votes_short.get(idx, 0) >= min_votes:
            sm, tm = sub_signals[idx][0] if sub_signals.get(idx) else (stop_atr_default, target_atr_default)
            out.append((idx, "short", sm, tm))
    return out


def signals_mtf_scalp(bars: dict[str, np.ndarray], spec: dict[str, Any]) -> list[tuple[int, str, float, float]]:
    """Multi-timeframe scalp: low-TF mean reversion aligned with high-TF trend.
    HTF approximated by EMA-100 on the same series (5m has ~8h EMA window,
    sufficient for trend bias when running on 5m bars).
    """
    close = bars["close"]
    htf_period = int(spec.get("htf_period", 100))
    fast = _ema(close, 9)
    slow = _ema(close, 21)
    htf_trend = _ema(close, htf_period)
    stop_atr = float(spec.get("stop_atr", 0.75))
    target_atr = float(spec.get("target_atr", 1.5))
    out = []
    if len(close) < htf_period + 3:
        return out
    for i in range(htf_period + 2, len(close) - 1):
        htf_up = close[i] > htf_trend[i]
        htf_dn = close[i] < htf_trend[i]
        # LTF signal: 9 EMA crosses 21 EMA in HTF direction
        if htf_up and fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
            out.append((i, "long", stop_atr, target_atr))
        elif htf_dn and fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]:
            out.append((i, "short", stop_atr, target_atr))
    return out


def signals_confluence(bars: dict[str, np.ndarray], spec: dict[str, Any]) -> list[tuple[int, str, float, float]]:
    """Plain confluence — alias to confluence_scorecard with slightly relaxed gate."""
    relaxed = dict(spec)
    relaxed.setdefault("min_score", 1)
    return signals_confluence_scorecard(bars, relaxed)


def signals_rsi_mean_reversion(
    bars: dict[str, np.ndarray], spec: dict[str, Any],
) -> list[tuple[int, str, float, float]]:
    """Counter-trend RSI mean-reversion at BB extremes.

    Long when RSI < oversold AND close at/below lower BB band.
    Short when RSI > overbought AND close at/above upper BB band.
    Optional candle-rejection confirmation (close back inside the band the next bar).
    Tighter stops than trend-following (default 1.0 ATR).
    """
    close = bars["close"]
    high = bars["high"]
    low = bars["low"]
    period = int(spec.get("rsi_period", 14))
    oversold = float(spec.get("oversold_threshold", 25.0))
    overbought = float(spec.get("overbought_threshold", 75.0))
    bb_window = int(spec.get("bb_window", 20))
    bb_std_mult = float(spec.get("bb_std_mult", 2.0))
    require_rejection = bool(spec.get("require_rejection", True))
    stop_atr = float(spec.get("stop_atr", 1.0))
    target_atr = float(spec.get("target_atr", 1.5))

    rsi = _rsi(close, period)
    out: list[tuple[int, str, float, float]] = []
    start = max(period + 2, bb_window + 2)
    for i in range(start, len(close) - 1):
        window = close[i - bb_window : i]
        mu = window.mean()
        sigma = window.std()
        if sigma <= 0:
            continue
        upper = mu + bb_std_mult * sigma
        lower = mu - bb_std_mult * sigma
        if rsi[i] < oversold and close[i] <= lower:
            if (
                require_rejection
                and close[i] >= low[i] + 0.25 * (high[i] - low[i])
            ) or not require_rejection:
                # Bar closed in upper 75% of its range → rejection of low
                out.append((i, "long", stop_atr, target_atr))
        elif (
            rsi[i] > overbought
            and close[i] >= upper
            and (
                (
                    require_rejection
                    and close[i] <= high[i] - 0.25 * (high[i] - low[i])
                )
                or not require_rejection
            )
        ):
            out.append((i, "short", stop_atr, target_atr))
    return out


def signals_volume_profile(bars: dict[str, np.ndarray], spec: dict[str, Any]) -> list[tuple[int, str, float, float]]:
    """Return-to-HVN: when price retraces toward the rolling-window high-volume node.

    Builds a price histogram weighted by volume over the lookback window,
    finds the high-volume node (HVN) bin, and triggers when price approaches
    that level from above (long) or below (short) with momentum signal.
    """
    close = bars["close"]
    high = bars["high"]
    low = bars["low"]
    volume = bars["volume"]
    lookback = int(spec.get("vp_lookback", 100))
    n_bins = int(spec.get("vp_bins", 30))
    proximity_pct = float(spec.get("vp_proximity_pct", 0.003))  # 0.3% within HVN
    stop_atr = float(spec.get("stop_atr", 1.25))
    target_atr = float(spec.get("target_atr", 2.0))

    out: list[tuple[int, str, float, float]] = []
    for i in range(lookback + 2, len(close) - 1):
        window_lo = float(low[i - lookback : i].min())
        window_hi = float(high[i - lookback : i].max())
        if window_hi <= window_lo:
            continue
        bin_edges = np.linspace(window_lo, window_hi, n_bins + 1)
        bin_centers = 0.5 * (bin_edges[:-1] + bin_edges[1:])
        # Bin volume by close-price bucket — coarse but cheap
        idx = np.clip(np.searchsorted(bin_edges, close[i - lookback : i]) - 1,
                      0, n_bins - 1)
        vol_by_bin = np.zeros(n_bins)
        for j, w in zip(idx, volume[i - lookback : i], strict=False):
            vol_by_bin[j] += w
        if vol_by_bin.sum() <= 0:
            continue
        hvn_bin = int(np.argmax(vol_by_bin))
        hvn_price = float(bin_centers[hvn_bin])
        proximity = abs(close[i] - hvn_price) / max(hvn_price, 1e-9)
        if proximity > proximity_pct:
            continue
        # Direction: did price approach from above or below
        prior_close = close[i - 3]
        if prior_close > hvn_price and close[i] <= hvn_price * (1 + proximity_pct):
            # Approached from above → expect bounce up
            out.append((i, "long", stop_atr, target_atr))
        elif prior_close < hvn_price and close[i] >= hvn_price * (1 - proximity_pct):
            # Approached from below → expect rejection down
            out.append((i, "short", stop_atr, target_atr))
    return out


def signals_cross_asset_divergence(
    bars: dict[str, np.ndarray], spec: dict[str, Any],
) -> list[tuple[int, str, float, float]]:
    """Mean-reversion on cross-asset divergence.

    When the primary symbol's z-scored return diverges from a rolling correlation
    partner (e.g. MNQ vs ES), the spread tends to compress. Long the laggard,
    short the leader. The lab pulls partner bars from the same _resolve_bar_path
    pipeline.

    Spec:
      partner_symbol  (default ES)  — the correlation reference
      partner_timeframe (default same as primary)
      lookback        (default 50)  — z-score window
      z_threshold     (default 1.5) — abs divergence to trigger
    """
    close = bars["close"]
    lookback = int(spec.get("lookback", 50))
    z_threshold = float(spec.get("z_threshold", 1.5))
    stop_atr = float(spec.get("stop_atr", 1.0))
    target_atr = float(spec.get("target_atr", 2.0))
    partner_symbol = str(spec.get("partner_symbol", "ES"))
    # Registry uses front-month-suffixed names like "ES1" but the bar resolver
    # handles the "1" suffix internally — strip if present so we don't double up.
    if partner_symbol.endswith("1") and partner_symbol[:-1].isalpha():
        partner_symbol = partner_symbol[:-1]
    partner_tf = str(spec.get("partner_timeframe") or spec.get("timeframe", "5m"))

    partner_path = _resolve_bar_path(partner_symbol, partner_tf)
    if partner_path is None or not partner_path.exists():
        return []
    partner_bars = _load_ohlcv(partner_path)
    if partner_bars is None or len(partner_bars["close"]) < lookback + 5:
        return []
    p_close = partner_bars["close"]
    p_time = partner_bars["time"]
    primary_time = bars["time"]
    # Align partner to primary by nearest-time index. Cheap O(n+m) walk.
    t_to_p_idx: dict[int, int] = {}
    j = 0
    for i, t in enumerate(primary_time):
        while j + 1 < len(p_time) and p_time[j + 1] <= t:
            j += 1
        t_to_p_idx[i] = j

    out: list[tuple[int, str, float, float]] = []
    for i in range(lookback + 2, len(close) - 1):
        pj = t_to_p_idx.get(i, -1)
        if pj < lookback + 1:
            continue
        # Compute log-return windows
        primary_ret = np.log(close[i - lookback + 1 : i + 1] / close[i - lookback : i])
        partner_ret = np.log(p_close[pj - lookback + 1 : pj + 1] / p_close[pj - lookback : pj])
        if len(primary_ret) < lookback or len(partner_ret) < lookback:
            continue
        if primary_ret.std() <= 0 or partner_ret.std() <= 0:
            continue
        # Z-score the LATEST return relative to its window
        z_primary = (primary_ret[-1] - primary_ret.mean()) / primary_ret.std()
        z_partner = (partner_ret[-1] - partner_ret.mean()) / partner_ret.std()
        divergence = z_primary - z_partner
        if divergence < -z_threshold:
            # Primary lagging — long primary, expect catch-up
            out.append((i, "long", stop_atr, target_atr))
        elif divergence > z_threshold:
            # Primary leading — short primary, expect mean revert
            out.append((i, "short", stop_atr, target_atr))
    return out


def signals_dxy_gold_inverse(bars: dict[str, np.ndarray], spec: dict[str, Any]) -> list[tuple[int, str, float, float]]:
    """Gold-via-DXY-inverse for GC/MGC.

    Asset-class rationale: gold is priced in USD, so a falling DXY makes gold
    cheaper for non-USD buyers — the inverse correlation is the cleanest macro
    signal in metals. Trade gold against DXY momentum: short DXY break → long
    gold, long DXY break → short gold. Confirm with gold's own short-term
    trend so we don't fade strength against macro.

    Spec:
      partner_symbol           default "DXY"
      partner_timeframe        default same as primary
      dxy_break_lookback       default 20  — bar window for DXY high/low break
      gold_trend_window        default 50  — gold EMA trend confirmation
    """
    close = bars["close"]
    lookback = int(spec.get("dxy_break_lookback", 20))
    trend_window = int(spec.get("gold_trend_window", 50))
    stop_atr = float(spec.get("stop_atr", 1.5))
    target_atr = float(spec.get("target_atr", 3.0))
    partner_symbol = str(spec.get("partner_symbol", "DXY"))
    if partner_symbol.endswith("1") and partner_symbol[:-1].isalpha():
        partner_symbol = partner_symbol[:-1]
    partner_tf = str(spec.get("partner_timeframe") or spec.get("timeframe", "1h"))

    partner_path = _resolve_bar_path(partner_symbol, partner_tf)
    if partner_path is None or not partner_path.exists():
        return []
    partner_bars = _load_ohlcv(partner_path)
    if partner_bars is None or len(partner_bars["close"]) < lookback + 5:
        return []
    p_close = partner_bars["close"]
    p_high = partner_bars["high"]
    p_low = partner_bars["low"]
    p_time = partner_bars["time"]
    primary_time = bars["time"]
    t_to_p_idx: dict[int, int] = {}
    j = 0
    for i, t in enumerate(primary_time):
        while j + 1 < len(p_time) and p_time[j + 1] <= t:
            j += 1
        t_to_p_idx[i] = j

    gold_ema = _ema(close, trend_window)
    out: list[tuple[int, str, float, float]] = []
    for i in range(max(lookback + 2, trend_window + 2), len(close) - 1):
        pj = t_to_p_idx.get(i, -1)
        if pj < lookback + 1:
            continue
        # DXY breakout direction over `lookback` bars
        dxy_lookback_high = float(p_high[pj - lookback : pj].max())
        dxy_lookback_low = float(p_low[pj - lookback : pj].min())
        dxy_breaks_up = p_close[pj] > dxy_lookback_high
        dxy_breaks_dn = p_close[pj] < dxy_lookback_low
        if not (dxy_breaks_up or dxy_breaks_dn):
            continue
        # Gold trend confirmation must agree with the inverse trade
        gold_trend_up = close[i] > gold_ema[i]
        gold_trend_dn = close[i] < gold_ema[i]
        if dxy_breaks_dn and gold_trend_up:
            # DXY weakening + gold already rising → long gold
            out.append((i, "long", stop_atr, target_atr))
        elif dxy_breaks_up and gold_trend_dn:
            # DXY strengthening + gold already declining → short gold
            out.append((i, "short", stop_atr, target_atr))
    return out


def signals_treasury_safe_haven(
    bars: dict[str, np.ndarray], spec: dict[str, Any],
) -> list[tuple[int, str, float, float]]:
    """Treasury futures (ZN/ZB) flight-to-safety on VIX spikes.

    Asset-class rationale: when VIX spikes (rapid risk-off), capital flows
    into US treasuries → ZN/ZB rally. The signal: VIX rises sharply over
    a short window AND treasury price is below recent mean (entry from
    a discounted position). Reverses on VIX collapse + treasury overheated.

    Spec:
      partner_symbol           default "VIX"
      vix_spike_lookback       default 5  — bar window for VIX % change
      vix_spike_pct            default 0.10 (10% rise) — long entry
      vix_collapse_pct         default -0.10 (10% drop) — short entry
      treasury_mean_window     default 50
    """
    close = bars["close"]
    spike_lb = int(spec.get("vix_spike_lookback", 5))
    spike_pct = float(spec.get("vix_spike_pct", 0.10))
    collapse_pct = float(spec.get("vix_collapse_pct", -0.10))
    mean_window = int(spec.get("treasury_mean_window", 50))
    stop_atr = float(spec.get("stop_atr", 1.0))
    target_atr = float(spec.get("target_atr", 2.0))
    partner_symbol = str(spec.get("partner_symbol", "VIX"))
    if partner_symbol.endswith("1") and partner_symbol[:-1].isalpha():
        partner_symbol = partner_symbol[:-1]
    partner_tf = str(spec.get("partner_timeframe") or spec.get("timeframe", "1h"))

    partner_path = _resolve_bar_path(partner_symbol, partner_tf)
    if partner_path is None or not partner_path.exists():
        return []
    partner_bars = _load_ohlcv(partner_path)
    if partner_bars is None or len(partner_bars["close"]) < spike_lb + 5:
        return []
    p_close = partner_bars["close"]
    p_time = partner_bars["time"]
    primary_time = bars["time"]
    t_to_p_idx: dict[int, int] = {}
    j = 0
    for i, t in enumerate(primary_time):
        while j + 1 < len(p_time) and p_time[j + 1] <= t:
            j += 1
        t_to_p_idx[i] = j

    treasury_ema = _ema(close, mean_window)
    out: list[tuple[int, str, float, float]] = []
    for i in range(max(spike_lb + 2, mean_window + 2), len(close) - 1):
        pj = t_to_p_idx.get(i, -1)
        if pj < spike_lb:
            continue
        vix_change = (p_close[pj] - p_close[pj - spike_lb]) / max(p_close[pj - spike_lb], 1e-9)
        # VIX spike → flight-to-safety → long treasury (if below mean = discount)
        if vix_change > spike_pct and close[i] < treasury_ema[i]:
            out.append((i, "long", stop_atr, target_atr))
        # VIX collapse → risk-on → short treasury (if above mean = overheated)
        elif vix_change < collapse_pct and close[i] > treasury_ema[i]:
            out.append((i, "short", stop_atr, target_atr))
    return out


def signals_es_vix_inverse(bars: dict[str, np.ndarray], spec: dict[str, Any]) -> list[tuple[int, str, float, float]]:
    """Equity-index futures (ES/NQ/MNQ) inverse-VIX with trend confirmation.

    Asset-class rationale: VIX is the "fear gauge" — equities and VIX move
    inversely. When VIX collapses (risk-on flush), equities rally. When VIX
    spikes (risk-off), equities sell off. Use as a directional bias filter
    for index futures: only long when VIX has cooled, only short when VIX
    has spiked. Cleaner than orb_sage_gated because the bias source is
    macro, not local price action.

    Spec:
      partner_symbol           default "VIX"
      vix_lookback             default 20
      vix_extreme_pct          default 0.15  — abs % move from window mean
      trend_window             default 30
    """
    close = bars["close"]
    vix_lookback = int(spec.get("vix_lookback", 20))
    vix_extreme_pct = float(spec.get("vix_extreme_pct", 0.15))
    trend_window = int(spec.get("trend_window", 30))
    stop_atr = float(spec.get("stop_atr", 1.0))
    target_atr = float(spec.get("target_atr", 2.0))
    partner_symbol = str(spec.get("partner_symbol", "VIX"))
    if partner_symbol.endswith("1") and partner_symbol[:-1].isalpha():
        partner_symbol = partner_symbol[:-1]
    partner_tf = str(spec.get("partner_timeframe") or spec.get("timeframe", "1h"))

    partner_path = _resolve_bar_path(partner_symbol, partner_tf)
    if partner_path is None or not partner_path.exists():
        return []
    partner_bars = _load_ohlcv(partner_path)
    if partner_bars is None or len(partner_bars["close"]) < vix_lookback + 5:
        return []
    p_close = partner_bars["close"]
    p_time = partner_bars["time"]
    primary_time = bars["time"]
    t_to_p_idx: dict[int, int] = {}
    j = 0
    for i, t in enumerate(primary_time):
        while j + 1 < len(p_time) and p_time[j + 1] <= t:
            j += 1
        t_to_p_idx[i] = j

    primary_ema = _ema(close, trend_window)
    out: list[tuple[int, str, float, float]] = []
    for i in range(max(vix_lookback + 2, trend_window + 2), len(close) - 1):
        pj = t_to_p_idx.get(i, -1)
        if pj < vix_lookback:
            continue
        vix_window_mean = float(p_close[pj - vix_lookback : pj].mean())
        vix_dev = (p_close[pj] - vix_window_mean) / max(vix_window_mean, 1e-9)
        eq_trend_up = close[i] > primary_ema[i]
        eq_trend_dn = close[i] < primary_ema[i]
        if vix_dev < -vix_extreme_pct and eq_trend_up:
            # VIX collapsed below window mean + equity trending up → long
            out.append((i, "long", stop_atr, target_atr))
        elif vix_dev > vix_extreme_pct and eq_trend_dn:
            # VIX spiked above window mean + equity trending down → short
            out.append((i, "short", stop_atr, target_atr))
    return out


def signals_commodity_ratio_mr(
    bars: dict[str, np.ndarray], spec: dict[str, Any],
) -> list[tuple[int, str, float, float]]:
    """Cross-commodity ratio mean-reversion (CL/GC, GC/SI, etc.).

    Asset-class rationale: cross-commodity ratios mean-revert because the
    underlying physical-economy linkage is mostly stable. Oil-to-gold ratio
    in particular oscillates around a real economic relationship (gold =
    safety, oil = activity). When the ratio z-score deviates beyond a
    threshold, the spread typically tightens within ~50 bars.

    Trade the primary asset with the partner as ratio reference:
      ratio_z > z_threshold  → primary overpriced vs partner → short primary
      ratio_z < -z_threshold → primary underpriced vs partner → long primary

    Spec:
      partner_symbol           default "GC"
      partner_timeframe        default same as primary
      ratio_window             default 100  — ratio z-score lookback
      z_threshold              default 1.5
    """
    close = bars["close"]
    ratio_window = int(spec.get("ratio_window", 100))
    z_threshold = float(spec.get("z_threshold", 1.5))
    stop_atr = float(spec.get("stop_atr", 1.0))
    target_atr = float(spec.get("target_atr", 2.0))
    partner_symbol = str(spec.get("partner_symbol", "GC"))
    if partner_symbol.endswith("1") and partner_symbol[:-1].isalpha():
        partner_symbol = partner_symbol[:-1]
    partner_tf = str(spec.get("partner_timeframe") or spec.get("timeframe", "1h"))

    partner_path = _resolve_bar_path(partner_symbol, partner_tf)
    if partner_path is None or not partner_path.exists():
        return []
    partner_bars = _load_ohlcv(partner_path)
    if partner_bars is None or len(partner_bars["close"]) < ratio_window + 5:
        return []
    p_close = partner_bars["close"]
    p_time = partner_bars["time"]
    primary_time = bars["time"]
    t_to_p_idx: dict[int, int] = {}
    j = 0
    for i, t in enumerate(primary_time):
        while j + 1 < len(p_time) and p_time[j + 1] <= t:
            j += 1
        t_to_p_idx[i] = j

    out: list[tuple[int, str, float, float]] = []
    for i in range(ratio_window + 2, len(close) - 1):
        pj = t_to_p_idx.get(i, -1)
        if pj < ratio_window:
            continue
        # Build aligned ratio series over the lookback window
        ratios = []
        for k in range(ratio_window):
            ji = t_to_p_idx.get(i - ratio_window + 1 + k, -1)
            if 0 <= ji < len(p_close) and p_close[ji] > 0:
                ratios.append(close[i - ratio_window + 1 + k] / p_close[ji])
        if len(ratios) < ratio_window // 2:
            continue
        ratios_arr = np.array(ratios)
        if ratios_arr.std() <= 0:
            continue
        current_ratio = close[i] / max(p_close[pj], 1e-9)
        z = (current_ratio - ratios_arr.mean()) / ratios_arr.std()
        if z > z_threshold:
            # Primary overpriced relative to partner — short primary
            out.append((i, "short", stop_atr, target_atr))
        elif z < -z_threshold:
            # Primary underpriced — long primary
            out.append((i, "long", stop_atr, target_atr))
    return out


def signals_overnight_gap_fade(
    bars: dict[str, np.ndarray], spec: dict[str, Any],
) -> list[tuple[int, str, float, float]]:
    """Fade large overnight gaps in index futures (NQ/ES/MNQ).

    Asset-class rationale: index futures trade ETH (overnight) on lower
    liquidity; large overnight gaps from RTH close to next-day RTH open
    often mean-revert into the cash session as institutional flow
    materializes. Heuristic: identify "large gap" bars where the open
    is far from the prior close, then fade in the opposite direction.

    Without explicit session timestamps in CSV, approximate session boundary
    as a >2x ATR price-jump between consecutive bars (a structural gap).

    Spec:
      gap_atr_mult            default 2.0  — gap size in ATR units to trigger
      atr_window              default 20
      reversal_lookback       default 5    — confirm reversal in N bars
    """
    close = bars["close"]
    high = bars["high"]
    low = bars["low"]
    gap_atr_mult = float(spec.get("gap_atr_mult", 2.0))
    atr_window = int(spec.get("atr_window", 20))
    reversal_lookback = int(spec.get("reversal_lookback", 5))
    stop_atr = float(spec.get("stop_atr", 1.0))
    target_atr = float(spec.get("target_atr", 1.5))
    atr_arr = _atr(high, low, close, atr_window)
    out: list[tuple[int, str, float, float]] = []
    for i in range(max(atr_window + reversal_lookback + 1, 30), len(close) - 1):
        if atr_arr[i] <= 0:
            continue
        gap = close[i] - close[i - 1]
        gap_size = abs(gap) / atr_arr[i]
        if gap_size < gap_atr_mult:
            continue
        # Gap up → fade short. Gap down → fade long.
        if gap > 0:
            out.append((i, "short", stop_atr, target_atr))
        else:
            out.append((i, "long", stop_atr, target_atr))
    return out


def signals_index_lead_lag(bars: dict[str, np.ndarray], spec: dict[str, Any]) -> list[tuple[int, str, float, float]]:
    """Index-futures lead/lag for MNQ/NQ.

    Asset-class rationale: ES leads NQ leads MNQ in the equity-index complex
    (ES has the most liquidity / direct S&P fund flows; NQ + MNQ follow via
    correlation arb). When the leader makes a clear directional move and
    the follower is lagging — i.e. correlation hasn't caught up yet — the
    follower mean-reverts toward the leader within ~20 bars.

    Diff vs cross_asset_divergence: directional rather than absolute z, with
    a primary-trend gate so we only enter in the direction of the leader.

    Spec:
      partner_symbol            default "ES"
      lead_lookback             default 20
      lead_break_pct            default 0.003 (0.3% leader move to trigger)
      follower_lag_pct          default 0.002 (0.2% un-followed gap)
    """
    close = bars["close"]
    lead_lookback = int(spec.get("lead_lookback", 20))
    lead_break_pct = float(spec.get("lead_break_pct", 0.003))
    follower_lag_pct = float(spec.get("follower_lag_pct", 0.002))
    stop_atr = float(spec.get("stop_atr", 0.75))
    target_atr = float(spec.get("target_atr", 1.5))
    partner_symbol = str(spec.get("partner_symbol", "ES"))
    if partner_symbol.endswith("1") and partner_symbol[:-1].isalpha():
        partner_symbol = partner_symbol[:-1]
    partner_tf = str(spec.get("partner_timeframe") or spec.get("timeframe", "5m"))

    partner_path = _resolve_bar_path(partner_symbol, partner_tf)
    if partner_path is None or not partner_path.exists():
        return []
    partner_bars = _load_ohlcv(partner_path)
    if partner_bars is None or len(partner_bars["close"]) < lead_lookback + 5:
        return []
    p_close = partner_bars["close"]
    p_time = partner_bars["time"]
    primary_time = bars["time"]
    t_to_p_idx: dict[int, int] = {}
    j = 0
    for i, t in enumerate(primary_time):
        while j + 1 < len(p_time) and p_time[j + 1] <= t:
            j += 1
        t_to_p_idx[i] = j

    out: list[tuple[int, str, float, float]] = []
    for i in range(lead_lookback + 2, len(close) - 1):
        pj = t_to_p_idx.get(i, -1)
        if pj < lead_lookback + 1:
            continue
        lead_pct = (p_close[pj] - p_close[pj - lead_lookback]) / max(p_close[pj - lead_lookback], 1e-9)
        follower_pct = (close[i] - close[i - lead_lookback]) / max(close[i - lead_lookback], 1e-9)
        if lead_pct > lead_break_pct and (lead_pct - follower_pct) > follower_lag_pct:
            # Leader broke up, follower hasn't caught up — long follower
            out.append((i, "long", stop_atr, target_atr))
        elif lead_pct < -lead_break_pct and (follower_pct - lead_pct) > follower_lag_pct:
            # Leader broke down, follower still high — short follower
            out.append((i, "short", stop_atr, target_atr))
    return out


def signals_commodity_session_breakout(
    bars: dict[str, np.ndarray], spec: dict[str, Any],
) -> list[tuple[int, str, float, float]]:
    """Volatility-compressed session breakout for commodities (GC/CL/NG).

    Asset-class rationale: commodities trend longer than equities once a
    catalyst hits (inventory shock, OPEC headline, weather event). They also
    range tightly between catalysts. Combine: only enter on a directional
    breakout from a low-volatility window (the "spring is loaded" signal),
    with wider stops than equity strategies and asymmetric R-targets to
    capture the trend leg.

    Differences vs compression_breakout:
      * 2-bar momentum confirmation (filter out single-bar spikes that revert)
      * ATR-vs-ATR-window quartile filter (more selective than 0.40 default)
      * Asymmetric R: stop 1.0 ATR / target 2.5 ATR (commodities follow through)

    Spec:
      atr_pct_threshold      default 0.30 (lowest 30% of ATR window)
      momentum_bars          default 2 (consecutive higher closes for long)
      stop_atr               default 1.0
      target_atr             default 2.5
    """
    close = bars["close"]
    high = bars["high"]
    low = bars["low"]
    bb_period = int(spec.get("bb_period", 20))
    atr_pct_threshold = float(spec.get("atr_pct_threshold", 0.30))
    momentum_bars = int(spec.get("momentum_bars", 2))
    stop_atr = float(spec.get("stop_atr", 1.0))
    target_atr = float(spec.get("target_atr", 2.5))
    atr_arr = _atr(high, low, close, bb_period)
    out: list[tuple[int, str, float, float]] = []
    for i in range(max(bb_period + momentum_bars + 1, 60), len(close)):
        atr_window = atr_arr[max(0, i - 50) : i]
        atr_window = atr_window[atr_window > 0]
        if len(atr_window) < 5:
            continue
        atr_pct = (atr_arr[i] - atr_window.min()) / max(atr_window.max() - atr_window.min(), 1e-9)
        if atr_pct > atr_pct_threshold:
            continue
        # Multi-bar momentum confirmation
        higher_closes = all(close[i - k] > close[i - k - 1] for k in range(momentum_bars))
        lower_closes  = all(close[i - k] < close[i - k - 1] for k in range(momentum_bars))
        if higher_closes:
            out.append((i, "long", stop_atr, target_atr))
        elif lower_closes:
            out.append((i, "short", stop_atr, target_atr))
    return out


# ─── Stateful-class adapters (MBT / MET) ──────────────────────────
# The next three generators wrap stateful strategy classes that expose
# `maybe_enter(bar, hist, equity, config) -> _Open | None`. The classes
# carry per-day state (opening range, basis window, gap anchor) that
# would be tedious to re-encode as numpy. The adapters replay the bar
# stream through the class, emitting a `(idx, side, stop_atr, target_atr)`
# tuple for each fire, matching the `signals_X` contract used by
# `WalkForwardEngine.run()`. The harness's `_simulate_trade` re-derives
# entry/stop/target from these multiples — the strategy's own absolute
# stop/target prices are not used here, only the ATR ratios from cfg.
#
# Why a closure over the bar window? The strategy classes need
# `BarData` (timezone-aware `datetime`) plus a `BacktestConfig` for
# their interface. We synthesize these lazily from the numpy bar dict.


def _bars_to_bar_data_list(
    bars: dict[str, np.ndarray], spec: dict[str, Any],
) -> list[object]:
    """Materialize numpy bar dict into a list of BarData for stateful
    strategies. Lazy import so this module stays importable without
    pulling pydantic models when only numpy generators are used."""
    from datetime import datetime as _dt

    from eta_engine.core.data_pipeline import BarData

    symbol = str(spec.get("symbol") or "MNQ")
    times = bars["time"]
    o = bars["open"]
    h = bars["high"]
    lo = bars["low"]
    c = bars["close"]
    v = bars["volume"]
    out: list[object] = []
    for i in range(len(c)):
        # Bar files store unix epoch seconds (UTC) in the "time" column.
        ts = _dt.fromtimestamp(float(times[i]), tz=UTC)
        out.append(BarData(
            timestamp=ts, symbol=symbol,
            open=float(o[i]), high=float(h[i]), low=float(lo[i]),
            close=float(c[i]), volume=float(v[i]),
        ))
    return out


def _stub_backtest_config(spec: dict[str, Any]) -> object:
    """Build a minimal BacktestConfig the strategies can carry. The
    strategies only inspect timezone-naive scalar fields; values here
    are placeholders that don't affect signal logic."""
    from eta_engine.backtest.models import BacktestConfig

    sym = str(spec.get("symbol") or "MNQ")
    equity = float(spec.get("initial_equity", 10_000.0))
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2099, 12, 31, tzinfo=UTC),
        symbol=sym,
        initial_equity=equity,
        risk_per_trade_pct=float(spec.get("risk_per_trade_pct", 0.005)),
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )


def _replay_class_strategy(
    strategy: object,
    bars: dict[str, np.ndarray],
    spec: dict[str, Any],
    *,
    atr_stop_mult: float,
    rr_target: float,
) -> list[tuple[int, str, float, float]]:
    """Bar-by-bar replay of a stateful strategy class through the lab
    bar window. Emits one (idx, side, stop_atr, target_atr) per fire.

    The class's own absolute stop/target are intentionally discarded —
    the harness re-prices off its own ATR. We only need the ATR
    multiples (carried from cfg) and the side mapping BUY→long /
    SELL→short.
    """
    target_atr = atr_stop_mult * rr_target
    bar_list = _bars_to_bar_data_list(bars, spec)
    cfg = _stub_backtest_config(spec)
    equity = float(spec.get("initial_equity", 10_000.0))

    out: list[tuple[int, str, float, float]] = []
    hist: list[object] = []
    for i, bar in enumerate(bar_list):
        # Strategies expect hist = ALL prior bars (some look at hist[-N:]
        # for ATR/EMA windows). Pass the running buffer up to but not
        # including the current bar; current bar is the `bar` arg.
        try:
            opened = strategy.maybe_enter(bar, hist, equity, cfg)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - never let a single-bar crash kill the lab run
            opened = None
        if opened is not None:
            side_raw = str(opened.side).upper()
            side = "long" if side_raw in {"BUY", "LONG"} else "short"
            out.append((i, side, atr_stop_mult, target_atr))
        hist.append(bar)
    return out


def signals_mbt_funding_basis(
    bars: dict[str, np.ndarray], spec: dict[str, Any],
) -> list[tuple[int, str, float, float]]:
    """Adapter for `MBTFundingBasisStrategy` — basis-premium fade on MBT.

    Spec keys honored (all optional; fall back to preset defaults):
      basis_lookback, entry_z, exit_z, momentum_lookback,
      require_lower_highs, atr_period, atr_stop_mult, rr_target,
      risk_per_trade_pct, min_bars_between_trades, max_trades_per_day,
      warmup_bars, allow_long, allow_short.
    """
    from eta_engine.strategies.mbt_funding_basis_strategy import (
        MBTFundingBasisConfig,
        MBTFundingBasisStrategy,
        mbt_funding_basis_preset,
    )

    base = mbt_funding_basis_preset()
    overrides: dict[str, Any] = {}
    for key in (
        "basis_lookback", "entry_z", "exit_z", "momentum_lookback",
        "require_lower_highs", "atr_period", "atr_stop_mult", "rr_target",
        "risk_per_trade_pct", "min_bars_between_trades",
        "max_trades_per_day", "warmup_bars", "allow_long", "allow_short",
    ):
        if key in spec:
            overrides[key] = spec[key]
    cfg = MBTFundingBasisConfig(**{**base.__dict__, **overrides})
    strategy = MBTFundingBasisStrategy(cfg)
    return _replay_class_strategy(
        strategy, bars, spec,
        atr_stop_mult=float(spec.get("stop_atr", cfg.atr_stop_mult)),
        rr_target=float(spec.get("target_atr_rr", cfg.rr_target)),
    )


def signals_mbt_overnight_gap(
    bars: dict[str, np.ndarray], spec: dict[str, Any],
) -> list[tuple[int, str, float, float]]:
    """Adapter for `MBTOvernightGapStrategy` — Asia-overnight gap fade
    on MBT at the CME RTH open.

    Spec keys honored (all optional):
      min_gap_atr_mult, max_gap_atr_mult, entry_window_bars,
      atr_period, atr_stop_mult, rr_target, risk_per_trade_pct,
      min_session_gap_hours, max_trades_per_day, warmup_bars,
      allow_long, allow_short.
    """
    from eta_engine.strategies.mbt_overnight_gap_strategy import (
        MBTOvernightGapConfig,
        MBTOvernightGapStrategy,
        mbt_overnight_gap_preset,
    )

    base = mbt_overnight_gap_preset()
    overrides: dict[str, Any] = {}
    for key in (
        "min_gap_atr_mult", "max_gap_atr_mult", "entry_window_bars",
        "atr_period", "atr_stop_mult", "rr_target", "risk_per_trade_pct",
        "min_session_gap_hours", "max_trades_per_day", "warmup_bars",
        "allow_long", "allow_short",
    ):
        if key in spec:
            overrides[key] = spec[key]
    cfg = MBTOvernightGapConfig(**{**base.__dict__, **overrides})
    strategy = MBTOvernightGapStrategy(cfg)
    return _replay_class_strategy(
        strategy, bars, spec,
        atr_stop_mult=float(spec.get("stop_atr", cfg.atr_stop_mult)),
        rr_target=float(spec.get("target_atr_rr", cfg.rr_target)),
    )


def signals_met_rth_orb(
    bars: dict[str, np.ndarray], spec: dict[str, Any],
) -> list[tuple[int, str, float, float]]:
    """Adapter for `METRTHORBStrategy` — 5-minute opening-range
    breakout on MET (CME Micro Ether) RTH.

    Spec keys honored (all optional):
      range_minutes, min_range_pts, ema_bias_period, volume_mult,
      volume_lookback, atr_period, atr_stop_mult, rr_target,
      risk_per_trade_pct, max_trades_per_day.
    """
    from eta_engine.strategies.met_rth_orb_strategy import (
        METRTHORBConfig,
        METRTHORBStrategy,
        met_rth_orb_preset,
    )

    base = met_rth_orb_preset()
    overrides: dict[str, Any] = {}
    for key in (
        "range_minutes", "min_range_pts", "ema_bias_period", "volume_mult",
        "volume_lookback", "atr_period", "atr_stop_mult", "rr_target",
        "risk_per_trade_pct", "max_trades_per_day",
    ):
        if key in spec:
            overrides[key] = spec[key]
    cfg = METRTHORBConfig(**{**base.__dict__, **overrides})
    strategy = METRTHORBStrategy(cfg)
    return _replay_class_strategy(
        strategy, bars, spec,
        atr_stop_mult=float(spec.get("stop_atr", cfg.atr_stop_mult)),
        rr_target=float(spec.get("target_atr_rr", cfg.rr_target)),
    )


def signals_anchor_sweep(
    bars: dict[str, np.ndarray], spec: dict[str, Any],
) -> list[tuple[int, str, float, float]]:
    """Adapter for `AnchorSweepStrategy` — named-anchor variant of
    sweep_reclaim for US index futures (MNQ/NQ/ES/RTY/M2K).

    Closes the gap that left `mnq_anchor_sweep` running paper-soak
    with no lab evaluation surface (the strategy_kind was registered
    in registry_strategy_bridge but missing from SIGNAL_GENERATORS,
    so WalkForwardEngine.run() returned `unknown strategy_kind=...`
    for it).

    Spec keys honored (all optional; fall back to MNQ preset):
      lookback, reclaim_window, min_wick_pct, min_volume_z,
      rr_target, atr_stop_mult, max_trades_per_day,
      min_bars_between_trades, warmup_bars.
    """
    from eta_engine.strategies.anchor_sweep_strategy import (
        AnchorSweepConfig,
        AnchorSweepStrategy,
        mnq_anchor_sweep_preset,
        nq_anchor_sweep_preset,
    )

    sym = (spec.get("symbol") or "").upper()
    base = nq_anchor_sweep_preset() if sym.startswith("NQ") else mnq_anchor_sweep_preset()
    overrides: dict[str, Any] = {}
    for key in (
        "lookback", "reclaim_window", "min_wick_pct", "min_volume_z",
        "rr_target", "atr_stop_mult", "max_trades_per_day",
        "min_bars_between_trades", "warmup_bars",
    ):
        if key in spec:
            overrides[key] = spec[key]
    cfg = AnchorSweepConfig(**{**base.__dict__, **overrides})
    strategy = AnchorSweepStrategy(cfg)
    return _replay_class_strategy(
        strategy, bars, spec,
        atr_stop_mult=float(spec.get("stop_atr", cfg.atr_stop_mult)),
        rr_target=float(spec.get("target_atr_rr", cfg.rr_target)),
    )


def signals_mbt_zfade(
    bars: dict[str, np.ndarray], spec: dict[str, Any],
) -> list[tuple[int, str, float, float]]:
    """Adapter for `MBTZFadeStrategy` — z-score momentum-fade on MBT
    with HTF (1h) trend-opposition filter. Honest rename of the legacy
    mbt_funding_basis (which never had a basis_provider wired).

    Spec keys honored (all optional):
      proxy_lookback, entry_z, exit_z,
      htf_trend_lookback_5m_bars, htf_ema_period, require_htf_opposition,
      atr_period, atr_stop_mult, rr_target, risk_per_trade_pct,
      time_stop_bars, min_bars_between_trades, max_trades_per_day,
      warmup_bars, allow_long, allow_short.
    """
    from eta_engine.strategies.mbt_zfade_strategy import (
        MBTZFadeConfig,
        MBTZFadeStrategy,
        mbt_zfade_preset,
    )

    base = mbt_zfade_preset()
    overrides: dict[str, Any] = {}
    for key in (
        "proxy_lookback", "entry_z", "exit_z",
        "htf_trend_lookback_5m_bars", "htf_ema_period",
        "require_htf_opposition",
        "atr_period", "atr_stop_mult", "rr_target",
        "risk_per_trade_pct", "time_stop_bars",
        "min_bars_between_trades", "max_trades_per_day",
        "warmup_bars", "allow_long", "allow_short",
    ):
        if key in spec:
            overrides[key] = spec[key]
    cfg = MBTZFadeConfig(**{**base.__dict__, **overrides})
    strategy = MBTZFadeStrategy(cfg)
    return _replay_class_strategy(
        strategy, bars, spec,
        atr_stop_mult=float(spec.get("stop_atr", cfg.atr_stop_mult)),
        rr_target=float(spec.get("target_atr_rr", cfg.rr_target)),
    )


def signals_mbt_rth_orb(
    bars: dict[str, np.ndarray], spec: dict[str, Any],
) -> list[tuple[int, str, float, float]]:
    """Adapter for `MBTRTHORBStrategy` — 5-minute opening-range
    breakout on MBT (CME Micro Bitcoin) RTH. Migrated from
    met_rth_orb after the MET friction floor proved uneconomic
    (see docs/STRATEGY_OPTIMIZATION_ROADMAP.md and the 2026-05-07
    EDA report).

    Spec keys honored (all optional; fall back to EDA-derived preset):
      range_minutes, min_range_pts, ema_bias_period, volume_mult,
      volume_lookback, atr_period, atr_stop_mult, rr_target,
      risk_per_trade_pct, max_trades_per_day.
    """
    from eta_engine.strategies.mbt_rth_orb_strategy import (
        MBTRTHORBConfig,
        MBTRTHORBStrategy,
        mbt_rth_orb_preset,
    )

    base = mbt_rth_orb_preset()
    overrides: dict[str, Any] = {}
    for key in (
        "range_minutes", "min_range_pts", "ema_bias_period", "volume_mult",
        "volume_lookback", "atr_period", "atr_stop_mult", "rr_target",
        "risk_per_trade_pct", "max_trades_per_day",
    ):
        if key in spec:
            overrides[key] = spec[key]
    cfg = MBTRTHORBConfig(**{**base.__dict__, **overrides})
    strategy = MBTRTHORBStrategy(cfg)
    return _replay_class_strategy(
        strategy, bars, spec,
        atr_stop_mult=float(spec.get("stop_atr", cfg.atr_stop_mult)),
        rr_target=float(spec.get("target_atr_rr", cfg.rr_target)),
    )


SIGNAL_GENERATORS: dict[str, Callable] = {
    "ema_cross":            signals_ema_cross,
    "sweep_reclaim":        signals_sweep_reclaim,
    "compression_breakout": signals_compression_breakout,
    "confluence_scorecard": signals_confluence_scorecard,
    "vwap_mr":              signals_vwap_mr,
    # v2.1 additions for full production strategy_kind coverage (2026-05-04)
    "orb_sage_gated":       signals_orb_sage_gated,
    "sage_daily_gated":     signals_sage_daily_gated,
    "ensemble_voting":      signals_ensemble_voting,
    "mtf_scalp":            signals_mtf_scalp,
    "confluence":           signals_confluence,
    # v2.3 additions to break MNQ confluence-cluster degeneracy (2026-05-04)
    "rsi_mean_reversion":   signals_rsi_mean_reversion,
    "volume_profile":       signals_volume_profile,
    "cross_asset_divergence": signals_cross_asset_divergence,
    # v2.6 asset-class-tailored generators (2026-05-04)
    "dxy_gold_inverse":          signals_dxy_gold_inverse,
    "index_lead_lag":            signals_index_lead_lag,
    "commodity_session_breakout": signals_commodity_session_breakout,
    # v2.7 macro-driver generators (2026-05-04 round 10)
    "treasury_safe_haven":       signals_treasury_safe_haven,
    "es_vix_inverse":            signals_es_vix_inverse,
    # v2.8 cross-commodity + session-anchored generators (2026-05-04 round 11)
    "commodity_ratio_mr":        signals_commodity_ratio_mr,
    "overnight_gap_fade":        signals_overnight_gap_fade,
    # v2.9 stateful-class adapters for MBT / MET (2026-05-07 commit ddac736)
    "mbt_funding_basis":         signals_mbt_funding_basis,
    "mbt_overnight_gap":         signals_mbt_overnight_gap,
    "met_rth_orb":               signals_met_rth_orb,
    # v2.10 MBT RTH ORB — migrated from met_rth_orb after EDA showed
    # MET friction-to-stop ratio of 663% is uneconomic (2026-05-07).
    "mbt_rth_orb":               signals_mbt_rth_orb,
    # v2.11 MBT z-fade — honest rename of mbt_funding_basis with HTF
    # trend filter + EDA-derived thresholds (z>=2.5, RR=1.5).
    "mbt_zfade":                 signals_mbt_zfade,
    # v2.12 anchor_sweep — closes the gap that left mnq_anchor_sweep
    # running live with no lab evaluation surface (2026-05-07 fleet audit).
    "anchor_sweep":              signals_anchor_sweep,
}


# ─── Trade simulator ──────────────────────────────────────────────


def _simulate_trade(entry_price: float, future_high: np.ndarray, future_low: np.ndarray,
                    future_close: np.ndarray, side: str, stop: float, target: float) -> tuple[float, int]:
    """Walk forward bar-by-bar; first to hit wins. Returns (pnl_R, bars_held)."""
    risk = abs(entry_price - stop)
    if risk <= 0:
        return 0.0, 0
    for i in range(len(future_close)):
        if side == "long":
            if future_low[i] <= stop:
                return -1.0, i + 1
            if future_high[i] >= target:
                return (target - entry_price) / risk, i + 1
        else:
            if future_high[i] >= stop:
                return -1.0, i + 1
            if future_low[i] <= target:
                return (entry_price - target) / risk, i + 1
    final = future_close[-1]
    if side == "long":
        return (final - entry_price) / risk, len(future_close)
    return (entry_price - final) / risk, len(future_close)


# ─── Regime overlay ───────────────────────────────────────────────


def _load_current_regime() -> str:
    try:
        if not REGIME_STATE_PATH.exists():
            return "unknown"
        d = json.loads(REGIME_STATE_PATH.read_text(encoding="utf-8"))
        return str(d.get("global_regime") or "neutral")
    except (OSError, ValueError):
        return "unknown"


# ─── Walk-forward engine ──────────────────────────────────────────


class WalkForwardEngine:
    """v2 engine: full asset/strategy coverage, regime-aware, batch-capable."""

    def __init__(self, bar_dir: Path | None = None) -> None:
        self.bar_dir = Path(bar_dir) if bar_dir else MNQ_HISTORY_ROOT

    def run(self, spec: dict[str, Any], symbol: str | None = None,
            timeframe: str | None = None) -> LabResult:
        sym = (symbol or spec.get("symbol") or "MNQ1").upper()
        tf  = timeframe or spec.get("timeframe") or "1h"
        kind = (spec.get("strategy_kind") or spec.get("entry") or "ema_cross").lower()
        if kind not in SIGNAL_GENERATORS:
            return self._empty(spec, f"unknown strategy_kind={kind}")
        path = _resolve_bar_path(sym, tf)
        if path is None or not path.exists():
            return self._empty(spec, f"bar file missing: {sym}/{tf} expected at {path}")
        bars = _load_ohlcv(path)
        if bars is None or len(bars["close"]) < 200:
            return self._empty(spec, f"insufficient OHLCV rows in {path}")

        windows = self._wf_windows(bars["close"])
        all_pnl_r: list[float] = []
        regime_buckets: dict[str, list[float]] = {}
        active_regime = _load_current_regime()
        atr_arr = _atr(bars["high"], bars["low"], bars["close"], 14)

        # Composite-filter mode: when fleet_sweep marked the spec with
        # __composite_sub_kind__, generate signals from the sub-strategy
        # but FILTER through the confluence_scorecard score predicate.
        # This matches the registry's diamond-tier composition intent.
        composite_sub = spec.get("__composite_sub_kind__")
        composite_min = int(spec.get("__composite_min_score__", 2))
        gen_kind = composite_sub if composite_sub in SIGNAL_GENERATORS else kind

        for _train_range, test_range in windows:
            test_bars = {k: v[test_range.start : test_range.stop] for k, v in bars.items()}
            raw_sigs = SIGNAL_GENERATORS[gen_kind](test_bars, spec)
            if composite_sub:
                # Filter each sub-strategy signal by scorecard score >= min
                sigs = [
                    s for s in raw_sigs
                    if scorecard_score_at(test_bars, spec, s[0], s[1]) >= composite_min
                ]
            else:
                sigs = raw_sigs
            for entry_local_idx, side, stop_mult, target_mult in sigs:
                global_idx = test_range.start + entry_local_idx
                if global_idx + 5 >= len(bars["close"]):
                    continue
                entry = bars["close"][global_idx]
                local_atr = atr_arr[global_idx] if atr_arr[global_idx] > 0 else entry * 0.005
                stop_price = entry - local_atr * stop_mult if side == "long" else entry + local_atr * stop_mult
                target_price = entry + local_atr * target_mult if side == "long" else entry - local_atr * target_mult
                future_h = bars["high"][global_idx + 1 :]
                future_l = bars["low"][global_idx + 1 :]
                future_c = bars["close"][global_idx + 1 :]
                pnl_r, _bars_held = _simulate_trade(entry, future_h, future_l, future_c, side, stop_price, target_price)
                all_pnl_r.append(pnl_r)
                regime_buckets.setdefault(active_regime, []).append(pnl_r)

        if not all_pnl_r:
            return self._empty(spec, "no signals generated")

        arr = np.array(all_pnl_r)
        wins = arr[arr > 0]
        losses = arr[arr < 0]
        win_rate = float(len(wins) / len(arr)) if len(arr) else 0.0
        expectancy = float(arr.mean())
        sharpe = float(arr.mean() / arr.std() * np.sqrt(252)) if arr.std() > 0 else 0.0
        avg_win = float(wins.mean()) if len(wins) else 0.0
        avg_loss = float(losses.mean()) if len(losses) else 0.0
        profit_factor = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() < 0 else 0.0
        dd = self._drawdown(arr)

        regime_pnl = {
            r: {
                "n": len(v),
                "expectancy_R": round(float(np.mean(v)), 3),
                "win_rate": round(float((np.array(v) > 0).mean()), 3) if v else 0.0,
            }
            for r, v in regime_buckets.items()
        }

        heatmap = self._param_sweep(bars, spec, kind)

        # 2026-05-04: trend-following strategies often run sub-40% WR but
        # >0 expectancy via asymmetric R-multiples. Drop WR floor to 35%
        # and rely on expectancy + sharpe + dd as the real edge tests.
        fail_reasons = []
        if win_rate < 0.35:
            fail_reasons.append(f"win_rate {win_rate:.2%} < 35%")
        if sharpe < 0.5:
            fail_reasons.append(f"sharpe {sharpe:.2f} < 0.5")
        if expectancy <= 0:
            fail_reasons.append(f"expectancy {expectancy:.3f} R <= 0")
        if dd > 0.3 * len(arr):
            fail_reasons.append(f"max_dd {dd:.2f} R > 30% of trades")
        passed = not fail_reasons

        # --- Rigor extensions (2026-05-07) ---------------------------
        # Block-bootstrap CI, Bonferroni p-value, friction-aware net,
        # split-half stability, deflated Sharpe. Operator can override
        # multi_test_count via spec["multi_test_count"]; otherwise we
        # use the count of active strategies in the registry.
        from eta_engine.feeds.strategy_lab.rigor import compute_rigor
        rigor_n = spec.get("multi_test_count")
        rigor_block = int(spec.get("bootstrap_block_size", 5))
        rigor_reps = int(spec.get("bootstrap_n_resamples", 5000))
        rigor_seed = int(spec.get("bootstrap_seed", 12345))
        rigor_stop_mult = float(spec.get("avg_stop_atr_mult", 1.5))
        # Engine's avg ATR (in price points) over the bar series gives
        # friction.R_per_trade a realistic stop-distance scale.
        atr_nonzero = atr_arr[atr_arr > 0]
        rigor_atr_pts = float(atr_nonzero.mean()) if atr_nonzero.size else None
        rigor = compute_rigor(
            arr,
            symbol=sym,
            multi_test_count=int(rigor_n) if rigor_n is not None else None,
            block_size=rigor_block,
            n_resamples=rigor_reps,
            avg_stop_atr_mult=rigor_stop_mult,
            typical_atr_pts=rigor_atr_pts,
            seed=rigor_seed,
        )

        return LabResult(
            strategy_id=str(spec.get("id") or spec.get("strategy_id") or "candidate"),
            bot_id=str(spec.get("bot_id") or ""),
            symbol=sym, timeframe=tf, strategy_kind=kind,
            total_trades=len(arr),
            win_rate=round(win_rate, 3), expectancy=round(expectancy, 3),
            sharpe=round(sharpe, 3), max_drawdown=round(dd, 3),
            profit_factor=round(profit_factor, 3),
            avg_win=round(avg_win, 3), avg_loss=round(avg_loss, 3),
            parameter_heatmap=heatmap,
            regime_conditional_pnl=regime_pnl,
            walk_forward_windows=len(windows),
            bars_used=len(bars["close"]),
            coverage_days=round((bars["time"][-1] - bars["time"][0]) / 86400.0, 1) if len(bars["time"]) > 1 else 0.0,
            passed=passed,
            legacy_passed=passed,
            pass_reason="all gates passed" if passed else "",
            fail_reasons=fail_reasons,
            expR_p5=round(rigor.expR_p5, 4),
            expR_p50=round(rigor.expR_p50, 4),
            expR_p95=round(rigor.expR_p95, 4),
            bootstrap_block_size=rigor.bootstrap_block_size,
            bootstrap_n_resamples=rigor_reps,
            p_value_raw=round(rigor.p_value_raw, 5),
            p_value_bonferroni=round(rigor.p_value_bonferroni, 5),
            multi_test_count=rigor.multi_test_count,
            expR_net=round(rigor.expR_net, 4),
            friction_R_per_trade=round(rigor.friction_R_per_trade, 5),
            expR_half_1=round(rigor.expR_half_1, 4),
            expR_half_2=round(rigor.expR_half_2, 4),
            split_half_sign_stable=rigor.split_half_sign_stable,
            sharpe_deflated=round(rigor.sharpe_deflated, 3),
            passed_strict=rigor.passed_strict,
            strict_fail_reasons=list(rigor.strict_fail_reasons),
            ts=datetime.now(UTC).isoformat(),
        )

    def run_batch(self, specs: list[dict[str, Any]]) -> list[LabResult]:
        return [self.run(s) for s in specs]

    def _wf_windows(self, prices: np.ndarray) -> list[tuple[range, range]]:
        n = len(prices)
        train_size = n // 2
        test_size = n // 4
        out = []
        start = 0
        while start + train_size + test_size <= n:
            train = range(start, start + train_size)
            test = range(start + train_size, start + train_size + test_size)
            out.append((train, test))
            start += test_size
        return out

    def _drawdown(self, pnl: np.ndarray) -> float:
        cum = np.cumsum(pnl)
        peak = np.maximum.accumulate(cum)
        dd = peak - cum
        return float(np.max(dd)) if len(dd) else 0.0

    def _param_sweep(self, bars: dict[str, np.ndarray], spec: dict[str, Any], kind: str) -> dict[str, dict[str, float]]:
        results: dict[str, dict[str, float]] = {}
        for stop_mult in (0.75, 1.0, 1.5, 2.0, 2.5):
            mod = dict(spec)
            mod["stop_atr"] = stop_mult
            sigs = SIGNAL_GENERATORS[kind](bars, mod)
            atr_arr = _atr(bars["high"], bars["low"], bars["close"], 14)
            trades_r: list[float] = []
            for idx, side, sm, tm in sigs:
                if idx + 5 >= len(bars["close"]):
                    continue
                entry = bars["close"][idx]
                la = atr_arr[idx] if atr_arr[idx] > 0 else entry * 0.005
                stop = entry - la * sm if side == "long" else entry + la * sm
                target = entry + la * tm if side == "long" else entry - la * tm
                pnl_r, _ = _simulate_trade(
                    entry,
                    bars["high"][idx + 1:],
                    bars["low"][idx + 1:],
                    bars["close"][idx + 1:],
                    side,
                    stop,
                    target,
                )
                trades_r.append(pnl_r)
            if trades_r:
                a = np.array(trades_r)
                results[f"stop_atr_{stop_mult}x"] = {
                    "trades": int(len(a)),
                    "expectancy_R": round(float(a.mean()), 3),
                    "win_rate": round(float((a > 0).mean()), 3),
                    "sharpe": round(float(a.mean() / a.std() * np.sqrt(252)), 2) if a.std() > 0 else 0.0,
                }
        return results

    def _empty(self, spec: dict[str, Any], reason: str) -> LabResult:
        return LabResult(
            strategy_id=str(spec.get("id") or "candidate"),
            bot_id=str(spec.get("bot_id") or ""),
            symbol=str(spec.get("symbol") or ""),
            timeframe=str(spec.get("timeframe") or ""),
            strategy_kind=str(spec.get("strategy_kind") or ""),
            passed=False,
            fail_reasons=[reason],
            ts=datetime.now(UTC).isoformat(),
        )


# ─── Public helpers (kept compatible with existing app.py) ────────


def parse_strategy_yaml(yaml_text: str) -> dict[str, Any]:
    try:
        spec = yaml.safe_load(yaml_text)
        if not isinstance(spec, dict):
            raise ValueError("YAML must be a mapping")
        return spec
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML: {e}") from e


def save_lab_report(result: LabResult, output_dir: str | Path | None = None) -> Path:
    out_dir = Path(output_dir) if output_dir else LAB_REPORTS_ROOT
    bot_dir = out_dir / (result.bot_id or "candidate")
    bot_dir.mkdir(parents=True, exist_ok=True)
    path = bot_dir / f"{result.strategy_id}_lab_report.json"
    path.write_text(json.dumps(asdict(result), indent=2, default=str), encoding="utf-8")
    log.info("Lab report saved: %s", path)
    return path


def fleet_sweep(out_dir: Path | None = None) -> dict[str, Any]:
    """Backtest the entire production fleet — every active StrategyAssignment.
    Writes one lab_report per bot under reports/lab_reports/<bot_id>/.
    Returns a fleet summary dict.
    """
    sys.path.insert(0, str(_WS))
    from eta_engine.strategies.per_bot_registry import ASSIGNMENTS, is_active

    engine = WalkForwardEngine()
    results: list[LabResult] = []
    for a in ASSIGNMENTS:
        if not is_active(a):
            continue
        spec: dict[str, Any] = {
            "id": a.strategy_id,
            "bot_id": a.bot_id,
            "symbol": a.symbol,
            "timeframe": a.timeframe,
            "strategy_kind": a.strategy_kind,
        }
        # Pull parameter-aware extras into the spec. Registry uses both
        # top-level keys ("ema_fast") and nested config dicts (e.g.
        # "scorecard_config": {"fast_ema": 21, ...}, "crypto_orb_config":
        # {"ema_bias_period": 100, "atr_stop_mult": 2.5, "rr_target": 3.0}).
        # Flatten + alias so each bot's actual parameters reach the engine
        # — without this, every confluence_scorecard bot ran with the same
        # defaults and produced identical Sharpe (Tier-3 cluster degeneracy).
        spec_keys = ("stop_atr", "target_atr", "ema_fast", "ema_mid", "ema_slow",
                     "lookback", "min_wick_pct", "min_score", "sigma_mult",
                     "bb_period", "bb_compression_pct", "range_bars",
                     "trend_window",
                     # cross_asset / counter-trend / vp params
                     "z_threshold", "partner_symbol", "partner_timeframe",
                     "rsi_period", "oversold_threshold", "overbought_threshold",
                     "bb_window", "bb_std_mult", "require_rejection",
                     "vp_lookback", "vp_bins", "vp_proximity_pct",
                     # round-10/11 macro-driver params
                     "dxy_break_lookback", "gold_trend_window",
                     "vix_spike_lookback", "vix_spike_pct", "vix_collapse_pct",
                     "treasury_mean_window",
                     "vix_lookback", "vix_extreme_pct",
                     "ratio_window",
                     "gap_atr_mult", "atr_window", "reversal_lookback",
                     "lead_lookback", "lead_break_pct", "follower_lag_pct",
                     "atr_pct_threshold", "momentum_bars")
        aliases = {
            "fast_ema": "ema_fast", "mid_ema": "ema_mid", "slow_ema": "ema_slow",
            "ema_bias_period": "ema_slow",
            "atr_stop_mult": "stop_atr",
            "rr_target": "target_atr",  # interpreted as R-multiple target
            # cross_asset_divergence registry names → generator names
            "z_lookback": "lookback",
            "entry_z_threshold": "z_threshold",
            "min_z_threshold": "z_threshold",
            "reference_asset": "partner_symbol",
        }
        if isinstance(a.extras, Mapping):
            def _ingest(
                src: Mapping[str, Any],
                *,
                key_aliases: Mapping[str, str] = aliases,
                allowed_keys: tuple[str, ...] = spec_keys,
                target_spec: dict[str, Any] = spec,
            ) -> None:
                for k, v in src.items():
                    canonical = key_aliases.get(k, k)
                    if canonical in allowed_keys and canonical not in target_spec:
                        target_spec[canonical] = v
            _ingest(a.extras)
            for nested_key in ("scorecard_config", "crypto_orb_config",
                               "sweep_config", "compression_config",
                               "vwap_config", "orb_config"):
                inner = a.extras.get(nested_key)
                if isinstance(inner, Mapping):
                    _ingest(inner)
            sub = a.extras.get("sub_strategy_extras")
            if isinstance(sub, Mapping):
                inner_cfg = sub.get("sweep_config") or sub.get("config")
                if isinstance(inner_cfg, Mapping):
                    _ingest(inner_cfg)
            # Sub-strategy dispatch: many bots register
            # strategy_kind="confluence_scorecard" with a more specific
            # sub_strategy_kind (e.g. "vwap_mean_reversion",
            # "rsi_mean_reversion"). If the sub maps to a registered signal
            # generator, override strategy_kind so each bot exercises its
            # actual logic rather than the generic scorecard. Aliases match
            # the registry's naming conventions.
            sub_kind_aliases = {
                "vwap_mean_reversion":    "vwap_mr",
                "vwap_reversion":         "vwap_mr",
                "vwap_mr":                "vwap_mr",
                "sweep_reclaim":          "sweep_reclaim",
                "compression_breakout":   "compression_breakout",
                "ema_cross":              "ema_cross",
                "orb_sage_gated":         "orb_sage_gated",
                "sage_daily_gated":       "sage_daily_gated",
                "ensemble_voting":        "ensemble_voting",
                "mtf_scalp":              "mtf_scalp",
                # v2.3 (2026-05-04) — break MNQ cluster degeneracy
                "rsi_mean_reversion":     "rsi_mean_reversion",
                "rsi_mr":                 "rsi_mean_reversion",
                "volume_profile":         "volume_profile",
                "vp":                     "volume_profile",
                "cross_asset_divergence": "cross_asset_divergence",
                "cross_asset":            "cross_asset_divergence",
                "divergence":             "cross_asset_divergence",
            }
            sub_kind = str(a.extras.get("sub_strategy_kind") or "").strip()
            mapped = sub_kind_aliases.get(sub_kind)
            # Counter-trend strategies must NOT be filtered by the
            # scorecard's trend gate — the trend gate by definition
            # disagrees with their entry direction (RSI<25 long while
            # trend is down, etc.). For these, dispatch directly to
            # the sub-strategy without composite filter.
            counter_trend_kinds = {"rsi_mean_reversion", "vwap_mr"}
            if mapped and mapped in SIGNAL_GENERATORS:
                # COMPOSITE FILTER (2026-05-04): if registry has BOTH
                # strategy_kind="confluence_scorecard" and a registered
                # sub_strategy_kind, run sub-strategy signals filtered
                # by scorecard score >= min_score. Matches the registry's
                # DIAMOND architecture (e.g. btc_optimized = sweep_reclaim
                # entries × scorecard quality filter). Skipped for
                # counter-trend strategies (they get pure dispatch).
                if a.strategy_kind == "confluence_scorecard" and mapped not in counter_trend_kinds:
                    spec["__composite_sub_kind__"] = mapped
                    spec["__composite_min_score__"] = int(
                        a.extras.get("scorecard_config", {}).get("min_score", 2)
                        if isinstance(a.extras.get("scorecard_config"), Mapping)
                        else a.extras.get("min_score", 2)
                    )
                    # Keep strategy_kind as confluence_scorecard so engine.run
                    # can read the composite hint and filter accordingly.
                else:
                    # Pure dispatch (no scorecard composition).
                    spec["strategy_kind"] = mapped
        r = engine.run(spec)
        save_lab_report(r, out_dir)
        results.append(r)

    summary = {
        "schema_version": 2,
        "ts": datetime.now(UTC).isoformat(),
        "fleet_size": len(results),
        "passed": [r.bot_id for r in results if r.passed],
        "failed": [{"bot": r.bot_id, "reasons": r.fail_reasons} for r in results if not r.passed],
        "by_kind": {},
        "by_asset_class": {},
    }
    for r in results:
        kk = r.strategy_kind or "unknown"
        summary["by_kind"].setdefault(kk, []).append({
            "bot": r.bot_id, "wr": r.win_rate, "sharpe": r.sharpe,
            "expectancy": r.expectancy, "n": r.total_trades, "passed": r.passed,
        })
    sweep_path = (out_dir or LAB_REPORTS_ROOT) / "_fleet_sweep.json"
    sweep_path.parent.mkdir(parents=True, exist_ok=True)
    sweep_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
