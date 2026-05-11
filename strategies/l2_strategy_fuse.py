"""
EVOLUTIONARY TRADING ALGO  //  strategies.l2_strategy_fuse
==========================================================
Per-strategy consecutive-loss circuit breaker — pauses a strategy
after N losing trades in a row, independent of the daily-loss limit.

Why this exists
---------------
Daily-loss limits trigger AFTER the day's damage is done.  A
strategy can hit -$200 in 2 trades or in 8 — and the daily limit
only fires after the second case.  A consecutive-loss fuse fires
EARLIER: "you've lost 5 in a row, something is structurally
wrong, stop."

Common patterns the fuse catches:
  - Regime shift the spread_regime_filter missed
  - Bug in the strategy state machine (off-by-one in signal logic)
  - Data quality issue feeding garbage to the strategy
  - Counterparty issue (broker rejecting orders silently)

Mechanic
--------
Per (strategy, symbol):
  - Track terminal-fill outcomes (TARGET = +1, STOP = -1, TIMEOUT skipped)
  - When consecutive losses reach ``fuse_threshold`` (default 5), set
    ``blown_at`` timestamp
  - Strategy is in 'BLOWN' state until ``cooldown_seconds`` elapses
    (default 3600 = 1 hour) OR operator manually resets
  - During BLOWN state, the trading_gate refuses new entries

Reset
-----
Three reset paths:
  1. ``cooldown_seconds`` of wallclock elapses → auto-reset
  2. Operator calls ``reset_fuse(strategy, symbol)`` → manual reset
  3. A winning trade resets the consecutive counter (only counts the
     trades that succeeded post-reset)
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
BROKER_FILL_LOG = LOG_DIR / "broker_fills.jsonl"
FUSE_STATE_FILE = ROOT.parent / "var" / "eta_engine" / "state" / "l2_strategy_fuses.json"


DEFAULT_FUSE_THRESHOLD = 5
DEFAULT_COOLDOWN_SECONDS = 3600


@dataclass
class FuseState:
    """Per-(strategy, symbol) fuse state."""
    strategy_id: str
    symbol: str
    consecutive_losses: int
    blown: bool
    blown_at: str | None
    last_outcome_ts: str | None


@dataclass
class FuseRegistry:
    states: dict[str, FuseState] = field(default_factory=dict)
    # Key format: f"{strategy_id}|{symbol}"


def _key(strategy_id: str, symbol: str) -> str:
    return f"{strategy_id}|{symbol}"


def load_registry(*, _path: Path | None = None) -> FuseRegistry:
    path = _path if _path is not None else FUSE_STATE_FILE
    if not path.exists():
        return FuseRegistry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        states: dict[str, FuseState] = {}
        for k, v in data.get("states", {}).items():
            states[k] = FuseState(**v)
        return FuseRegistry(states=states)
    except (OSError, json.JSONDecodeError, TypeError):
        return FuseRegistry()


def save_registry(reg: FuseRegistry, *, _path: Path | None = None) -> None:
    path = _path if _path is not None else FUSE_STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "states": {k: asdict(v) for k, v in reg.states.items()}
        }, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"l2_strategy_fuse WARN: save failed: {e}", file=sys.stderr)


def record_outcome(strategy_id: str, symbol: str, *, won: bool,
                     ts: datetime | None = None,
                     fuse_threshold: int = DEFAULT_FUSE_THRESHOLD,
                     _path: Path | None = None) -> FuseState:
    """Record a terminal trade outcome.  Updates consecutive count and
    blows the fuse when threshold is hit.  Returns the new state."""
    reg = load_registry(_path=_path)
    k = _key(strategy_id, symbol)
    state = reg.states.get(k) or FuseState(
        strategy_id=strategy_id, symbol=symbol,
        consecutive_losses=0, blown=False, blown_at=None,
        last_outcome_ts=None,
    )
    ts = ts or datetime.now(UTC)
    state.last_outcome_ts = ts.isoformat()
    if won:
        state.consecutive_losses = 0
        # Winning trade post-blow doesn't auto-reset the fuse — operator
        # or cooldown must clear it.  But if not blown, the counter just
        # resets.
    else:
        state.consecutive_losses += 1
        if state.consecutive_losses >= fuse_threshold and not state.blown:
            state.blown = True
            state.blown_at = ts.isoformat()
    reg.states[k] = state
    save_registry(reg, _path=_path)
    return state


def check_fuse(strategy_id: str, symbol: str,
                *, cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
                now: datetime | None = None,
                _path: Path | None = None) -> dict:
    """Check whether a strategy is currently fuse-blown.  Auto-resets
    if cooldown_seconds has elapsed since blown_at."""
    reg = load_registry(_path=_path)
    k = _key(strategy_id, symbol)
    state = reg.states.get(k)
    if state is None or not state.blown:
        return {"blocked": False, "reason": "ok",
                 "consecutive_losses": state.consecutive_losses if state else 0}
    now = now or datetime.now(UTC)
    if state.blown_at:
        try:
            blown_dt = datetime.fromisoformat(state.blown_at.replace("Z", "+00:00"))
            age = (now - blown_dt).total_seconds()
            if age >= cooldown_seconds:
                # Auto-reset
                state.blown = False
                state.blown_at = None
                state.consecutive_losses = 0
                reg.states[k] = state
                save_registry(reg, _path=_path)
                return {"blocked": False, "reason": "cooldown_elapsed",
                         "consecutive_losses": 0}
        except ValueError:
            pass
    return {"blocked": True, "reason": "strategy_fuse_blown",
             "consecutive_losses": state.consecutive_losses,
             "blown_at": state.blown_at,
             "cooldown_remaining_seconds": (
                 cooldown_seconds - (now -
                                       datetime.fromisoformat(
                                           state.blown_at.replace("Z", "+00:00")
                                       )).total_seconds()
                 if state.blown_at else None
             )}


def reset_fuse(strategy_id: str, symbol: str,
                *, _path: Path | None = None) -> bool:
    """Operator manual reset.  Returns True if a fuse was actually cleared."""
    reg = load_registry(_path=_path)
    k = _key(strategy_id, symbol)
    if k not in reg.states:
        return False
    state = reg.states[k]
    if not state.blown:
        return False
    state.blown = False
    state.blown_at = None
    state.consecutive_losses = 0
    reg.states[k] = state
    save_registry(reg, _path=_path)
    return True
