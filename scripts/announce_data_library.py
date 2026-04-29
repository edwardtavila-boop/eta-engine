"""
EVOLUTIONARY TRADING ALGO  //  scripts.announce_data_library
=============================================================
Emit the current ``data.library`` inventory as a single
``Actor.JARVIS`` event on the decision journal so JARVIS (and any
operator scanning the journal) knows what's testable without
walking the filesystem.

Designed to be re-run after data fetch jobs complete — the latest
JARVIS event with ``intent="data_inventory"`` is the canonical
"what's available right now" snapshot.

Usage::

    python -m eta_engine.scripts.announce_data_library
        [--journal var/eta_engine/state/decision_journal.jsonl]
        [--dry-run]

The dry-run flag prints the markdown summary but doesn't append the
event — useful for operator-side eyeballing before publishing.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import (  # noqa: E402
    ETA_DATA_INVENTORY_SNAPSHOT_PATH,
    ETA_RUNTIME_DECISION_JOURNAL_PATH,
    ensure_parent,
)

if TYPE_CHECKING:
    from eta_engine.data.audit import BotAudit
    from eta_engine.data.library import DataLibrary, DatasetMeta
    from eta_engine.data.requirements import DataRequirement

_DEFAULT_JOURNAL = ETA_RUNTIME_DECISION_JOURNAL_PATH
_DEFAULT_SNAPSHOT = ETA_DATA_INVENTORY_SNAPSHOT_PATH

_INTRADAY_TIMEFRAMES = frozenset({"1s", "5s", "10s", "30s", "1m", "5m", "15m", "30m", "1h", "2h", "4h"})
_FRESHNESS_THRESHOLDS_DAYS = {
    "intraday": {"fresh": 2.0, "warm": 10.0},
    "daily_or_higher": {"fresh": 3.0, "warm": 14.0},
}


def _requirement_payload(req: DataRequirement) -> dict[str, Any]:
    return {
        "kind": req.kind,
        "symbol": req.symbol,
        "timeframe": req.timeframe,
        "critical": req.critical,
        "note": req.note,
    }


def _expected_dataset_symbol(req: DataRequirement) -> str:
    if req.kind in {"bars", "correlation"}:
        return req.symbol.upper()
    if req.kind == "funding":
        return f"{req.symbol.upper()}FUND"
    if req.kind == "onchain":
        return f"{req.symbol.upper()}ONCHAIN"
    if req.kind == "sentiment":
        return f"{req.symbol.upper()}SENT"
    if req.kind == "macro":
        return f"{req.symbol.upper()}MACRO"
    return req.symbol.upper()


def _resolution_payload(req: DataRequirement, dataset: DatasetMeta) -> dict[str, Any]:
    expected_symbol = _expected_dataset_symbol(req)
    requested_timeframe = req.timeframe
    effective_timeframe = requested_timeframe or ("D" if req.kind in {"onchain", "sentiment", "macro"} else None)
    dataset_symbol = dataset.symbol.upper()
    timeframe_matches = effective_timeframe is None or dataset.timeframe == effective_timeframe

    if dataset_symbol == expected_symbol:
        mode = "timeframe_fallback" if not timeframe_matches else (
            "synthetic" if expected_symbol != req.symbol.upper() else "direct"
        )
    else:
        mode = "proxy"

    payload: dict[str, Any] = {
        "mode": mode,
        "requested_symbol": req.symbol,
        "requested_timeframe": requested_timeframe,
        "expected_dataset_symbol": expected_symbol,
        "dataset_symbol": dataset.symbol,
        "dataset_timeframe": dataset.timeframe,
    }
    if req.kind == "sentiment" and dataset_symbol == "FEAR_GREEDMACRO":
        payload["quality_note"] = "crypto-wide Fear & Greed proxy for symbol-specific sentiment"
    return payload


def _available_item(
    req: DataRequirement,
    dataset: DatasetMeta,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "requirement": _requirement_payload(req),
        "dataset": _dataset_payload(dataset, generated_at=generated_at),
        "resolution": _resolution_payload(req, dataset),
    }


def _freshness_family(timeframe: str) -> str:
    return "intraday" if timeframe in _INTRADAY_TIMEFRAMES else "daily_or_higher"


def _dataset_freshness(dataset: DatasetMeta, generated_at: datetime) -> dict[str, Any]:
    now = generated_at if generated_at.tzinfo is not None else generated_at.replace(tzinfo=UTC)
    end_ts = dataset.end_ts if dataset.end_ts.tzinfo is not None else dataset.end_ts.replace(tzinfo=UTC)
    age_days = max(0.0, (now - end_ts).total_seconds() / 86_400.0)
    family = _freshness_family(dataset.timeframe)
    thresholds = _FRESHNESS_THRESHOLDS_DAYS[family]
    if age_days <= thresholds["fresh"]:
        status = "fresh"
    elif age_days <= thresholds["warm"]:
        status = "warm"
    else:
        status = "stale"
    return {
        "status": status,
        "age_days": round(age_days, 2),
        "family": family,
        "fresh_days": thresholds["fresh"],
        "warm_days": thresholds["warm"],
    }


def _dataset_payload(dataset: DatasetMeta, *, generated_at: datetime | None = None) -> dict[str, Any]:
    payload = {
        "key": dataset.key,
        "symbol": dataset.symbol,
        "timeframe": dataset.timeframe,
        "schema_kind": dataset.schema_kind,
        "rows": dataset.row_count,
        "start": dataset.start_ts.isoformat(),
        "end": dataset.end_ts.isoformat(),
        "days": round(dataset.days_span(), 2),
        "path": str(dataset.path),
    }
    if generated_at is not None:
        payload["freshness"] = _dataset_freshness(dataset, generated_at)
    return payload


def _canonical_datasets(datasets: list[DatasetMeta]) -> dict[tuple[str, str], DatasetMeta]:
    """Pick the dataset DataLibrary.get() should prefer for each symbol/timeframe."""
    canonical: dict[tuple[str, str], DatasetMeta] = {}
    for dataset in datasets:
        key = (dataset.symbol.upper(), dataset.timeframe)
        current = canonical.get(key)
        if current is None or (dataset.row_count, dataset.end_ts) > (current.row_count, current.end_ts):
            canonical[key] = dataset
    return canonical


def _critical_freshness_payload(
    audit: BotAudit,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Compact per-bot rollup for critical data freshness."""
    counts = {"fresh": 0, "warm": 0, "stale": 0, "missing": len(audit.missing_critical)}
    stale: list[dict[str, Any]] = []
    warm: list[dict[str, Any]] = []

    if audit.deactivated:
        return {
            "status": "deactivated",
            "total_critical": 0,
            "available_critical": 0,
            "missing_critical": 0,
            "counts": counts,
            "stale": [],
            "warm": [],
        }

    for req, dataset in audit.available:
        if not req.critical:
            continue
        item = _available_item(req, dataset, generated_at=generated_at)
        freshness = item["dataset"].get("freshness", {})
        status = freshness.get("status")
        if status in {"fresh", "warm", "stale"}:
            counts[status] += 1
        if status == "stale":
            stale.append(item)
        elif status == "warm":
            warm.append(item)

    if counts["missing"]:
        status = "blocked"
    elif counts["stale"]:
        status = "stale"
    elif counts["warm"]:
        status = "warm"
    else:
        status = "fresh"

    return {
        "status": status,
        "total_critical": sum(counts.values()),
        "available_critical": counts["fresh"] + counts["warm"] + counts["stale"],
        "missing_critical": counts["missing"],
        "counts": counts,
        "stale": stale,
        "warm": warm,
    }


def _optional_freshness_payload(
    audit: BotAudit,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Compact per-bot advisory rollup for optional data freshness."""
    counts = {"fresh": 0, "warm": 0, "stale": 0, "missing": len(audit.missing_optional)}
    stale: list[dict[str, Any]] = []
    warm: list[dict[str, Any]] = []

    if audit.deactivated:
        return {
            "status": "deactivated",
            "total_optional": 0,
            "available_optional": 0,
            "missing_optional": 0,
            "counts": {"fresh": 0, "warm": 0, "stale": 0, "missing": 0},
            "stale": [],
            "warm": [],
        }

    for req, dataset in audit.available:
        if req.critical:
            continue
        item = _available_item(req, dataset, generated_at=generated_at)
        freshness = item["dataset"].get("freshness", {})
        status = freshness.get("status")
        if status in {"fresh", "warm", "stale"}:
            counts[status] += 1
        if status == "stale":
            stale.append(item)
        elif status == "warm":
            warm.append(item)

    if counts["stale"]:
        status = "stale"
    elif counts["warm"]:
        status = "warm"
    elif counts["missing"]:
        status = "missing"
    elif counts["fresh"]:
        status = "fresh"
    else:
        status = "none"

    return {
        "status": status,
        "total_optional": sum(counts.values()),
        "available_optional": counts["fresh"] + counts["warm"] + counts["stale"],
        "missing_optional": counts["missing"],
        "counts": counts,
        "stale": stale,
        "warm": warm,
    }


def _audit_payload(audit: BotAudit, *, generated_at: datetime | None = None) -> dict[str, Any]:
    return {
        "bot_id": audit.bot_id,
        "runnable": audit.is_runnable,
        "deactivated": audit.deactivated,
        "critical_coverage_pct": round(audit.critical_coverage_pct, 2),
        "critical_freshness": _critical_freshness_payload(audit, generated_at=generated_at),
        "optional_freshness": _optional_freshness_payload(audit, generated_at=generated_at),
        "available": [
            _available_item(req, dataset, generated_at=generated_at)
            for req, dataset in audit.available
        ],
        "missing_critical": [_requirement_payload(req) for req in audit.missing_critical],
        "missing_optional": [_requirement_payload(req) for req in audit.missing_optional],
        "sources_hint": list(audit.sources_hint),
    }


def _freshness_summary(datasets: list[DatasetMeta], generated_at: datetime) -> dict[str, Any]:
    canonical_by_pair = _canonical_datasets(datasets)
    canonical_keys = {dataset.key for dataset in canonical_by_pair.values()}
    payload_by_key = {
        dataset.key: _dataset_payload(dataset, generated_at=generated_at)
        for dataset in datasets
    }
    items: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    for dataset in datasets:
        item = payload_by_key[dataset.key]
        item["canonical"] = dataset.key in canonical_keys
        canonical = canonical_by_pair[(dataset.symbol.upper(), dataset.timeframe)]
        if not item["canonical"]:
            canonical_payload = payload_by_key[canonical.key]
            item["superseded_by"] = {
                "key": canonical.key,
                "rows": canonical.row_count,
                "end": canonical.end_ts.isoformat(),
                "freshness": canonical_payload["freshness"],
            }
            superseded.append(item)
        items.append(item)
    counts = {"fresh": 0, "warm": 0, "stale": 0}
    canonical_counts = {"fresh": 0, "warm": 0, "stale": 0}
    for item in items:
        counts[item["freshness"]["status"]] += 1
        if item["canonical"]:
            canonical_counts[item["freshness"]["status"]] += 1
    return {
        "thresholds_days": _FRESHNESS_THRESHOLDS_DAYS,
        "counts": counts,
        "canonical_counts": canonical_counts,
        "stale": [item for item in items if item["freshness"]["status"] == "stale"],
        "canonical_stale": [
            item
            for item in items
            if item["canonical"] and item["freshness"]["status"] == "stale"
        ],
        "superseded": superseded,
        "superseded_stale": [
            item
            for item in superseded
            if item["freshness"]["status"] == "stale"
        ],
        "warm": [item for item in items if item["freshness"]["status"] == "warm"],
    }


def _bot_optional_freshness_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = {
        "fresh": 0,
        "warm": 0,
        "stale": 0,
        "missing": 0,
        "none": 0,
        "deactivated": 0,
    }
    stale_bots: list[dict[str, Any]] = []
    warm_bots: list[dict[str, Any]] = []
    missing_bots: list[dict[str, Any]] = []

    for item in items:
        bot_id = item["bot_id"]
        freshness = item["optional_freshness"]
        status = freshness["status"]
        if status in status_counts:
            status_counts[status] += 1
        if freshness["counts"]["stale"]:
            stale_bots.append({
                "bot_id": bot_id,
                "stale_count": freshness["counts"]["stale"],
                "stale": freshness["stale"],
            })
        if freshness["counts"]["warm"]:
            warm_bots.append({
                "bot_id": bot_id,
                "warm_count": freshness["counts"]["warm"],
                "warm": freshness["warm"],
            })
        if freshness["counts"]["missing"]:
            missing_bots.append({
                "bot_id": bot_id,
                "missing_optional": item["missing_optional"],
            })

    return {
        "status_counts": status_counts,
        "stale_bots": stale_bots,
        "warm_bots": warm_bots,
        "missing_bots": missing_bots,
    }


def _bot_resolution_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    mode_counts = {
        "direct": 0,
        "proxy": 0,
        "synthetic": 0,
        "timeframe_fallback": 0,
        "unknown": 0,
    }
    proxy_bots: list[dict[str, Any]] = []
    synthetic_bots: list[dict[str, Any]] = []
    timeframe_fallback_bots: list[dict[str, Any]] = []

    for item in items:
        bot_id = item["bot_id"]
        for available in item["available"]:
            resolution = available.get("resolution") or {}
            mode = resolution.get("mode", "unknown")
            if mode not in mode_counts:
                mode = "unknown"
            mode_counts[mode] += 1
            entry = {
                "bot_id": bot_id,
                "requirement": available["requirement"],
                "dataset_key": available["dataset"]["key"],
            }
            quality_note = resolution.get("quality_note")
            if quality_note:
                entry["quality_note"] = quality_note
            if mode == "proxy":
                proxy_bots.append(entry)
            elif mode == "synthetic":
                synthetic_bots.append(entry)
            elif mode == "timeframe_fallback":
                timeframe_fallback_bots.append(entry)

    return {
        "mode_counts": mode_counts,
        "proxy_bots": proxy_bots,
        "synthetic_bots": synthetic_bots,
        "timeframe_fallback_bots": timeframe_fallback_bots,
    }


def _bot_critical_freshness_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = {
        "fresh": 0,
        "warm": 0,
        "stale": 0,
        "blocked": 0,
        "deactivated": 0,
    }
    stale_bots: list[dict[str, Any]] = []
    warm_bots: list[dict[str, Any]] = []
    blocked_bots: list[dict[str, Any]] = []

    for item in items:
        bot_id = item["bot_id"]
        freshness = item["critical_freshness"]
        status = freshness["status"]
        if status in status_counts:
            status_counts[status] += 1
        if status == "stale":
            stale_bots.append({
                "bot_id": bot_id,
                "stale_count": freshness["counts"]["stale"],
                "stale": freshness["stale"],
            })
        elif status == "warm":
            warm_bots.append({
                "bot_id": bot_id,
                "warm_count": freshness["counts"]["warm"],
                "warm": freshness["warm"],
            })
        elif status == "blocked":
            blocked_bots.append({
                "bot_id": bot_id,
                "missing_critical": item["missing_critical"],
            })

    return {
        "status_counts": status_counts,
        "stale_bots": stale_bots,
        "warm_bots": warm_bots,
        "blocked_bots": blocked_bots,
    }


def build_inventory_snapshot(
    lib: DataLibrary,
    audits: list[BotAudit],
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the latest data inventory payload for dashboards and gates."""
    ts = generated_at or datetime.now(UTC)
    datasets = lib.list()
    dataset_payload = [_dataset_payload(dataset, generated_at=ts) for dataset in datasets]
    runnable = [a.bot_id for a in audits if a.is_runnable and not a.deactivated]
    blocked = [a for a in audits if not a.is_runnable]
    deactivated = [a.bot_id for a in audits if a.deactivated]
    bot_items = [_audit_payload(a, generated_at=ts) for a in audits]
    return {
        "schema_version": 1,
        "generated_at": ts.isoformat(),
        "dataset_count": len(dataset_payload),
        "symbol_count": len(lib.symbols()),
        "timeframe_count": len(lib.timeframes()),
        "roots": [str(r) for r in lib.roots],
        "datasets": dataset_payload,
        "freshness": _freshness_summary(datasets, ts),
        "bot_coverage": {
            "total": len(audits),
            "runnable_count": len(runnable),
            "blocked_count": len(blocked),
            "deactivated_count": len(deactivated),
            "runnable": runnable,
            "blocked": {
                a.bot_id: {
                    "missing_critical": [_requirement_payload(r) for r in a.missing_critical],
                    "sources_hint": list(a.sources_hint),
                }
                for a in blocked
            },
            "deactivated": deactivated,
            "critical_freshness": _bot_critical_freshness_summary(bot_items),
            "optional_freshness": _bot_optional_freshness_summary(bot_items),
            "resolution_summary": _bot_resolution_summary(bot_items),
            "items": bot_items,
        },
    }


def write_inventory_snapshot(path: Path, payload: dict[str, Any]) -> Path:
    """Write the latest inventory snapshot as pretty JSON."""
    ensure_parent(path).write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    from eta_engine.data.library import default_library
    from eta_engine.obs.decision_journal import (
        Actor,
        DecisionJournal,
        JournalEvent,
        Outcome,
    )

    p = argparse.ArgumentParser(prog="announce_data_library")
    p.add_argument(
        "--journal",
        type=Path,
        default=_DEFAULT_JOURNAL,
        help="Decision journal JSONL (default: var/eta_engine/state/decision_journal.jsonl)",
    )
    p.add_argument(
        "--snapshot",
        type=Path,
        default=_DEFAULT_SNAPSHOT,
        help="Latest inventory JSON snapshot (default: var/eta_engine/state/data_inventory_latest.json)",
    )
    p.add_argument("--no-snapshot", action="store_true", help="do not write the latest JSON snapshot")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    lib = default_library()
    print(lib.summary_markdown())
    print()

    # Bot-coverage audit. Surfaces which bots can run vs which are
    # blocked on missing data feeds (especially crypto).
    from eta_engine.data.audit import audit_all
    from eta_engine.data.audit import summary_markdown as audit_summary
    audits = audit_all(library=lib)
    print(audit_summary(audits))
    print()

    if args.dry_run:
        print("(dry-run: no JARVIS event or snapshot written)")
        return 0

    inventory_snapshot = build_inventory_snapshot(lib, audits)
    payload = inventory_snapshot["datasets"]
    runnable = [a.bot_id for a in audits if a.is_runnable and not a.deactivated]
    deactivated = [a.bot_id for a in audits if a.deactivated]
    blocked = {
        a.bot_id: {
            "missing_critical": [
                {"kind": r.kind, "symbol": r.symbol, "timeframe": r.timeframe}
                for r in a.missing_critical
            ],
            "sources_hint": list(a.sources_hint),
        }
        for a in audits if not a.is_runnable
    }

    journal = DecisionJournal(args.journal)
    journal.append(
        JournalEvent(
            actor=Actor.JARVIS,
            intent="data_inventory",
            rationale=(
                f"library refreshed: {len(payload)} datasets, "
                f"{len(lib.symbols())} symbols, {len(lib.timeframes())} timeframes; "
                f"{len(runnable)}/{len(audits)} active bots runnable, "
                f"{len(blocked)} blocked on missing critical feeds, "
                f"{len(deactivated)} deactivated"
            ),
            gate_checks=[
                f"+datasets:{len(payload)}",
                f"+runnable_bots:{len(runnable)}",
                f"-blocked_bots:{len(blocked)}",
            ],
            outcome=Outcome.NOTED if not blocked else Outcome.BLOCKED,
            metadata={
                "datasets": payload,
                "roots": [str(r) for r in lib.roots],
                "runnable_bots": runnable,
                "deactivated_bots": deactivated,
                "blocked_bots": blocked,
                "freshness": inventory_snapshot["freshness"],
            },
        )
    )
    if not args.no_snapshot:
        write_inventory_snapshot(args.snapshot, inventory_snapshot)
        print(f"[announce_data_library] latest inventory snapshot written to {args.snapshot}")
    print(f"[announce_data_library] JARVIS event appended to {args.journal}")
    return 0 if not blocked else 1


if __name__ == "__main__":
    sys.exit(main())
