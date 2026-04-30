"""Build an executable, live-safe strategy-supercharge retest manifest.

The scorecard answers "what should we improve next?" This manifest answers
"what exact framework command should run first?" It is intentionally advisory:
commands retest and diagnose strategy quality, never mutate live routing.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402,I001

_A_C_PREFIX = "A_C_"
_B_PREFIX = "B_"
_HOLD_PREFIX = "HOLD_"


def _phase(row: dict[str, object]) -> str:
    return str(row.get("supercharge_phase") or "")


def _rank(row: dict[str, object]) -> int:
    try:
        return int(row.get("supercharge_rank") or 0)
    except (TypeError, ValueError):
        return 0


def _bot_id(row: dict[str, object]) -> str:
    return str(row.get("bot_id") or "").strip()


def _research_grid_command(bot_id: str) -> list[str]:
    return [
        "python",
        "-m",
        "eta_engine.scripts.run_research_grid",
        "--source",
        "registry",
        "--bots",
        bot_id,
        "--report-policy",
        "runtime",
    ]


def _registry_assignment(bot_id: str) -> object | None:
    try:
        from eta_engine.strategies.per_bot_registry import get_for_bot

        return get_for_bot(bot_id)
    except Exception:  # noqa: BLE001 - manifest generation must stay advisory/fail-soft.
        return None


def _row_or_registry_value(row: dict[str, object], key: str) -> object:
    value = row.get(key)
    if value not in (None, ""):
        return value
    assignment = _registry_assignment(_bot_id(row))
    return getattr(assignment, key, None) if assignment is not None else None


def _bars_per_day(timeframe: object) -> float:
    tf = str(timeframe or "").strip().lower()
    if tf in {"d", "1d", "day", "daily"}:
        return 1.0
    if tf in {"w", "1w", "week", "weekly"}:
        return 1.0 / 7.0
    match = re.fullmatch(r"(\d+)(m|h)", tf)
    if match is None:
        return 24.0
    value = int(match.group(1))
    unit = match.group(2)
    if value <= 0:
        return 24.0
    minutes = value if unit == "m" else value * 60
    return 1440.0 / minutes


def _smoke_max_bars(row: dict[str, object]) -> int:
    try:
        window_days = int(_row_or_registry_value(row, "window_days") or 90)
    except (TypeError, ValueError):
        window_days = 90
    timeframe = _row_or_registry_value(row, "timeframe")
    return max(2000, math.ceil(window_days * _bars_per_day(timeframe) * 1.5))


def _smoke_research_grid_command(row: dict[str, object]) -> list[str]:
    bot_id = _bot_id(row)
    return [
        *_research_grid_command(bot_id),
        "--max-bars-per-cell",
        str(_smoke_max_bars(row)),
    ]


def _data_repair_command(bot_id: str) -> list[str]:
    return [
        "python",
        "-m",
        "eta_engine.scripts.bot_strategy_readiness",
        "--bot-id",
        bot_id,
        "--snapshot",
        "--json",
        "--no-write",
    ]


def _live_preflight_command(bot_id: str) -> list[str]:
    return [
        "python",
        "-m",
        "eta_engine.scripts.preflight_bot_promotion",
        "--bot-id",
        bot_id,
        "--json",
    ]


def _action_for(row: dict[str, object]) -> dict[str, object]:
    bot_id = _bot_id(row)
    gate = str(row.get("next_gate") or "")
    phase = _phase(row)
    if phase.startswith(_HOLD_PREFIX):
        return {
            "action_type": "hold",
            "command": None,
            "smoke_command": None,
            "execution_phase": "HOLD",
            "operator_note": "No retune command is emitted for hold lanes.",
        }
    if phase.startswith(_B_PREFIX):
        return {
            "action_type": "live_preflight_guard",
            "command": _live_preflight_command(bot_id),
            "smoke_command": None,
            "execution_phase": "B_DEFERRED_UNTIL_A_C_STABLE",
            "operator_note": "B remains deferred until A+C retests are stable.",
        }
    if gate == "data_repair_before_retune":
        return {
            "action_type": "data_repair_recheck",
            "command": _data_repair_command(bot_id),
            "smoke_command": None,
            "execution_phase": "A_C_NOW",
            "operator_note": "Repair critical data coverage before strategy retuning.",
        }
    return {
        "action_type": "research_grid_retest",
        "command": _research_grid_command(bot_id),
        "smoke_command": _smoke_research_grid_command(row),
        "execution_phase": "A_C_NOW",
        "operator_note": "Retest the registered strategy under runtime-only report policy.",
    }


def _manifest_row(row: dict[str, object], *, order: int) -> dict[str, object]:
    action = _action_for(row)
    return {
        **row,
        **action,
        "manifest_order": order,
        "safe_to_mutate_live": False,
        "writes_live_routing": False,
        "writes_runtime_only": action["command"] is not None,
    }


def _sorted_rows(scorecard: dict[str, object]) -> list[dict[str, object]]:
    rows = scorecard.get("rows")
    if not isinstance(rows, list):
        return []
    dict_rows = [dict(row) for row in rows if isinstance(row, dict)]
    return sorted(dict_rows, key=lambda row: (_rank(row), _bot_id(row)))


def build_manifest(
    *,
    scorecard: dict[str, object] | None = None,
    include_b_later: bool = False,
    max_targets: int | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    """Return the framework-readable A+C retest manifest."""
    if scorecard is None:
        from eta_engine.scripts.strategy_supercharge_scorecard import build_scorecard

        scorecard = build_scorecard()

    rows = [_manifest_row(row, order=i) for i, row in enumerate(_sorted_rows(scorecard))]
    a_c_now = [row for row in rows if _phase(row).startswith(_A_C_PREFIX)]
    b_later = [row for row in rows if _phase(row).startswith(_B_PREFIX)]
    hold = [row for row in rows if _phase(row).startswith(_HOLD_PREFIX)]
    batch = list(a_c_now)
    if include_b_later:
        batch.extend(b_later)
    if max_targets is not None and max_targets >= 0:
        batch = batch[:max_targets]
    commands = [
        row["command"]
        for row in batch
        if isinstance(row.get("command"), list)
    ]
    rows_by_bot = {
        _bot_id(row): row
        for row in rows
        if _bot_id(row)
    }
    next_bot = str(batch[0].get("bot_id") or "") if batch else ""
    return {
        "schema_version": 1,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "source": "strategy_supercharge_manifest",
        "status": "ready",
        "strategy": str(scorecard.get("strategy") or "A_C_THEN_B"),
        "scorecard_summary": scorecard.get("summary") if isinstance(scorecard.get("summary"), dict) else {},
        "summary": {
            "total_bots": len(rows),
            "a_c_now": len(a_c_now),
            "b_deferred": len(b_later),
            "hold": len(hold),
            "next_bot": next_bot,
            "commands": len(commands),
            "include_b_later": include_b_later,
        },
        "rows": rows,
        "rows_by_bot": rows_by_bot,
        "next_batch": batch,
        "b_later": b_later,
        "hold": hold,
        "commands": commands,
    }


def write_manifest(
    manifest: dict[str, object],
    path: Path = workspace_roots.ETA_STRATEGY_SUPERCHARGE_MANIFEST_PATH,
) -> Path:
    """Atomically write the manifest snapshot and return the target path."""
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="strategy_supercharge_manifest")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--no-write", action="store_true", help="build without writing the canonical snapshot")
    parser.add_argument("--include-b-later", action="store_true", help="append B live-preflight rows after A+C")
    parser.add_argument("--max-targets", type=int, default=None, help="limit emitted next_batch rows")
    parser.add_argument("--out", type=Path, default=workspace_roots.ETA_STRATEGY_SUPERCHARGE_MANIFEST_PATH)
    args = parser.parse_args(argv)

    manifest = build_manifest(
        include_b_later=args.include_b_later,
        max_targets=args.max_targets,
    )
    written = None if args.no_write else write_manifest(manifest, args.out)
    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True, default=str))
    else:
        target = f" -> {written}" if written is not None else " (no-write)"
        summary = manifest["summary"] if isinstance(manifest.get("summary"), dict) else {}
        print(
            "strategy_supercharge_manifest "
            f"a_c_now={summary.get('a_c_now', 0)} "
            f"b_deferred={summary.get('b_deferred', 0)} "
            f"next={summary.get('next_bot', '')}{target}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
