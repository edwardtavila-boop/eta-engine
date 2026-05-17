"""Shared loader for public retune advisory cache surfaces."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

RETUNE_TRUTH_CHECK = "diamond_retune_truth_check_latest.json"
PUBLIC_RETUNE_TRUTH = "public_diamond_retune_truth_latest.json"
PUBLIC_BROKER_CLOSE_TRUTH = "public_broker_close_truth_latest.json"
STRATEGY_EXPERIMENT_MARKERS = "strategy_experiment_markers.json"


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dict_field(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _preferred_message(messages: list[str], *needles: str) -> str:
    for needle in needles:
        for message in messages:
            if needle in message:
                return message
    return messages[0] if messages else ""


def _parse_ts(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _active_experiment(health_dir: Path, focus_bot: str | None) -> dict[str, Any] | None:
    if not focus_bot:
        return None
    markers = _read_json_dict(health_dir / STRATEGY_EXPERIMENT_MARKERS)
    bot_markers = _dict_field(markers, "bots")
    marker = _dict_field(bot_markers, str(focus_bot))
    if not marker:
        return None

    started_at_raw = marker.get("started_at")
    started_at = _parse_ts(started_at_raw)
    experiment: dict[str, Any] = {
        "experiment_id": marker.get("experiment_id"),
        "started_at": started_at_raw,
        "partial_profit_enabled": marker.get("partial_profit_enabled"),
        "note": marker.get("note"),
        "source_path": str(health_dir / STRATEGY_EXPERIMENT_MARKERS),
    }
    if started_at is None:
        return experiment

    from eta_engine.scripts.closed_trade_ledger import (  # noqa: PLC0415
        DEFAULT_PRODUCTION_DATA_SOURCES,
        load_close_records,
    )

    rows = load_close_records(
        bot_filter=str(focus_bot),
        data_sources=DEFAULT_PRODUCTION_DATA_SOURCES,
    )
    post_rows: list[dict[str, Any]] = []
    latest_focus_close_ts = ""
    latest_pre_change_close_ts = ""
    for row in rows:
        raw_row_ts = str(row.get("close_ts") or row.get("ts") or "")
        row_ts = _parse_ts(raw_row_ts)
        if row_ts is not None:
            latest_focus_close_ts = raw_row_ts
            if row_ts < started_at:
                latest_pre_change_close_ts = raw_row_ts
        if row_ts is not None and row_ts >= started_at:
            post_rows.append(row)

    gross_profit = sum(max(_as_float(row.get("realized_pnl")), 0.0) for row in post_rows)
    gross_loss = abs(sum(min(_as_float(row.get("realized_pnl")), 0.0) for row in post_rows))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    wins = sum(1 for row in post_rows if _as_float(row.get("realized_r")) > 0)

    experiment.update(
        {
            "post_change_closed_trade_count": len(post_rows),
            "post_change_total_realized_pnl": round(
                sum(_as_float(row.get("realized_pnl")) for row in post_rows),
                2,
            ),
            "post_change_cumulative_r": round(
                sum(_as_float(row.get("realized_r")) for row in post_rows),
                4,
            ),
            "post_change_profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
            "post_change_win_rate_pct": round((wins / len(post_rows)) * 100, 2) if post_rows else None,
            "age_hours": round((datetime.now(UTC) - started_at).total_seconds() / 3600, 2),
            "latest_focus_close_ts": latest_focus_close_ts or None,
            "latest_pre_change_close_ts": latest_pre_change_close_ts or None,
            "awaiting_first_post_change_close": len(post_rows) == 0,
        }
    )
    return experiment


def summarize_active_experiment(experiment: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(experiment, dict) or not experiment:
        return None

    experiment_id = str(experiment.get("experiment_id") or "unnamed_experiment")
    started_at = str(experiment.get("started_at") or "n/a")
    partial_profit_enabled = experiment.get("partial_profit_enabled")
    partial_profit_text = str(partial_profit_enabled) if partial_profit_enabled is not None else "n/a"

    closes = experiment.get("post_change_closed_trade_count")
    pnl = experiment.get("post_change_total_realized_pnl")
    pf = experiment.get("post_change_profit_factor")

    closes_text = str(closes) if closes is not None else "n/a"
    pnl_text = f"${float(pnl):+,.2f}" if isinstance(pnl, int | float) else "n/a"
    pf_text = f"{float(pf):.2f}" if isinstance(pf, int | float) else "n/a"

    awaiting_first_post_change_close = experiment.get("awaiting_first_post_change_close") is True
    if awaiting_first_post_change_close:
        outcome_line = f"{experiment_id}: awaiting first post-change close"
    else:
        close_count = 0
        if not isinstance(closes, bool):
            try:
                close_count = int(closes or 0)
            except (TypeError, ValueError):
                close_count = 0
        if close_count <= 0:
            outcome_line = experiment_id
        else:
            parts = [f"{experiment_id}: {close_count} post-change close{'s' if close_count != 1 else ''}"]
            for raw_value, label, formatter in (
                (experiment.get("post_change_cumulative_r"), "R", lambda value: f"{value:+.2f}"),
                (
                    experiment.get("post_change_total_realized_pnl"),
                    "PnL",
                    lambda value: f"{'-' if value < 0 else ''}${abs(float(value)):,.2f}",
                ),
                (experiment.get("post_change_profit_factor"), "PF", lambda value: f"{value:.2f}"),
            ):
                if isinstance(raw_value, bool) or raw_value is None:
                    continue
                try:
                    numeric_value = float(raw_value)
                except (TypeError, ValueError):
                    continue
                parts.append(f"{label} {formatter(numeric_value)}")
            outcome_line = " | ".join(parts)

    return {
        "headline": f"{experiment_id} since {started_at}",
        "outcome_line": outcome_line,
        "partial_profit_enabled_text": partial_profit_text,
        "post_change_closed_trade_count_text": closes_text,
        "post_change_total_realized_pnl_text": pnl_text,
        "post_change_profit_factor_text": pf_text,
    }


def _warning_needles(diagnosis: str) -> tuple[str, ...]:
    if diagnosis == "public_focus_provenance_gap":
        return (
            "Public broker-backed close sample materially exceeds",
            "trade_closes source is thin",
            "blind reclassification is unsafe",
            "Public retune focus and local canonical retune receipt disagree.",
        )
    if diagnosis == "public_local_focus_mismatch":
        return (
            "Public retune focus and local canonical retune receipt disagree.",
            "Public broker-backed close sample materially exceeds",
            "trade_closes source is thin",
            "blind reclassification is unsafe",
        )
    return (
        "Public broker-backed close sample materially exceeds",
        "Public retune focus and local canonical retune receipt disagree.",
        "trade_closes source is thin",
        "blind reclassification is unsafe",
    )


def _action_needles(diagnosis: str) -> tuple[str, ...]:
    if diagnosis in {"public_focus_provenance_gap", "public_local_focus_mismatch"}:
        return (
            "canonical trade_closes writer",
            "local closed-trade ledger and diamond_retune_status writers",
            "Do not blindly reclassify",
        )
    return (
        "canonical trade_closes writer",
        "Do not blindly reclassify",
        "local closed-trade ledger and diamond_retune_status writers",
    )


def build_retune_advisory(health_dir: Path) -> dict[str, Any]:
    truth_check = _read_json_dict(health_dir / RETUNE_TRUTH_CHECK)
    public_retune = _read_json_dict(health_dir / PUBLIC_RETUNE_TRUTH)
    public_close = _read_json_dict(health_dir / PUBLIC_BROKER_CLOSE_TRUTH)

    retune_surface = _dict_field(public_retune, "surface")
    retune_normalized = _dict_field(retune_surface, "normalized")
    retune_summary = _dict_field(retune_surface, "summary")
    close_surface = _dict_field(public_close, "surface")
    close_normalized = _dict_field(close_surface, "normalized")

    warnings = _string_list(truth_check, "warnings")
    action_items = _string_list(truth_check, "action_items")
    diagnosis = str(truth_check.get("diagnosis") or "")

    focus_bot = (
        retune_normalized.get("focus_bot")
        or close_normalized.get("focus_bot")
        or public_retune.get("focus_bot")
        or public_close.get("focus_bot")
    )
    focus_issue = (
        retune_normalized.get("focus_issue")
        or close_normalized.get("focus_issue")
        or public_retune.get("focus_issue")
        or public_close.get("focus_issue")
    )
    focus_state = (
        retune_normalized.get("focus_state")
        or close_normalized.get("focus_state")
        or public_retune.get("focus_state")
        or public_close.get("focus_state")
    )
    focus_closed_trade_count = (
        close_normalized.get("focus_closed_trade_count")
        or retune_normalized.get("focus_closed_trade_count")
        or retune_summary.get("broker_truth_focus_closed_trade_count")
        or public_close.get("focus_closed_trade_count")
    )
    focus_total_realized_pnl = (
        close_normalized.get("focus_total_realized_pnl")
        if close_normalized.get("focus_total_realized_pnl") is not None
        else (
            retune_normalized.get("focus_total_realized_pnl")
            if retune_normalized.get("focus_total_realized_pnl") is not None
            else public_close.get("focus_total_realized_pnl")
        )
    )
    focus_profit_factor = (
        close_normalized.get("focus_profit_factor")
        if close_normalized.get("focus_profit_factor") is not None
        else (
            retune_normalized.get("focus_profit_factor")
            if retune_normalized.get("focus_profit_factor") is not None
            else public_close.get("focus_profit_factor")
        )
    )
    available = bool(
        focus_bot is not None
        or focus_closed_trade_count is not None
        or focus_total_realized_pnl is not None
        or truth_check
        or public_retune
        or public_close
    )
    active_experiment = _active_experiment(health_dir, str(focus_bot) if focus_bot else None)

    preferred_warning = _preferred_message(warnings, *_warning_needles(diagnosis))
    preferred_action = _preferred_message(action_items, *_action_needles(diagnosis))
    if (
        isinstance(active_experiment, dict)
        and active_experiment
        and int(active_experiment.get("post_change_closed_trade_count") or 0) == 0
    ):
        experiment_id = str(active_experiment.get("experiment_id") or "post_fix_experiment")
        started_at = str(active_experiment.get("started_at") or "unknown_start")
        latest_pre_change_close_ts = str(
            active_experiment.get("latest_pre_change_close_ts")
            or active_experiment.get("latest_focus_close_ts")
            or ""
        )
        if not preferred_warning:
            preferred_warning = (
                f"No broker-proof closed trades yet for {focus_bot} since the active {experiment_id} "
                f"experiment started at {started_at}."
            )
        if not preferred_action:
            if latest_pre_change_close_ts:
                preferred_action = (
                    f"Await the first post-fix close for {focus_bot}; latest broker-proof close for this bot was "
                    f"{latest_pre_change_close_ts}, before experiment start {started_at}."
                )
            else:
                preferred_action = (
                    f"Await the first post-fix close for {focus_bot}; the active {experiment_id} experiment started "
                    f"at {started_at} and has not produced a closed trade yet."
                )

    return {
        "available": available,
        "focus_bot": focus_bot,
        "focus_issue": focus_issue,
        "focus_state": focus_state,
        "focus_closed_trade_count": focus_closed_trade_count,
        "focus_total_realized_pnl": focus_total_realized_pnl,
        "focus_profit_factor": focus_profit_factor,
        "broker_mtd_pnl": close_normalized.get("broker_mtd_pnl") or public_close.get("broker_mtd_pnl"),
        "today_realized_pnl": close_normalized.get("today_realized_pnl") or public_close.get("today_realized_pnl"),
        "total_unrealized_pnl": close_normalized.get("total_unrealized_pnl")
        or public_close.get("total_unrealized_pnl"),
        "open_position_count": close_normalized.get("open_position_count") or public_close.get("open_position_count"),
        "reporting_timezone": close_normalized.get("reporting_timezone") or public_close.get("reporting_timezone"),
        "broker_snapshot_source": close_normalized.get("broker_snapshot_source")
        or public_close.get("broker_snapshot_source"),
        "broker_snapshot_state": close_normalized.get("broker_snapshot_state")
        or public_close.get("broker_snapshot_state"),
        "active_experiment": active_experiment,
        "diagnosis": diagnosis or None,
        "status": truth_check.get("status"),
        "mismatch_count": truth_check.get("mismatch_count"),
        "preferred_warning": preferred_warning or None,
        "preferred_action": preferred_action or None,
        "warnings": warnings,
        "action_items": action_items,
        "sources": {
            "retune_truth_check": str(health_dir / RETUNE_TRUTH_CHECK),
            "public_retune_truth": str(health_dir / PUBLIC_RETUNE_TRUTH),
            "public_broker_close_truth": str(health_dir / PUBLIC_BROKER_CLOSE_TRUTH),
        },
    }
