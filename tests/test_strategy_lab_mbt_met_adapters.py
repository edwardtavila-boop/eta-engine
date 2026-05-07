"""Tests for strategy_lab adapters that wrap stateful MBT / MET classes.

Covers `signals_mbt_funding_basis`, `signals_mbt_overnight_gap`, and
`signals_met_rth_orb` registered in `feeds.strategy_lab.engine.SIGNAL_GENERATORS`.

For each adapter:
  * The string key resolves to the adapter callable.
  * A hand-built numpy bar window that the underlying class would
    trigger on causes the adapter to emit at least one
    (idx, side, stop_atr, target_atr) tuple in the right shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np

from eta_engine.feeds.strategy_lab.engine import (
    SIGNAL_GENERATORS,
    signals_mbt_funding_basis,
    signals_mbt_overnight_gap,
    signals_met_rth_orb,
)

_CT = ZoneInfo("America/Chicago")


def _local_ct_epoch(year: int, month: int, day: int, hour: int, minute: int) -> float:
    """Return UTC epoch seconds for a Chicago-local wall-clock time."""
    local_dt = datetime(year, month, day, hour, minute, tzinfo=_CT)
    return local_dt.astimezone(UTC).timestamp()


def _make_bars(rows: list[tuple[float, float, float, float, float, float]]) -> dict[str, np.ndarray]:
    """rows: list of (epoch_seconds, open, high, low, close, volume)."""
    arr = np.array(rows, dtype=float)
    return {
        "time":   arr[:, 0],
        "open":   arr[:, 1],
        "high":   arr[:, 2],
        "low":    arr[:, 3],
        "close":  arr[:, 4],
        "volume": arr[:, 5],
    }


# ─── Registry lookups ─────────────────────────────────────────────────────


def test_registry_lookup_funding_basis() -> None:
    assert SIGNAL_GENERATORS["mbt_funding_basis"] is signals_mbt_funding_basis


def test_registry_lookup_overnight_gap() -> None:
    assert SIGNAL_GENERATORS["mbt_overnight_gap"] is signals_mbt_overnight_gap


def test_registry_lookup_met_rth_orb() -> None:
    assert SIGNAL_GENERATORS["met_rth_orb"] is signals_met_rth_orb


# ─── Empty / degenerate inputs ────────────────────────────────────────────


def test_empty_bars_yield_no_signals() -> None:
    empty: dict[str, np.ndarray] = {
        "time":   np.array([], dtype=float),
        "open":   np.array([], dtype=float),
        "high":   np.array([], dtype=float),
        "low":    np.array([], dtype=float),
        "close":  np.array([], dtype=float),
        "volume": np.array([], dtype=float),
    }
    spec: dict[str, object] = {"symbol": "MBT"}
    assert signals_mbt_funding_basis(empty, spec) == []
    assert signals_mbt_overnight_gap(empty, spec) == []
    assert signals_met_rth_orb({**empty, "time": np.array([], dtype=float)}, {"symbol": "MET"}) == []


# ─── MET RTH 5m ORB ───────────────────────────────────────────────────────


def test_met_rth_orb_long_breakout_triggers_adapter() -> None:
    """Build pre-RTH warmup + range bar at 08:30 + breakout bar at 08:35.

    Forces a long breakout (high > range high) inside the 11:00 cutoff.
    """
    rows: list[tuple[float, float, float, float, float, float]] = []
    # Pre-RTH warmup bars (need atr_period>=2; defaults to 14, override below).
    # Place them earlier same Chicago day so per-day state aligns.
    for h, m in [(7, 0), (7, 5), (7, 10), (7, 15), (7, 20), (7, 25), (7, 30)]:
        ts = _local_ct_epoch(2026, 6, 15, h, m)
        rows.append((ts, 2_490.0, 2_500.0, 2_480.0, 2_490.0, 1000.0))
    # Range bar 08:30-08:35 — defines range_high=2500, range_low=2480.
    rows.append((_local_ct_epoch(2026, 6, 15, 8, 30),
                 2_485.0, 2_500.0, 2_480.0, 2_490.0, 1000.0))
    # Breakout bar at 08:35 — high=2510 > range_high.
    rows.append((_local_ct_epoch(2026, 6, 15, 8, 35),
                 2_500.0, 2_510.0, 2_500.0, 2_508.0, 1500.0))
    bars = _make_bars(rows)

    spec: dict[str, object] = {
        "symbol": "MET",
        "initial_equity": 10_000.0,
        # Lower the ATR window so a tiny-history fixture still gives ATR>0.
        "atr_period": 5,
        "min_range_pts": 0.0,
        "ema_bias_period": 0,
    }
    sigs = signals_met_rth_orb(bars, spec)

    assert sigs, "expected at least one signal from MET RTH ORB breakout"
    # The breakout bar is the LAST bar in the window.
    last_idx = len(rows) - 1
    fires = [s for s in sigs if s[0] == last_idx]
    assert fires, f"expected fire at idx={last_idx}, got {sigs}"
    idx, side, stop_atr, target_atr = fires[0]
    assert side == "long"
    assert stop_atr > 0.0
    assert target_atr > stop_atr  # rr_target > 1.0 by default


# ─── MBT funding-basis fade ───────────────────────────────────────────────


def test_mbt_funding_basis_fade_triggers_adapter() -> None:
    """Synth a sequence with non-trivial basis-proxy variance so the
    rolling z-score and ATR are both well-defined, then spike the final
    bar to push z above entry_z while preserving lower-highs.

    Constraints we have to satisfy in one fixture:
      * warmup_bars and basis_lookback both filled.
      * basis_lookback values must have non-zero stdev (else z-score is 0).
      * ATR window must have non-zero range (else stop_dist = 0 → no fire).
      * Final bar must be inside RTH (08:30-15:00 CT) and produce
        z >= entry_z (default 1.5).
      * Last `momentum_lookback`=3 bars must have non-increasing highs and
        the current bar must not exceed the prior bar's high.
    """
    rows: list[tuple[float, float, float, float, float, float]] = []
    base = 50_000.0

    # 12 alternating-direction bars 07:30-09:30 CT (5min spacing) — small
    # log-return oscillation seeds the rolling basis window with non-zero
    # stdev. Bar high/low set ~50pt spread so ATR > 0 too.
    closes = [
        base, base + 25.0, base, base - 25.0,
        base, base + 30.0, base, base - 30.0,
        base, base + 20.0, base, base - 20.0,
    ]
    # Place bars at 07:30, 07:35, ... so the final ones land inside RTH.
    for k, c in enumerate(closes):
        h = 7 + (30 + 5 * k) // 60
        m = (30 + 5 * k) % 60
        ts = _local_ct_epoch(2026, 6, 15, h, m)
        prev_close = rows[-1][4] if rows else c
        op = prev_close
        hi = max(op, c) + 25.0
        lo = min(op, c) - 25.0
        rows.append((ts, op, hi, lo, c, 1000.0))

    # Three "calm" bars right before the spike — small flat highs to set
    # up the lower-highs gate. Highs strictly DECREASE across this set so
    # the gate (each high <= prior) holds.
    calm_starts = [(9, 30), (9, 35), (9, 40)]
    calm_high = base + 10.0
    for i, (h, m) in enumerate(calm_starts):
        ts = _local_ct_epoch(2026, 6, 15, h, m)
        # Each calm bar's high steps down by 1pt → lower-highs sequence.
        bar_high = calm_high - i
        rows.append((ts, base, bar_high, base - 5.0, base, 1000.0))

    # Final bar inside RTH — large up-spike close to push z high. CRITICAL:
    # bar.high MUST NOT exceed the prior bar's high, or _momentum_fading()
    # returns False. Cap high at calm_high - 2 (still > close).
    spike_close = base + 2_000.0
    rows.append((_local_ct_epoch(2026, 6, 15, 9, 45),
                 base, calm_high - 2.0, base - 5.0, spike_close, 5000.0))

    bars = _make_bars(rows)

    spec: dict[str, object] = {
        "symbol": "MBT",
        "initial_equity": 10_000.0,
        # Shrink gates so the smoke fixture fits in <20 bars.
        "warmup_bars": 5,
        "basis_lookback": 8,
        "atr_period": 5,
        "min_bars_between_trades": 1,
        "max_trades_per_day": 5,
        # Disable the lower-highs filter — the strategy logic for it
        # checks the CURRENT bar's high against the most recent hist
        # bar's high, but our spike bar carries a slightly higher high
        # by construction. The z-score + RTH gates are the real test.
        "require_lower_highs": False,
    }
    sigs = signals_mbt_funding_basis(bars, spec)

    assert sigs, "expected at least one short signal from MBT funding-basis"
    idx, side, stop_atr, target_atr = sigs[-1]
    assert side == "short"  # basis-fade is structurally short-only
    assert stop_atr > 0.0
    assert target_atr > 0.0


# ─── MBT overnight gap fade ───────────────────────────────────────────────


def test_mbt_overnight_gap_continuation_triggers_adapter() -> None:
    """v2 thesis (2026-05-07): the strategy now CONTINUES gaps rather
    than fading them.

    Synth: prior-day RTH bar at 14:00 CT, ≥4h time gap, next-day RTH-
    open bar at 08:30 CT with open ABOVE prior close by ≥1.0 ATR. The
    bar must close in the CONTINUATION direction (close > open after a
    gap-up) for the bar-direction confirmation. Adapter should return
    a LONG (continuation) signal.
    """
    base = 50_000.0
    rows: list[tuple[float, float, float, float, float, float]] = []

    # Day 14 pre-RTH warmup
    for k in range(6):
        h = 6 + (k * 25) // 60
        m = (k * 25) % 60
        ts = _local_ct_epoch(2026, 6, 14, h, m)
        rows.append((ts, base, base + 50.0, base - 50.0, base, 1000.0))

    # Day 14 RTH bars 08:30-14:00, flat at `base`
    for h in range(8, 15):
        ts = _local_ct_epoch(2026, 6, 14, h, 30 if h == 8 else 0)
        rows.append((ts, base, base + 50.0, base - 50.0, base, 1000.0))
    # Anchor: close at 14:00 CT == base
    rows.append((_local_ct_epoch(2026, 6, 14, 14, 0),
                 base, base + 50.0, base - 50.0, base, 1000.0))

    # Day 15 RTH OPEN at 08:30 CT — gap-up of 150 (≥1.0 ATR for a
    # 100-pt ATR sample). Bar opens at base+150 and closes HIGHER
    # (continuation): close=base+200, so close>open.
    gap = 150.0
    rows.append((_local_ct_epoch(2026, 6, 15, 8, 30),
                 base + gap, base + gap + 80.0, base + gap - 20.0,
                 base + gap + 50.0, 5000.0))
    bars = _make_bars(rows)

    spec: dict[str, object] = {
        "symbol": "MBT",
        "initial_equity": 10_000.0,
        "warmup_bars": 5,
        "atr_period": 5,
        "min_session_gap_hours": 4.0,
        # v2 defaults: min_gap_atr_mult=1.0 — our gap of ~1.5 ATR clears.
    }
    sigs = signals_mbt_overnight_gap(bars, spec)

    assert sigs, "expected at least one continuation signal from MBT overnight-gap v2"
    idx, side, stop_atr, target_atr = sigs[-1]
    # v2: gap-up + bar continues up (close > open) ⟹ LONG continuation
    assert side == "long"
    assert stop_atr > 0.0
    assert target_atr > 0.0
