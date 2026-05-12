"""
EVOLUTIONARY TRADING ALGO  //  strategies.sweep_reclaim_strategy
==================================================================
Liquidity sweep + reclaim mean-reversion — works for both MNQ
intraday (vs prior day high/low, premarket extremes, ORB
extremes, VWAP) and BTC futures (vs daily/weekly high/low, Asia
session, daily/weekly opens, round-number levels).

User mandate (2026-04-27): "convert sweeps into mechanical
triggers so your bot can actually test them" — this strategy IS
the mechanical translation of the sweep/reclaim discretionary
pattern.

Mechanic
--------
1. Track a rolling library of "important levels" — recent N-bar
   highs/lows act as proxy liquidity zones that real Wyckoff
   springs / upthrusts trade against.
2. On each bar, detect a SWEEP: bar's low pierces below a recent
   level (long setup) OR bar's high pierces above (short setup).
3. After a sweep, watch the next 1-3 bars for a RECLAIM:
   * Long: subsequent close > swept level
   * Short: subsequent close < swept level
4. Confluence quality gates the trade:
   * Wick percentage of sweep bar (>= 40% recommended)
   * Reclaim speed (within reclaim_window bars)
   * Volume expansion on reclaim vs N-bar avg
   * Distance to next "real" level (target)

This is the same Wyckoff spring/upthrust mechanic the user spec'd
in his strategy notes — translated into pure mechanical triggers
that a backtest can fire on every bar without human intervention.

Configurable for asset class
----------------------------
Same code, different config:
* MNQ 5m: lookback=20-50 bars (= 1-3 RTH sessions), wick_pct=0.40,
  volume_z_min=0.5, atr_stop_mult=1.0
* BTC 1h: lookback=24-48 bars (= 1-2 days), wick_pct=0.30,
  volume_z_min=0.3, atr_stop_mult=1.5

Asset-class preset factories at the bottom of the file.

Limitations
-----------
* Single-bar sweep detection — multi-bar swing-based sweeps would
  need a swing-extreme library (TODO if backtest shows promise).
* No directional bias filter built-in — caller is expected to
  wrap in SageDailyGatedStrategy or RegimeGatedStrategy if
  directional bias matters.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class SweepReclaimConfig:
    """Knobs for the sweep+reclaim mean-reversion strategy."""

    # Level lookback — how many recent bars define the "liquidity
    # zone" highs/lows we sweep against. 20 bars ≈ 1 RTH session
    # on MNQ 5m; 24 bars ≈ 1 day on BTC 1h.
    level_lookback: int = 20

    # Reclaim window — after a sweep, how many bars do we wait for
    # the close to recover the level before invalidating?
    reclaim_window: int = 3

    # Wick quality — sweep bar's wick (extending beyond the level)
    # must be at least this fraction of the bar's total range.
    # Higher = more selective ("real" sweep with rejection).
    min_wick_pct: float = 0.40

    # Volume confirmation — reclaim bar's volume should exceed
    # the N-bar average by this z-score (in std-deviations).
    volume_z_lookback: int = 20
    min_volume_z: float = 0.0      # 0 = disabled; 0.5 = mild; 1.0 = strict

    # Risk
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    rr_target: float = 2.0
    risk_per_trade_pct: float = 0.005

    # Hygiene
    min_bars_between_trades: int = 6
    max_trades_per_day: int = 4
    warmup_bars: int = 50

    # Direction
    allow_long: bool = True
    allow_short: bool = True

    # ── Phase 3 L2 overlay (v2 upgrade) ──────────────────────────
    # When enabled, the strategy calls confirm_sweep_with_l2 before
    # firing.  If the gate returns passed=False (thin book at the
    # swept level), the signal is dropped.  Pre-data this is a no-op
    # via the no_l2_yet pass-through.  Post-data (when
    # mark_captures_expected() is called) the gate fails CLOSED on
    # missing data — operator must wire mark_captures_expected at
    # session start.
    enable_l2_overlay: bool = True
    l2_min_stop_qty: int = 50          # min visible contra-side qty
    l2_hidden_qty_floor: int | None = None  # iceberg estimate; None = off
    l2_window_seconds: int = 60         # how far back to search for pre-touch
    l2_symbol: str = "MNQ"             # symbol for depth-file lookup


class SweepReclaimStrategy:
    """Mechanical Wyckoff spring / upthrust translation."""

    def __init__(self, config: SweepReclaimConfig | None = None) -> None:
        self.cfg = config or SweepReclaimConfig()
        # Rolling window of recent highs/lows for level detection.
        # Each element is (bar_idx, high, low). When a bar's low
        # pierces below min(prior_lows) we have a long sweep candidate.
        self._level_window: deque[tuple[int, float, float]] = deque(
            maxlen=self.cfg.level_lookback + 5,
        )
        # Volume window for z-score normalization
        self._volume_window: deque[float] = deque(
            maxlen=self.cfg.volume_z_lookback,
        )
        # Pending sweeps awaiting reclaim
        # Each: (side, swept_level, bar_idx_when_swept, sweep_bar_close)
        self._pending_sweeps: list[tuple[str, float, int, float]] = []
        # State
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None
        # Audit
        self._n_long_sweeps_seen: int = 0
        self._n_short_sweeps_seen: int = 0
        self._n_reclaims_fired: int = 0
        self._n_wick_quality_rejects: int = 0
        self._n_volume_quality_rejects: int = 0
        self._n_reclaim_window_expired: int = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "bars_seen": self._bars_seen,
            "long_sweeps_seen": self._n_long_sweeps_seen,
            "short_sweeps_seen": self._n_short_sweeps_seen,
            "reclaims_fired": self._n_reclaims_fired,
            "wick_quality_rejects": self._n_wick_quality_rejects,
            "volume_quality_rejects": self._n_volume_quality_rejects,
            "reclaim_window_expired": self._n_reclaim_window_expired,
        }

    # -- detection helpers -------------------------------------------------

    def _detect_sweep(self, bar: BarData) -> None:
        """Check if this bar swept a recent level. If so, record a
        pending sweep awaiting reclaim."""
        if len(self._level_window) < self.cfg.level_lookback:
            return
        # Use prior bars only (not current) to define "recent" levels
        prior = list(self._level_window)[-self.cfg.level_lookback:-1]
        if not prior:
            return
        recent_low = min(low for _, _, low in prior)
        recent_high = max(high for _, high, _ in prior)

        # Wick quality on THIS bar (the sweep bar)
        bar_range = max(bar.high - bar.low, 1e-9)

        # Long sweep: low pierces below recent_low
        if (
            self.cfg.allow_long
            and bar.low < recent_low
            # Wick beyond level
            and (recent_low - bar.low) / bar_range >= self.cfg.min_wick_pct
        ):
            self._pending_sweeps.append(
                ("BUY", recent_low, self._bars_seen, bar.close),
            )
            self._n_long_sweeps_seen += 1
        elif (
            self.cfg.allow_long
            and bar.low < recent_low
        ):
            self._n_wick_quality_rejects += 1

        # Short sweep: high pierces above recent_high
        if (
            self.cfg.allow_short
            and bar.high > recent_high
            and (bar.high - recent_high) / bar_range >= self.cfg.min_wick_pct
        ):
            self._pending_sweeps.append(
                ("SELL", recent_high, self._bars_seen, bar.close),
            )
            self._n_short_sweeps_seen += 1
        elif (
            self.cfg.allow_short
            and bar.high > recent_high
        ):
            self._n_wick_quality_rejects += 1

    def _check_reclaim(self, bar: BarData) -> tuple[str, float] | None:
        """Check if any pending sweep is reclaimed by THIS bar's close.
        Returns (side, swept_level) for the first valid reclaim, or None.
        Expired sweeps (past reclaim_window) are dropped."""
        valid: list[tuple[str, float, int, float]] = []
        winner: tuple[str, float] | None = None
        for side, level, sweep_idx, _sweep_close in self._pending_sweeps:
            age = self._bars_seen - sweep_idx
            if age > self.cfg.reclaim_window:
                self._n_reclaim_window_expired += 1
                continue
            # Reclaim check
            if winner is None:
                if side == "BUY" and bar.close > level:
                    winner = ("BUY", level)
                    continue  # don't keep this one, it fired
                if side == "SELL" and bar.close < level:
                    winner = ("SELL", level)
                    continue
            valid.append((side, level, sweep_idx, _sweep_close))
        self._pending_sweeps = valid
        return winner

    def _volume_z_score(self, bar: BarData) -> float:
        """Z-score of bar volume vs recent window. 0 if insufficient data."""
        if len(self._volume_window) < self.cfg.volume_z_lookback:
            return 0.0
        vols = list(self._volume_window)
        mean = sum(vols) / len(vols)
        var = sum((v - mean) ** 2 for v in vols) / len(vols)
        std = var ** 0.5
        if std <= 0.0:
            return 0.0
        return (bar.volume - mean) / std

    # -- main entry point --------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        bar_date = bar.timestamp.date()
        if self._last_day != bar_date:
            self._last_day = bar_date
            self._trades_today = 0
        self._bars_seen += 1

        # Update volume window BEFORE z-score reads
        self._volume_window.append(bar.volume)

        # Detect sweep (uses prior level window, not yet updated for this bar)
        self._detect_sweep(bar)

        # Check reclaim (potentially fires)
        winner = self._check_reclaim(bar)

        # Update level window for next bar
        self._level_window.append((self._bars_seen, bar.high, bar.low))

        if self._bars_seen < self.cfg.warmup_bars:
            return None
        if winner is None:
            return None
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx)
            < self.cfg.min_bars_between_trades
        ):
            return None

        side, swept_level = winner

        # Volume confirmation
        if self.cfg.min_volume_z > 0:
            vz = self._volume_z_score(bar)
            if vz < self.cfg.min_volume_z:
                self._n_volume_quality_rejects += 1
                return None

        # Risk sizing
        atr_window = hist[-self.cfg.atr_period:] if hist else []
        if len(atr_window) < 2:
            return None
        atr = sum(b.high - b.low for b in atr_window) / len(atr_window)
        if atr <= 0.0:
            return None
        stop_dist = self.cfg.atr_stop_mult * atr
        if stop_dist <= 0.0:
            return None
        risk_usd = equity * self.cfg.risk_per_trade_pct
        qty = risk_usd / stop_dist
        if qty <= 0.0:
            return None

        entry = bar.close
        # FIX: replace magic number 1.0 (was 1 USD on BTC = 0 ticks, vs
        # 4 ticks on MNQ) with a wick-aware ATR-floored buffer that
        # scales properly across instruments and is proportional to the
        # actual sweep depth (the structurally meaningful quantity).
        if side == "BUY":
            wick_depth = max(entry - bar.low, 0.0)
            wick_buffer = max(0.5 * wick_depth, 0.25 * atr)
            structure_stop = bar.low - wick_buffer
            atr_stop = entry - stop_dist
            stop = min(structure_stop, atr_stop)
            stop_dist_actual = entry - stop
            target = entry + self.cfg.rr_target * stop_dist_actual
        else:
            wick_depth = max(bar.high - entry, 0.0)
            wick_buffer = max(0.5 * wick_depth, 0.25 * atr)
            structure_stop = bar.high + wick_buffer
            atr_stop = entry + stop_dist
            stop = max(structure_stop, atr_stop)
            stop_dist_actual = stop - entry
            target = entry - self.cfg.rr_target * stop_dist_actual

        # ── Phase 3 v2: L2 overlay confirmation ────────────────────
        # Consult confirm_sweep_with_l2 to verify the swept level had
        # real stop liquidity sitting behind it.  When captures aren't
        # running yet, the gate's no_l2_yet pass-through preserves
        # legacy behavior.
        if self.cfg.enable_l2_overlay:
            try:
                from eta_engine.strategies.l2_overlay import confirm_sweep_with_l2
                l2_side = "LONG" if side == "BUY" else "SHORT"
                gate = confirm_sweep_with_l2(
                    symbol=self.cfg.l2_symbol,
                    swept_level=swept_level,
                    touch_dt=bar.timestamp,
                    side=l2_side,
                    min_stop_qty=self.cfg.l2_min_stop_qty,
                    window_seconds=self.cfg.l2_window_seconds,
                    hidden_qty_floor=self.cfg.l2_hidden_qty_floor,
                )
                if not gate.passed:
                    self._n_volume_quality_rejects += 1  # repurpose counter
                    return None
            except (ImportError, Exception):
                # Defensive: if overlay can't be loaded, fall back to
                # legacy behavior rather than crashing.
                pass

        from eta_engine.backtest.engine import _Open

        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        self._n_reclaims_fired += 1
        return _Open(
            entry_bar=bar, side=side, qty=qty, entry_price=entry,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=10.0, leverage=1.0,
            regime=f"sweep_reclaim_{side.lower()}_lvl{swept_level:.1f}",
        )


# ---------------------------------------------------------------------------
# Asset-class presets — keep config separation from BTC's RegimeGate convention
# ---------------------------------------------------------------------------


def mnq_intraday_sweep_preset() -> SweepReclaimConfig:
    """Calibrated for MNQ 5m bars during RTH. DeepSeek-tuned 2026-05-02."""
    return SweepReclaimConfig(
        level_lookback=30,
        reclaim_window=3,
        min_wick_pct=0.40,
        volume_z_lookback=20,
        min_volume_z=0.5,
        atr_period=14,
        atr_stop_mult=1.5,
        rr_target=2.0,
        risk_per_trade_pct=0.005,
        min_bars_between_trades=6,
        max_trades_per_day=2,
        warmup_bars=50,
    )


def nq_intraday_sweep_preset() -> SweepReclaimConfig:
    """Calibrated for NQ 5m bars during RTH.

    Identical mechanic to MNQ (same Nasdaq-100 underlying, same
    volatility profile per bar). NQ is $20/point vs MNQ's $2/point
    but the strategy uses ATR-stop sized by ``risk_per_trade_pct *
    equity / stop_distance`` so contract-size differences are
    absorbed automatically by the qty calculation.

    Defined as a separate factory (not an alias) so future NQ-
    specific tuning has a clean home.
    """
    return SweepReclaimConfig(
        level_lookback=30,
        reclaim_window=3,
        min_wick_pct=0.40,
        volume_z_lookback=20,
        min_volume_z=0.5,
        atr_period=14,
        atr_stop_mult=1.0,
        rr_target=2.0,
        risk_per_trade_pct=0.005,
        min_bars_between_trades=6,
        max_trades_per_day=4,
        warmup_bars=50,
    )


def btc_daily_sweep_preset() -> SweepReclaimConfig:
    """Calibrated for BTC 1h bars 24/7.

    Lookback 48 bars (= 2 days) captures daily-pivot sweeps;
    wick threshold 30% (BTC has more inherent volatility so
    proportionally smaller wicks still mean something); ATR-stop
    1.5; cooldown 12 bars between trades.
    """
    return SweepReclaimConfig(
        level_lookback=48,
        reclaim_window=3,
        min_wick_pct=0.30,
        volume_z_lookback=24,
        min_volume_z=0.3,
        atr_period=14,
        atr_stop_mult=1.5,
        rr_target=2.0,
        risk_per_trade_pct=0.005,
        min_bars_between_trades=12,
        max_trades_per_day=2,
        warmup_bars=72,
    )


def eth_daily_sweep_preset() -> SweepReclaimConfig:
    """Calibrated for ETH 1h bars. DeepSeek-tuned 2026-05-02.
    PAPER-SOAK TUNED: atr_stop 1.0→2.0 (1.0x was too tight — ETH 1h
    swings $80-150/bar. 2.0x gives $120-300 stop, enough to survive noise).
    rr_target bumped 2.0→2.5 for positive expectancy at lower WR."""
    return SweepReclaimConfig(
        level_lookback=48, reclaim_window=3,
        min_wick_pct=0.40, volume_z_lookback=24, min_volume_z=0.5,
        atr_period=14, atr_stop_mult=2.0,  # was 1.0 — too tight for ETH 1h
        rr_target=2.5,                      # was 2.0
        risk_per_trade_pct=0.005,
        min_bars_between_trades=12, max_trades_per_day=2, warmup_bars=72,
    )


def sol_daily_sweep_preset() -> SweepReclaimConfig:
    """Calibrated for SOL 1h bars.

    SOL is materially more volatile than ETH (historically ~1.5-2x
    BTC vol). Wider ATR-stop, lower wick threshold, looser volume
    z. RR target raised to 2.5 to compensate for the wider stops
    so the per-trade reward stays comparable.
    """
    return SweepReclaimConfig(
        level_lookback=48,
        reclaim_window=3,
        min_wick_pct=0.20,
        volume_z_lookback=24,
        min_volume_z=0.1,
        atr_period=14,
        atr_stop_mult=2.2,        # wider still
        rr_target=2.5,             # bump RR to keep edge per-trade
        risk_per_trade_pct=0.004,  # smaller risk per trade (higher vol)
        min_bars_between_trades=12,
        max_trades_per_day=2,
        warmup_bars=72,
    )


# ---------------------------------------------------------------------------
# Commodity presets — parameter-perfect per ticker
# ---------------------------------------------------------------------------


def gc_sweep_preset() -> SweepReclaimConfig:
    """Gold (GC) 1h — $100/pt, $25-40/h ATR, ~$250k notional/contract.
    Tuned 2026-05-08: atr_stop 2.5→3.0 (wider for gold's macro swings),
    rr_target 2.5→3.5 (bigger wins), max_trades 2→1 (selectivity)."""
    return SweepReclaimConfig(
        level_lookback=72, reclaim_window=3,
        min_wick_pct=0.40, volume_z_lookback=24, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=3.0, rr_target=3.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=1, warmup_bars=72,
    )


def cl_sweep_preset() -> SweepReclaimConfig:
    """Crude oil (CL) 1h — $1000/pt, $150-250/h ATR, ~$65k notional.
    Tuned 2026-05-08: atr_stop 1.5→2.5 (oil whipsaws), rr_target 2.5→3.0."""
    return SweepReclaimConfig(
        level_lookback=48, reclaim_window=3,
        min_wick_pct=0.30, volume_z_lookback=24, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=2.5, rr_target=3.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=72,
    )


def ng_sweep_preset() -> SweepReclaimConfig:
    """Natural gas (NG) 1h — $2-4/MMBtu, ATR $0.15-0.30/h on $2,500 notional.
    Most volatile commodity — widest ATR stop (4.0x = $0.60-1.20),
    highest RR (3.0 to compensate), highest vol_z filter (0.5).
    PAPER-SOAK TUNED: ATR stop 3.0→4.0, vol_z 0.2→0.5."""
    return SweepReclaimConfig(
        level_lookback=48, reclaim_window=3,
        min_wick_pct=0.25, volume_z_lookback=24, min_volume_z=0.5,
        atr_period=14, atr_stop_mult=4.0, rr_target=3.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=72,
    )


def eur_sweep_preset() -> SweepReclaimConfig:
    """Euro FX (6E) 1h — $125k notional, $5-10/h ATR.
    Tuned 2026-05-08: atr_stop 1.0→1.5 (FX ranges need room), rr_target 2.0→2.5."""
    return SweepReclaimConfig(
        level_lookback=72, reclaim_window=3,
        min_wick_pct=0.30, volume_z_lookback=24, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=1.5, rr_target=2.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=3, warmup_bars=72,
    )


def mes_sweep_preset() -> SweepReclaimConfig:
    """Micro ES (MES) 1h — $50/pt on $2,500 notional, 1/10th ES.
    Same volatility as MNQ/NQ index futures. MNQ-tuned params."""
    return SweepReclaimConfig(
        level_lookback=30, reclaim_window=3,
        min_wick_pct=0.40, volume_z_lookback=20, min_volume_z=0.5,
        atr_period=14, atr_stop_mult=1.5, rr_target=2.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=6,
        max_trades_per_day=4, warmup_bars=50,
    )


def mes_v2_sweep_preset() -> SweepReclaimConfig:
    """MES 5m rehab preset.

    This mirrors the registry's mes_sweep_reclaim_v2 overrides so lab,
    launch, and live dispatch do not silently inherit BTC defaults.
    """
    return SweepReclaimConfig(
        level_lookback=24, reclaim_window=3,
        min_wick_pct=0.25, volume_z_lookback=24, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=1.5, rr_target=2.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=3, warmup_bars=72,
    )


def m2k_sweep_preset() -> SweepReclaimConfig:
    """Micro Russell (M2K) 1h — $5/pt on $1,000 notional, 1/10th RTY.
    Similar vol to MNQ, slightly wider ranges."""
    return SweepReclaimConfig(
        level_lookback=30, reclaim_window=3,
        min_wick_pct=0.35, volume_z_lookback=20, min_volume_z=0.4,
        atr_period=14, atr_stop_mult=1.5, rr_target=2.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=8,
        max_trades_per_day=3, warmup_bars=50,
    )


def mym_sweep_preset() -> SweepReclaimConfig:
    """MYM 1h rehab preset for micro Dow exposure."""
    return SweepReclaimConfig(
        level_lookback=48, reclaim_window=3,
        min_wick_pct=0.30, volume_z_lookback=24, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=2.0, rr_target=2.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=72,
    )


def ym_sweep_preset() -> SweepReclaimConfig:
    """Mini Dow (YM) 1h — $5/pt on $5,000 notional, 1/2 ES.
    Wider ranges than MNQ, more structured trends."""
    return SweepReclaimConfig(
        level_lookback=30, reclaim_window=3,
        min_wick_pct=0.35, volume_z_lookback=20, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=2.0, rr_target=2.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=8,
        max_trades_per_day=3, warmup_bars=50,
    )


def mgc_sweep_preset() -> SweepReclaimConfig:
    """MGC 1h micro-gold preset.

    2026-05-12 refinement (quant audit synthesis):
      - atr_stop_mult 3.0 → 2.5
      - rr_target    3.0 → 3.5
      - min_volume_z 0.3 → 0.5 (NY-open hour focus)

    Rationale: mgc_sweep CPCV showed n=157, mean OOS sharpe +0.190
    with 96% positive splits — a real but small edge.  Per-trade R
    is positive but USD P&L is negative (PF=0.726) → friction
    dominated.  Range-bound mid-2026 gold means:
      - Tighter stop captures the mean-reversion edge before
        whipsaw eats it (3x ATR was permissive)
      - Wider target captures further reversion (sweeps in chop
        often go 3.5R+ before flopping)
      - Higher volume z gate filters London-session thin chop
        and concentrates on NY-open execution windows
    Falsifier: PF crosses 1.0 across the next 60 trades.  Red team:
    multiple-testing across 3 simultaneous knob tweaks — treat the
    +0.19 mean as the prior; require +0.30 OOS sharpe before claiming
    improvement is real.  min_wick_pct stays 0.40 — v2's relaxed 0.30
    wick demonstrated this is load-bearing.
    """
    return SweepReclaimConfig(
        level_lookback=48, reclaim_window=3,
        min_wick_pct=0.40, volume_z_lookback=24, min_volume_z=0.5,
        atr_period=14, atr_stop_mult=2.5, rr_target=3.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=72,
    )


def mgc_v2_sweep_preset() -> SweepReclaimConfig:
    """Failed MGC relaxed-wick rehab preset kept for audit reproducibility."""
    return SweepReclaimConfig(
        level_lookback=32, reclaim_window=3,
        min_wick_pct=0.30, volume_z_lookback=24, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=3.0, rr_target=3.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=72,
    )


def mcl_sweep_preset() -> SweepReclaimConfig:
    """MCL 1h micro-crude rehab preset."""
    return SweepReclaimConfig(
        level_lookback=48, reclaim_window=3,
        min_wick_pct=0.30, volume_z_lookback=24, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=2.0, rr_target=2.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=72,
    )


def zn_sweep_preset() -> SweepReclaimConfig:
    """10-Year T-Note (ZN) 1h — $1,000/pt on $110,000 notional.
    Tightest ranges — wider lookback, tighter stop, higher RR."""
    return SweepReclaimConfig(
        level_lookback=72, reclaim_window=4,
        min_wick_pct=0.40, volume_z_lookback=24, min_volume_z=0.5,
        atr_period=14, atr_stop_mult=1.0, rr_target=3.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=72,
    )


SWEEP_PRESET_FACTORIES = {
    "btc": btc_daily_sweep_preset,
    "cl": cl_sweep_preset,
    "eth": eth_daily_sweep_preset,
    "eur": eur_sweep_preset,
    "gc": gc_sweep_preset,
    "m2k": m2k_sweep_preset,
    "mcl": mcl_sweep_preset,
    "mes": mes_sweep_preset,
    "mes_v2": mes_v2_sweep_preset,
    "mgc": mgc_sweep_preset,
    "mgc_v2": mgc_v2_sweep_preset,
    "mnq": mnq_intraday_sweep_preset,
    "mym": mym_sweep_preset,
    "ng": ng_sweep_preset,
    "nq": nq_intraday_sweep_preset,
    "sol": sol_daily_sweep_preset,
    "ym": ym_sweep_preset,
    "zn": zn_sweep_preset,
}
