"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_reconciliation
========================================================
Reconciles broker truth against the supervisor's belief state.
Detects ghost positions, phantom rejects, and stale open_position
records that survived a crash.

Why this exists
---------------
The supervisor (jarvis_strategy_supervisor) maintains bot.open_position
as its belief about which positions are live.  The broker (IBKR) is
the ultimate source of truth.  These can diverge:
  - Supervisor crashes mid-fill → broker has position, supervisor doesn't
  - Broker rejects but supervisor optimistically pre-set open_position
    (the hardening pass's PATTERN B fixed most of this, but race
    conditions remain)
  - Network blip between order ack and fill report
  - Manual operator close in the broker GUI while supervisor still
    believes the position is open

Untreated, divergence means:
  - Risk limits miscount (supervisor thinks 0 contracts, broker has 5)
  - Strategy fires re-entry on a position that's already open
  - Loss-limit detection misses a position closed externally

This script:
  1. Reads supervisor's open_position state (persisted in
     var/eta_engine/state/open_positions.json or similar)
  2. Reads broker fill log (broker_fills.jsonl) and reconstructs
     positions by signal_id
  3. Reports discrepancies per (bot_id, symbol)

Output verdicts
---------------
- IN_SYNC        : supervisor and broker agree
- GHOST_POSITION : broker has position, supervisor doesn't know
- PHANTOM_BELIEF : supervisor thinks there's a position, broker doesn't
- QTY_MISMATCH   : both agree something is open, but qty differs

Operator action
---------------
Any non-IN_SYNC verdict pages the operator.  Resolution is manual:
  - GHOST → close on broker side; investigate why supervisor didn't
    persist the open
  - PHANTOM → clear supervisor's belief; investigate the missing
    close event

Run
---
::

    python -m eta_engine.scripts.l2_reconciliation
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR = ROOT.parent / "var" / "eta_engine" / "state"
BROKER_FILL_LOG = LOG_DIR / "broker_fills.jsonl"
SUPERVISOR_STATE = STATE_DIR / "supervisor_open_positions.json"
RECONCILIATION_LOG = LOG_DIR / "l2_reconciliation.jsonl"


@dataclass
class PositionRecord:
    bot_id: str
    symbol: str
    side: str  # "LONG" | "SHORT"
    qty: int


@dataclass
class Discrepancy:
    bot_id: str
    symbol: str
    side: str
    supervisor_qty: int
    broker_qty: int
    verdict: str  # IN_SYNC | GHOST_POSITION | PHANTOM_BELIEF | QTY_MISMATCH
    notes: list[str] = field(default_factory=list)


@dataclass
class ReconciliationReport:
    n_supervisor_positions: int
    n_broker_positions: int
    n_in_sync: int
    n_discrepancies: int
    discrepancies: list[Discrepancy] = field(default_factory=list)


def load_supervisor_positions(*, _path: Path | None = None) -> list[PositionRecord]:
    """Read supervisor's persisted open_positions state.

    Format expected: [{bot_id, symbol, side, qty}, ...]
    Returns empty list if state file missing.
    """
    path = _path if _path is not None else SUPERVISOR_STATE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("positions", [])
        if not isinstance(data, list):
            return []
        out: list[PositionRecord] = []
        for rec in data:
            try:
                out.append(
                    PositionRecord(
                        bot_id=str(rec.get("bot_id", "?")),
                        symbol=str(rec.get("symbol", "?")),
                        side=str(rec.get("side", "?")).upper(),
                        qty=int(rec.get("qty", 0)),
                    )
                )
            except (TypeError, ValueError):
                continue
        return out
    except (OSError, json.JSONDecodeError):
        return []


def reconstruct_broker_positions(*, _path: Path | None = None, since_days: int = 14) -> list[PositionRecord]:
    """Walk broker_fills.jsonl and reconstruct net positions per
    (signal_id, symbol).  Entry fills add; exit fills subtract.
    Returns the list of open positions (net qty > 0)."""
    path = _path if _path is not None else BROKER_FILL_LOG
    if not path.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    by_sig: dict[str, dict] = {}
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
                sid = rec.get("signal_id")
                if not sid:
                    continue
                exit_reason = str(rec.get("exit_reason", "")).upper()
                qty = int(rec.get("qty_filled", 0))
                side = str(rec.get("side", "?")).upper()
                entry = by_sig.setdefault(
                    sid,
                    {
                        "side": side,
                        "entered": 0,
                        "exited": 0,
                        "symbol": None,
                        "bot_id": None,
                    },
                )
                if exit_reason == "ENTRY":
                    entry["entered"] += qty
                    entry["symbol"] = rec.get("symbol") or entry["symbol"]
                    entry["bot_id"] = rec.get("bot_id") or entry["bot_id"]
                elif exit_reason in ("TARGET", "STOP", "TIMEOUT", "CANCEL"):
                    entry["exited"] += qty
    except OSError:
        return []
    # Build open-position list
    open_positions: list[PositionRecord] = []
    for sid, entry in by_sig.items():
        net = entry["entered"] - entry["exited"]
        if net > 0:
            symbol = entry["symbol"]
            if not symbol:
                # Infer from signal_id prefix
                parts = sid.split("-")
                symbol = parts[0] if parts else "?"
            bot_id = entry["bot_id"] or "?"
            open_positions.append(
                PositionRecord(
                    bot_id=bot_id,
                    symbol=symbol,
                    side=entry["side"],
                    qty=net,
                )
            )
    return open_positions


def reconcile(*, _supervisor_path: Path | None = None, _broker_path: Path | None = None) -> ReconciliationReport:
    """Compare supervisor belief vs broker truth.  Returns discrepancies."""
    supervisor_pos = load_supervisor_positions(_path=_supervisor_path)
    broker_pos = reconstruct_broker_positions(_path=_broker_path)

    # Index by (bot_id, symbol, side)
    sup_idx: dict[tuple[str, str, str], int] = {}
    for p in supervisor_pos:
        key = (p.bot_id, p.symbol, p.side)
        sup_idx[key] = sup_idx.get(key, 0) + p.qty
    brk_idx: dict[tuple[str, str, str], int] = {}
    for p in broker_pos:
        key = (p.bot_id, p.symbol, p.side)
        brk_idx[key] = brk_idx.get(key, 0) + p.qty

    all_keys = set(sup_idx.keys()) | set(brk_idx.keys())
    discrepancies: list[Discrepancy] = []
    n_in_sync = 0
    for key in sorted(all_keys):
        bot_id, symbol, side = key
        sup_q = sup_idx.get(key, 0)
        brk_q = brk_idx.get(key, 0)
        if sup_q == brk_q:
            n_in_sync += 1
            continue
        if sup_q == 0 and brk_q > 0:
            verdict = "GHOST_POSITION"
        elif sup_q > 0 and brk_q == 0:
            verdict = "PHANTOM_BELIEF"
        else:
            verdict = "QTY_MISMATCH"
        discrepancies.append(
            Discrepancy(
                bot_id=bot_id,
                symbol=symbol,
                side=side,
                supervisor_qty=sup_q,
                broker_qty=brk_q,
                verdict=verdict,
            )
        )

    return ReconciliationReport(
        n_supervisor_positions=sum(sup_idx.values()),
        n_broker_positions=sum(brk_idx.values()),
        n_in_sync=n_in_sync,
        n_discrepancies=len(discrepancies),
        discrepancies=discrepancies,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = reconcile()
    try:
        with RECONCILIATION_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(UTC).isoformat(), **asdict(report)}, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: reconciliation log write failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(asdict(report), indent=2))
        return 1 if report.n_discrepancies > 0 else 0

    print()
    print("=" * 78)
    print("L2 RECONCILIATION  (supervisor belief vs broker truth)")
    print("=" * 78)
    print(f"  supervisor positions : {report.n_supervisor_positions}")
    print(f"  broker positions     : {report.n_broker_positions}")
    print(f"  in sync              : {report.n_in_sync}")
    print(f"  discrepancies        : {report.n_discrepancies}")
    if report.discrepancies:
        print()
        print(f"  {'Bot':<25s} {'Symbol':<8s} {'Side':<6s} {'Sup':<5s} {'Brk':<5s} Verdict")
        print(f"  {'-' * 25:<25s} {'-' * 8:<8s} {'-' * 6:<6s} {'-' * 5:<5s} {'-' * 5:<5s} {'-' * 20}")
        for d in report.discrepancies:
            print(
                f"  {d.bot_id:<25s} {d.symbol:<8s} {d.side:<6s} {d.supervisor_qty:<5d} {d.broker_qty:<5d} {d.verdict}"
            )
    print()
    return 1 if report.n_discrepancies > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
