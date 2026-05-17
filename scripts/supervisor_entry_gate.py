from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from logging import Logger
    from pathlib import Path

from eta_engine.scripts import workspace_roots


def compact_strategy_readiness(row: dict[str, Any]) -> dict[str, Any]:
    """Return the bot-level readiness fields safe for supervisor heartbeat."""
    return {
        "status": "ready",
        "bot_id": row.get("bot_id"),
        "strategy_id": row.get("strategy_id"),
        "launch_lane": row.get("launch_lane"),
        "data_status": row.get("data_status"),
        "promotion_status": row.get("promotion_status"),
        "can_paper_trade": bool(row.get("can_paper_trade")),
        "can_live_trade": bool(row.get("can_live_trade")),
        "next_action": row.get("next_action"),
    }


def load_bot_strategy_readiness_snapshot(
    path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Load canonical strategy readiness for heartbeat enrichment."""
    target = path or workspace_roots.ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "status": "missing",
            "path": str(target),
            "summary": {},
            "generated_at": None,
        }, {}
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "unreadable",
            "path": str(target),
            "summary": {},
            "generated_at": None,
            "error": str(exc),
        }, {}

    if not isinstance(payload, dict):
        return {
            "status": "unreadable",
            "path": str(target),
            "summary": {},
            "generated_at": None,
            "error": "bot strategy readiness snapshot must be a JSON object",
        }, {}

    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    readiness_by_bot = {
        str(row["bot_id"]): compact_strategy_readiness(row)
        for row in rows
        if isinstance(row, dict) and row.get("bot_id")
    }
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {
        "status": "ready",
        "path": str(target),
        "schema_version": payload.get("schema_version"),
        "generated_at": payload.get("generated_at"),
        "summary": summary,
    }, readiness_by_bot


def load_strategy_readiness_rows_for_entry_gate(
    path: Path,
    *,
    previous_mtime_ns: int | None,
    previous_rows: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], int | None]:
    """Return cached bot readiness rows for paper_live/live entry gating."""
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return {}, previous_mtime_ns
    if previous_mtime_ns != mtime_ns:
        _payload, rows = load_bot_strategy_readiness_snapshot(path)
        return rows, mtime_ns
    return previous_rows, previous_mtime_ns


def strategy_readiness_block_reason(
    *,
    mode: str,
    readiness: dict[str, Any] | None,
) -> tuple[str | None, str | None, str | None]:
    """Return the reject reason tuple for a blocked strategy readiness row."""
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"paper_live", "live"}:
        return None, None, None
    if not readiness:
        return None, None, None

    field = "can_live_trade" if normalized_mode == "live" else "can_paper_trade"
    if bool(readiness.get(field)):
        return None, None, None

    lane = str(readiness.get("launch_lane") or readiness.get("promotion_status") or "not_approved")
    return f"strategy_readiness_block:{lane}", lane, field


def resolve_diamond_retune_status_path(
    configured_state_dir: Path,
    runtime_state_dir: Path,
) -> Path:
    """Resolve retune status from the supervisor's configured state root."""
    try:
        configured_state_dir.resolve().relative_to(runtime_state_dir.resolve())
    except (OSError, ValueError):
        return configured_state_dir / "diamond_retune_status_latest.json"
    return runtime_state_dir / "diamond_retune_status_latest.json"


def load_negative_broker_edge_rows_for_entry_gate(
    path: Path,
    *,
    previous_mtime_ns: int | None,
    previous_rows: dict[str, dict[str, Any]],
    logger: Logger,
) -> tuple[dict[str, dict[str, Any]], int | None]:
    """Return bots whose broker-backed sample is large enough and negative."""
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return {}, previous_mtime_ns
    if previous_mtime_ns == mtime_ns:
        return previous_rows, previous_mtime_ns

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("diamond retune status read failed for entry gate: %s", exc)
        return previous_rows, previous_mtime_ns

    rows = payload.get("bots") if isinstance(payload, dict) and isinstance(payload.get("bots"), list) else []
    negative: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        bot_id = str(row.get("bot_id") or "")
        evidence = row.get("broker_close_evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        if bot_id and str(evidence.get("edge_status") or "") == "sample_met_negative_edge":
            negative[bot_id] = row
    return negative, mtime_ns


def broker_retune_block_reason(
    *,
    mode: str,
    row: dict[str, Any] | None,
) -> str | None:
    """Return the reject reason for a broker-negative retune hold."""
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"paper_live", "live"}:
        return None
    if not row:
        return None
    return "broker_negative_edge_retune_hold"
