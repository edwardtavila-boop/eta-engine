"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_supervisor_state_persister
====================================================================
Drop-in helper the supervisor calls on every heartbeat to persist
its belief-state about open positions.  The file it writes is the
SAME file ``l2_reconciliation.py`` reads — closing the reconciliation
loop.

Why this exists
---------------
``l2_reconciliation.py`` compares broker truth (broker_fills.jsonl)
against supervisor belief (supervisor_open_positions.json).  Without
this module nothing writes that file, so the reconciliation script
sees an empty supervisor side and every broker position appears as
a GHOST_POSITION.

The supervisor (jarvis_strategy_supervisor) keeps ``open_positions``
in memory.  This module exposes ``persist_open_positions()`` which
the operator wires into the supervisor heartbeat (every 30s typical).
Each call serializes the current belief atomically.

Wiring (one line added to supervisor heartbeat)
-----------------------------------------------
::

    from eta_engine.scripts.l2_supervisor_state_persister import (
        persist_open_positions,
    )

    def supervisor_heartbeat():
        # ... existing heartbeat logic ...
        persist_open_positions(
            [
                {"bot_id": bot.bot_id, "symbol": bot.symbol,
                 "side": pos.side, "qty": pos.qty}
                for bot in supervisor.bots
                for pos in bot.open_positions
            ]
        )

Atomicity
---------
Writes go to ``<file>.tmp`` first then ``os.replace`` — guarantees
the reconciliation reader never sees a half-written file.

Schema (matches l2_reconciliation expectations)
-----------------------------------------------
::

    {
        "ts":        "<ISO 8601 UTC>",
        "n_open":    3,
        "positions": [
            {"bot_id": "ETA-L2-BookImbalance-MNQ",
             "symbol": "MNQ",
             "side":   "LONG",
             "qty":    1},
            ...
        ]
    }

The dict-wrapped form is read by ``l2_reconciliation.load_supervisor_positions``
which handles both ``[…]`` and ``{positions: […]}``.

Run (manually inspect last write)
---------------------------------
::

    python -m eta_engine.scripts.l2_supervisor_state_persister --show
"""
from __future__ import annotations

# ruff: noqa: ANN401, PLR2004
# ANN401: the public persister accepts arbitrary dict-or-dataclass records
# from the supervisor; constraining to a typed protocol would break callers.
import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT.parent / "var" / "eta_engine" / "state"
SUPERVISOR_STATE = STATE_DIR / "supervisor_open_positions.json"


@dataclass
class PersistResult:
    """Return value from persist_open_positions — operator can log
    or alert on staleness/failure."""
    ok: bool
    n_positions: int
    path: str
    error: str | None = None


def _normalize_record(rec: Any) -> dict | None:
    """Best-effort normalization of a single position record.  Accepts
    either a dict-like {bot_id, symbol, side, qty} or an object with
    matching attributes.  Returns None if mandatory fields missing or
    types invalid — caller drops it."""
    try:
        if isinstance(rec, dict):
            bot_id = rec.get("bot_id")
            symbol = rec.get("symbol")
            side = rec.get("side")
            qty = rec.get("qty")
        else:
            bot_id = getattr(rec, "bot_id", None)
            symbol = getattr(rec, "symbol", None)
            side = getattr(rec, "side", None)
            qty = getattr(rec, "qty", None)
        if bot_id is None or symbol is None or side is None or qty is None:
            return None
        qty_int = int(qty)
        if qty_int <= 0:
            # Closed/flat positions don't belong in open_positions.
            return None
        side_norm = str(side).upper()
        if side_norm not in ("LONG", "SHORT"):
            return None
        return {
            "bot_id": str(bot_id),
            "symbol": str(symbol),
            "side": side_norm,
            "qty": qty_int,
        }
    except (TypeError, ValueError):
        return None


def persist_open_positions(
    positions: list[Any],
    *,
    _path: Path | None = None,
    _now: datetime | None = None,
) -> PersistResult:
    """Atomically write supervisor's open-positions belief.

    Parameters
    ----------
    positions : list
        Iterable of dict or dataclass with keys/attrs:
        bot_id, symbol, side, qty.  Records that fail normalization
        are silently dropped (logged via stderr).
    _path : Path (test seam)
        Override target file.  Default = STATE_DIR/supervisor_open_positions.json.
    _now : datetime (test seam)
        Override timestamp.

    Returns
    -------
    PersistResult
        ok=False indicates the supervisor should re-try next heartbeat;
        a stale state file is a louder reconciliation failure than no
        file at all, so the operator paging on staleness is the safety
        net.

    Side effects
    ------------
    Writes ``<path>.tmp`` then ``os.replace`` — atomic per POSIX/Win.
    Creates parent dir if missing.  Does NOT raise on disk errors
    (returns ok=False) — supervisor is the user of last resort and
    we never want a state write to crash the trading loop.
    """
    path = _path if _path is not None else SUPERVISOR_STATE
    now = _now or datetime.now(UTC)

    # Normalize — drop bad records but proceed with the rest.
    cleaned: list[dict] = []
    n_dropped = 0
    for rec in positions:
        normalized = _normalize_record(rec)
        if normalized is None:
            n_dropped += 1
            continue
        cleaned.append(normalized)
    if n_dropped:
        print(
            f"WARN: state_persister dropped {n_dropped} malformed position "
            f"record(s); persisted {len(cleaned)}",
            file=sys.stderr,
        )

    payload = {
        "ts": now.isoformat(),
        "n_open": len(cleaned),
        "positions": cleaned,
    }

    # Atomic write.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
        os.replace(tmp_path, path)
    except OSError as e:
        return PersistResult(
            ok=False, n_positions=len(cleaned),
            path=str(path), error=f"OSError: {e}",
        )

    return PersistResult(
        ok=True, n_positions=len(cleaned), path=str(path),
    )


def read_persisted_state(*, _path: Path | None = None) -> dict | None:
    """Read back the most recent persisted state (for diagnostics)."""
    path = _path if _path is not None else SUPERVISOR_STATE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def staleness_seconds(*, _path: Path | None = None,
                       _now: datetime | None = None) -> float | None:
    """Return seconds since the state file was last persisted.  None
    if the file is missing or malformed."""
    path = _path if _path is not None else SUPERVISOR_STATE
    data = read_persisted_state(_path=path)
    if data is None:
        return None
    ts = data.get("ts")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    now = _now or datetime.now(UTC)
    return (now - dt).total_seconds()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show", action="store_true",
                    help="Print the current persisted state and exit")
    ap.add_argument("--staleness", action="store_true",
                    help="Print seconds since last persist; nonzero "
                         "exit if file missing/malformed")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.staleness:
        age = staleness_seconds()
        if age is None:
            print("state file missing or malformed", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({"staleness_seconds": age}))
        else:
            print(f"staleness: {age:.1f}s "
                  f"({age / 60:.1f}min)")
        # Operator gate — warn if older than 5 minutes
        return 1 if age > 300 else 0

    if args.show:
        state = read_persisted_state()
        if state is None:
            print("(no persisted state)")
            return 0
        if args.json:
            print(json.dumps(state, indent=2))
            return 0
        print()
        print("=" * 78)
        print("SUPERVISOR OPEN POSITIONS  (most recent persist)")
        print("=" * 78)
        print(f"  ts        : {state.get('ts')}")
        print(f"  n_open    : {state.get('n_open')}")
        print(f"  path      : {SUPERVISOR_STATE}")
        positions = state.get("positions", [])
        if not positions:
            print("  (no open positions)")
        else:
            print()
            print(f"  {'Bot':<32s} {'Symbol':<8s} {'Side':<6s} {'Qty':<5s}")
            print(f"  {'-'*32:<32s} {'-'*8:<8s} {'-'*6:<6s} {'-'*5}")
            for p in positions:
                print(f"  {p.get('bot_id', '?'):<32s} "
                      f"{p.get('symbol', '?'):<8s} "
                      f"{p.get('side', '?'):<6s} "
                      f"{int(p.get('qty', 0)):<5d}")
        print()
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
