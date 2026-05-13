"""Layer 4: Data health check — full inventory of every CSV bar file on disk,
cross-referenced against per-bot registry data requirements.

Produces a per-bot GREEN/AMBER/RED status with row counts, time spans, and
missing-critical detail. Designed as both a standalone CLI and a library
callable from fleet_supervisor.

Usage
-----
    python -m eta_engine.scripts.data_health_check
    python -m eta_engine.scripts.data_health_check --json
    python -m eta_engine.scripts.data_health_check --bot btc_sage_daily_etf
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from eta_engine.data.library import default_library  # noqa: E402

if TYPE_CHECKING:
    from eta_engine.data.audit import BotAudit
    from eta_engine.data.library import DataLibrary, DatasetMeta


@dataclass
class DatasetSummary:
    """One data file on disk, summarized."""

    symbol: str
    timeframe: str
    path: str
    row_count: int
    start_ts: str
    end_ts: str
    days_span: float


@dataclass
class BotHealthRow:
    bot_id: str
    status: str
    critical_available: list[DatasetSummary] = field(default_factory=list)
    critical_missing: list[str] = field(default_factory=list)
    optional_available: list[DatasetSummary] = field(default_factory=list)
    optional_missing: list[str] = field(default_factory=list)


def _describe_dataset(ds: DatasetMeta) -> DatasetSummary:
    return DatasetSummary(
        symbol=ds.symbol,
        timeframe=ds.timeframe,
        path=str(ds.path),
        row_count=ds.row_count,
        start_ts=ds.start_ts.strftime("%Y-%m-%d"),
        end_ts=ds.end_ts.strftime("%Y-%m-%d"),
        days_span=round((ds.end_ts - ds.start_ts).total_seconds() / 86400, 1),
    )


def _make_row(audit: BotAudit) -> BotHealthRow:
    available_crit = [_describe_dataset(ds) for req, ds in audit.available if req.critical]
    available_opt = [_describe_dataset(ds) for req, ds in audit.available if not req.critical]
    missing_crit = [f"{r.kind}:{r.symbol}/{r.timeframe or '-'}" for r in audit.missing_critical]
    missing_opt = [f"{r.kind}:{r.symbol}/{r.timeframe or '-'}" for r in audit.missing_optional]
    if audit.deactivated:
        status = "DEACTIVATED"
    elif audit.missing_critical:
        status = "RED"
    elif audit.missing_optional:
        status = "AMBER"
    elif audit.available:
        status = "GREEN"
    else:
        status = "UNKNOWN"
    return BotHealthRow(
        bot_id=audit.bot_id,
        status=status,
        critical_available=available_crit,
        critical_missing=missing_crit,
        optional_available=available_opt,
        optional_missing=missing_opt,
    )


def run_health_check(
    *,
    library: DataLibrary | None = None,
    bot_filter: str | None = None,
) -> list[BotHealthRow]:
    from eta_engine.data.audit import audit_all

    lib = library or default_library()
    audits = audit_all(lib)
    rows = [_make_row(a) for a in audits]
    if bot_filter:
        rows = [r for r in rows if r.bot_id == bot_filter]
    return rows


def _summary_line(rows: list[BotHealthRow]) -> dict:
    green = sum(1 for r in rows if r.status == "GREEN")
    amber = sum(1 for r in rows if r.status == "AMBER")
    red = sum(1 for r in rows if r.status == "RED")
    deact = sum(1 for r in rows if r.status == "DEACTIVATED")
    unk = sum(1 for r in rows if r.status == "UNKNOWN")
    return {"green": green, "amber": amber, "red": red, "deactivated": deact, "unknown": unk, "total": len(rows)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="data_health_check")
    p.add_argument("--json", action="store_true")
    p.add_argument("--bot", type=str, default=None)
    args = p.parse_args(argv)

    rows = run_health_check(bot_filter=args.bot)
    if args.json:
        payload = {
            "summary": _summary_line(rows),
            "bots": [
                {
                    "bot_id": r.bot_id,
                    "status": r.status,
                    "critical_available": [
                        {"symbol": d.symbol, "tf": d.timeframe, "rows": d.row_count, "span_days": round(d.days_span)}
                        for d in r.critical_available
                    ],
                    "critical_missing": r.critical_missing,
                    "optional_missing": r.optional_missing,
                }
                for r in rows
            ],
            "generated": datetime.now(tz=UTC).isoformat(),
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        header = f"{'Bot':<24} {'Status':<12} {'Critical datasets':<60} {'Missing critical'}"
        print(header)
        print("-" * 140)
        for r in rows:
            avail_str = (
                ", ".join(f"{d.symbol}/{d.timeframe}({d.row_count}r,{d.days_span:.0f}d)" for d in r.critical_available)
                or "—"
            )
            miss_str = ", ".join(r.critical_missing) or "—"
            status_icon = {
                "GREEN": "GREEN",
                "AMBER": "AMBER",
                "RED": "RED  ",
                "DEACTIVATED": "DEACT",
                "UNKNOWN": "?????",
            }.get(r.status, r.status)
            print(f"{r.bot_id:<24} {status_icon:<12} {avail_str:<60} {miss_str}")
        summary = _summary_line(rows)
        print(
            f"\nGREEN={summary['green']} AMBER={summary['amber']} RED={summary['red']} "
            f"DEACT={summary['deactivated']} / {summary['total']} total"
        )

        # Show the global data catalog
        print(f"\n{'=' * 60}")
        print("Global data catalog")
        print("=" * 60)
        lib = default_library()
        for ds in sorted(lib.list(), key=lambda d: (d.symbol, d.timeframe)):
            days = (ds.end_ts - ds.start_ts).total_seconds() / 86400
            print(
                f"  {ds.symbol:<20} {ds.timeframe:<6} {ds.row_count:>8} rows  {days:>8.0f}d  "
                f"{ds.start_ts.strftime('%Y-%m-%d')} → {ds.end_ts.strftime('%Y-%m-%d')}  {ds.path}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
