"""
EVOLUTIONARY TRADING ALGO  //  strategies.trading_gate
======================================================
Pre-trade circuit breaker.  Reads the most recent disk-space and
capture-health digests and decides whether new entries are allowed.

Why this exists
---------------
Per the 2026-05-11 risk review (B5):
> disk_space_monitor writes JSONL only — no consumer reads it.
> CRITICAL=2GB free → captures fail any moment → strategies
> silently revert to legacy → loss-limit hit.  This is
> observability, not a circuit breaker.

This module is the missing link: strategies (or the order router
that wraps them) call ``check_pre_trade_gate(symbol)`` BEFORE
placing a new entry order.  If the gate returns blocked=True,
the entry is rejected and the reason is logged.

Sticky vs fresh
---------------
The gate is NOT sticky on its own — every call re-reads the
latest digest.  That means a CRITICAL→GREEN transition (operator
clears space) immediately re-enables trading.  If you want
operator-acknowledgement-required behaviour (paranoid mode),
maintain a separate sentinel file that a human operator must
delete after confirming the issue.

Rate limit
----------
Gate evaluation reads disk on every call but caches results for
``_CACHE_TTL_SECONDS`` (default 30s) to avoid hammering the disk
for high-frequency callers.  Live entries fire at most a few per
minute, so the cache TTL never gates a real decision in a
meaningful way.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
DISK_LOG = LOG_DIR / "disk_space.jsonl"
CAPTURE_HEALTH_LOG = LOG_DIR / "capture_health.jsonl"
GATE_LOG = LOG_DIR / "trading_gate.jsonl"

# Verdict levels that BLOCK trading
BLOCKING_DISK_VERDICTS = {"RED", "CRITICAL", "ERROR"}
BLOCKING_CAPTURE_VERDICTS = {"RED", "ERROR"}

# How stale can the most-recent digest be before we treat it as missing?
MAX_DISK_DIGEST_AGE_SECONDS = 600    # 10 minutes
MAX_CAPTURE_DIGEST_AGE_SECONDS = 1800  # 30 minutes

# Cache TTL on the gate result so high-frequency callers don't pound disk
_CACHE_TTL_SECONDS = 30.0


@dataclass
class GateDecision:
    """Pre-trade gate verdict."""
    blocked: bool
    reason: str           # "ok" | "disk_<verdict>" | "capture_<verdict>"
                          # "disk_digest_stale" | "capture_digest_stale"
                          # "no_disk_digest" | "no_capture_digest"
    disk_verdict: str | None = None
    capture_verdict: str | None = None
    disk_age_seconds: float | None = None
    capture_age_seconds: float | None = None
    detail: dict | None = None


# Module-level cache (declared AFTER GateDecision so the type annotation
# resolves cleanly without forward-reference quotes).
_cached_result: tuple[float, GateDecision] | None = None


def _last_jsonl_record(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return None


def _digest_age_seconds(record: dict | None,
                         *, now: datetime | None = None) -> float | None:
    if record is None:
        return None
    ts = record.get("ts") or record.get("timestamp_utc")
    if ts is None:
        return None
    now = now or datetime.now(UTC)
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(ts, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(ts), UTC)
        except (OSError, OverflowError, ValueError):
            return None
    else:
        return None
    return (now - dt).total_seconds()


def check_pre_trade_gate(symbol: str | None = None,
                          *, force_refresh: bool = False,
                          now: datetime | None = None) -> GateDecision:
    """Read the latest disk-space + capture-health digests.  Return
    a GateDecision.  Strategies call this BEFORE placing entries.

    Args:
        symbol:        symbol about to be traded (currently unused;
                       reserved for future per-symbol gating)
        force_refresh: bypass the 30s result cache (useful in tests)
        now:           override clock (test injection)

    Result is logged to GATE_LOG when blocked=True OR the verdict
    transitions blocked↔unblocked.
    """
    global _cached_result
    now_dt = now or datetime.now(UTC)
    now_ts = time.time()
    if not force_refresh and _cached_result is not None:
        cached_at, cached_decision = _cached_result
        if now_ts - cached_at < _CACHE_TTL_SECONDS:
            return cached_decision

    disk = _last_jsonl_record(DISK_LOG)
    cap = _last_jsonl_record(CAPTURE_HEALTH_LOG)

    disk_age = _digest_age_seconds(disk, now=now_dt)
    cap_age = _digest_age_seconds(cap, now=now_dt)
    disk_verdict = disk.get("verdict") if disk else None
    cap_verdict = cap.get("verdict") if cap else None

    # Decision tree.  Order matters: CRITICAL beats stale beats missing.
    decision: GateDecision

    if disk is None:
        decision = GateDecision(
            blocked=True, reason="no_disk_digest",
            disk_verdict=None, capture_verdict=cap_verdict,
            disk_age_seconds=None, capture_age_seconds=cap_age,
        )
    elif disk_verdict in BLOCKING_DISK_VERDICTS:
        decision = GateDecision(
            blocked=True, reason=f"disk_{disk_verdict}",
            disk_verdict=disk_verdict, capture_verdict=cap_verdict,
            disk_age_seconds=disk_age, capture_age_seconds=cap_age,
            detail={"worst_partition": disk.get("worst_partition")},
        )
    elif disk_age is not None and disk_age > MAX_DISK_DIGEST_AGE_SECONDS:
        decision = GateDecision(
            blocked=True, reason="disk_digest_stale",
            disk_verdict=disk_verdict, capture_verdict=cap_verdict,
            disk_age_seconds=disk_age, capture_age_seconds=cap_age,
            detail={"max_age": MAX_DISK_DIGEST_AGE_SECONDS},
        )
    elif cap is not None and cap_verdict in BLOCKING_CAPTURE_VERDICTS:
        decision = GateDecision(
            blocked=True, reason=f"capture_{cap_verdict}",
            disk_verdict=disk_verdict, capture_verdict=cap_verdict,
            disk_age_seconds=disk_age, capture_age_seconds=cap_age,
            detail={"issues": cap.get("issues", [])},
        )
    elif cap_age is not None and cap_age > MAX_CAPTURE_DIGEST_AGE_SECONDS:
        decision = GateDecision(
            blocked=True, reason="capture_digest_stale",
            disk_verdict=disk_verdict, capture_verdict=cap_verdict,
            disk_age_seconds=disk_age, capture_age_seconds=cap_age,
            detail={"max_age": MAX_CAPTURE_DIGEST_AGE_SECONDS},
        )
    else:
        decision = GateDecision(
            blocked=False, reason="ok",
            disk_verdict=disk_verdict, capture_verdict=cap_verdict,
            disk_age_seconds=disk_age, capture_age_seconds=cap_age,
        )

    _maybe_log_decision(symbol, decision, now_dt)
    _cached_result = (now_ts, decision)
    return decision


def _maybe_log_decision(symbol: str | None, decision: GateDecision,
                         now: datetime) -> None:
    """Append to gate log when blocked OR when a transition just happened.
    We don't write every check (would be noisy at 30s cache TTL)."""
    global _cached_result
    prev = _cached_result[1] if _cached_result else None
    transition = (prev is not None and prev.blocked != decision.blocked)
    if not (decision.blocked or transition):
        return
    record = {
        "ts": now.isoformat(),
        "symbol": symbol,
        "blocked": decision.blocked,
        "reason": decision.reason,
        "disk_verdict": decision.disk_verdict,
        "capture_verdict": decision.capture_verdict,
        "disk_age_seconds": decision.disk_age_seconds,
        "capture_age_seconds": decision.capture_age_seconds,
        "detail": decision.detail,
        "transition": transition,
    }
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with GATE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as e:
        # D6: surface alert-write failures to stderr so the cron
        # operator captures them, instead of silently swallowing.
        print(f"trading_gate WARN: could not write to {GATE_LOG}: {e}",
              file=sys.stderr)


def _reset_cache_for_tests() -> None:
    """Test helper — flush the 30s cache so consecutive checks see
    different digests."""
    global _cached_result
    _cached_result = None
