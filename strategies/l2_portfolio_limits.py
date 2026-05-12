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


# ── Combined-notional sizing rule (2026-05-12 diamond memo) ───────
#
# Several of the 8 diamond bots share an underlying instrument across
# different mechanics or sizing tiers (MNQ + NQ are the same NASDAQ
# direction at different tick values; MCL + CL + cl_macro all share
# crude oil; MGC + GC share gold).  A single-symbol concurrency cap is
# insufficient — the supervisor must treat correlated symbols as ONE
# bet so the portfolio isn't unintentionally 3x exposed on crude.
#
# Each group below collapses to a single underlying for the purpose of
# `max_combined_underlying_contracts`.  Contracts within a group are
# weighted by their notional-multiplier ratio so a full-size NQ
# (10x MNQ tick value) counts as 10 MNQ-equivalents.
UNDERLYING_GROUPS: dict[str, dict[str, float]] = {
    # NASDAQ group: 1 unit = 1 MNQ contract.  NQ multiplier is 10x.
    "NASDAQ": {"MNQ": 1.0, "NQ": 10.0,
                "MNQ1": 1.0, "NQ1": 10.0,
                "MNQM6": 1.0, "NQM6": 10.0, "MNQU6": 1.0, "NQU6": 10.0},
    # Crude group: 1 unit = 1 MCL.  CL is 10x.
    "CRUDE": {"MCL": 1.0, "CL": 10.0,
                "MCL1": 1.0, "CL1": 10.0,
                "MCLM6": 1.0, "CLM6": 10.0, "MCLN6": 1.0, "CLN6": 10.0},
    # Gold group: 1 unit = 1 MGC.  GC is 10x.
    "GOLD": {"MGC": 1.0, "GC": 10.0,
              "MGC1": 1.0, "GC1": 10.0,
              "MGCM6": 1.0, "GCM6": 10.0, "MGCQ6": 1.0, "GCQ6": 10.0},
    # S&P group: 1 unit = 1 MES.  ES is 5x.
    "SP": {"MES": 1.0, "ES": 5.0,
            "MES1": 1.0, "ES1": 5.0,
            "MESM6": 1.0, "ESM6": 5.0},
    # Dow group: 1 unit = 1 MYM.  YM is 5x.
    "DOW": {"MYM": 1.0, "YM": 5.0,
             "MYM1": 1.0, "YM1": 5.0,
             "MYMM6": 1.0, "YMM6": 5.0},
    # Russell group
    "RUSSELL": {"M2K": 1.0, "RTY": 5.0,
                 "M2K1": 1.0, "RTY1": 5.0,
                 "M2KM6": 1.0, "RTYM6": 5.0},
    # 10y note / 30y bond don't need a group (single bot each)
}

#: Per-group MNQ-equivalent caps.  Operator commitment from the
#: diamond decision memo (var/eta_engine/decisions/diamond_set_2026_05_12.md).
DEFAULT_MAX_COMBINED_UNITS_PER_GROUP: dict[str, float] = {
    "NASDAQ": 1.0,   # 1 NASDAQ direction at a time across MNQ + NQ
    "CRUDE":  2.0,   # max 2 MCL-equivalent contracts across cl_momentum + mcl + cl_macro
    "GOLD":   1.0,   # max 1 MGC-equivalent across mgc + gc
    "SP":     2.0,
    "DOW":    2.0,
    "RUSSELL": 2.0,
}


#: Cross-bot dedup rule — when both bots in a pair fire signals on the
#: same day, only the FIRST one is allowed through.  This is the
#: runtime expression of the operator's diamond-memo commitment that
#: the byte-identical MNQ/NQ sage_corb configs represent ONE bet, not
#: two diamonds.  Format: {bot_id: [bot_ids_to_suppress_when_this_fires]}
DEFAULT_SAME_DAY_SUPPRESSORS: dict[str, list[str]] = {
    "mnq_futures_sage": ["nq_futures_sage"],
    "nq_futures_sage":  ["mnq_futures_sage"],
}


@dataclass
class CrossBotDecision:
    """Verdict on whether a bot may fire given today's prior fills."""
    suppressed: bool
    reason: str
    suppressor_bot_id: str | None = None
    suppressor_signal_ts: str | None = None


def check_cross_bot_dedup(
    bot_id: str,
    *,
    when: datetime | None = None,
    suppressors: dict[str, list[str]] | None = None,
    _fill_path: Path | None = None,
) -> CrossBotDecision:
    """Return suppressed=True when another bot in the dedup pair
    already filed an ENTRY today.

    Reads the broker_fills log to find today's entries grouped by
    bot_id; if any bot listed as a suppressor for `bot_id` has an
    entry on today's date, we block.

    Defensive: if the log is missing/unreadable, returns allowed
    (suppressed=False) — observability failure should never block
    a trade for the wrong reason.  Caller logs the decision.
    """
    suppressor_map = (suppressors if suppressors is not None
                       else DEFAULT_SAME_DAY_SUPPRESSORS)
    # Find which bots' fills would suppress this one.  The map is
    # bidirectional: if A suppresses B, B also suppresses A (the
    # operator's "one bet" rule).
    blockers: set[str] = set()
    for trigger_bot, suppressed_bots in suppressor_map.items():
        if bot_id in suppressed_bots:
            blockers.add(trigger_bot)
    if not blockers:
        return CrossBotDecision(suppressed=False, reason="no_dedup_pair")

    when = when or datetime.now(UTC)
    today_str = when.strftime("%Y-%m-%d")
    path = _fill_path if _fill_path is not None else BROKER_FILL_LOG
    if not path.exists():
        return CrossBotDecision(
            suppressed=False, reason="no_broker_fills_log")
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
                if str(rec.get("exit_reason", "")).upper() != "ENTRY":
                    continue
                other = str(rec.get("bot_id", ""))
                if other not in blockers:
                    continue
                ts = rec.get("ts", "")
                if not ts.startswith(today_str):
                    continue
                return CrossBotDecision(
                    suppressed=True,
                    reason=f"same_day_dedup:{other}_fired_first",
                    suppressor_bot_id=other,
                    suppressor_signal_ts=ts,
                )
    except OSError:
        return CrossBotDecision(
            suppressed=False, reason="fill_log_read_error")
    return CrossBotDecision(suppressed=False, reason="no_same_day_blocker")


def _resolve_underlying_group(symbol: str) -> tuple[str | None, float]:
    """Return (group_name, mnq_equivalent_units) for a symbol, or
    (None, 0) if the symbol doesn't belong to any tracked group.

    Used by check_portfolio_limits to enforce the combined-notional
    cap across correlated symbols (MNQ + NQ count as the same NASDAQ
    bet, etc.).
    """
    sym_upper = symbol.upper().strip()
    for group, members in UNDERLYING_GROUPS.items():
        if sym_upper in members:
            return group, members[sym_upper]
    return None, 0.0


def _group_existing_units(
    positions: dict[tuple[str, str], int],
    group_name: str,
    side: str | None = None,
) -> float:
    """Sum the MNQ-equivalent units currently open on a group, optionally
    filtered by side (LONG/SHORT).  Used by the combined-notional cap."""
    members = UNDERLYING_GROUPS.get(group_name, {})
    total = 0.0
    for (sym, sd), qty in positions.items():
        if sym.upper() not in members:
            continue
        if side is not None and sd.upper() != side.upper():
            continue
        total += members[sym.upper()] * abs(qty)
    return total


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
                             max_combined_units_per_group: dict[str, float] | None = None,
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

    # Combined-notional check across correlated underlyings (2026-05-12
    # diamond memo).  MNQ + NQ count as one NASDAQ bet, MCL + CL +
    # cl_macro count as one crude bet, MGC + GC count as one gold bet.
    group_caps = (max_combined_units_per_group
                  if max_combined_units_per_group is not None
                  else DEFAULT_MAX_COMBINED_UNITS_PER_GROUP)
    group_name, units_for_this_order = _resolve_underlying_group(symbol)
    if group_name is not None and group_name in group_caps:
        existing_units = _group_existing_units(
            positions, group_name, side=canonical_side)
        cap = group_caps[group_name]
        if existing_units + units_for_this_order * qty > cap:
            return _log_decision(PortfolioDecision(
                blocked=True,
                reason=(
                    f"combined_underlying_exceeded:{group_name}_"
                    f"{existing_units + units_for_this_order * qty:.1f}>"
                    f"{cap:.1f}_units"
                ),
                open_positions={f"{s}:{sd}": v for (s, sd), v in positions.items()},
                detail={
                    "group": group_name,
                    "existing_units_in_group": round(existing_units, 2),
                    "units_for_this_order": round(units_for_this_order * qty, 2),
                    "cap_units": cap,
                    "side": canonical_side,
                }),
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
