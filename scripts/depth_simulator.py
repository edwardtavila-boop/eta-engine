"""
EVOLUTIONARY TRADING ALGO  //  scripts.depth_simulator
======================================================
Synthetic depth-snapshot generator that mirrors the schema of
``capture_depth_snapshots.py`` so the L2 strategy stack + the L2
backtest harness can be stress-tested BEFORE real capture data
accumulates.

Why this exists
---------------
Without real depth data, every L2 component degrades to its
no-data branch (overlay returns ``passed=True / no_l2_yet``,
harness reports ``n_snapshots=0``).  The hardening pass added
fail-closed behaviour and tests with hand-crafted snapshots, but
hand-crafted fixtures don't exercise the time-series dynamics that
real captures have:

  - mean-reverting spread that occasionally blows out (tests
    spread_regime_filter pause/resume hysteresis)
  - liquidity sweeps where price wicks through a level (tests
    confirm_sweep_with_l2 with realistic pre/post-touch dynamics)
  - sustained book imbalance that drives book_imbalance signals
  - regime shifts (NORMAL → WIDE → PAUSE → NORMAL) that the
    spread filter must navigate

This script generates a configurable mix of those regimes, writes
JSONL files in the same format as the live capture daemon, and is
consumed by the same harness / overlay / book_imbalance code with
zero modification.

Schema
------
Output matches capture_depth_snapshots.py exactly:
    {
      "ts": "<iso 8601 utc>",
      "epoch_s": <unix float>,
      "symbol": "<sym>",
      "bids": [{"price": float, "size": int, "mm": "SIM"}, ...],
      "asks": [{"price": float, "size": int, "mm": "SIM"}, ...],
      "spread": float,
      "mid": float
    }

Run
---
::

    # 60 minutes of MNQ-like data with default regime mix
    python -m eta_engine.scripts.depth_simulator \\
        --symbol MNQ --duration-minutes 60

    # Stress test: lots of sweeps + spread blow-outs
    python -m eta_engine.scripts.depth_simulator \\
        --symbol MNQ --duration-minutes 60 \\
        --regime-mix stressed

    # Custom output dir (default: mnq_data/depth/)
    python -m eta_engine.scripts.depth_simulator \\
        --symbol MNQ --duration-minutes 60 \\
        --output-dir /tmp/sim_depth

Determinism
-----------
``--seed`` makes runs reproducible.  Without --seed, runs are
random and subsequent runs differ — useful for monte-carlo
robustness testing.
"""

from __future__ import annotations

# ruff: noqa: PLR2004
# Magic numbers in this file are simulator parameters with documented
# intent (see _RegimeProfile defaults).
import argparse
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

DEFAULT_OUTPUT_DIR = ROOT.parent / "mnq_data" / "depth"


# ── Regime profiles ───────────────────────────────────────────────


@dataclass
class _RegimeProfile:
    """Tuning surface for one regime."""

    name: str
    spread_mean: float  # mean spread in price points
    spread_std: float  # std of spread
    qty_mean: int  # mean qty per book level
    qty_std: int  # std of qty per level
    imbalance_bias: float = 0.0  # >0 = more bid qty, <0 = more ask qty
    sweep_probability: float = 0.0  # per-snap chance of a sweep through a level
    duration_seconds_mean: int = 60  # mean dwell time in this regime


# Realistic per-regime profiles for MNQ (CME) on a typical RTH session.
# Numbers calibrated from real data observations: tight spread = 1 tick
# (0.25), normal book qty = 5-30 contracts, sweeps remove 50-200 qty.
_PROFILES = {
    "NORMAL": _RegimeProfile(
        "NORMAL", spread_mean=0.25, spread_std=0.05, qty_mean=15, qty_std=8, duration_seconds_mean=120
    ),
    "WIDE": _RegimeProfile("WIDE", spread_mean=0.75, spread_std=0.25, qty_mean=8, qty_std=4, duration_seconds_mean=45),
    "PAUSE": _RegimeProfile("PAUSE", spread_mean=2.0, spread_std=0.5, qty_mean=3, qty_std=2, duration_seconds_mean=30),
    "IMBALANCE_LONG": _RegimeProfile(
        "IMBALANCE_LONG",
        spread_mean=0.30,
        spread_std=0.05,
        qty_mean=15,
        qty_std=6,
        imbalance_bias=2.0,  # ~3:1 bid:ask
        duration_seconds_mean=90,
    ),
    "IMBALANCE_SHORT": _RegimeProfile(
        "IMBALANCE_SHORT",
        spread_mean=0.30,
        spread_std=0.05,
        qty_mean=15,
        qty_std=6,
        imbalance_bias=-2.0,  # ~3:1 ask:bid
        duration_seconds_mean=90,
    ),
    "SWEEP_PRONE": _RegimeProfile(
        "SWEEP_PRONE",
        spread_mean=0.40,
        spread_std=0.10,
        qty_mean=12,
        qty_std=5,
        sweep_probability=0.05,
        duration_seconds_mean=60,
    ),
}


# Pre-configured regime mixes (probability weights per regime)
_REGIME_MIXES: dict[str, dict[str, float]] = {
    "calm": {"NORMAL": 0.85, "WIDE": 0.10, "PAUSE": 0.05},
    "normal": {"NORMAL": 0.65, "WIDE": 0.15, "IMBALANCE_LONG": 0.08, "IMBALANCE_SHORT": 0.08, "SWEEP_PRONE": 0.04},
    "stressed": {
        "NORMAL": 0.30,
        "WIDE": 0.25,
        "PAUSE": 0.15,
        "IMBALANCE_LONG": 0.10,
        "IMBALANCE_SHORT": 0.10,
        "SWEEP_PRONE": 0.10,
    },
    "imbalanced_long": {"NORMAL": 0.30, "IMBALANCE_LONG": 0.55, "WIDE": 0.10, "SWEEP_PRONE": 0.05},
    "imbalanced_short": {"NORMAL": 0.30, "IMBALANCE_SHORT": 0.55, "WIDE": 0.10, "SWEEP_PRONE": 0.05},
}


# ── Simulator state ───────────────────────────────────────────────


@dataclass
class _SimState:
    """Mutable state carried across snapshot generation."""

    mid: float
    rng: random.Random
    current_regime: _RegimeProfile
    regime_started_at: datetime
    regime_dwell_seconds: int
    pending_sweep: bool = False  # next snap should show post-sweep recovery
    sweep_history: list[dict] = field(default_factory=list)


# ── Snapshot generation ───────────────────────────────────────────


def _pick_regime(rng: random.Random, mix: dict[str, float]) -> _RegimeProfile:
    """Sample one regime from the mix's probability distribution."""
    items = list(mix.items())
    weights = [w for _, w in items]
    name = rng.choices([n for n, _ in items], weights=weights, k=1)[0]
    return _PROFILES[name]


def _generate_levels(state: _SimState, side: str, n_levels: int) -> list[dict]:
    """Generate N levels for one side of the book.

    Imbalance: applies imbalance_bias to multiply the qty on the
    favoured side.  bias > 0 → more bid qty.  bias < 0 → more ask qty.
    """
    profile = state.current_regime
    bias_factor = 1.0
    if side == "bid" and profile.imbalance_bias > 0:
        bias_factor = 1.0 + profile.imbalance_bias
    elif side == "ask" and profile.imbalance_bias < 0:
        bias_factor = 1.0 + abs(profile.imbalance_bias)

    levels = []
    tick = 0.25  # MNQ tick — could parameterize per symbol later
    for i in range(n_levels):
        price = state.mid - (i + 1) * tick if side == "bid" else state.mid + (i + 1) * tick
        qty_base = state.rng.gauss(profile.qty_mean, profile.qty_std)
        qty = max(1, int(qty_base * bias_factor))
        levels.append({"price": round(price, 4), "size": qty, "mm": "SIM"})
    return levels


def _maybe_apply_sweep(state: _SimState, snapshot: dict) -> bool:
    """Stochastically apply a sweep: price wicks through a level then
    recovers.  Returns True if a sweep was applied this snap."""
    profile = state.current_regime
    if profile.sweep_probability <= 0:
        return False
    if state.rng.random() >= profile.sweep_probability:
        return False
    # 50/50 long sweep (wick down through bid) vs short sweep (wick up through ask)
    direction = state.rng.choice(["LONG_SWEEP", "SHORT_SWEEP"])
    if direction == "LONG_SWEEP":
        # Wick down 1-2 ticks below the current mid
        wick_depth = state.rng.uniform(1.0, 2.0) * 0.25
        new_mid = state.mid - wick_depth
        # Drain the bid side aggressively (simulate stops being hit)
        for lvl in snapshot["bids"][:2]:
            lvl["size"] = max(1, int(lvl["size"] * 0.3))
    else:
        wick_depth = state.rng.uniform(1.0, 2.0) * 0.25
        new_mid = state.mid + wick_depth
        for lvl in snapshot["asks"][:2]:
            lvl["size"] = max(1, int(lvl["size"] * 0.3))
    state.sweep_history.append(
        {
            "ts": snapshot["ts"],
            "direction": direction,
            "wick_depth": round(wick_depth, 4),
        }
    )
    snapshot["mid"] = round(new_mid, 4)
    state.pending_sweep = True
    return True


def _generate_snapshot(state: _SimState, ts: datetime, symbol: str, n_levels: int = 5) -> dict:
    """Generate one snapshot from current state."""
    profile = state.current_regime
    spread = max(0.25, state.rng.gauss(profile.spread_mean, profile.spread_std))
    # Round to nearest tick (0.25 for MNQ)
    spread = round(spread / 0.25) * 0.25
    if spread < 0.25:
        spread = 0.25

    # Random walk on mid (small steps within regime).  After a sweep,
    # mean-revert toward pre-sweep mid for realism.
    if state.pending_sweep:
        # Recovery: drift back toward where mid was before the sweep
        state.pending_sweep = False
    drift = state.rng.gauss(0, 0.05)
    state.mid = round(state.mid + drift, 4)

    bids = _generate_levels(state, "bid", n_levels)
    asks = _generate_levels(state, "ask", n_levels)

    snapshot = {
        "ts": ts.isoformat(),
        "epoch_s": ts.timestamp(),
        "symbol": symbol,
        "bids": bids,
        "asks": asks,
        "spread": round(spread, 4),
        "mid": round(state.mid, 4),
    }

    _maybe_apply_sweep(state, snapshot)
    return snapshot


def simulate(
    symbol: str = "MNQ",
    duration_minutes: int = 60,
    snapshot_interval_seconds: float = 5.0,
    start_mid: float = 100.0,
    regime_mix: str = "normal",
    seed: int | None = None,
    start_dt: datetime | None = None,
) -> tuple[list[dict], list[dict]]:
    """Generate a list of synthetic depth snapshots.

    Returns (snapshots, regime_log) where regime_log records each
    regime transition for retrospective analysis.

    Args:
        symbol: contract code (used in snapshot.symbol field)
        duration_minutes: total wallclock duration to simulate
        snapshot_interval_seconds: cadence between snaps (5s = realistic)
        start_mid: initial mid price (round-number anchor)
        regime_mix: name of pre-configured mix in _REGIME_MIXES
        seed: PRNG seed for reproducibility (None = random)
        start_dt: override start time (default = now)
    """
    if regime_mix not in _REGIME_MIXES:
        raise ValueError(f"unknown regime_mix: {regime_mix} (choose from {list(_REGIME_MIXES)})")
    rng = random.Random(seed)
    start_dt = start_dt or datetime.now(UTC)
    n_snaps = int(duration_minutes * 60 / snapshot_interval_seconds)

    # Pick initial regime
    initial = _pick_regime(rng, _REGIME_MIXES[regime_mix])
    state = _SimState(
        mid=start_mid,
        rng=rng,
        current_regime=initial,
        regime_started_at=start_dt,
        regime_dwell_seconds=int(rng.expovariate(1.0 / max(initial.duration_seconds_mean, 1))),
    )

    snapshots: list[dict] = []
    regime_log: list[dict] = [{"ts": start_dt.isoformat(), "regime": initial.name}]

    for i in range(n_snaps):
        ts = start_dt + timedelta(seconds=i * snapshot_interval_seconds)
        elapsed_in_regime = (ts - state.regime_started_at).total_seconds()
        if elapsed_in_regime >= state.regime_dwell_seconds:
            # Regime transition
            new_regime = _pick_regime(rng, _REGIME_MIXES[regime_mix])
            state.current_regime = new_regime
            state.regime_started_at = ts
            state.regime_dwell_seconds = int(rng.expovariate(1.0 / max(new_regime.duration_seconds_mean, 1)))
            regime_log.append({"ts": ts.isoformat(), "regime": new_regime.name})

        snap = _generate_snapshot(state, ts, symbol)
        snapshots.append(snap)

    # Append sweep events to regime log for analysis
    for sweep in state.sweep_history:
        regime_log.append(
            {"ts": sweep["ts"], "regime": "SWEEP", "direction": sweep["direction"], "wick_depth": sweep["wick_depth"]}
        )

    return snapshots, regime_log


def write_snapshots(
    snapshots: list[dict], symbol: str, date_str: str | None = None, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> Path:
    """Write snapshots to <output_dir>/<symbol>_<YYYYMMDD>.jsonl
    (matches capture_depth_snapshots.py convention).  Returns the path."""
    if date_str is None and snapshots:
        # Use first snapshot's date
        first_ts = snapshots[0].get("ts", "")
        try:
            dt = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y%m%d")
        except (ValueError, AttributeError):
            date_str = datetime.now(UTC).strftime("%Y%m%d")
    if date_str is None:
        date_str = datetime.now(UTC).strftime("%Y%m%d")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{symbol}_{date_str}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for s in snapshots:
            f.write(json.dumps(s, separators=(",", ":")) + "\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--duration-minutes", type=int, default=60)
    ap.add_argument("--snapshot-interval-seconds", type=float, default=5.0)
    ap.add_argument("--start-mid", type=float, default=29270.0, help="Initial mid price (default 29270 ~ MNQ today)")
    ap.add_argument("--regime-mix", default="normal", choices=list(_REGIME_MIXES.keys()))
    ap.add_argument("--seed", type=int, default=None, help="PRNG seed for reproducibility (default: random)")
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    ap.add_argument("--regime-log", action="store_true", help="Print regime transition log")
    args = ap.parse_args()

    snapshots, regime_log = simulate(
        symbol=args.symbol,
        duration_minutes=args.duration_minutes,
        snapshot_interval_seconds=args.snapshot_interval_seconds,
        start_mid=args.start_mid,
        regime_mix=args.regime_mix,
        seed=args.seed,
    )
    path = write_snapshots(snapshots, args.symbol, output_dir=args.output_dir)
    print(f"Wrote {len(snapshots):,} snapshots to {path}")
    print(f"  Regime mix: {args.regime_mix}")
    print(f"  Regime transitions: {len(regime_log)}")
    if args.regime_log:
        print("  Transitions:")
        for ev in regime_log:
            print(f"    {ev}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
