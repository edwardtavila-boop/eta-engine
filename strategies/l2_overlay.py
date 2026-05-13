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

Failure modes — fail OPEN vs fail CLOSED
----------------------------------------
PRE-data deployment (Phase 1 captures not yet running):
  All gates return ``GateResult(passed=True, reason="no_l2_yet")``
  so the overlay is safe to wire BEFORE captures accumulate.

POST-data deployment (captures expected):
  Once ``mark_captures_expected(symbol)`` has been called for the
  day, the overlay flips to FAIL CLOSED on missing/stale data —
  empty snapshots return ``GateResult(passed=False,
  reason="captures_stale_fail_closed")``.  Without this, an
  unnoticed capture-daemon crash silently degrades L2-aware
  strategies back to legacy behavior.

The expected-mode sentinel is per-(symbol, date) and held in
process memory.  Operators flip it on at trading-session start
via ``mark_captures_expected`` (called from the bot startup hook
or per-symbol initialization).
"""

# ruff: noqa: SIM115, SIM108
# SIM115: opener captured into a variable so the same with-block
# handles either path.open() OR gzip.open() — context-manager wrap
# happens at the call site.
# SIM108: explicit if/else is clearer than nested ternaries here.
from __future__ import annotations

import contextlib
import gzip
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
DEPTH_DIR = ROOT.parent / "mnq_data" / "depth"

# Per-(symbol, YYYYMMDD) sentinel: True means "Phase-1 captures are
# expected to be running today, so empty snapshots are a FAILURE not
# a 'pre-data' state."  Operators call mark_captures_expected at the
# start of each session.
_CAPTURES_EXPECTED: set[tuple[str, str]] = set()

# Default freshness window — if the most recent snap in the file is
# older than 2 × snap_interval, treat as stale.  Configured per
# capture daemon; default conservative.
DEFAULT_SNAPSHOT_INTERVAL_SECONDS = 5.0
STALENESS_FACTOR = 2.0


@dataclass
class GateResult:
    """Pass/fail result with a reason for logging."""

    passed: bool
    reason: str
    detail: dict | None = None


def mark_captures_expected(symbol: str, *, when: datetime | None = None) -> None:
    """Flip the per-(symbol, date) sentinel to 'captures expected'.
    Call this at the start of each trading session (per symbol).
    After this, missing/stale L2 data fails CLOSED instead of OPEN.
    """
    when = when or datetime.now(UTC)
    _CAPTURES_EXPECTED.add((symbol, when.strftime("%Y%m%d")))


def clear_captures_expected(symbol: str | None = None) -> None:
    """Clear the expected-mode sentinel (test helper / session reset).
    If symbol is None, clears all entries."""
    if symbol is None:
        _CAPTURES_EXPECTED.clear()
        return
    to_remove = {key for key in _CAPTURES_EXPECTED if key[0] == symbol}
    _CAPTURES_EXPECTED.difference_update(to_remove)


def _captures_expected_today(symbol: str, when: datetime | None = None) -> bool:
    when = when or datetime.now(UTC)
    return (symbol, when.strftime("%Y%m%d")) in _CAPTURES_EXPECTED


def _no_data_result(
    symbol: str,
    target_dt: datetime,
    *,
    pass_reason: str = "no_l2_yet",
    fail_reason: str = "captures_stale_fail_closed",
    detail: dict | None = None,
) -> GateResult:
    """Decide whether a no-data condition fails OPEN (pre-data) or
    CLOSED (captures expected).  Centralized so every gate uses the
    same logic."""
    if _captures_expected_today(symbol, target_dt):
        return GateResult(passed=False, reason=fail_reason, detail=detail)
    return GateResult(passed=True, reason=pass_reason, detail=detail)


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


@contextlib.contextmanager
def _open_depth_file(path: Path) -> Iterator[TextIO]:
    """Context manager opening either .jsonl or .jsonl.gz (whichever
    exists).  Raises FileNotFoundError if neither.  Replaces the prior
    lambda-as-opener pattern (S1) for clearer resource lifetime."""
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            yield f
        return
    gz = path.with_suffix(path.suffix + ".gz")
    if gz.exists():
        with gzip.open(gz, "rt", encoding="utf-8") as f:
            yield f
        return
    raise FileNotFoundError(f"neither {path} nor {gz} exists")


def _load_snapshots_around(symbol: str, target_dt: datetime, window_seconds: int = 30) -> list[dict]:
    """Return depth snapshots within ±window_seconds of target_dt.

    Tolerates .jsonl.gz files (post-rotation) AND missing files.
    Returns empty list when no L2 history exists yet.

    Note: this function does NOT enforce captures_expected itself —
    callers consult ``_no_data_result`` to decide pass-OPEN vs
    pass-CLOSED based on the per-symbol sentinel.
    """
    path = _depth_path(symbol, target_dt)
    target_epoch = target_dt.timestamp()
    snapshots: list[dict] = []
    try:
        with _open_depth_file(path) as f:
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
    except FileNotFoundError:
        return []
    except OSError:
        return []
    return snapshots


def _file_freshness(symbol: str, target_dt: datetime) -> dict:
    """Inspect the depth file and return freshness metadata.

    Returns {"file_exists": bool, "max_epoch": float | None,
             "age_seconds": float | None, "n_lines": int}.

    Used by callers to decide 'is the daemon alive?' independently
    of whether any snapshot fell into the requested window.
    """
    path = _depth_path(symbol, target_dt)
    out: dict = {"file_exists": False, "max_epoch": None, "age_seconds": None, "n_lines": 0}
    max_epoch = 0.0
    n_lines = 0
    try:
        with _open_depth_file(path) as f:
            out["file_exists"] = True
            for line in f:
                line = line.strip()
                if not line:
                    continue
                n_lines += 1
                try:
                    s = json.loads(line)
                    e = float(s.get("epoch_s", 0))
                    if e > max_epoch:
                        max_epoch = e
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
    except FileNotFoundError:
        return out
    except OSError:
        return out
    out["n_lines"] = n_lines
    if max_epoch > 0:
        out["max_epoch"] = max_epoch
        out["age_seconds"] = round(target_dt.timestamp() - max_epoch, 2)
    return out


# ── Phase 3: sweep_reclaim_v2 confirmation ────────────────────────


def confirm_sweep_with_l2(
    *,
    symbol: str,
    swept_level: float,
    touch_dt: datetime,
    side: str,
    min_stop_qty: int = 50,
    window_seconds: int = 60,
    hidden_qty_floor: int | None = None,
) -> GateResult:
    """For a wick that pierced ``swept_level`` at ``touch_dt`` on
    ``side`` (LONG = swept low, SHORT = swept high), verify that
    BEFORE the touch there was at least ``min_stop_qty`` of
    contra-side liquidity sitting AT or NEAR the swept level.

    LONG sweep (price wicked down through swept_level): we expect
    BIDS at-or-just-below the swept level got hit (real stop run).
    SHORT sweep: we expect ASKS at-or-just-above got hit.

    I2 changes:
      - Wider lookback window (60s default, was 15s) to catch
        sweeps that happen between snap intervals
      - Take MAX visible size in the window (was: closest snap),
        because hidden orders refill the visible queue on the next
        snap after a print
      - Optional ``hidden_qty_floor``: if set, gate is satisfied
        when the visible+floor sum exceeds min_stop_qty.  Floor
        defaults to None (off); operator can set per-symbol after
        analyzing real fill data.

    No-L2 case:
      - Pre-data (no captures_expected sentinel): passed=True / no_l2_yet
      - Post-data: passed=False / captures_stale_fail_closed
    """
    pre_touch = touch_dt - timedelta(seconds=10)
    snapshots = _load_snapshots_around(symbol, pre_touch, window_seconds=window_seconds)
    if not snapshots:
        freshness = _file_freshness(symbol, touch_dt)
        return _no_data_result(symbol, touch_dt, detail={"freshness": freshness})

    # Use ALL snapshots in the window (not just the closest) so we
    # see the max visible size at the level — that's the real stop
    # cluster size, refilled across the window.
    pre_snaps = [s for s in snapshots if s.get("epoch_s", 0) <= pre_touch.timestamp()]
    if not pre_snaps:
        # No pre-touch snap at all — all snaps in window are AFTER
        # the touch.  This can happen at the first snap of the day.
        # Fail-OPEN even in expected-mode because we genuinely have
        # no pre-touch evidence either way.
        return GateResult(passed=True, reason="no_pre_touch_snapshot")

    side_u = side.upper()
    # Compute max visible qty across all pre-touch snaps in the window
    max_qty = 0
    chosen_snap_epoch = 0.0
    for snap in pre_snaps:
        if side_u in {"LONG", "BUY"}:
            levels = snap.get("bids", [])
            relevant = [lv for lv in levels if lv.get("price", 0) <= swept_level + 0.01]
        else:
            levels = snap.get("asks", [])
            relevant = [lv for lv in levels if lv.get("price", 0) >= swept_level - 0.01]
        qty = sum(lv.get("size", 0) for lv in relevant)
        if qty > max_qty:
            max_qty = qty
            chosen_snap_epoch = float(snap.get("epoch_s", 0))

    # Apply hidden-qty floor (additive — represents typical iceberg
    # behind visible at this level on this venue)
    effective_qty = max_qty + (hidden_qty_floor or 0)

    detail = {
        "max_visible_qty": max_qty,
        "qty_at_level": max_qty,
        "hidden_qty_floor": hidden_qty_floor or 0,
        "effective_qty": effective_qty,
        "min_required": min_stop_qty,
        "swept_level": swept_level,
        "side": side,
        "n_snaps_considered": len(pre_snaps),
        "chosen_snap_epoch": chosen_snap_epoch,
        "window_seconds": window_seconds,
    }
    if effective_qty >= min_stop_qty:
        return GateResult(passed=True, reason="real_sweep_confirmed", detail=detail)
    return GateResult(passed=False, reason="thin_book_at_swept_level", detail=detail)


# ── Phase 3: volume_profile_v2 confirmation ───────────────────────


def confirm_poc_pull_with_l2(
    *,
    symbol: str,
    entry_dt: datetime,
    entry_side: str,
    min_imbalance_ratio: float = 1.5,
    max_snapshot_staleness_seconds: float = 30.0,
) -> GateResult:
    """When entering toward POC, the entry-side of the book should
    show heavier queue weight than the opposite side at the entry
    moment — confirms the order flow is actually pulling toward POC.

    LONG entry: bid_qty / ask_qty >= min_imbalance_ratio
    SHORT entry: ask_qty / bid_qty >= min_imbalance_ratio

    I2 change: bound the staleness of the chosen snapshot — if the
    closest pre-entry snap is more than ``max_snapshot_staleness_seconds``
    older than entry_dt, treat as stale (fail-closed in expected-mode).

    No-L2 case:
      - Pre-data: passed=True / no_l2_yet
      - Post-data: passed=False / captures_stale_fail_closed
    """
    snapshots = _load_snapshots_around(symbol, entry_dt, window_seconds=30)
    if not snapshots:
        freshness = _file_freshness(symbol, entry_dt)
        return _no_data_result(symbol, entry_dt, detail={"freshness": freshness})

    # Most recent snapshot at/before entry
    pre = [s for s in snapshots if s.get("epoch_s", 0) <= entry_dt.timestamp()]
    snap = max(pre, key=lambda s: s.get("epoch_s", 0)) if pre else snapshots[0]
    snap_epoch = float(snap.get("epoch_s", 0))
    snap_age = entry_dt.timestamp() - snap_epoch
    if snap_age > max_snapshot_staleness_seconds:
        return _no_data_result(
            symbol,
            entry_dt,
            pass_reason="snapshot_too_stale",
            fail_reason="snapshot_too_stale_fail_closed",
            detail={"snap_age_seconds": round(snap_age, 2), "max_allowed": max_snapshot_staleness_seconds},
        )

    bid_qty = sum(lv.get("size", 0) for lv in snap.get("bids", []))
    ask_qty = sum(lv.get("size", 0) for lv in snap.get("asks", []))
    if bid_qty == 0 or ask_qty == 0:
        # Anomalous book — treat consistently with the strategy-level
        # zero-side fail-closed rule (book_imbalance I8).
        return GateResult(
            passed=False,
            reason="empty_book_side",
            detail={"bid_qty": bid_qty, "ask_qty": ask_qty, "snap_age_seconds": round(snap_age, 2)},
        )

    if entry_side.upper() in {"LONG", "BUY"}:
        ratio = bid_qty / ask_qty
    else:
        ratio = ask_qty / bid_qty
    detail = {
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "ratio": round(ratio, 2),
        "min_required": min_imbalance_ratio,
        "snap_age_seconds": round(snap_age, 2),
    }
    if ratio >= min_imbalance_ratio:
        return GateResult(passed=True, reason="poc_pull_confirmed", detail=detail)
    return GateResult(passed=False, reason="weak_imbalance", detail=detail)


# ── Phase 3: anchor_sweep_v2 confirmation ─────────────────────────


def confirm_anchor_touch_with_l2(
    *,
    symbol: str,
    anchor_price: float,
    touch_dt: datetime,
    min_qty_within_pts: float = 5.0,
    min_qty: int = 30,
    max_snapshot_staleness_seconds: float = 60.0,
) -> GateResult:
    """Before the bar that touched the named anchor (PDH/PDL/etc),
    verify there was real liquidity sitting WITHIN ``min_qty_within_pts``
    of the anchor.  If the anchor had no real qty around it, the
    "sweep" was just price drifting through air — not a stop run.

    No-L2 case:
      - Pre-data: passed=True / no_l2_yet
      - Post-data: passed=False / captures_stale_fail_closed
    """
    pre = touch_dt - timedelta(seconds=5)
    snapshots = _load_snapshots_around(symbol, pre, window_seconds=30)
    if not snapshots:
        freshness = _file_freshness(symbol, touch_dt)
        return _no_data_result(symbol, touch_dt, detail={"freshness": freshness})

    # Use the snapshot closest to pre — but bound staleness
    snap = min(snapshots, key=lambda s: abs(s.get("epoch_s", 0) - pre.timestamp()))
    snap_age = abs(pre.timestamp() - float(snap.get("epoch_s", 0)))
    if snap_age > max_snapshot_staleness_seconds:
        return _no_data_result(
            symbol,
            touch_dt,
            pass_reason="snapshot_too_stale",
            fail_reason="snapshot_too_stale_fail_closed",
            detail={"snap_age_seconds": round(snap_age, 2), "max_allowed": max_snapshot_staleness_seconds},
        )

    near = []
    for side_key in ("bids", "asks"):
        for lv in snap.get(side_key, []):
            if abs(lv.get("price", 0) - anchor_price) <= min_qty_within_pts:
                near.append(lv)

    qty_near = sum(lv.get("size", 0) for lv in near)
    detail = {
        "qty_near": qty_near,
        "min_required": min_qty,
        "anchor_price": anchor_price,
        "n_levels_near": len(near),
        "snap_age_seconds": round(snap_age, 2),
    }
    if qty_near >= min_qty:
        return GateResult(passed=True, reason="anchor_had_liquidity", detail=detail)
    return GateResult(passed=False, reason="anchor_was_air", detail=detail)
