"""
EVOLUTIONARY TRADING ALGO  //  strategies.l2_overlay
====================================================
Phase-3 of the IBKR Pro upgrade path: L2 confirmation overlay for
existing strategies (volume_profile_v2, sweep_reclaim_v2,
anchor_sweep_v2).

Why this exists
---------------
Per docs/IBKR_PRO_DATA_INVENTORY.md Phase 3:
> volume_profile_mnq → v2 consuming buy/sell-split volume.  Should
> preserve strict-gate pass while reducing false-POC pulls.
> sweep_reclaim family → v2 confirming wick sweep with actual
> stop-cluster L2 data at the swept level.
> anchor_sweep → v2 with pre-touch depth check on the anchor.

Rather than fork each strategy file (3+ branches that drift), this
module exposes a thin OVERLAY that any existing strategy can opt
into.  At signal time the overlay reads the most recent depth
snapshot from `mnq_data/depth/<SYMBOL>_<YYYYMMDD>.jsonl` and
applies a confirmation gate:

  - sweep_reclaim_v2: was there at least N contracts of stop
    liquidity sitting at the swept level BEFORE the wick pierced
    it?  (real sweep vs technical noise)
  - volume_profile_v2: did the entry-side of the book have higher
    queue weight than the opposite side?  (POC pull confirmation)
  - anchor_sweep_v2: depth snapshot at anchor-touch time had real
    qty inside ATR distance of the anchor

Storage
-------
This module is read-only on the depth files; it never writes.

Failure modes
-------------
If no depth snapshot exists for the bar's timestamp window, the
overlay returns ``GateResult(passed=True, reason="no_l2_yet")`` —
the legacy strategy decision flows through unchanged.  This makes
the overlay safe to wire into live strategies BEFORE Phase 1
captures have accumulated meaningful L2 history.
"""
# ruff: noqa: SIM115, SIM108
# SIM115: opener is captured into a variable so the same with-block
# handles either path.open() OR gzip.open() — context-manager wrap
# happens at the call site.
# SIM108: explicit if/else is clearer than nested ternaries here.
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPTH_DIR = ROOT.parent / "mnq_data" / "depth"


@dataclass
class GateResult:
    """Pass/fail result with a reason for logging."""
    passed: bool
    reason: str
    detail: dict | None = None


# Depth snapshot schema (per capture_depth_snapshots.py):
# {
#   "ts": "2026-05-08T14:32:11.123456+00:00",
#   "epoch_s": 1746719531.123,
#   "symbol": "MNQ1",
#   "bids": [{"price": 29014.50, "size": 12, "mm": "CME"}, ...],
#   "asks": [{"price": 29014.75, "size":  5, "mm": "CME"}, ...],
#   "spread": 0.25,
#   "mid": 29014.625
# }


def _depth_path(symbol: str, target_dt: datetime) -> Path:
    """Resolve the depth-snapshot file for symbol on target_dt's date."""
    yyyymmdd = target_dt.strftime("%Y%m%d")
    return DEPTH_DIR / f"{symbol}_{yyyymmdd}.jsonl"


def _load_snapshots_around(symbol: str, target_dt: datetime,
                            window_seconds: int = 30) -> list[dict]:
    """Return depth snapshots within ±window_seconds of target_dt.

    Tolerates .jsonl.gz files (post-rotation) AND missing files.
    Returns empty list when no L2 history exists yet — caller
    treats that as "overlay no-op, legacy decision flows through"."""
    path = _depth_path(symbol, target_dt)
    gz_path = path.with_suffix(path.suffix + ".gz")
    snapshots: list[dict] = []

    if path.exists():
        opener = path.open
        kwargs: dict = {"encoding": "utf-8"}
    elif gz_path.exists():
        opener = lambda mode="r": gzip.open(gz_path, mode + "t", encoding="utf-8")  # noqa: E731
        kwargs = {}
    else:
        return []

    target_epoch = target_dt.timestamp()
    try:
        with opener("r", **kwargs) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    s = json.loads(line)
                    epoch = float(s.get("epoch_s", 0))
                    if abs(epoch - target_epoch) <= window_seconds:
                        snapshots.append(s)
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
    except OSError:
        return []
    return snapshots


# ── Phase 3: sweep_reclaim_v2 confirmation ────────────────────────


def confirm_sweep_with_l2(*, symbol: str, swept_level: float,
                          touch_dt: datetime, side: str,
                          min_stop_qty: int = 50) -> GateResult:
    """For a wick that pierced ``swept_level`` at ``touch_dt`` on
    ``side`` (LONG = swept low, SHORT = swept high), verify that
    BEFORE the touch there was at least ``min_stop_qty`` of
    contra-side liquidity sitting AT or NEAR the swept level.

    LONG sweep (price wicked down through swept_level): we expect
    BIDS at-or-just-below the swept level got hit (real stop run).
    SHORT sweep: we expect ASKS at-or-just-above got hit.

    No-L2 case: returns passed=True with reason='no_l2_yet'."""
    pre_touch = touch_dt - timedelta(seconds=10)
    snapshots = _load_snapshots_around(symbol, pre_touch, window_seconds=15)
    if not snapshots:
        return GateResult(passed=True, reason="no_l2_yet")

    # Use the snapshot CLOSEST to (but before) the touch
    pre_snaps = [s for s in snapshots if s.get("epoch_s", 0) <= pre_touch.timestamp()]
    if not pre_snaps:
        return GateResult(passed=True, reason="no_pre_touch_snapshot")
    snap = max(pre_snaps, key=lambda s: s.get("epoch_s", 0))

    side_u = side.upper()
    if side_u in {"LONG", "BUY"}:
        # Long sweep — swept the LOW.  Look for BIDS at-or-below swept_level.
        levels = snap.get("bids", [])
        relevant = [lv for lv in levels if lv.get("price", 0) <= swept_level + 0.01]
    else:
        # Short sweep — swept the HIGH.  Look for ASKS at-or-above swept_level.
        levels = snap.get("asks", [])
        relevant = [lv for lv in levels if lv.get("price", 0) >= swept_level - 0.01]

    qty_at_level = sum(lv.get("size", 0) for lv in relevant)
    detail = {"qty_at_level": qty_at_level, "min_required": min_stop_qty,
              "swept_level": swept_level, "side": side}
    if qty_at_level >= min_stop_qty:
        return GateResult(passed=True, reason="real_sweep_confirmed", detail=detail)
    return GateResult(passed=False, reason="thin_book_at_swept_level",
                      detail=detail)


# ── Phase 3: volume_profile_v2 confirmation ───────────────────────


def confirm_poc_pull_with_l2(*, symbol: str, entry_dt: datetime,
                              entry_side: str,
                              min_imbalance_ratio: float = 1.5) -> GateResult:
    """When entering toward POC, the entry-side of the book should
    show heavier queue weight than the opposite side at the entry
    moment — confirms the order flow is actually pulling toward POC.

    LONG entry: bid_qty / ask_qty >= min_imbalance_ratio
    SHORT entry: ask_qty / bid_qty >= min_imbalance_ratio

    No-L2 case: passed=True / no_l2_yet."""
    snapshots = _load_snapshots_around(symbol, entry_dt, window_seconds=5)
    if not snapshots:
        return GateResult(passed=True, reason="no_l2_yet")

    # Most recent snapshot at/before entry
    pre = [s for s in snapshots if s.get("epoch_s", 0) <= entry_dt.timestamp()]
    snap = max(pre, key=lambda s: s.get("epoch_s", 0)) if pre else snapshots[0]

    bid_qty = sum(lv.get("size", 0) for lv in snap.get("bids", []))
    ask_qty = sum(lv.get("size", 0) for lv in snap.get("asks", []))
    if bid_qty == 0 or ask_qty == 0:
        return GateResult(passed=True, reason="empty_book_side", detail={"bid_qty": bid_qty, "ask_qty": ask_qty})

    if entry_side.upper() in {"LONG", "BUY"}:
        ratio = bid_qty / ask_qty
    else:
        ratio = ask_qty / bid_qty
    detail = {"bid_qty": bid_qty, "ask_qty": ask_qty, "ratio": round(ratio, 2),
              "min_required": min_imbalance_ratio}
    if ratio >= min_imbalance_ratio:
        return GateResult(passed=True, reason="poc_pull_confirmed", detail=detail)
    return GateResult(passed=False, reason="weak_imbalance", detail=detail)


# ── Phase 3: anchor_sweep_v2 confirmation ─────────────────────────


def confirm_anchor_touch_with_l2(*, symbol: str, anchor_price: float,
                                  touch_dt: datetime,
                                  min_qty_within_pts: float = 5.0,
                                  min_qty: int = 30) -> GateResult:
    """Before the bar that touched the named anchor (PDH/PDL/etc),
    verify there was real liquidity sitting WITHIN ``min_qty_within_pts``
    of the anchor.  If the anchor had no real qty around it, the
    "sweep" was just price drifting through air — not a stop run.

    No-L2 case: passed=True / no_l2_yet."""
    pre = touch_dt - timedelta(seconds=5)
    snapshots = _load_snapshots_around(symbol, pre, window_seconds=10)
    if not snapshots:
        return GateResult(passed=True, reason="no_l2_yet")

    # Use the snapshot closest to pre
    snap = min(snapshots, key=lambda s: abs(s.get("epoch_s", 0) - pre.timestamp()))

    near = []
    for side_key in ("bids", "asks"):
        for lv in snap.get(side_key, []):
            if abs(lv.get("price", 0) - anchor_price) <= min_qty_within_pts:
                near.append(lv)

    qty_near = sum(lv.get("size", 0) for lv in near)
    detail = {"qty_near": qty_near, "min_required": min_qty,
              "anchor_price": anchor_price, "n_levels_near": len(near)}
    if qty_near >= min_qty:
        return GateResult(passed=True, reason="anchor_had_liquidity", detail=detail)
    return GateResult(passed=False, reason="anchor_was_air", detail=detail)
