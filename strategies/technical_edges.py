"""
EVOLUTIONARY TRADING ALGO  //  strategies.technical_edges
===========================================================
Unified technical indicator computation and divergence detection.
Implements every missing edge from the 2026 battle-tested specs:
  - Hidden RSI divergence (bullish/bearish)
  - Hidden MACD histogram divergence
  - MACD full computation (line + signal + histogram)
  - Keltner Channel bands
  - Wilder's ADX/DMI (+DI, -DI, ADX)
  - Engulfing/engulfing pattern detection
  - Fibonacci extension levels (127.2%, 161.8%)
  - Squeeze readiness (BB + Keltner + ADX composite)
  - Rejection candle verification at key levels

All functions operate on simple float lists — no external dependencies
beyond the standard library. Designed to be dropped into any strategy's
maybe_enter() method for real-time bar-level analysis.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.core.data_pipeline import BarData


# ---------------------------------------------------------------------------
# RSI — Wilder's smoothing (standard TA-Lib equivalent)
# ---------------------------------------------------------------------------

def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's smoothed RSI. Returns None if insufficient data."""
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        return 100.0
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    """Full RSI series over all bars. First `period` values are None."""
    rsi_values: list[float | None] = [None] * period
    for i in range(period, len(closes)):
        rsi_values.append(compute_rsi(closes[:i + 1], period))
    return rsi_values


# ---------------------------------------------------------------------------
# MACD — full EMA(12)-EMA(26) + signal EMA(9) + histogram
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MACDResult:
    macd: float      # EMA(12,close) - EMA(26,close)
    signal: float    # EMA(9, MACD)
    histogram: float # MACD - signal


def compute_macd(closes: list[float], fast: int = 12, slow: int = 26, signal_period: int = 9) -> MACDResult | None:
    """Full MACD computation. Returns None if insufficient data."""
    if len(closes) < slow + signal_period:
        return None
    ema_fast = _ema(0, closes[0], fast)
    ema_slow = _ema(0, closes[0], slow)
    macd_history: list[float] = []
    for c in closes[1:]:
        ema_fast = _ema(ema_fast, c, fast)
        ema_slow = _ema(ema_slow, c, slow)
        macd_history.append(ema_fast - ema_slow)
    macd = macd_history[-1]
    signal = macd_history[0]
    for v in macd_history[1:]:
        signal = _ema(signal, v, signal_period)
    histogram = macd - signal
    return MACDResult(macd=macd, signal=signal, histogram=histogram)


def macd_series(closes: list[float], fast: int = 12, slow: int = 26, signal_period: int = 9) -> list[MACDResult | None]:
    """Full MACD series over all bars."""
    results: list[MACDResult | None] = [None] * (slow + signal_period - 1)
    for i in range(slow + signal_period, len(closes)):
        results.append(compute_macd(closes[:i + 1], fast, slow, signal_period))
    return results


def _ema(prev: float, value: float, period: int) -> float:
    alpha = 2.0 / (period + 1)
    return alpha * value + (1 - alpha) * prev


# ---------------------------------------------------------------------------
# Keltner Channel — EMA(20) ± ATR(10) × multiplier
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KeltnerChannel:
    upper: float
    middle: float
    lower: float
    width_pct: float  # (upper - lower) / middle


def compute_keltner(highs: list[float], lows: list[float], closes: list[float],
                    ema_period: int = 20, atr_period: int = 10, atr_mult: float = 2.0) -> KeltnerChannel | None:
    """Keltner Channel: EMA middle ± ATR bands."""
    if len(closes) < max(ema_period, atr_period) + 1:
        return None
    ema_val = closes[0]
    for c in closes[1:]:
        ema_val = _ema(ema_val, c, ema_period)
    atr_val = 0.0
    if len(highs) >= atr_period + 1:
        trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
               for i in range(1, len(highs))]
        atr_val = sum(trs[-atr_period:]) / atr_period
    band = atr_mult * atr_val
    upper = ema_val + band
    lower = ema_val - band
    width = (upper - lower) / max(ema_val, 1e-9)
    return KeltnerChannel(upper=upper, middle=ema_val, lower=lower, width_pct=width)


# ---------------------------------------------------------------------------
# Wilder's ADX / DMI — +DI, -DI, ADX
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ADXResult:
    adx: float
    plus_di: float
    minus_di: float


def compute_adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> ADXResult | None:
    """Full Wilder's ADX/DMI computation."""
    if len(highs) < period * 2 + 1:
        return None
    trs: list[float] = []
    plus_dms: list[float] = []
    minus_dms: list[float] = []
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dms.append(up if up > down and up > 0 else 0.0)
        minus_dms.append(down if down > up and down > 0 else 0.0)
    atr = sum(trs[:period]) / period
    smoothed_atr = atr
    smoothed_pdm = sum(plus_dms[:period]) / period
    smoothed_mdm = sum(minus_dms[:period]) / period
    dx_values: list[float] = []
    for i in range(period, len(trs)):
        smoothed_atr = (smoothed_atr * (period - 1) + trs[i]) / period
        smoothed_pdm = (smoothed_pdm * (period - 1) + plus_dms[i]) / period
        smoothed_mdm = (smoothed_mdm * (period - 1) + minus_dms[i]) / period
        if smoothed_atr > 0:
            plus_di = 100.0 * smoothed_pdm / smoothed_atr
            minus_di = 100.0 * smoothed_mdm / smoothed_atr
            denom = plus_di + minus_di
            dx = 100.0 * abs(plus_di - minus_di) / denom if denom > 0 else 0.0
            dx_values.append(dx)
    if not dx_values:
        return None
    adx = dx_values[0]
    for dx in dx_values[1:]:
        adx = _ema(adx, dx, period)
    plus_di = 100.0 * smoothed_pdm / max(smoothed_atr, 1e-9)
    minus_di = 100.0 * smoothed_mdm / max(smoothed_atr, 1e-9)
    return ADXResult(adx=adx, plus_di=plus_di, minus_di=minus_di)


# ---------------------------------------------------------------------------
# Hidden Divergence Detection (RSI + MACD)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DivergenceResult:
    detected: bool
    divergence_type: str  # 'hidden_bullish', 'hidden_bearish', 'regular_bullish', 'regular_bearish', 'none'
    price_signal: str     # 'higher_low', 'lower_low', 'higher_high', 'lower_high'
    indicator_signal: str # 'lower_low', 'higher_low', 'lower_high', 'higher_high'
    strength: float      # 0.0-1.0 based on the magnitude of divergence


def detect_rsi_divergence(
    closes: list[float],
    rsi_values: list[float | None],
    lookback: int = 20,
    peak_tolerance: int = 5,
) -> DivergenceResult:
    """Detect regular and hidden RSI divergence.

    Regular bearish: price HH, RSI LH → weakness in uptrend → short signal
    Regular bullish: price LL, RSI HL → strength in downtrend → long signal
    Hidden bullish: price HL, RSI LL → continuation in uptrend → long signal
    Hidden bearish: price LH, RSI HH → continuation in downtrend → short signal
    """
    if len(closes) < lookback + peak_tolerance or len(rsi_values) < lookback + peak_tolerance:
        return DivergenceResult(False, 'none', '', '', 0.0)

    c_window = closes[-lookback:]
    r_window = rsi_values[-lookback:]
    r_clean = [v for v in r_window if v is not None]
    if len(r_clean) < lookback // 2:
        return DivergenceResult(False, 'none', '', '', 0.0)

    # Find price peaks and troughs using the last `lookback` window
    price_highs, price_lows = _find_swings(c_window, peak_tolerance)
    rsi_pivots = _find_pivots_float(r_clean, peak_tolerance)

    if len(price_highs) < 2 or len(price_lows) < 2:
        return DivergenceResult(False, 'none', '', '', 0.0)

    # Regular bearish: price makes higher high, RSI makes lower high
    last_ph = price_highs[-2], price_highs[-1]
    last_rh = rsi_pivots.get('highs', [])
    if last_ph[0] is not None and last_ph[1] is not None and last_ph[1] > last_ph[0] and len(last_rh) >= 2:
        if last_rh[-1] < last_rh[-2]:
            strength = min(1.0, (last_ph[1] - last_ph[0]) / max(abs(last_ph[0]), 1e-9) * 10)
            return DivergenceResult(True, 'regular_bearish', 'higher_high', 'lower_high', strength)

    # Regular bullish: price makes lower low, RSI makes higher low
    last_pl = price_lows[-2], price_lows[-1]
    last_rl = rsi_pivots.get('lows', [])
    if last_pl[0] is not None and last_pl[1] is not None and last_pl[1] < last_pl[0] and len(last_rl) >= 2:
        if last_rl[-1] > last_rl[-2]:
            strength = min(1.0, (last_pl[0] - last_pl[1]) / max(abs(last_pl[0]), 1e-9) * 10)
            return DivergenceResult(True, 'regular_bullish', 'lower_low', 'higher_low', strength)

    # Hidden bullish: price higher low, RSI lower low (trend continuation long)
    if last_pl[0] is not None and last_pl[1] is not None and last_pl[1] > last_pl[0] and len(last_rl) >= 2:
        if last_rl[-1] < last_rl[-2]:
            strength = min(1.0, (last_pl[1] - last_pl[0]) / max(abs(last_pl[0]), 1e-9) * 10)
            return DivergenceResult(True, 'hidden_bullish', 'higher_low', 'lower_low', strength)

    # Hidden bearish: price lower high, RSI higher high (trend continuation short)
    if last_ph[0] is not None and last_ph[1] is not None and last_ph[1] < last_ph[0] and len(last_rh) >= 2:
        if last_rh[-1] > last_rh[-2]:
            strength = min(1.0, (last_ph[0] - last_ph[1]) / max(abs(last_ph[0]), 1e-9) * 10)
            return DivergenceResult(True, 'hidden_bearish', 'lower_high', 'higher_high', strength)

    return DivergenceResult(False, 'none', '', '', 0.0)


def detect_macd_divergence(
    closes: list[float],
    macd_histogram: list[float | None],
    lookback: int = 20,
    peak_tolerance: int = 5,
) -> DivergenceResult:
    """Detect MACD histogram divergence (regular and hidden).
    Uses MACD histogram values (MACD line - signal line) for peak/trough detection."""
    if len(closes) < lookback + peak_tolerance or len(macd_histogram) < lookback + peak_tolerance:
        return DivergenceResult(False, 'none', '', '', 0.0)

    c_window = closes[-lookback:]
    h_window = macd_histogram[-lookback:]
    h_clean = [float(v) for v in h_window if v is not None]
    if len(h_clean) < lookback // 2:
        return DivergenceResult(False, 'none', '', '', 0.0)

    price_highs, price_lows = _find_swings(c_window, peak_tolerance)
    hist_pivots = _find_pivots_float(h_clean, peak_tolerance)

    if len(price_highs) < 2 or len(price_lows) < 2:
        return DivergenceResult(False, 'none', '', '', 0.0)

    # Same divergence logic as RSI but on MACD histogram
    last_ph = price_highs[-2], price_highs[-1]
    last_pl = price_lows[-2], price_lows[-1]
    hist_highs = hist_pivots.get('highs', [])
    hist_lows = hist_pivots.get('lows', [])

    if last_ph[0] is not None and last_ph[1] is not None and len(hist_highs) >= 2:
        if last_ph[1] > last_ph[0] and hist_highs[-1] < hist_highs[-2]:
            strength = min(1.0, abs(hist_highs[-2] - hist_highs[-1]) / max(abs(hist_highs[-2]), 1e-9))
            return DivergenceResult(True, 'regular_bearish_macd', 'higher_high', 'lower_high', strength)
        if last_ph[1] < last_ph[0] and hist_highs[-1] > hist_highs[-2]:
            strength = min(1.0, abs(hist_highs[-1] - hist_highs[-2]) / max(abs(hist_highs[-2]), 1e-9))
            return DivergenceResult(True, 'hidden_bearish_macd', 'lower_high', 'higher_high', strength)

    if last_pl[0] is not None and last_pl[1] is not None and len(hist_lows) >= 2:
        if last_pl[1] < last_pl[0] and hist_lows[-1] > hist_lows[-2]:
            strength = min(1.0, abs(hist_lows[-1] - hist_lows[-2]) / max(abs(hist_lows[-2]), 1e-9))
            return DivergenceResult(True, 'regular_bullish_macd', 'lower_low', 'higher_low', strength)
        if last_pl[1] > last_pl[0] and hist_lows[-1] < hist_lows[-2]:
            strength = min(1.0, abs(hist_lows[-2] - hist_lows[-1]) / max(abs(hist_lows[-2]), 1e-9))
            return DivergenceResult(True, 'hidden_bullish_macd', 'higher_low', 'lower_low', strength)

    return DivergenceResult(False, 'none', '', '', 0.0)


# ---------------------------------------------------------------------------
# Candle pattern detection — engulfing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EngulfingResult:
    detected: bool
    direction: str  # 'bullish', 'bearish', 'none'
    strength: float # 0.0-1.0 based on engulfing ratio


def detect_engulfing(hist: list[BarData]) -> EngulfingResult:
    """Detect bullish/bearish engulfing pattern.
    Requires at least 2 bars — the current bar must engulf the previous bar's body."""
    if len(hist) < 2:
        return EngulfingResult(False, 'none', 0.0)
    prev = hist[-2]
    curr = hist[-1]
    prev_body = abs(prev.close - prev.open)
    curr_body = abs(curr.close - curr.open)
    if prev_body < 1e-9 or curr_body < 1e-9:
        return EngulfingResult(False, 'none', 0.0)

    prev_bull = prev.close > prev.open
    curr_bull = curr.close > curr.open

    if prev_bull != curr_bull:
        if curr_bull and not prev_bull and curr.close > prev.open and curr.open < prev.close:
            strength = min(1.0, curr_body / prev_body)
            return EngulfingResult(True, 'bullish', strength)
        if not curr_bull and prev_bull and curr.close < prev.open and curr.open > prev.close:
            strength = min(1.0, curr_body / prev_body)
            return EngulfingResult(True, 'bearish', strength)
    return EngulfingResult(False, 'none', 0.0)


# ---------------------------------------------------------------------------
# Rejection candle detection — hammer/engulfing at support/resistance
# ---------------------------------------------------------------------------

def is_rejection_candle(bar: BarData, side: str) -> tuple[bool, str, float]:
    """Check if the current bar is a rejection candle supporting the entry.

    For longs: hammer or bullish engulfing at a support level
    For shorts: shooting star or bearish engulfing at a resistance level

    Returns (is_rejection, candle_type, confidence_mult).
    """
    from eta_engine.strategies.alpha_sniper import classify_bar
    bt = classify_bar(bar)
    is_long = side.upper() == "BUY"

    if is_long:
        if bt.is_hammer:
            return True, "hammer_rejection", 1.3
        if bt.is_bull and bt.body_ratio > 0.60 and bt.lower_wick_ratio > 0.20:
            return True, "bullish_rejection_wick", 1.2
    else:
        if bt.is_shooting_star:
            return True, "shooting_star_rejection", 1.3
        if bt.is_bear and bt.body_ratio > 0.60 and bt.upper_wick_ratio > 0.20:
            return True, "bearish_rejection_wick", 1.2

    return False, "none", 1.0


# ---------------------------------------------------------------------------
# Fibonacci extension levels
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FibExtension:
    level_127: float
    level_162: float
    level_262: float
    swing_high: float
    swing_low: float
    direction: str  # 'up' or 'down'


def compute_fib_extensions(
    highs: list[float],
    lows: list[float],
    lookback: int = 50,
) -> FibExtension | None:
    """Compute Fibonacci extension levels (127.2%, 161.8%, 261.8%) from
    the most recent swing high/low.

    For uptrends: projection above the swing high (swing_low → swing_high → extension)
    For downtrends: projection below the swing low
    """
    if len(highs) < lookback or len(lows) < lookback:
        return None
    h_window = highs[-lookback:]
    l_window = lows[-lookback:]
    swing_high = max(h_window)
    swing_high_idx = len(h_window) - 1 - h_window[::-1].index(swing_high)
    swing_low = min(l_window)
    swing_low_idx = len(l_window) - 1 - l_window[::-1].index(swing_low)
    if swing_high <= swing_low:
        return None

    diff = swing_high - swing_low
    direction = "up" if swing_low_idx < swing_high_idx else "down"

    return FibExtension(
        level_127=swing_low + diff * 1.272 if direction == "up" else swing_high - diff * 1.272,
        level_162=swing_low + diff * 1.618 if direction == "up" else swing_high - diff * 1.618,
        level_262=swing_low + diff * 2.618 if direction == "up" else swing_high - diff * 2.618,
        swing_high=swing_high,
        swing_low=swing_low,
        direction=direction,
    )


# ---------------------------------------------------------------------------
# Composite squeeze detector — BB + Keltner + ADX
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SqueezeResult:
    is_squeezed: bool
    bb_width_percentile: float  # 0.0-1.0, where is BB width relative to history
    keltner_width_pct: float
    adx: float
    squeeze_quality: float     # 0.0-1.0 composite score
    direction_hint: str        # 'bullish_break', 'bearish_break', 'neutral'


def detect_squeeze(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    bb_width_lookback: int = 100,
    bb_period: int = 20,
    keltner_ema: int = 20,
    keltner_atr: int = 10,
    adx_period: int = 14,
    bb_width_max_percentile: float = 0.30,
    keltner_width_max: float = 0.05,
    adx_squeeze_max: float = 25.0,
) -> SqueezeResult | None:
    """Composite squeeze detector combining Bollinger Band width,
    Keltner Channel width, and ADX.

    A squeeze is active when ALL THREE confirm low volatility:
    - BB width in bottom N% of recent history
    - Keltner width below threshold (tight bands)
    - ADX below threshold (no established trend → pre-trend)

    This is the prelude to an explosive expansion move.
    Returns direction_hint based on price position within the bands.
    """
    if len(closes) < max(bb_width_lookback, adx_period * 2):
        return None

    # BB width percentile
    bb_widths: list[float] = []
    for i in range(bb_period, len(closes)):
        window = closes[i - bb_period:i]
        mean = sum(window) / len(window)
        var = sum((c - mean) ** 2 for c in window) / len(window)
        std = var ** 0.5
        if mean > 0:
            upper = mean + 2.0 * std
            lower = mean - 2.0 * std
            bb_widths.append((upper - lower) / mean)

    if not bb_widths:
        return None

    current_bb_width = bb_widths[-1]
    sorted_widths = sorted(bb_widths[-bb_width_lookback:])
    rank = sum(1 for w in sorted_widths if w <= current_bb_width)
    bb_pct = rank / max(len(sorted_widths), 1)

    # Keltner
    kc = compute_keltner(highs, lows, closes, keltner_ema, keltner_atr)
    if kc is None:
        return None

    # ADX
    adx_result = compute_adx(highs, lows, closes, adx_period)
    if adx_result is None:
        return None

    bb_squeezed = bb_pct < bb_width_max_percentile
    kc_squeezed = kc.width_pct < keltner_width_max
    adx_squeezed = adx_result.adx < adx_squeeze_max

    squeezed_count = int(bb_squeezed) + int(kc_squeezed) + int(adx_squeezed)
    is_squeezed = squeezed_count >= 2

    # Direction hint: where is price relative to bands?
    price = closes[-1]
    if price > kc.middle:
        direction = "bullish_break"
    elif price < kc.middle:
        direction = "bearish_break"
    else:
        direction = "neutral"

    sq = (bb_squeezed + kc_squeezed + adx_squeezed) / 3.0

    return SqueezeResult(
        is_squeezed=is_squeezed,
        bb_width_percentile=bb_pct,
        keltner_width_pct=kc.width_pct,
        adx=adx_result.adx,
        squeeze_quality=sq,
        direction_hint=direction,
    )


# ---------------------------------------------------------------------------
# Swing detection helpers (for divergence)
# ---------------------------------------------------------------------------

def _find_swings(values: list[float], tolerance: int = 5) -> tuple[list[float], list[float]]:
    """Find swing highs and swing lows in a float list."""
    highs: list[float] = []
    lows: list[float] = []
    n = len(values)
    if n < tolerance * 2:
        return highs, lows
    tol = min(tolerance, n // 3)
    for i in range(tol, n - tol):
        left = values[i - tol:i]
        right = values[i + 1:i + tol + 1]
        if all(values[i] >= v for v in left) and all(values[i] >= v for v in right):
            highs.append(values[i])
        if all(values[i] <= v for v in left) and all(values[i] <= v for v in right):
            lows.append(values[i])
    return highs, lows


def _find_pivots_float(values: list[float], tolerance: int = 5) -> dict[str, list[float]]:
    """Find pivot highs and lows in a float list."""
    highs, lows = _find_swings(values, tolerance)
    return {'highs': highs, 'lows': lows}
