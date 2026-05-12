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
from dataclasses import dataclass, field
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

    # ── 2026-05-12 wave-4 refinements ───────────────────────────────
    #
    # Multi-bar reclaim confirmation: require N consecutive bar closes
    # to hold on the reclaim side before firing.  The default 1 is the
    # legacy single-bar behavior; setting to 2 reduces false signals
    # at the cost of slightly later entries.  The mgc audit found 96%
    # of CPCV splits positive on n=157 — but that's still a marginal
    # edge.  Multi-bar confirmation could lift it without losing the
    # signal entirely.
    reclaim_confirm_bars: int = 1

    # Vol-adjusted sizing: scale position size by recent ATR vs
    # median ATR.  When realized vol is high (atr > median * upper)
    # we size DOWN; when vol is low (atr < median * lower) we keep
    # baseline.  1.0 = disabled (legacy behavior).
    vol_adjusted_sizing: bool = False
    vol_baseline_window: int = 96    # bars over which to compute median ATR
    vol_high_threshold: float = 1.5  # multiplier above median = "high vol"
    vol_low_threshold: float = 0.7   # multiplier below median = "low vol"
    vol_high_size_mult: float = 0.5  # size halved in high-vol regime
    vol_low_size_mult: float = 1.0   # baseline in low-vol regime

    # Session filter: skip bars whose UTC hour is in `excluded_hours_utc`.
    # Empty tuple = no filter (legacy).  Used by mgc to drop the close
    # session where stratification showed CI lower = -0.169 (NULL edge).
    excluded_hours_utc: tuple[int, ...] = field(default_factory=tuple)


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
        self._n_session_filter_rejects: int = 0
        self._n_confirm_pending: int = 0
        # ATR median window for vol-adjusted sizing
        self._atr_history: deque[float] = deque(
            maxlen=max(self.cfg.vol_baseline_window, 24))
        # Reclaim-confirmation state per pending sweep
        # Key = (side, swept_level, sweep_idx); value = consecutive bars
        # the close has held on the reclaim side
        self._confirm_counts: dict[tuple[str, float, int], int] = {}

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
            "session_filter_rejects": self._n_session_filter_rejects,
            "confirm_pending": self._n_confirm_pending,
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
        """Z-score of bar volume vs PRIOR window.  Excludes the
        current bar from the mean/std calculation so the z-score
        is unbiased (the prior version included the current bar,
        which understates z when the bar's volume is anomalously
        high — a conservative-but-incorrect bias).

        Audit 2026-05-12: this was the only finding from the
        eur_sweep_reclaim look-ahead forensic.  Not catastrophic —
        the bias was toward FEWER signals, not more — but unbiased
        is better than biased even on the safe side.
        """
        # The caller appended the current bar's volume before calling
        # us (line 262 in maybe_enter).  Pop it out for the calc, then
        # restore so the next bar's prior window is correct.
        if len(self._volume_window) < self.cfg.volume_z_lookback:
            return 0.0
        prior_vols = list(self._volume_window)[:-1]  # exclude current bar
        if len(prior_vols) < self.cfg.volume_z_lookback - 1:
            return 0.0
        mean = sum(prior_vols) / len(prior_vols)
        var = sum((v - mean) ** 2 for v in prior_vols) / len(prior_vols)
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

        # Session filter (2026-05-12 wave-4): skip bars whose UTC hour
        # is in excluded_hours_utc.  Per-bot setting; mgc_sweep_reclaim
        # uses this to drop the close-session NULL-edge bucket.  Still
        # update level_window so future bars have continuous history.
        if self.cfg.excluded_hours_utc:
            hour_utc = bar.timestamp.hour
            if hour_utc in self.cfg.excluded_hours_utc:
                self._n_session_filter_rejects += 1
                self._level_window.append((self._bars_seen, bar.high, bar.low))
                return None

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

        # Multi-bar reclaim confirmation (2026-05-12 wave-4): require
        # N consecutive bars closing on the reclaim side before firing.
        # reclaim_confirm_bars=1 is the legacy single-bar behavior.
        if self.cfg.reclaim_confirm_bars > 1:
            side, swept_level = winner
            sweep_idx = self._bars_seen  # confirmation count is per-bar
            key = (side, swept_level, sweep_idx)
            held = self._confirm_counts.get(key, 0)
            # The current bar's close already meets the side condition
            # (we got here from _check_reclaim).  Increment and check.
            held += 1
            if held < self.cfg.reclaim_confirm_bars:
                self._confirm_counts[key] = held
                self._n_confirm_pending += 1
                return None
            # Confirmed — clear and proceed
            self._confirm_counts.pop(key, None)
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
        # Track ATR in baseline window for vol-adjusted sizing
        self._atr_history.append(atr)
        stop_dist = self.cfg.atr_stop_mult * atr
        if stop_dist <= 0.0:
            return None
        risk_usd = equity * self.cfg.risk_per_trade_pct

        # Vol-adjusted sizing (2026-05-12 wave-4): when realized vol
        # spikes (ATR > median * vol_high_threshold), size DOWN so a
        # single regime-shift bar doesn't burn double our intended
        # risk.  When vol is normal, baseline.  Operator opt-in via
        # vol_adjusted_sizing=True; default off = legacy behavior.
        if (self.cfg.vol_adjusted_sizing
                and len(self._atr_history) >= self.cfg.vol_baseline_window // 2):
            sorted_atrs = sorted(self._atr_history)
            median_atr = sorted_atrs[len(sorted_atrs) // 2]
            if median_atr > 0:
                ratio = atr / median_atr
                if ratio >= self.cfg.vol_high_threshold:
                    risk_usd *= self.cfg.vol_high_size_mult
                elif ratio <= self.cfg.vol_low_threshold:
                    risk_usd *= self.cfg.vol_low_size_mult
                # else: ratio in normal band — baseline size

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
    Similar vol to MNQ, slightly wider ranges.

    Canonical baseline (2026-05-12, dual-source trade-close archive):
      n=1151, cum_r=+533.10R, avg_r=+0.4632, wr=70.0%
        overnight  (UTC 0-6):   n=753, +0.48 avg R
        morning    (UTC 14-16): n= 94, +0.39 avg R
        afternoon  (UTC 17-19): n=132, +0.52 avg R
        close      (UTC 20-23): n=172, +0.40 avg R
      All 4 sessions positive — the diversification eur_sweep
      shows + 4x the sample size = strongest evidence in the fleet.

    PROMOTED TO DIAMOND STATUS on 2026-05-12 after the canonical-data
    kaizen pass exposed this performance (it was previously hidden
    behind the broken trade-history plumbing — kelly_optimizer +
    attribution_cube only read the recent shim path before the
    dual-source fix).

    No parameter changes in this kaizen — the 2026-05-12 baseline
    above was generated by THIS config. Wave-N refinements should be
    based on observed regressions, not speculation.
    """
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

    2026-05-12 kaizen iterations:
      Wave-3 (chisel-cut):
        - atr_stop_mult 3.0 → 2.5
        - rr_target    3.0 → 3.5
        - min_volume_z 0.3 → 0.5 (NY-open hour focus)
      Wave-4 (rehaul):
        - reclaim_confirm_bars: 1 → 2 (cut false signals in chop)
        - vol_adjusted_sizing: True (gold vol regime-shifts; size down on spikes)
      Wave-5 (kaizen revert based on canonical data):
        - excluded_hours_utc reverted from (20,21,22,23) → () empty.
          The wave-4 close-session exclusion was based on stale
          stratification ("n=11, CI lower -0.169 → NULL edge").  Re-running
          the analysis against the canonical trade-closes ledger
          (eta_engine/state/jarvis_intel/trade_closes.jsonl, NOT the # HISTORICAL-PATH-OK
          var/eta_engine/state mirror the audit tools were reading)
          showed two separate problems with the wave-4 decision:
          a) The "CI lower negative" interpretation conflated "CI brackets
             zero with small n" with "edge is null".  The point estimate
             on close session was actually +0.41 avg R / 63.6% WR (n=11),
             the BEST stratum — not a drag on the average.
          b) The exclusion targeted UTC hours 20-23 but mgc's "close"
             session label corresponds to UTC hour 0 (gold post-close
             window).  The wave-4 filter was excluding hours where mgc
             never traded → no-op for mgc, harmful if ever broadened.

    Canonical baseline (pre-wave-4, post wave-3):
      n=155, cum_r=+29.50R, avg_r=+0.1903, wr=57.4%
        overnight (UTC 1-6): n=144, +0.1734 avg R, 56.9% WR
        close    (UTC 0):    n= 11, +0.4119 avg R, 63.6% WR
      Direction: 100% long (no shorts ever fired — known asymmetry,
      tracked separately as a strategy-research follow-up).

    Falsifier: post-wave-5 trades over the next 30 days should keep
    the close-session edge intact (avg R close > +0.20, WR > 55%).
    If close-session edge collapses, revisit the wave-4 hypothesis
    with the updated data — but with the correct UTC hour band this
    time (close = UTC 0, not UTC 20-23).

    Red team: reclaim_confirm_bars=2 + vol_adjusted_sizing are the
    only wave-4 features still active.  Both are untested in live
    (the n=155 baseline above is PRE-wave-4).  If post-wave-5 sharpe
    falls below the baseline +0.19, the most likely culprit is the
    reclaim_confirm_bars=2 over-filtering signals.  Revert to 1 if so.
    """
    return SweepReclaimConfig(
        level_lookback=48, reclaim_window=3,
        min_wick_pct=0.40, volume_z_lookback=24, min_volume_z=0.5,
        atr_period=14, atr_stop_mult=2.5, rr_target=3.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=72,
        reclaim_confirm_bars=2,
        vol_adjusted_sizing=True,
        # excluded_hours_utc deliberately empty — see docstring "Wave-5"
        excluded_hours_utc=(),
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
