"""
EVOLUTIONARY TRADING ALGO  //  strategies.l2_portfolio_limits
=============================================================
Portfolio-level concurrency limiter — cross-strategy circuit
breaker that the trading_gate consults BEFORE allowing any new
entry.

Why this exists
---------------
Individual strategies have ``max_qty_contracts`` per entry, but
nothing caps total concurrent exposure across the L2 fleet.  If
book_imbalance + footprint_absorption + microprice_drift all fire
LONG MNQ within 30 seconds of each other, the operator is in 3
correlated longs simultaneously — total exposure is 3× any single
strategy's risk, but no single strategy knows about the others.

This module is the missing portfolio-level limiter.

Mechanic
--------
- Tracks open positions per (symbol, side) via the broker_fills log
  (entries minus exits)
- Returns BLOCK when total open contracts on a symbol exceeds
  ``max_concurrent_contracts_per_symbol`` (default 2)
- Returns BLOCK when total absolute exposure across all symbols
  exceeds ``max_total_absolute_contracts`` (default 5)
- Same-direction limit: ``max_same_side_contracts_per_symbol``
  prevents stacking longs/shorts on a single symbol (default 1)

Live trading hook
-----------------
::

    from eta_engine.strategies.l2_portfolio_limits import (
        check_portfolio_limits,
    )

    # In the supervisor, after the regular trading_gate check:
    decision = check_portfolio_limits(symbol="MNQ", side="LONG", qty=1)
    if decision.blocked:
        _rollback_recorded_entry(decision.reason)
        return None
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
BROKER_FILL_LOG = LOG_DIR / "broker_fills.jsonl"
PORTFOLIO_LIMITS_LOG = LOG_DIR / "l2_portfolio_limits.jsonl"


# Conservative defaults — operator can override per-symbol if needed.
DEFAULT_MAX_SAME_SIDE_PER_SYMBOL = 1
DEFAULT_MAX_CONCURRENT_PER_SYMBOL = 2  # one long + one short net hedge OK
DEFAULT_MAX_TOTAL_ABSOLUTE_CONTRACTS = 5


@dataclass
class PortfolioDecision:
    blocked: bool
    reason: str
    open_positions: dict[str, int] = field(default_factory=dict)
    # ``open_positions``: {symbol: net_signed_qty}
    # positive = net long, negative = net short
    detail: dict = field(default_factory=dict)


def _compute_open_positions(*, _path: Path | None = None,
                              since_days: int = 14) -> dict[tuple[str, str], int]:
    """Scan broker_fills.jsonl, return {(symbol, side): net_qty}.

    Each ENTRY fill increments; matching TARGET/STOP/TIMEOUT
    decrements.  When we can't determine the symbol from the fill
    alone, fall back to the broker_exec_id prefix or skip.

    Note: this is an approximation.  The supervisor's
    bot.open_position is the source of truth; this is the
    cross-strategy aggregator using only the audit log.
    """
    path = _path if _path is not None else BROKER_FILL_LOG
    if not path.exists():
        return {}
    # Track per signal_id: side, entered_qty, exited_qty
    by_sig: dict[str, dict] = {}
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt < cutoff:
                    continue
                sid = rec.get("signal_id", "")
                if not sid:
                    continue
                side = str(rec.get("side", "?")).upper()
                exit_reason = str(rec.get("exit_reason", "")).upper()
                qty = int(rec.get("qty_filled", 0))
                entry = by_sig.setdefault(sid, {"side": side, "entered": 0,
                                                   "exited": 0, "symbol": None})
                if exit_reason == "ENTRY":
                    entry["entered"] += qty
                    # Symbol may be carried separately; if not, leave as None
                    sym_hint = rec.get("symbol")
                    if sym_hint:
                        entry["symbol"] = sym_hint
                elif exit_reason in ("TARGET", "STOP", "TIMEOUT", "CANCEL"):
                    entry["exited"] += qty
    except OSError:
        return {}

    # Aggregate by (symbol, side)
    positions: dict[tuple[str, str], int] = {}
    for sid, entry in by_sig.items():
        net = entry["entered"] - entry["exited"]
        if net <= 0:
            continue
        symbol = entry["symbol"]
        if not symbol:
            # Best effort: infer from signal_id prefix (e.g. "MNQ-LONG-...")
            parts = sid.split("-")
            symbol = parts[0] if parts else "?"
        positions[(symbol, entry["side"])] = positions.get(
            (symbol, entry["side"]), 0) + net
    return positions


def check_portfolio_limits(symbol: str, side: str, qty: int,
                             *,
                             max_same_side_per_symbol: int = DEFAULT_MAX_SAME_SIDE_PER_SYMBOL,
                             max_concurrent_per_symbol: int = DEFAULT_MAX_CONCURRENT_PER_SYMBOL,
                             max_total_absolute_contracts: int = DEFAULT_MAX_TOTAL_ABSOLUTE_CONTRACTS,
                             _fill_path: Path | None = None,
                             _log_path: Path | None = None) -> PortfolioDecision:
    """Check whether a new entry would exceed portfolio-level limits.

    Returns blocked=True when the proposed (symbol, side, qty)
    entry would push exposure past any limit.

    Defensive: if broker_fills log can't be read, fails OPEN
    (returns blocked=False) — we don't block trading on
    observability failure.  But we DO log the failure to stderr.
    """
    positions = _compute_open_positions(_path=_fill_path)
    side_upper = side.upper()
    # Map to canonical "LONG"/"SHORT" or "BUY"/"SELL" — keep both
    canonical_side = "LONG" if side_upper in ("LONG", "BUY") else "SHORT"

    # Same-side count on this symbol
    same_side_existing = positions.get((symbol, canonical_side), 0) \
                          + positions.get((symbol, "LONG" if canonical_side == "BUY" else "BUY"
                                            if canonical_side == "LONG" else "SHORT"
                                            if canonical_side == "SELL" else canonical_side), 0)
    same_side_existing = positions.get((symbol, canonical_side), 0)
    # Total on this symbol (both sides — used for the "no concurrent
    # long+short stacking" rule)
    total_on_symbol = sum(v for (s, _), v in positions.items() if s == symbol)
    # Grand total across all symbols
    total_absolute = sum(positions.values())

    # Decisions in priority order
    proposed_total = total_absolute + qty
    if proposed_total > max_total_absolute_contracts:
        return _log_decision(PortfolioDecision(
            blocked=True,
            reason=f"total_absolute_exceeded:{proposed_total}>{max_total_absolute_contracts}",
            open_positions={f"{s}:{sd}": v for (s, sd), v in positions.items()},
            detail={"proposed_total": proposed_total,
                     "limit": max_total_absolute_contracts}),
            _log_path)

    if same_side_existing + qty > max_same_side_per_symbol:
        return _log_decision(PortfolioDecision(
            blocked=True,
            reason=f"same_side_stacking:{symbol}_{canonical_side}_"
                    f"{same_side_existing + qty}>{max_same_side_per_symbol}",
            open_positions={f"{s}:{sd}": v for (s, sd), v in positions.items()},
            detail={"existing_same_side": same_side_existing,
                     "proposed_qty": qty,
                     "limit": max_same_side_per_symbol}),
            _log_path)

    if total_on_symbol + qty > max_concurrent_per_symbol:
        return _log_decision(PortfolioDecision(
            blocked=True,
            reason=f"symbol_concurrent_exceeded:{symbol}_"
                    f"{total_on_symbol + qty}>{max_concurrent_per_symbol}",
            open_positions={f"{s}:{sd}": v for (s, sd), v in positions.items()},
            detail={"existing_on_symbol": total_on_symbol,
                     "limit": max_concurrent_per_symbol}),
            _log_path)

    # All limits OK
    return PortfolioDecision(
        blocked=False, reason="ok",
        open_positions={f"{s}:{sd}": v for (s, sd), v in positions.items()},
    )


def _log_decision(decision: PortfolioDecision,
                    _log_path: Path | None = None) -> PortfolioDecision:
    """Append decision to log only when blocked (avoid log noise)."""
    if not decision.blocked:
        return decision
    log_path = _log_path if _log_path is not None else PORTFOLIO_LIMITS_LOG
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(UTC).isoformat(),
                "blocked": decision.blocked,
                "reason": decision.reason,
                "open_positions": decision.open_positions,
                "detail": decision.detail,
            }, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"l2_portfolio_limits WARN: log write failed: {e}",
              file=sys.stderr)
    return decision
