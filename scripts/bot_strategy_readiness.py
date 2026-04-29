"""Build a machine-readable bot strategy/data readiness matrix.

This is an operator-facing companion to ``paper_live_launch_check``: it keeps
strategy assignment, promotion status, baseline presence, and critical data
coverage in one compact row per bot.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402

if TYPE_CHECKING:
    from eta_engine.data.library import DataLibrary
    from eta_engine.strategies.per_bot_registry import StrategyAssignment


@dataclass(frozen=True)
class ReadinessRow:
    """One bot's current strategy and launch-readiness posture."""

    bot_id: str
    strategy_id: str
    strategy_kind: str
    symbol: str
    timeframe: str
    active: bool
    promotion_status: str
    baseline_status: str
    data_status: str
    launch_lane: str
    can_paper_trade: bool
    can_live_trade: bool
    missing_critical: tuple[str, ...]
    missing_optional: tuple[str, ...]
    next_action: str


def _requirement_label(req: Any) -> str:  # noqa: ANN401 - small duck-typed helper for DataRequirement-like rows.
    return f"{req.kind}:{req.symbol}/{req.timeframe or '-'}"


def _baseline_entry(bot_id: str, strategy_id: str) -> dict[str, object] | None:
    from eta_engine.scripts.paper_live_launch_check import _load_baseline_entry

    return _load_baseline_entry(bot_id, strategy_id)


def _baseline_status(bot_id: str, strategy_id: str) -> str:
    return "baseline_present" if _baseline_entry(bot_id, strategy_id) is not None else "baseline_missing"


def _promotion_status(assignment: StrategyAssignment, *, active: bool) -> str:
    if not active:
        return "deactivated"
    explicit = assignment.extras.get("promotion_status")
    if isinstance(explicit, str) and explicit:
        return explicit
    baseline = _baseline_entry(assignment.bot_id, assignment.strategy_id)
    baseline_status = baseline.get("_promotion_status") if baseline else None
    if isinstance(baseline_status, str) and baseline_status:
        return baseline_status
    return "production" if baseline is not None else "unbaselined"


def _row_for_assignment(assignment: StrategyAssignment, *, library: DataLibrary) -> ReadinessRow:
    from eta_engine.data.audit import audit_bot
    from eta_engine.strategies.per_bot_registry import is_active

    active = is_active(assignment)
    promotion_status = _promotion_status(assignment, active=active)
    baseline_status = _baseline_status(assignment.bot_id, assignment.strategy_id)

    if not active:
        return ReadinessRow(
            bot_id=assignment.bot_id,
            strategy_id=assignment.strategy_id,
            strategy_kind=assignment.strategy_kind,
            symbol=assignment.symbol,
            timeframe=assignment.timeframe,
            active=False,
            promotion_status=promotion_status,
            baseline_status=baseline_status,
            data_status="deactivated",
            launch_lane="deactivated",
            can_paper_trade=False,
            can_live_trade=False,
            missing_critical=(),
            missing_optional=(),
            next_action="No action: bot is explicitly deactivated.",
        )

    audit = audit_bot(assignment.bot_id, library=library)
    missing_critical = tuple(_requirement_label(req) for req in (audit.missing_critical if audit else ()))
    missing_optional = tuple(_requirement_label(req) for req in (audit.missing_optional if audit else ()))

    if missing_critical:
        data_status = "blocked"
        launch_lane = "blocked_data"
        can_paper_trade = False
        next_action = "Fetch missing critical data: " + ", ".join(missing_critical)
    elif promotion_status in {"shadow_benchmark", "deprecated"}:
        data_status = "ready"
        launch_lane = "shadow_only"
        can_paper_trade = False
        next_action = "Keep as diagnostics only; do not paper-trade this lane."
    elif promotion_status == "non_edge_strategy":
        data_status = "ready"
        launch_lane = "non_edge"
        can_paper_trade = False
        next_action = "Keep separate from promotion-gated trading edges."
    elif promotion_status == "research_candidate":
        data_status = "ready"
        launch_lane = "research"
        can_paper_trade = False
        next_action = "Continue research retest; do not promote until strict gate and soak pass."
    elif promotion_status == "production":
        data_status = "ready"
        launch_lane = "live_preflight"
        can_paper_trade = True
        next_action = "Run per-bot promotion preflight before live routing."
    else:
        data_status = "ready"
        launch_lane = "paper_soak"
        can_paper_trade = True
        next_action = "Run paper-soak and broker drift checks before live routing."

    return ReadinessRow(
        bot_id=assignment.bot_id,
        strategy_id=assignment.strategy_id,
        strategy_kind=assignment.strategy_kind,
        symbol=assignment.symbol,
        timeframe=assignment.timeframe,
        active=True,
        promotion_status=promotion_status,
        baseline_status=baseline_status,
        data_status=data_status,
        launch_lane=launch_lane,
        can_paper_trade=can_paper_trade,
        can_live_trade=False,
        missing_critical=missing_critical,
        missing_optional=missing_optional,
        next_action=next_action,
    )


def build_readiness_matrix(
    *,
    library: DataLibrary | None = None,
    bot_ids: list[str] | tuple[str, ...] | None = None,
) -> list[ReadinessRow]:
    """Return strategy/data readiness rows for selected bots or all bots."""
    from eta_engine.data.library import default_library
    from eta_engine.strategies.per_bot_registry import all_assignments, get_for_bot

    lib = library or default_library()
    if bot_ids is None:
        assignments = all_assignments()
    else:
        assignments = []
        for bot_id in bot_ids:
            assignment = get_for_bot(bot_id)
            if assignment is not None:
                assignments.append(assignment)
    return [_row_for_assignment(assignment, library=lib) for assignment in assignments]


def build_snapshot(
    rows: list[ReadinessRow],
    *,
    generated_at: str | None = None,
) -> dict[str, object]:
    """Return a canonical snapshot payload for dashboards and automation."""
    lane_counts: dict[str, int] = {}
    for row in rows:
        lane_counts[row.launch_lane] = lane_counts.get(row.launch_lane, 0) + 1
    return {
        "schema_version": 1,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "source": "bot_strategy_readiness",
        "summary": {
            "total_bots": len(rows),
            "blocked_data": lane_counts.get("blocked_data", 0),
            "can_live_any": any(row.can_live_trade for row in rows),
            "can_paper_trade": sum(row.can_paper_trade for row in rows),
            "launch_lanes": dict(sorted(lane_counts.items())),
        },
        "rows": [asdict(row) for row in rows],
    }


def write_snapshot(
    snapshot: dict[str, object],
    path: Path = workspace_roots.ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH,
) -> Path:
    """Atomically write the readiness snapshot and return the target path."""
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot_strategy_readiness")
    parser.add_argument("--bot-id", action="append", default=[], help="bot id to include; repeatable")
    parser.add_argument("--root", action="append", default=[], help="data library root; repeatable")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON rows")
    parser.add_argument("--snapshot", action="store_true", help="emit/write canonical snapshot payload")
    parser.add_argument("--no-write", action="store_true", help="build snapshot without writing the artifact")
    parser.add_argument("--out", type=Path, default=workspace_roots.ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH)
    args = parser.parse_args(argv)

    library = None
    if args.root:
        from eta_engine.data.library import DataLibrary

        library = DataLibrary(roots=[Path(root) for root in args.root])

    rows = build_readiness_matrix(library=library, bot_ids=args.bot_id or None)
    if args.snapshot:
        snapshot = build_snapshot(rows)
        written = None if args.no_write else write_snapshot(snapshot, args.out)
        if args.json:
            print(json.dumps(snapshot, indent=2, sort_keys=True, default=str))
        else:
            target = f" -> {written}" if written is not None else " (no-write)"
            print(
                "bot_strategy_readiness snapshot "
                f"rows={snapshot['summary']['total_bots']} "
                f"lanes={snapshot['summary']['launch_lanes']}{target}"
            )
    elif args.json:
        print(json.dumps([asdict(row) for row in rows], indent=2, sort_keys=True))
    else:
        for row in rows:
            print(
                f"{row.launch_lane:<18} {row.bot_id:<24} {row.strategy_id:<28} "
                f"data={row.data_status} baseline={row.baseline_status}"
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
