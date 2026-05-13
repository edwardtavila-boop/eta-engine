"""Build a conservative strategy-supercharge target scorecard.

The scorecard is intentionally advisory. It ranks where to spend research and
retune effort next; it does not promote bots, change live routing, or flip
broker permissions.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402,I001


_PHASES: dict[str, dict[str, object]] = {
    "paper_soak": {
        "phase": "A_C_PAPER_SOAK",
        "rank": 0,
        "gate": "paper_soak_retest",
        "reason": "Paper-soak bot can be retuned and re-soaked before live-preflight risk.",
    },
    "research": {
        "phase": "A_C_RESEARCH_RETEST",
        "rank": 1,
        "gate": "research_grid_retest",
        "reason": "Research bot should be improved under strict research gates before paper soak.",
    },
    "shadow_only": {
        "phase": "A_C_SHADOW_REPAIR",
        "rank": 2,
        "gate": "shadow_repair_retest",
        "reason": "Shadow bot is safe to repair because it is not allowed to paper-trade.",
    },
    "blocked_data": {
        "phase": "A_C_DATA_REPAIR",
        "rank": 3,
        "gate": "data_repair_before_retune",
        "reason": "Data-blocked bot needs coverage repaired before strategy retuning is meaningful.",
    },
    "live_preflight": {
        "phase": "B_LIVE_PREFLIGHT_LATER",
        "rank": 4,
        "gate": "live_preflight_regression_guard",
        "reason": "Live-preflight bot should only be retuned after A+C scorecard gates are stable.",
    },
    "non_edge": {
        "phase": "HOLD_NON_EDGE",
        "rank": 8,
        "gate": "no_promotion_gate",
        "reason": "Non-edge bot is intentionally outside promotion-gated trading edges.",
    },
    "deactivated": {
        "phase": "HOLD_DEACTIVATED",
        "rank": 9,
        "gate": "operator_reactivation_required",
        "reason": "Deactivated bot must be explicitly reactivated before research retuning.",
    },
}


def _row_to_dict(row: object) -> dict[str, object]:
    if isinstance(row, dict):
        return dict(row)
    if is_dataclass(row) and not isinstance(row, type):
        return asdict(row)
    out: dict[str, object] = {}
    for key in (
        "bot_id",
        "strategy_id",
        "strategy_kind",
        "symbol",
        "timeframe",
        "active",
        "promotion_status",
        "baseline_status",
        "data_status",
        "launch_lane",
        "can_paper_trade",
        "can_live_trade",
        "missing_critical",
        "missing_optional",
        "next_action",
    ):
        if hasattr(row, key):
            out[key] = getattr(row, key)
    return out


def _phase_for(row: dict[str, object]) -> dict[str, object]:
    lane = str(row.get("launch_lane") or "")
    return _PHASES.get(
        lane,
        {
            "phase": "A_C_REVIEW",
            "rank": 5,
            "gate": "manual_lane_review",
            "reason": f"Unrecognized launch lane {lane!r}; review before retuning.",
        },
    )


def _score_row(row: object, *, index: int) -> dict[str, object]:
    data = _row_to_dict(row)
    phase = _phase_for(data)
    bot_id = str(data.get("bot_id") or data.get("id") or data.get("name") or "").strip()
    rank = int(phase["rank"])
    if data.get("baseline_status") != "baseline_present":
        rank += 1
    if data.get("data_status") not in {"ready", "deactivated"}:
        rank += 1
    scored = {
        **data,
        "bot_id": bot_id,
        "supercharge_phase": str(phase["phase"]),
        "supercharge_rank": rank,
        "supercharge_order": index,
        "next_gate": str(phase["gate"]),
        "target_reason": str(phase["reason"]),
        "requested_sequence": "A_C_THEN_B",
        "safe_to_mutate_live": False,
    }
    return scored


def build_scorecard(
    *,
    rows: list[object] | tuple[object, ...] | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    """Return a framework-readable ranking of strategy retune targets."""
    if rows is None:
        from eta_engine.scripts.bot_strategy_readiness import build_readiness_matrix

        rows = build_readiness_matrix()

    scored_rows = [_score_row(row, index=i) for i, row in enumerate(rows)]
    scored_rows.sort(
        key=lambda row: (
            int(row.get("supercharge_rank") or 0),
            str(row.get("bot_id") or ""),
        ),
    )
    rows_by_bot = {str(row["bot_id"]): row for row in scored_rows if row.get("bot_id")}
    phase_counts: dict[str, int] = {}
    for row in scored_rows:
        phase = str(row.get("supercharge_phase") or "UNKNOWN")
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
    a_c_rows = [row for row in scored_rows if str(row.get("supercharge_phase") or "").startswith("A_C_")]
    b_rows = [row for row in scored_rows if str(row.get("supercharge_phase") or "").startswith("B_")]
    hold_rows = [row for row in scored_rows if str(row.get("supercharge_phase") or "").startswith("HOLD_")]
    next_best = a_c_rows[0]["bot_id"] if a_c_rows else (scored_rows[0]["bot_id"] if scored_rows else "")
    return {
        "schema_version": 1,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "source": "strategy_supercharge_scorecard",
        "status": "ready",
        "strategy": "A_C_THEN_B",
        "summary": {
            "total_bots": len(scored_rows),
            "phase_counts": dict(sorted(phase_counts.items())),
            "a_c_targets": len(a_c_rows),
            "b_later_targets": len(b_rows),
            "hold_targets": len(hold_rows),
            "next_best_bot": str(next_best),
        },
        "rows": scored_rows,
        "rows_by_bot": rows_by_bot,
        "next_targets": a_c_rows[:5],
        "b_later": b_rows,
        "hold": hold_rows,
    }


def write_scorecard(
    scorecard: dict[str, object],
    path: Path = workspace_roots.ETA_STRATEGY_SUPERCHARGE_SCORECARD_PATH,
) -> Path:
    """Atomically write the scorecard snapshot and return the target path."""
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(scorecard, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="strategy_supercharge_scorecard")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--no-write", action="store_true", help="build without writing the canonical snapshot")
    parser.add_argument("--out", type=Path, default=workspace_roots.ETA_STRATEGY_SUPERCHARGE_SCORECARD_PATH)
    args = parser.parse_args(argv)

    scorecard = build_scorecard()
    written = None if args.no_write else write_scorecard(scorecard, args.out)
    if args.json:
        print(json.dumps(scorecard, indent=2, sort_keys=True, default=str))
    else:
        target = f" -> {written}" if written is not None else " (no-write)"
        summary = scorecard["summary"] if isinstance(scorecard.get("summary"), dict) else {}
        print(
            "strategy_supercharge_scorecard "
            f"rows={summary.get('total_bots', 0)} "
            f"next={summary.get('next_best_bot', '')}{target}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
