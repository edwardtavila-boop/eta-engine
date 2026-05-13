"""
EVOLUTIONARY TRADING ALGO  //  strategies.edge_layers
======================================================
Six independent edge-amplification layers that wrap ANY sub-strategy.
Each layer captures a real, measurable asymmetry in markets that the
current mechanical-trigger strategies miss entirely.

Edge layers (order matters — earlier = cheaper to compute, cascade vétos):

  1. SESSION PHASE GATE    — block entries during known no-edge periods
  2. EXHAUSTION DETECTOR   — block trend entries after N consecutive bars;
                               signal counter-trend entries at extremes
  3. EFFORT vs RESULT       — volume absorption detection; high vol + small
                               range = smart money absorbing at a level
  4. POST-EVENT DRIFT       — boost confidence after high-volume directional
                               bars (the next bar tends to continue)
  5. STRUCTURAL STOP ENGINE — composite stop = max(structural_level, ATR_band)
                               so stops don't sit inside bar-to-bar noise
  6. VOL-REGIME SIZING      — scale position size inversely with volatility
                               percentile (enter bigger when vol is low,
                               smaller when vol is high; vol mean-reverts)

Architecture
------------
EdgeAmplifier wraps any strategy with the same ``maybe_enter`` contract.
Each layer is a standalone method returning a verdict (pass/veto/boost/shrink).
Layers are configurable per asset class via presets.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Protocol

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData

    class _SubStrategy(Protocol):
        def maybe_enter(
            self,
            bar: BarData,
            hist: list[BarData],
            equity: float,
            config: BacktestConfig,
        ) -> _Open | None: ...


# ---------------------------------------------------------------------------
# Session phase gating — when NOT to trade (the cheapest edge of all)
# ---------------------------------------------------------------------------

# Futures RTH session phases (ET times)
_FUTURES_SESSION_PHASES: dict[str, tuple[time, time]] = {
    "open_drive": (time(9, 30), time(10, 30)),  # 1st hour — highest volume, trend-following edge
    "morning": (time(10, 30), time(12, 0)),  # still good, normal conviction
    "lunch": (time(12, 0), time(13, 30)),  # CHOP — block all entries
    "afternoon": (time(13, 30), time(15, 30)),  # mean-reversion edge builds
    "close": (time(15, 30), time(16, 0)),  # MOC imbalance, good for directional
    "post_close": (time(16, 0), time(23, 59)),  # globex — block entries
    "overnight": (time(0, 0), time(9, 30)),  # pre-market — block entries
}

# Crypto UTC session phases
_CRYPTO_SESSION_PHASES: dict[str, tuple[int, int]] = {
    "asia_open": (0, 3),  # Asia open — trend-following, BTC dominant
    "asia_flow": (3, 7),  # Asia mid-session — medium conviction
    "london_open": (7, 9),  # London open — ranges, mean-reversion
    "london_am": (9, 13),  # London mid — lower vol, avoid
    "ny_open": (13, 16),  # NY open — HIGHEST conviction, both directions
    "ny_afternoon": (16, 20),  # NY afternoon — still good
    "low_flow": (20, 0),  # Overnight gap — BLOCK entries, worst spreads
}

# Which phases allow which mode (trend_follow vs mean_revert vs both vs block)
_SESSION_MODE_MAP: dict[str, str] = {
    "open_drive": "trend",  # momentum breaks only
    "morning": "both",
    "lunch": "block",  # chop — no edge at all
    "afternoon": "mean_revert",  # fade the morning extremes
    "close": "both",  # MOC imbalance — directional but also mean-revert
    "post_close": "block",  # globex — too thin
    "overnight": "block",  # pre-market — no edge
    "asia_open": "trend",
    "asia_flow": "both",
    "london_open": "mean_revert",
    "london_am": "block",
    "ny_open": "both",
    "ny_afternoon": "both",
    "low_flow": "block",
}


def _get_session_phase_local(bar: BarData, tz_name: str) -> str | None:
    """Resolve session phase from bar's local time for futures assets."""
    try:
        from zoneinfo import ZoneInfo

        local = bar.timestamp.astimezone(ZoneInfo(tz_name)).time()
        local_t = time(local.hour, local.minute)
    except Exception:
        return None
    for phase, (start, end) in _FUTURES_SESSION_PHASES.items():
        if start <= local_t < end:
            return phase
    return None


def _get_crypto_session_phase(bar: BarData) -> str | None:
    """Resolve session phase from bar's UTC hour."""
    hour = bar.timestamp.hour
    for phase, (start, end) in _CRYPTO_SESSION_PHASES.items():
        if start <= hour < end:
            return phase
    return None


def session_phase_allows(mode: str, side: str, bar: BarData, tz_name: str = "America/New_York") -> tuple[bool, float]:
    """Check if current session phase allows a trade of the given mode/side.
    Returns (allowed, conviction_multiplier).
    Blocked=0.0x, weak=0.7x, normal=1.0x, boosted=1.3x."""
    phase = _get_session_phase_local(bar, tz_name)
    if phase is None:
        return True, 1.0
    allowed = _SESSION_MODE_MAP.get(phase, "both")
    if allowed == "block":
        return False, 0.0
    if allowed != "both" and allowed != mode:
        return False, 0.0
    if phase in ("open_drive", "ny_open"):
        return True, 1.3  # boost: highest-edge periods
    if phase in ("lunch", "low_flow", "overnight", "post_close", "london_am"):
        return True, 0.7  # shrink: low-conviction periods
    return True, 1.0


# ---------------------------------------------------------------------------
# Exhaustion detector — when a move has gone too far
# ---------------------------------------------------------------------------


@dataclass
class _ExhaustionState:
    """Per-bot tracking of bar-level directional streaks."""

    consecutive_up: int = 0
    consecutive_down: int = 0
    last_side: str | None = None  # 'up' or 'down'


# Module-level exhaustion tracker (keyed by bot_id or strategy instance)
_exhaustion_states: dict[int, _ExhaustionState] = {}


def exhaustion_check(
    hist: list[BarData],
    state_key: int = 0,
    max_consecutive_trend: int = 5,
    veto_consecutive: int = 6,
    counter_consecutive: int = 7,
) -> tuple[bool, float]:
    """Check if the last N bars show exhaustion of directional momentum.
    Returns (allowed, conviction_multiplier).
    - 3-4 consecutive: allowed but shrink confidence to 0.7x
    - 5 consecutive: veto trend entries entirely
    - 7+ consecutive: signal counter-trend entry at 0.5x (mean reversion likely)

    This is the canonical Wyckoff "buying climax" / "selling climax" edge —
    after a straight-line move, the probability of continuation decays
    exponentially. A 7-bar run is a 0.8% probability event on random walk.
    """
    if len(hist) < 3:
        return True, 1.0

    state = _exhaustion_states.setdefault(state_key, _ExhaustionState())
    current = hist[-1]
    current_side = "up" if current.close > current.open else "down"

    if current_side == state.last_side and state.last_side is not None:
        if current_side == "up":
            state.consecutive_up += 1
            state.consecutive_down = 0
        else:
            state.consecutive_down += 1
            state.consecutive_up = 0
    else:
        state.consecutive_up = 1 if current_side == "up" else 0
        state.consecutive_down = 1 if current_side == "down" else 0
    state.last_side = current_side

    streak = max(state.consecutive_up, state.consecutive_down)

    if streak >= counter_consecutive:
        return True, 0.5  # strong counter-trend signal
    if streak >= veto_consecutive:
        return False, 0.0  # block trend entries
    if streak >= max_consecutive_trend:
        return True, 0.7  # shrink: exhaustion building
    return True, 1.0


# ---------------------------------------------------------------------------
# Effort-vs-Result — volume absorption detection
# ---------------------------------------------------------------------------


def effort_vs_result(
    bar: BarData,
    hist: list[BarData],
    side: str,
    volume_z_lookback: int = 20,
    absorption_vol_z_min: float = 1.2,
    absorption_range_z_max: float = 0.5,
) -> bool:
    """Detect absorption: high volume in the WRONG direction with small range.
    Returns True if the entry should proceed, False to veto.

    The edge: When volume is 1.2+ std above average but the bar's range is
    only 0.5 std, someone is ABSORBING the flow — accumulating at a level.
    If the bar's close is IN the direction of absorption, the absorption
    supports your entry. If it's OPPOSITE, veto — the smart money is against you.

    Example: Long signal on bar that closed up 0.1% on 3x normal volume
    but the bar's high-low range was tiny = bull absorption → PASS.
    Short signal on same bar = absorption AGAINST the entry → VETO.
    """
    if len(hist) < volume_z_lookback + 1:
        return True  # not enough data — fail-open

    recent_vols = [b.volume for b in hist[-volume_z_lookback:]]
    mean_vol = sum(recent_vols) / len(recent_vols)
    std_vol = (sum((v - mean_vol) ** 2 for v in recent_vols) / len(recent_vols)) ** 0.5
    if std_vol <= 0:
        return True
    vol_z = (bar.volume - mean_vol) / std_vol

    recent_ranges = [b.high - b.low for b in hist[-volume_z_lookback:]]
    mean_range = sum(recent_ranges) / len(recent_ranges)
    std_range = (sum((r - mean_range) ** 2 for r in recent_ranges) / len(recent_ranges)) ** 0.5
    if std_range <= 0:
        return True

    bar_range = bar.high - bar.low
    range_z = (bar_range - mean_range) / std_range

    # Absorption: high vol + small range
    if vol_z < absorption_vol_z_min or range_z > absorption_range_z_max:
        return True  # no absorption detected

    # Which side is being absorbed?
    clv = (bar.close - bar.low) / max(bar_range, 1e-9)
    absorbing_up = clv > 0.65  # close near the high = absorption at highs (buying)
    absorbing_down = clv < 0.35  # close near the low = absorption at lows (selling)

    if side.upper() == "BUY" and absorbing_down:
        return False  # smart money selling into your long
    if side.upper() == "SELL" and absorbing_up:
        return False  # smart money buying into your short
    if side.upper() == "BUY" and absorbing_up:
        return True  # absorption CONFIRMS your long direction
    if side.upper() == "SELL" and absorbing_down:
        return True  # absorption CONFIRMS your short direction
    return True


# ---------------------------------------------------------------------------
# Post-event drift — continuation after high-volume directional bars
# ---------------------------------------------------------------------------


def post_event_drift(
    bar: BarData,
    hist: list[BarData],
    side: str,
    volume_z_lookback: int = 20,
    drift_vol_z_min: float = 2.0,
    drift_clv_min: float = 0.75,
    drift_recency_bars: int = 2,
) -> float:
    """Boost confidence when entry aligns with recent high-impact events.

    When a bar has volume 2.0+ std above average AND a strong
    directional close (CLV >= 0.75 or <= 0.25), the next 1-3 bars
    tend to CONTINUE in the same direction. This is post-event drift —
    the institutional order that caused the high-volume bar hasn't
    finished executing.

    Returns: confidence multiplier (1.0 = no boost, 1.5 = strong drift).
    """
    if len(hist) < volume_z_lookback + 2:
        return 1.0

    recent_vols = [b.volume for b in hist[-volume_z_lookback:]]
    mean_vol = sum(recent_vols) / len(recent_vols)
    std_vol = (sum((v - mean_vol) ** 2 for v in recent_vols) / len(recent_vols)) ** 0.5
    if std_vol <= 0:
        return 1.0

    for i in range(1, drift_recency_bars + 1):
        if len(hist) < i + 1:
            break
        check_bar = hist[-i]
        check_range = check_bar.high - check_bar.low
        if check_range <= 0:
            continue
        clv = (check_bar.close - check_bar.low) / check_range
        vol_z = (check_bar.volume - mean_vol) / std_vol

        if vol_z >= drift_vol_z_min:
            if clv >= drift_clv_min and side.upper() == "BUY":
                return 1.5  # strong bullish post-event drift
            if clv <= (1 - drift_clv_min) and side.upper() == "SELL":
                return 1.5  # strong bearish post-event drift
    return 1.0


# ---------------------------------------------------------------------------
# Structural stop engine — composite stop placement
# ---------------------------------------------------------------------------


def structural_stop(
    entry_price: float,
    side: str,
    hist: list[BarData],
    atr_stop_dist: float,
    structural_lookback: int = 10,
    structural_buffer_mult: float = 0.25,
) -> tuple[float, float]:
    """Compute a composite stop that includes a structural component.

    An ATR-only stop sits inside normal bar-to-bar noise on volatile
    assets (BTC 1h swing is $800-1500; ATR stop 1.5x = $750-1125).
    Adding a structural component — below the recent N-bar low for
    longs, above the recent N-bar high for shorts — saves trades
    that would otherwise get tagged by noise.

    The composite stop uses: max(atr_stop_dist, structural_distance).
    For longs: stop = entry_price - max(atr_stop_dist, entry_price - recent_low + buffer).
    For shorts: stop = entry_price + max(atr_stop_dist, recent_high - entry_price + buffer).

    Returns (composite_stop_price, composite_stop_distance).
    """
    if len(hist) < structural_lookback:
        return (entry_price - atr_stop_dist if side.upper() == "BUY" else entry_price + atr_stop_dist), atr_stop_dist

    recent = hist[-structural_lookback:]
    recent_low = min(b.low for b in recent)
    recent_high = max(b.high for b in recent)
    avg_range = sum(b.high - b.low for b in recent) / len(recent)
    structural_buffer = structural_buffer_mult * avg_range

    if side.upper() == "BUY":
        structural_stop_dist = entry_price - recent_low + structural_buffer
        composite_dist = max(atr_stop_dist, structural_stop_dist)
        composite_price = entry_price - composite_dist
        return composite_price, composite_dist
    else:
        structural_stop_dist = recent_high - entry_price + structural_buffer
        composite_dist = max(atr_stop_dist, structural_stop_dist)
        composite_price = entry_price + composite_dist
        return composite_price, composite_dist


# ---------------------------------------------------------------------------
# Vol-regime sizing — inversely scale with ATR percentile
# ---------------------------------------------------------------------------


def vol_regime_size_mult(
    hist: list[BarData],
    vol_lookback: int = 100,
    atr_period: int = 14,
) -> float:
    """Scale position size inversely with current volatility percentile.
    Low vol: 1.3x (volatility likely to expand → bigger reward per risk unit).
    High vol: 0.6x (volatility likely to contract → smaller, ride is wild).
    Extreme vol: 0.3x (panic conditions, ride small or not at all).

    The edge: volatility is mean-reverting. You want to enter the LARGEST
    position when vol is LOW (cheap insurance, room to run) and the
    SMALLEST when vol is HIGH (expensive insurance, likely contraction).
    """
    if len(hist) < vol_lookback + atr_period:
        return 1.0

    current_atr = sum(b.high - b.low for b in hist[-atr_period:]) / atr_period
    atr_history: list[float] = []
    for i in range(atr_period, vol_lookback):
        window = hist[-i - atr_period : -i]
        if len(window) >= atr_period:
            atr_history.append(sum(b.high - b.low for b in window) / atr_period)

    if not atr_history or current_atr <= 0:
        return 1.0

    sorted_atr = sorted(atr_history)
    rank = sum(1 for v in sorted_atr if v <= current_atr)
    pct = rank / max(len(sorted_atr) - 1, 1)

    if pct < 0.20:
        return 1.3  # bottom 20% = cheap vol = go big
    if pct < 0.40:
        return 1.15
    if pct < 0.60:
        return 1.0  # normal
    if pct < 0.80:
        return 0.7  # expensive vol = shrink
    if pct < 0.95:
        return 0.5  # very expensive = half-size
    return 0.3  # top 5% = panic = token size


# ---------------------------------------------------------------------------
# The unified EdgeAmplifier — wraps any strategy with all 6 layers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EdgeAmplifierConfig:
    """Knobs for all 6 edge layers. Presets per asset class below."""

    # Layer 1: Session phase (OFF by default — live-mode only feature)
    enable_session_gate: bool = False
    timezone_name: str = "America/New_York"
    is_crypto: bool = False  # set True for crypto assets (UTC session windows)

    # Layer 2: Exhaustion
    enable_exhaustion_gate: bool = False  # paper-soak: opt in after per-bot fill-rate proof
    exhaustion_max_trend: int = 12
    exhaustion_veto: int = 20
    exhaustion_counter: int = 25

    # Layer 3: Effort vs Result (absorption)
    enable_absorption_gate: bool = False  # paper-soak: avoid hidden liquidity vetoes by default
    absorption_vol_z_min: float = 1.2
    absorption_range_z_max: float = 0.5

    # Layer 4: Post-event drift
    enable_drift_boost: bool = False  # paper-soak: keep initial sizing deterministic
    drift_vol_z_min: float = 2.0
    drift_clv_min: float = 0.75
    drift_recency_bars: int = 2

    # Layer 5: Structural stops
    enable_structural_stops: bool = True
    structural_lookback: int = 10
    structural_buffer_mult: float = 0.25

    # Layer 6: Vol-regime sizing
    enable_vol_sizing: bool = True
    vol_regime_lookback: int = 100
    vol_atr_period: int = 14

    # Layer 7: Hidden RSI divergence confirmation
    enable_rsi_divergence: bool = False  # paper-soak: compute-heavy, test separately
    rsi_period: int = 14
    rsi_divergence_lookback: int = 20
    rsi_peak_tolerance: int = 5

    # Layer 8: Rejection candle verification
    enable_rejection_candle: bool = False  # paper-soak: confirmation boost only after proof

    # Layer 9: Squeeze quality gate
    enable_squeeze_gate: bool = False  # off by default — compute-heavy
    squeeze_bb_lookback: int = 100

    # Mode: what entry types to suppress (trend_follow='trend', mean_revert='mean', both='both')
    strategy_mode: str = "both"


class EdgeAmplifier:
    """Six independent edge layers wrapping any sub-strategy.

    Usage:
        sub = SweepReclaimStrategy(...)
        amplified = EdgeAmplifier(sub, EdgeAmplifierConfig(...))
        # strategy works like any other via maybe_enter()

    The EdgeAmplifier is itself a _SubStrategy — it composes with
    SageGatedORBStrategy, ConfluenceScorecardStrategy, etc.
    Anyone calling maybe_enter() doesn't know or care that there
    are six edge layers underneath.
    """

    def __init__(
        self,
        sub_strategy: _SubStrategy,
        config: EdgeAmplifierConfig | None = None,
    ) -> None:
        self._sub = sub_strategy
        self.cfg = config or EdgeAmplifierConfig()

    # -- main entry point --------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        opened = self._sub.maybe_enter(bar, hist, equity, config)
        if opened is None:
            return None

        side = opened.side

        # ── Layer 1: Session Phase Gate ──
        if self.cfg.enable_session_gate:
            if self.cfg.is_crypto:
                phase = _get_crypto_session_phase(bar)
                if phase:
                    allowed = _SESSION_MODE_MAP.get(phase, "both")
                    if allowed == "block":
                        return None
                    if allowed != "both" and allowed != self.cfg.strategy_mode:
                        return None
            else:
                allowed, _ = session_phase_allows(
                    self.cfg.strategy_mode,
                    side,
                    bar,
                    self.cfg.timezone_name,
                )
                if not allowed:
                    return None

        # ── Layer 2: Exhaustion Gate ──
        if self.cfg.enable_exhaustion_gate:
            hist_fwd = list(hist) + [bar]
            allowed, exhaust_mult = exhaustion_check(
                hist_fwd,
                state_key=id(self._sub),
                max_consecutive_trend=self.cfg.exhaustion_max_trend,
                veto_consecutive=self.cfg.exhaustion_veto,
                counter_consecutive=self.cfg.exhaustion_counter,
            )
            if not allowed:
                return None

        # ── Layer 3: Effort vs Result (Absorption) ──
        if self.cfg.enable_absorption_gate and not effort_vs_result(
            bar,
            hist,
            side,
            absorption_vol_z_min=self.cfg.absorption_vol_z_min,
            absorption_range_z_max=self.cfg.absorption_range_z_max,
        ):
            return None

        # ── Layer 4: Post-Event Drift Boost ──
        drift_mult = 1.0
        if self.cfg.enable_drift_boost:
            drift_mult = post_event_drift(
                bar,
                hist,
                side,
                drift_vol_z_min=self.cfg.drift_vol_z_min,
                drift_clv_min=self.cfg.drift_clv_min,
                drift_recency_bars=self.cfg.drift_recency_bars,
            )

        # ── Layer 5: Structural Stops ──
        if self.cfg.enable_structural_stops:
            atr_stop_dist = abs(opened.entry_price - opened.stop)
            new_stop, new_dist = structural_stop(
                opened.entry_price,
                side,
                hist,
                atr_stop_dist,
                structural_lookback=self.cfg.structural_lookback,
                structural_buffer_mult=self.cfg.structural_buffer_mult,
            )
            # rr_target may not exist on all _Open subclasses; compute from
            # existing stop/target distances when absent.
            rr = getattr(opened, "rr_target", None)
            if rr is None:
                risk_dist = max(abs(opened.entry_price - opened.stop), 1e-9)
                reward_dist = abs(opened.target - opened.entry_price)
                rr = reward_dist / risk_dist
            new_target = (
                opened.entry_price + rr * new_dist if side.upper() == "BUY" else opened.entry_price - rr * new_dist
            )
            opened = replace(opened, stop=new_stop, target=new_target)

        # ── Layer 6: Vol-Regime Sizing ──
        if self.cfg.enable_vol_sizing:
            vol_mult = vol_regime_size_mult(
                hist,
                vol_lookback=self.cfg.vol_regime_lookback,
                atr_period=self.cfg.vol_atr_period,
            )
        else:
            vol_mult = 1.0

        # ── Layer 7: Hidden RSI Divergence Confirmation ──
        # Hidden bullish divergence (price HL, RSI LL) confirms trend-continuation
        # longs. Hidden bearish (price LH, RSI HH) confirms trend-continuation shorts.
        # Regular divergence confirms reversals.
        rsi_div_mult = 1.0
        if self.cfg.enable_rsi_divergence:
            try:
                from eta_engine.strategies.technical_edges import (
                    compute_rsi,
                    detect_rsi_divergence,
                )

                closes = [b.close for b in hist[-self.cfg.rsi_divergence_lookback - self.cfg.rsi_period :]]
                if len(closes) >= self.cfg.rsi_period + 1:
                    rsi_vals = []
                    for i in range(self.cfg.rsi_period, len(closes)):
                        rsi_vals.append(compute_rsi(closes[: i + 1], self.cfg.rsi_period))
                    div = detect_rsi_divergence(
                        closes,
                        rsi_vals,
                        lookback=self.cfg.rsi_divergence_lookback,
                        peak_tolerance=self.cfg.rsi_peak_tolerance,
                    )
                    if div.detected:
                        is_long = side.upper() == "BUY"
                        if div.divergence_type in ("hidden_bullish", "regular_bullish") and is_long:
                            rsi_div_mult = 1.3  # strong confirmation
                        elif div.divergence_type in ("hidden_bearish", "regular_bearish") and not is_long:
                            rsi_div_mult = 1.3
                        elif div.divergence_type in ("regular_bearish",) and is_long:
                            rsi_div_mult = 0.0  # regular bearish = potential reversal → veto long
                            return None
                        elif div.divergence_type in ("regular_bullish",) and not is_long:
                            rsi_div_mult = 0.0  # regular bullish = potential reversal → veto short
                            return None
            except Exception:
                pass

        # ── Layer 8: Rejection Candle Verification ──
        # Hammer on long entry, shooting star on short entry = institutional
        # participation at key level. Boost entries that have a rejection candle.
        rejection_mult = 1.0
        if self.cfg.enable_rejection_candle:
            try:
                from eta_engine.strategies.technical_edges import is_rejection_candle

                is_rej, candle_type, rej_mult = is_rejection_candle(bar, side)
                if is_rej:
                    rejection_mult = rej_mult
            except Exception:
                pass

        # ── Layer 9: Squeeze Quality Gate ──
        # When a BB + Keltner + ADX squeeze is confirmed, the expansion
        # is explosive — boost sizing to capture the move. When NOT squeezed,
        # don't penalize but don't boost either.
        squeeze_mult = 1.0
        if self.cfg.enable_squeeze_gate:
            try:
                from eta_engine.strategies.technical_edges import detect_squeeze

                closes_sq = [b.close for b in hist[-self.cfg.squeeze_bb_lookback :]]
                highs_sq = [b.high for b in hist[-self.cfg.squeeze_bb_lookback :]]
                lows_sq = [b.low for b in hist[-self.cfg.squeeze_bb_lookback :]]
                sq = detect_squeeze(closes_sq, highs_sq, lows_sq, bb_width_lookback=self.cfg.squeeze_bb_lookback)
                if sq is not None and sq.is_squeezed:
                    if (side.upper() == "BUY" and sq.direction_hint == "bullish_break") or (
                        side.upper() == "SELL" and sq.direction_hint == "bearish_break"
                    ):
                        squeeze_mult = 1.5  # explosive expansion in our direction
                    else:
                        squeeze_mult = 1.1  # squeeze exists but direction unclear
            except Exception:
                pass

        # ── Aggregate all multipliers ──
        final_qty = opened.qty * drift_mult * vol_mult * rsi_div_mult * rejection_mult * squeeze_mult
        final_risk = opened.risk_usd * drift_mult * vol_mult * rsi_div_mult * rejection_mult * squeeze_mult
        final_conf = min(10.0, max(0.0, opened.confluence * drift_mult * rsi_div_mult * rejection_mult))

        return replace(
            opened,
            qty=final_qty,
            risk_usd=final_risk,
            confluence=final_conf,
            regime=opened.regime + "_edge_amplified",
        )


# ---------------------------------------------------------------------------
# Asset-class presets — ready to use, calibrated per market
# ---------------------------------------------------------------------------


def mnq_futures_preset() -> EdgeAmplifierConfig:
    """Edge amplifier for MNQ/NQ futures on 5m bars.
    RTH session, lunch block, exhaustion after 5 consecutive bars,
    structural stops below recent session extremes."""
    return EdgeAmplifierConfig(
        enable_session_gate=False,  # live-mode only
        timezone_name="America/New_York",
        is_crypto=False,
        strategy_mode="both",
        enable_exhaustion_gate=False,
        exhaustion_max_trend=6,
        exhaustion_veto=8,
        exhaustion_counter=10,
        enable_absorption_gate=False,
        absorption_vol_z_min=1.0,
        absorption_range_z_max=0.5,
        enable_drift_boost=False,
        drift_vol_z_min=2.0,
        drift_clv_min=0.75,
        drift_recency_bars=2,
        enable_structural_stops=True,
        structural_lookback=10,
        structural_buffer_mult=0.25,
        enable_vol_sizing=True,
        vol_regime_lookback=78,  # ~1 RTH session of 5m bars
        vol_atr_period=14,
    )


def btc_crypto_preset() -> EdgeAmplifierConfig:
    """Edge amplifier for BTC on 1h bars.
    UTC session windows, exhausted after 5 bars, wider absorption threshold,
    longer structural lookback for 24/7 context."""
    return EdgeAmplifierConfig(
        enable_session_gate=False,
        timezone_name="UTC",
        is_crypto=True,
        strategy_mode="both",
        enable_exhaustion_gate=False,
        exhaustion_max_trend=7,
        exhaustion_veto=10,
        exhaustion_counter=12,
        enable_absorption_gate=False,
        absorption_vol_z_min=1.2,
        absorption_range_z_max=0.5,
        enable_drift_boost=False,
        drift_vol_z_min=2.0,
        drift_clv_min=0.75,
        drift_recency_bars=2,
        enable_structural_stops=True,
        structural_lookback=24,  # 1 day of 1h bars
        structural_buffer_mult=0.5,
        enable_vol_sizing=True,
        vol_regime_lookback=168,  # 1 week of 1h bars
        vol_atr_period=14,
    )


def eth_crypto_preset() -> EdgeAmplifierConfig:
    """Edge amplifier for ETH on 1h bars. Wider structural buffer
    to absorb ETH's ~1.3x BTC volatility."""
    return EdgeAmplifierConfig(
        enable_session_gate=False,
        timezone_name="UTC",
        is_crypto=True,
        strategy_mode="both",
        enable_exhaustion_gate=False,
        exhaustion_max_trend=7,
        exhaustion_veto=10,
        exhaustion_counter=12,
        enable_absorption_gate=False,
        absorption_vol_z_min=1.0,  # ETH vol is bursty — slightly easier gate
        absorption_range_z_max=0.6,
        enable_drift_boost=False,
        drift_vol_z_min=2.0,
        drift_clv_min=0.70,
        drift_recency_bars=2,
        enable_structural_stops=True,
        structural_lookback=24,
        structural_buffer_mult=0.5,
        enable_vol_sizing=True,
        vol_regime_lookback=168,
        vol_atr_period=14,
    )


def sol_crypto_preset() -> EdgeAmplifierConfig:
    """Edge amplifier for SOL on 1h bars. Widest structural buffer
    to absorb SOL's ~2.5x BTC volatility. Stricter exhaustion gates
    because SOL trends harder and reverts faster when exhausted."""
    return EdgeAmplifierConfig(
        enable_session_gate=False,
        timezone_name="UTC",
        is_crypto=True,
        strategy_mode="both",
        enable_exhaustion_gate=False,
        exhaustion_max_trend=6,
        exhaustion_veto=9,
        exhaustion_counter=11,
        enable_absorption_gate=False,
        absorption_vol_z_min=1.5,  # SOL has more noise — higher threshold
        absorption_range_z_max=0.7,
        enable_drift_boost=False,
        drift_vol_z_min=2.5,  # SOL needs more extreme vol for post-event drift
        drift_clv_min=0.80,
        drift_recency_bars=1,
        enable_structural_stops=True,
        structural_lookback=24,
        structural_buffer_mult=0.75,  # wide buffer for SOL's 2.5x beta
        enable_vol_sizing=True,
        vol_regime_lookback=168,
        vol_atr_period=14,
    )
