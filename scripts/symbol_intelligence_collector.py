"""24/7 symbol-intelligence collection wrapper for the VPS.

The collector keeps the local symbol-intel lake and operator snapshot fresh
from existing truth surfaces. It is intentionally broker-safe: market-data
entitlement failures are reflected by the audit, not by submitting orders or
touching broker state.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from eta_engine.data.symbol_intel import SymbolIntelStore  # noqa: E402
from eta_engine.scripts import workspace_roots  # noqa: E402
from eta_engine.scripts.symbol_intelligence_audit import (  # noqa: E402
    PRIORITY_SYMBOLS,
    backfill_bars_from_history,
    backfill_decisions_from_journal,
    backfill_events_from_calendar,
    backfill_outcomes_from_closed_trade_ledger,
    backfill_quality_from_audit,
    run_audit,
    write_snapshot,
)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception:
        return {"read_error": True, "path": str(path)}


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_stale_lock(path: Path, *, stale_after_seconds: int, now: datetime) -> bool:
    raw = _read_json(path) or {}
    started = _parse_ts(raw.get("started_at_utc"))
    if started is None:
        age_seconds = time.time() - path.stat().st_mtime
    else:
        age_seconds = (now - started).total_seconds()
    return age_seconds > stale_after_seconds


@contextmanager
def acquire_lock(
    path: Path = workspace_roots.ETA_SYMBOL_INTELLIGENCE_COLLECTOR_LOCK_PATH,
    *,
    stale_after_seconds: int = 600,
    now: datetime | None = None,
) -> Iterator[None]:
    now = now or datetime.now(tz=UTC)
    workspace_roots.ensure_parent(path)
    if path.exists():
        if _is_stale_lock(path, stale_after_seconds=stale_after_seconds, now=now):
            path.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"symbol-intelligence collector already running: {path}")

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(path, flags)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "started_at_utc": now.isoformat()}, fh)
        yield
    finally:
        path.unlink(missing_ok=True)


def _collector_status(audit_payload: dict[str, Any], *, tws_watchdog: dict[str, Any] | None = None) -> str:
    if tws_watchdog and tws_watchdog.get("healthy") is False:
        return "degraded_gateway"
    if audit_payload["overall_status"] == "red":
        return "degraded"
    return "ok"


def run_collection(
    *,
    store: SymbolIntelStore | None = None,
    symbols: list[str] | tuple[str, ...] = PRIORITY_SYMBOLS,
    now: datetime | None = None,
    status_path: Path = workspace_roots.ETA_SYMBOL_INTELLIGENCE_COLLECTOR_STATUS_PATH,
    snapshot_path: Path = workspace_roots.ETA_SYMBOL_INTELLIGENCE_SNAPSHOT_PATH,
    history_root: Path = workspace_roots.MNQ_HISTORY_ROOT,
    calendar_path: Path = workspace_roots.ETA_RUNTIME_STATE_DIR / "event_calendar.yaml",
    journal_path: Path = workspace_roots.ETA_RUNTIME_DECISION_JOURNAL_PATH,
    closed_trade_path: Path = workspace_roots.ETA_CLOSED_TRADE_LEDGER_PATH,
    tws_watchdog_path: Path = workspace_roots.ETA_RUNTIME_STATE_DIR / "tws_watchdog.json",
    ibgateway_reauth_path: Path = workspace_roots.ETA_RUNTIME_STATE_DIR / "ibgateway_reauth.json",
    bot_symbol_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = now or datetime.now(tz=UTC)
    monotonic_start = time.monotonic()
    store = store or SymbolIntelStore()

    counts = {
        "bars": backfill_bars_from_history(history_root=history_root, store=store, symbols=symbols),
        "events": backfill_events_from_calendar(calendar_path=calendar_path, store=store, symbols=symbols),
        "decisions": backfill_decisions_from_journal(
            journal_path=journal_path,
            store=store,
            symbols=symbols,
            bot_symbol_map=bot_symbol_map,
        ),
        "outcomes": backfill_outcomes_from_closed_trade_ledger(source_path=closed_trade_path, store=store),
    }
    counts["quality"] = backfill_quality_from_audit(store=store, symbols=symbols, now=started)

    audit_payload = run_audit(symbols=symbols, store=store, now=started)
    write_snapshot(audit_payload, path=snapshot_path)
    tws_watchdog = _read_json(tws_watchdog_path)
    ibgateway_reauth = _read_json(ibgateway_reauth_path)
    finished = datetime.now(tz=UTC)
    payload = {
        "kind": "eta_symbol_intelligence_collector",
        "status": _collector_status(audit_payload, tws_watchdog=tws_watchdog),
        "started_at_utc": started.isoformat(),
        "finished_at_utc": finished.isoformat(),
        "duration_seconds": round(time.monotonic() - monotonic_start, 3),
        "bootstrap_counts": counts,
        "audit": {
            "overall_status": audit_payload["overall_status"],
            "symbols": audit_payload["symbols"],
        },
        "data_lake_root": str(store.root),
        "snapshot_path": str(snapshot_path),
        "status_path": str(status_path),
        "tws_watchdog": tws_watchdog,
        "ibgateway_reauth": ibgateway_reauth,
    }
    workspace_roots.ensure_parent(status_path)
    status_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="symbol_intelligence_collector")
    parser.add_argument("--json", action="store_true", help="print JSON status")
    parser.add_argument("--symbol", action="append", dest="symbols", help="symbol to collect/audit, repeatable")
    parser.add_argument("--stale-lock-minutes", type=int, default=10)
    args = parser.parse_args(argv)

    try:
        with acquire_lock(stale_after_seconds=max(args.stale_lock_minutes, 1) * 60):
            payload = run_collection(symbols=args.symbols or list(PRIORITY_SYMBOLS))
    except Exception as exc:
        payload = {
            "kind": "eta_symbol_intelligence_collector",
            "status": "error",
            "finished_at_utc": datetime.now(tz=UTC).isoformat(),
            "error": f"{type(exc).__name__}: {exc}",
        }
        workspace_roots.ensure_parent(workspace_roots.ETA_SYMBOL_INTELLIGENCE_COLLECTOR_STATUS_PATH)
        workspace_roots.ETA_SYMBOL_INTELLIGENCE_COLLECTOR_STATUS_PATH.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(payload["error"])
        return 2

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"symbol-intelligence collector {payload['status']} "
            f"audit={payload['audit']['overall_status']} duration={payload['duration_seconds']}s"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
