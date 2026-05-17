"""Compare public diamond retune truth to the local canonical receipts.

The live ops surface is the primary truth path for retune focus. Local
receipts under ``var/eta_engine/state`` are still useful, but when they drift
from the public surface we should report that as a local sync/watch-surface
issue instead of silently treating the local receipt as authoritative.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import (  # noqa: E402
    ETA_JARVIS_TRADE_CLOSES_PATH,
    ETA_LEGACY_JARVIS_TRADE_CLOSES_PATH,
    ETA_RUNTIME_STATE_DIR,
    ensure_parent,
)

DEFAULT_PUBLIC_URL = "https://ops.evolutionarytradingalgo.com/api/jarvis/diamond_retune_status"
DEFAULT_PUBLIC_BROKER_STATE_URL = "https://ops.evolutionarytradingalgo.com/api/live/broker_state"
DEFAULT_TIMEOUT_S = 12.0
DEFAULT_OUTPUT_PATH = ETA_RUNTIME_STATE_DIR / "health" / "diamond_retune_truth_check_latest.json"
DEFAULT_PUBLIC_CACHE_PATH = ETA_RUNTIME_STATE_DIR / "health" / "public_diamond_retune_truth_latest.json"
DEFAULT_PUBLIC_BROKER_CLOSE_CACHE_PATH = ETA_RUNTIME_STATE_DIR / "health" / "public_broker_close_truth_latest.json"
CANONICAL_TRADE_CLOSES_PATH = ETA_JARVIS_TRADE_CLOSES_PATH
LEGACY_TRADE_CLOSES_PATH = ETA_LEGACY_JARVIS_TRADE_CLOSES_PATH
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ETA-retune-truth-check/1.0",
    "Cache-Control": "no-store",
}
TIMESTAMP_KEYS = ("generated_at_utc", "generated_at", "ts", "as_of", "snapshot_ts", "timestamp", "server_ts")
COMPARE_FIELDS = (
    "focus_bot",
    "focus_issue",
    "focus_state",
    "focus_strategy_kind",
    "focus_best_session",
    "focus_worst_session",
    "focus_command",
    "focus_closed_trade_count",
    "focus_total_realized_pnl",
    "focus_profit_factor",
    "safe_to_mutate_live",
)


@dataclass
class TruthSurface:
    label: str
    source: str
    available: bool
    readable: bool = False
    status_code: int | None = None
    error: str | None = None
    observed_ts: str | None = None
    age_seconds: float | None = None
    normalized: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "source": self.source,
            "available": self.available,
            "readable": self.readable,
            "status_code": self.status_code,
            "error": self.error,
            "observed_ts": self.observed_ts,
            "age_seconds": round(self.age_seconds, 3) if self.age_seconds is not None else None,
            "normalized": self.normalized,
            "summary": self.summary,
        }


def _surface_payload_dict(surface: TruthSurface | dict[str, Any]) -> dict[str, Any]:
    return surface.to_dict() if isinstance(surface, TruthSurface) else dict(surface)


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_seconds(observed: datetime, now: datetime) -> float:
    return max(0.0, (now - observed).total_seconds())


def _extract_observed_timestamp(payload: dict[str, Any], fallback_path: Path | None = None) -> datetime | None:
    for key in TIMESTAMP_KEYS:
        observed = parse_timestamp(payload.get(key))
        if observed is not None:
            return observed
    if fallback_path is None:
        return None
    try:
        return datetime.fromtimestamp(fallback_path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _summary_dict(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("summary") if isinstance(payload.get("summary"), dict) else {}


def _first_value(*values: object) -> object | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def normalize_retune_truth(payload: dict[str, Any]) -> dict[str, Any]:
    summary = _summary_dict(payload)
    return {
        "focus_bot": _first_value(payload.get("focus_bot"), summary.get("broker_truth_focus_bot_id")),
        "focus_issue": _first_value(
            payload.get("focus_issue"),
            summary.get("broker_truth_focus_issue_code"),
            summary.get("issue_code"),
        ),
        "focus_state": _first_value(payload.get("focus_state"), summary.get("broker_truth_focus_state")),
        "focus_strategy_kind": _first_value(
            payload.get("focus_strategy_kind"),
            summary.get("broker_truth_focus_strategy_kind"),
            summary.get("strategy_kind"),
        ),
        "focus_best_session": _first_value(
            payload.get("focus_best_session"),
            summary.get("broker_truth_focus_best_session"),
            summary.get("best_session"),
        ),
        "focus_worst_session": _first_value(
            payload.get("focus_worst_session"),
            summary.get("broker_truth_focus_worst_session"),
            summary.get("worst_session"),
        ),
        "focus_command": _first_value(
            payload.get("focus_command"),
            summary.get("broker_truth_focus_next_command"),
            summary.get("next_command"),
        ),
        "focus_closed_trade_count": _first_value(
            summary.get("broker_truth_focus_closed_trade_count"),
            payload.get("focus_closed_trade_count"),
        ),
        "focus_total_realized_pnl": _first_value(
            summary.get("broker_truth_focus_total_realized_pnl"),
            payload.get("focus_total_realized_pnl"),
        ),
        "focus_profit_factor": _first_value(
            summary.get("broker_truth_focus_profit_factor"),
            payload.get("focus_profit_factor"),
        ),
        "safe_to_mutate_live": _first_value(payload.get("safe_to_mutate_live"), summary.get("safe_to_mutate_live")),
    }


def _retune_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary = _summary_dict(payload)
    keep: dict[str, Any] = {}
    for key in (
        "kind",
        "generated_at_utc",
        "status",
        "focus_bot",
        "focus_issue",
        "focus_state",
        "focus_strategy_kind",
        "focus_best_session",
        "focus_worst_session",
        "focus_command",
        "safe_to_mutate_live",
    ):
        if key in payload:
            keep[key] = payload[key]
    for key in (
        "broker_truth_focus_bot_id",
        "broker_truth_focus_issue_code",
        "broker_truth_focus_state",
        "broker_truth_focus_strategy_kind",
        "broker_truth_focus_best_session",
        "broker_truth_focus_worst_session",
        "broker_truth_focus_next_command",
        "broker_truth_focus_closed_trade_count",
        "broker_truth_focus_total_realized_pnl",
        "broker_truth_focus_profit_factor",
        "broker_truth_summary_line",
        "safe_to_mutate_live",
    ):
        if key in summary:
            keep[key] = summary[key]
    return keep


def _broker_close_window_summary(window: dict[str, Any]) -> dict[str, Any]:
    keep: dict[str, Any] = {}
    for key in (
        "label",
        "closed_outcome_count",
        "evaluated_outcome_count",
        "realized_pnl",
        "win_rate",
        "source",
        "since",
        "until",
    ):
        if key in window:
            keep[key] = window[key]
    return keep


def _compact_pnl_map_rows(rows: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        compact.append(
            {
                "bot_id": row.get("bot_id"),
                "symbol": row.get("symbol"),
                "sleeve": row.get("sleeve"),
                "closes": row.get("closes"),
                "realized_pnl": row.get("realized_pnl"),
                "impact_value": row.get("impact_value"),
                "source": row.get("source"),
            },
        )
    return compact


def _recent_outcomes_for_bot(
    close_history: dict[str, Any],
    bot_id: str,
    *,
    window_name: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    if not bot_id:
        return []
    windows = close_history.get("windows") if isinstance(close_history.get("windows"), dict) else {}
    selected = windows.get(window_name) if isinstance(windows.get(window_name), dict) else {}
    recent = selected.get("recent_outcomes") if isinstance(selected.get("recent_outcomes"), list) else []
    rows: list[dict[str, Any]] = []
    for row in recent:
        if not isinstance(row, dict) or str(row.get("bot_id") or "") != bot_id:
            continue
        rows.append(
            {
                "ts": row.get("ts"),
                "close_ts": row.get("close_ts"),
                "bot_id": row.get("bot_id"),
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "qty": row.get("qty"),
                "fill_price": row.get("fill_price"),
                "realized_pnl": row.get("realized_pnl"),
                "realized_r": row.get("realized_r"),
                "action_taken": row.get("action_taken"),
            },
        )
        if len(rows) >= limit:
            break
    return rows


def _normalize_public_broker_close_truth(
    payload: dict[str, Any],
    *,
    focus_normalized: dict[str, Any],
) -> dict[str, Any]:
    close_history = payload.get("close_history") if isinstance(payload.get("close_history"), dict) else {}
    windows = close_history.get("windows") if isinstance(close_history.get("windows"), dict) else {}
    focus_bot = str(focus_normalized.get("focus_bot") or "")
    mtd_window = windows.get("mtd") if isinstance(windows.get("mtd"), dict) else {}
    pnl_map = mtd_window.get("pnl_map") if isinstance(mtd_window.get("pnl_map"), dict) else {}
    return {
        "focus_bot": focus_bot or None,
        "focus_issue": focus_normalized.get("focus_issue"),
        "focus_state": focus_normalized.get("focus_state"),
        "focus_closed_trade_count": focus_normalized.get("focus_closed_trade_count"),
        "focus_total_realized_pnl": focus_normalized.get("focus_total_realized_pnl"),
        "focus_profit_factor": focus_normalized.get("focus_profit_factor"),
        "broker_snapshot_source": payload.get("broker_snapshot_source"),
        "broker_snapshot_state": payload.get("broker_snapshot_state"),
        "broker_snapshot_age_s": payload.get("broker_snapshot_age_s"),
        "broker_mtd_pnl": payload.get("broker_mtd_pnl"),
        "today_realized_pnl": payload.get("today_realized_pnl"),
        "total_unrealized_pnl": payload.get("total_unrealized_pnl"),
        "open_position_count": payload.get("open_position_count"),
        "today_actual_fills": payload.get("today_actual_fills"),
        "reporting_timezone": payload.get("reporting_timezone"),
        "source": payload.get("source"),
        "close_windows": {
            name: _broker_close_window_summary(window)
            for name, window in sorted(windows.items())
            if isinstance(window, dict)
        },
        "focus_recent_outcomes_mtd": _recent_outcomes_for_bot(close_history, focus_bot, window_name="mtd"),
        "mtd_pnl_map": {
            "limit": pnl_map.get("limit"),
            "top_winners": _compact_pnl_map_rows(
                pnl_map.get("top_winners") if isinstance(pnl_map.get("top_winners"), list) else [],
            ),
            "top_losers": _compact_pnl_map_rows(
                pnl_map.get("top_losers") if isinstance(pnl_map.get("top_losers"), list) else [],
            ),
        },
    }


def _public_broker_close_truth_summary(normalized: dict[str, Any]) -> dict[str, Any]:
    return {
        "focus_bot": normalized.get("focus_bot"),
        "focus_closed_trade_count": normalized.get("focus_closed_trade_count"),
        "focus_total_realized_pnl": normalized.get("focus_total_realized_pnl"),
        "focus_profit_factor": normalized.get("focus_profit_factor"),
        "broker_mtd_pnl": normalized.get("broker_mtd_pnl"),
        "today_realized_pnl": normalized.get("today_realized_pnl"),
        "total_unrealized_pnl": normalized.get("total_unrealized_pnl"),
        "open_position_count": normalized.get("open_position_count"),
        "broker_snapshot_source": normalized.get("broker_snapshot_source"),
        "reporting_timezone": normalized.get("reporting_timezone"),
    }


def _closed_trade_ledger_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("kind", "generated_at_utc", "closed_trade_count", "total_realized_pnl"):
        if key in payload:
            summary[key] = payload[key]
    if isinstance(payload.get("active_source_paths"), list):
        summary["active_source_paths"] = payload["active_source_paths"]
    return summary


def _load_json_surface(label: str, path: Path, *, now: datetime) -> TruthSurface:
    if not path.exists():
        return TruthSurface(label=label, source=str(path), available=False)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return TruthSurface(
            label=label,
            source=str(path),
            available=True,
            readable=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    if not isinstance(payload, dict):
        return TruthSurface(
            label=label,
            source=str(path),
            available=True,
            readable=False,
            error="payload is not a JSON object",
        )
    observed = _extract_observed_timestamp(payload, path)
    return TruthSurface(
        label=label,
        source=str(path),
        available=True,
        readable=True,
        observed_ts=observed.isoformat() if observed is not None else None,
        age_seconds=_age_seconds(observed, now) if observed is not None else None,
        normalized=normalize_retune_truth(payload),
        summary=_retune_summary(payload),
    )


def _load_json_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _probe_public_surface(url: str, *, timeout_s: float, now: datetime) -> TruthSurface:
    request = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            status_code = int(response.status)
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return TruthSurface(
            label="public_retune_truth",
            source=url,
            available=False,
            status_code=int(exc.code),
            error=f"HTTPError: {exc.code}",
        )
    except (urllib.error.URLError, TimeoutError) as exc:
        return TruthSurface(
            label="public_retune_truth",
            source=url,
            available=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    except (OSError, json.JSONDecodeError) as exc:
        return TruthSurface(
            label="public_retune_truth",
            source=url,
            available=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    if status_code != 200 or not isinstance(payload, dict):
        return TruthSurface(
            label="public_retune_truth",
            source=url,
            available=False,
            status_code=status_code,
            error=f"unexpected_status:{status_code}",
        )
    observed = _extract_observed_timestamp(payload)
    return TruthSurface(
        label="public_retune_truth",
        source=url,
        available=True,
        readable=True,
        status_code=status_code,
        observed_ts=observed.isoformat() if observed is not None else None,
        age_seconds=_age_seconds(observed, now) if observed is not None else None,
        normalized=normalize_retune_truth(payload),
        summary=_retune_summary(payload),
    )


def _probe_public_broker_state(
    url: str,
    *,
    timeout_s: float,
    now: datetime,
    focus_normalized: dict[str, Any],
) -> TruthSurface:
    request = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            status_code = int(response.status)
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return TruthSurface(
            label="public_broker_close_truth",
            source=url,
            available=False,
            status_code=int(exc.code),
            error=f"HTTPError: {exc.code}",
        )
    except (urllib.error.URLError, TimeoutError) as exc:
        return TruthSurface(
            label="public_broker_close_truth",
            source=url,
            available=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    except (OSError, json.JSONDecodeError) as exc:
        return TruthSurface(
            label="public_broker_close_truth",
            source=url,
            available=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    if status_code != 200 or not isinstance(payload, dict):
        return TruthSurface(
            label="public_broker_close_truth",
            source=url,
            available=False,
            status_code=status_code,
            error=f"unexpected_status:{status_code}",
        )
    observed = _extract_observed_timestamp(payload)
    normalized = _normalize_public_broker_close_truth(payload, focus_normalized=focus_normalized)
    return TruthSurface(
        label="public_broker_close_truth",
        source=url,
        available=True,
        readable=True,
        status_code=status_code,
        observed_ts=observed.isoformat() if observed is not None else None,
        age_seconds=_age_seconds(observed, now) if observed is not None else None,
        normalized=normalized,
        summary=_public_broker_close_truth_summary(normalized),
    )


def _field_mismatches(public: TruthSurface, local: TruthSurface) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for field_name in COMPARE_FIELDS:
        public_value = public.normalized.get(field_name)
        local_value = local.normalized.get(field_name)
        if public_value != local_value:
            mismatches.append(
                {
                    "field": field_name,
                    "public": public_value,
                    "local": local_value,
                },
            )
    return mismatches


def _local_bot_evidence_audit(bot_id: str) -> dict[str, Any]:
    if not bot_id:
        return {}
    from eta_engine.scripts.closed_trade_ledger import load_close_records  # noqa: PLC0415

    rows = load_close_records(bot_filter=bot_id, data_sources=None)
    if not rows:
        return {
            "bot_id": bot_id,
            "total_rows": 0,
            "by_data_source": {},
            "rows_with_realized_pnl": 0,
            "rows_with_close_ts": 0,
            "rows_with_nonempty_extra": 0,
            "rows_with_fill_metadata": 0,
            "historical_unverified_rows": 0,
            "historical_rows_with_fill_metadata": 0,
        }

    by_data_source: dict[str, int] = {}
    rows_with_realized_pnl = 0
    rows_with_close_ts = 0
    rows_with_nonempty_extra = 0
    rows_with_fill_metadata = 0
    historical_unverified_rows = 0
    historical_rows_with_fill_metadata = 0

    for row in rows:
        data_source = str(row.get("_data_source") or "")
        by_data_source[data_source] = by_data_source.get(data_source, 0) + 1
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        if row.get("realized_pnl") is not None or extra.get("realized_pnl") is not None:
            rows_with_realized_pnl += 1
        if row.get("close_ts") or extra.get("close_ts"):
            rows_with_close_ts += 1
        if extra:
            rows_with_nonempty_extra += 1
        has_fill_metadata = bool(
            extra.get("symbol")
            or extra.get("side")
            or extra.get("qty") is not None
            or extra.get("fill_price") is not None
            or extra.get("close_ts")
            or extra.get("realized_pnl") is not None
        )
        if has_fill_metadata:
            rows_with_fill_metadata += 1
        if data_source == "historical_unverified":
            historical_unverified_rows += 1
            if has_fill_metadata:
                historical_rows_with_fill_metadata += 1

    return {
        "bot_id": bot_id,
        "total_rows": len(rows),
        "by_data_source": dict(sorted(by_data_source.items())),
        "rows_with_realized_pnl": rows_with_realized_pnl,
        "rows_with_close_ts": rows_with_close_ts,
        "rows_with_nonempty_extra": rows_with_nonempty_extra,
        "rows_with_fill_metadata": rows_with_fill_metadata,
        "historical_unverified_rows": historical_unverified_rows,
        "historical_rows_with_fill_metadata": historical_rows_with_fill_metadata,
    }


def _trade_close_source_file_audit(path: Path, *, bot_id: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "line_count": 0,
            "bot_row_count": 0,
            "bot_rows_with_explicit_data_source": 0,
            "last_write_utc": None,
            "bot_latest_ts": None,
            "file_size_bytes": 0,
        }

    line_count = 0
    bot_row_count = 0
    bot_rows_with_explicit_data_source = 0
    bot_latest_ts: datetime | None = None

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            line_count += 1
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if str(payload.get("bot_id") or "") != bot_id:
                continue
            bot_row_count += 1
            if str(payload.get("data_source") or "").strip():
                bot_rows_with_explicit_data_source += 1
            observed = parse_timestamp(payload.get("ts"))
            if observed is not None and (bot_latest_ts is None or observed > bot_latest_ts):
                bot_latest_ts = observed

    try:
        stat = path.stat()
        last_write_utc = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
        file_size_bytes = int(stat.st_size)
    except OSError:
        last_write_utc = None
        file_size_bytes = 0

    return {
        "path": str(path),
        "exists": True,
        "line_count": line_count,
        "bot_row_count": bot_row_count,
        "bot_rows_with_explicit_data_source": bot_rows_with_explicit_data_source,
        "last_write_utc": last_write_utc,
        "bot_latest_ts": bot_latest_ts.isoformat() if bot_latest_ts is not None else None,
        "file_size_bytes": file_size_bytes,
    }


def _trade_close_source_audit(bot_id: str) -> dict[str, Any]:
    if not bot_id:
        return {}
    return {
        "bot_id": bot_id,
        "canonical": _trade_close_source_file_audit(CANONICAL_TRADE_CLOSES_PATH, bot_id=bot_id),
        "legacy": _trade_close_source_file_audit(LEGACY_TRADE_CLOSES_PATH, bot_id=bot_id),
    }


def _public_focus_provenance_gap(
    *,
    public_focus_bot: str,
    public_broker_close_truth: TruthSurface,
    local_evidence: dict[str, Any],
    source_audit: dict[str, Any],
) -> dict[str, Any]:
    if not public_focus_bot:
        return {}

    canonical_source = source_audit.get("canonical") if isinstance(source_audit.get("canonical"), dict) else {}
    legacy_source = source_audit.get("legacy") if isinstance(source_audit.get("legacy"), dict) else {}
    public_closed_trade_count = int(public_broker_close_truth.normalized.get("focus_closed_trade_count") or 0)
    canonical_bot_row_count = int(canonical_source.get("bot_row_count") or 0)
    canonical_explicit_count = int(canonical_source.get("bot_rows_with_explicit_data_source") or 0)
    legacy_bot_row_count = int(legacy_source.get("bot_row_count") or 0)
    historical_unverified_rows = int(local_evidence.get("historical_unverified_rows") or 0)
    historical_rows_with_fill_metadata = int(local_evidence.get("historical_rows_with_fill_metadata") or 0)
    rows_with_fill_metadata = int(local_evidence.get("rows_with_fill_metadata") or 0)
    gap_count = max(0, public_closed_trade_count - canonical_bot_row_count)
    support_ratio = (
        round(canonical_bot_row_count / public_closed_trade_count, 4)
        if public_closed_trade_count > 0
        else None
    )

    status = "no_public_focus_count"
    diagnosis = "public_focus_closed_trade_count_unavailable"
    warning = ""
    action = ""
    recommended_truth_source = "public_ops_cache" if public_broker_close_truth.readable else "canonical_local"

    if public_closed_trade_count > 0:
        materially_exceeds_canonical = gap_count >= 10 and canonical_bot_row_count <= max(
            5,
            public_closed_trade_count // 4,
        )
        if materially_exceeds_canonical:
            status = "material_gap"
            diagnosis = "public_broker_proof_exceeds_local_canonical"
            warning = (
                f"Public broker-backed close sample materially exceeds the local canonical trade_closes sample "
                f"for {public_focus_bot} ({public_closed_trade_count} vs {canonical_bot_row_count})."
            )
            action = (
                f"Refresh or repair the canonical trade_closes writer at "
                f"{canonical_source.get('path') or CANONICAL_TRADE_CLOSES_PATH} from the authoritative "
                "VPS/public close source before trusting local broker-proof counts."
            )
            recommended_truth_source = "public_ops_cache"
        elif gap_count > 0:
            status = "gap"
            diagnosis = "public_broker_proof_ahead_of_local_canonical"
            warning = (
                f"Public broker-backed close sample is ahead of the local canonical trade_closes sample "
                f"for {public_focus_bot} ({public_closed_trade_count} vs {canonical_bot_row_count})."
            )
            action = (
                "Keep public/VPS broker truth ahead of local advisory consumers until the canonical "
                "trade_closes path catches up."
            )
            recommended_truth_source = "public_ops_cache"
        else:
            status = "aligned"
            diagnosis = "public_broker_proof_supported_by_local_canonical"
            recommended_truth_source = "canonical_local"

    return {
        "bot_id": public_focus_bot,
        "status": status,
        "diagnosis": diagnosis,
        "public_focus_closed_trade_count": public_closed_trade_count,
        "canonical_bot_row_count": canonical_bot_row_count,
        "canonical_bot_rows_with_explicit_data_source": canonical_explicit_count,
        "legacy_bot_row_count": legacy_bot_row_count,
        "historical_unverified_rows": historical_unverified_rows,
        "historical_rows_with_fill_metadata": historical_rows_with_fill_metadata,
        "rows_with_fill_metadata": rows_with_fill_metadata,
        "gap_count": gap_count,
        "canonical_support_ratio": support_ratio,
        "warning": warning,
        "action": action,
        "recommended_truth_source": recommended_truth_source,
    }


def build_diamond_retune_truth_report(
    *,
    state_root: Path = ETA_RUNTIME_STATE_DIR,
    public_url: str = DEFAULT_PUBLIC_URL,
    public_broker_state_url: str = DEFAULT_PUBLIC_BROKER_STATE_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    local_path = state_root / "diamond_retune_status_latest.json"
    ledger_path = state_root / "closed_trade_ledger_latest.json"
    public_surface = _probe_public_surface(public_url, timeout_s=timeout_s, now=now)
    local_surface = _load_json_surface("local_retune_truth", local_path, now=now)
    ledger_payload = _load_json_payload(ledger_path) or {}
    ledger_summary = _closed_trade_ledger_summary(ledger_payload) if ledger_payload else {}
    mismatches = (
        _field_mismatches(public_surface, local_surface)
        if public_surface.available and public_surface.readable and local_surface.available and local_surface.readable
        else []
    )
    public_focus_bot = str(public_surface.normalized.get("focus_bot") or "") if public_surface.readable else ""
    local_focus_bot = str(local_surface.normalized.get("focus_bot") or "") if local_surface.readable else ""
    public_broker_close_truth = (
        _probe_public_broker_state(
            public_broker_state_url,
            timeout_s=timeout_s,
            now=now,
            focus_normalized=public_surface.normalized,
        )
        if public_surface.available and public_surface.readable
        else TruthSurface(
            label="public_broker_close_truth",
            source=public_broker_state_url,
            available=False,
            readable=False,
            error="public_retune_truth_unavailable",
        )
    )
    public_focus_local_evidence = _local_bot_evidence_audit(public_focus_bot) if public_focus_bot else {}
    public_focus_source_audit = _trade_close_source_audit(public_focus_bot) if public_focus_bot else {}
    public_focus_provenance_gap = (
        _public_focus_provenance_gap(
            public_focus_bot=public_focus_bot,
            public_broker_close_truth=public_broker_close_truth,
            local_evidence=public_focus_local_evidence,
            source_audit=public_focus_source_audit,
        )
        if public_focus_bot
        else {}
    )
    local_focus_local_evidence = (
        _local_bot_evidence_audit(local_focus_bot)
        if local_focus_bot and local_focus_bot != public_focus_bot
        else {}
    )
    local_focus_source_audit = (
        _trade_close_source_audit(local_focus_bot)
        if local_focus_bot and local_focus_bot != public_focus_bot
        else {}
    )

    warnings: list[str] = []
    action_items: list[str] = []

    def _append_unique(items: list[str], message: str) -> None:
        if message and message not in items:
            items.append(message)

    if public_surface.available and public_surface.readable:
        if local_surface.available and local_surface.readable:
            if mismatches:
                status = "warning"
                healthy = False
                diagnosis = "public_local_focus_mismatch"
                _append_unique(
                    warnings,
                    "Public retune focus and local canonical retune receipt disagree. "
                    "Treat the public ops surface as authoritative strategy truth.",
                )
                _append_unique(
                    action_items,
                    "Refresh or repair the local closed-trade ledger and diamond_retune_status writers before using "
                    "local retune receipts for operator decisions.",
                )
                historical_rows = int(public_focus_local_evidence.get("historical_unverified_rows") or 0)
                historical_fill_rows = int(public_focus_local_evidence.get("historical_rows_with_fill_metadata") or 0)
                if historical_rows > 0 and historical_fill_rows == 0:
                    _append_unique(
                        warnings,
                        f"Local historical rows for {public_focus_bot} lack broker fill metadata, "
                        "so blind reclassification is unsafe.",
                    )
                    _append_unique(
                        action_items,
                        f"Do not blindly reclassify local historical_unverified rows for {public_focus_bot}; "
                        "prefer VPS/public sync or canonical writer repair.",
                    )
                canonical_source = (
                    public_focus_source_audit.get("canonical")
                    if isinstance(public_focus_source_audit.get("canonical"), dict)
                    else {}
                )
                legacy_source = (
                    public_focus_source_audit.get("legacy")
                    if isinstance(public_focus_source_audit.get("legacy"), dict)
                    else {}
                )
                canonical_bot_rows = int(canonical_source.get("bot_row_count") or 0)
                legacy_bot_rows = int(legacy_source.get("bot_row_count") or 0)
                canonical_line_count = int(canonical_source.get("line_count") or 0)
                legacy_line_count = int(legacy_source.get("line_count") or 0)
                if legacy_bot_rows > canonical_bot_rows and canonical_bot_rows <= max(5, legacy_bot_rows // 10):
                    _append_unique(
                        warnings,
                        f"Local canonical trade_closes source is thin for {public_focus_bot} "
                        f"({canonical_bot_rows} bot rows across {canonical_line_count} lines) while the legacy archive "
                        f"holds {legacy_bot_rows} bot rows across {legacy_line_count} lines.",
                    )
                    _append_unique(
                        action_items,
                        f"Refresh or repair the canonical trade_closes writer at "
                        f"{canonical_source.get('path') or CANONICAL_TRADE_CLOSES_PATH} from the authoritative "
                        "VPS/public close source before trusting local broker-proof counts.",
                    )
                if public_focus_provenance_gap.get("status") == "material_gap":
                    _append_unique(warnings, str(public_focus_provenance_gap.get("warning") or ""))
                    _append_unique(action_items, str(public_focus_provenance_gap.get("action") or ""))
            else:
                if public_focus_provenance_gap.get("status") == "material_gap":
                    status = "warning"
                    healthy = False
                    diagnosis = "public_focus_provenance_gap"
                    _append_unique(warnings, str(public_focus_provenance_gap.get("warning") or ""))
                    _append_unique(action_items, str(public_focus_provenance_gap.get("action") or ""))
                else:
                    status = "healthy"
                    healthy = True
                    diagnosis = "public_local_focus_match"
        elif local_surface.available and not local_surface.readable:
            status = "warning"
            healthy = False
            diagnosis = "local_retune_receipt_invalid"
            _append_unique(
                action_items,
                "Repair the local diamond_retune_status_latest.json payload; "
                "keep using public ops truth.",
            )
        else:
            status = "warning"
            healthy = False
            diagnosis = "local_retune_receipt_missing"
            _append_unique(
                action_items,
                "Regenerate local diamond_retune_status_latest.json, "
                "but do not override public ops truth.",
            )
    elif local_surface.available and local_surface.readable:
        status = "warning"
        healthy = False
        diagnosis = "public_retune_truth_unavailable"
        _append_unique(
            action_items,
            "Recheck the public ops route or retry from the VPS-local truth path "
            "before trusting local-only retune focus.",
        )
    else:
        status = "critical"
        healthy = False
        diagnosis = "retune_truth_unavailable"
        _append_unique(
            action_items,
            "Neither public nor local retune truth is readable; "
            "repair the truth path before making retune decisions.",
        )

    return {
        "kind": "eta_diamond_retune_truth_check",
        "generated_at_utc": now.isoformat(),
        "healthy": healthy,
        "status": status,
        "diagnosis": diagnosis,
        "mismatch_count": len(mismatches),
        "field_mismatches": mismatches,
        "warnings": warnings,
        "action_items": action_items,
        "public_surface": public_surface.to_dict(),
        "public_broker_close_truth": public_broker_close_truth.to_dict(),
        "local_surface": local_surface.to_dict(),
        "local_closed_trade_ledger": ledger_summary,
        "public_focus_local_evidence_audit": public_focus_local_evidence,
        "public_focus_trade_close_source_audit": public_focus_source_audit,
        "public_focus_provenance_gap": public_focus_provenance_gap,
        "local_focus_local_evidence_audit": local_focus_local_evidence,
        "local_focus_trade_close_source_audit": local_focus_source_audit,
    }


def write_diamond_retune_truth_report(
    report: dict[str, Any],
    *,
    output_path: Path = DEFAULT_OUTPUT_PATH,
) -> Path:
    ensure_parent(output_path)
    output_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return output_path


def write_public_retune_truth_cache(
    surface: TruthSurface | dict[str, Any],
    *,
    output_path: Path = DEFAULT_PUBLIC_CACHE_PATH,
) -> Path:
    payload = _surface_payload_dict(surface)
    existing_cache = _load_json_payload(output_path) or {}
    existing_surface = existing_cache.get("surface") if isinstance(existing_cache.get("surface"), dict) else {}
    incoming_normalized = payload.get("normalized") if isinstance(payload.get("normalized"), dict) else {}
    incoming_summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    existing_normalized = (
        existing_surface.get("normalized") if isinstance(existing_surface.get("normalized"), dict) else {}
    )
    existing_summary = existing_surface.get("summary") if isinstance(existing_surface.get("summary"), dict) else {}
    incoming_focus_bot = str(incoming_normalized.get("focus_bot") or "").strip()
    existing_focus_bot = str(existing_normalized.get("focus_bot") or "").strip()
    if existing_focus_bot and not incoming_focus_bot:
        merged_payload = dict(existing_surface)
        merged_payload.update(payload)
        if not incoming_normalized:
            merged_payload["normalized"] = existing_normalized
        if not incoming_summary:
            merged_payload["summary"] = existing_summary
        payload = merged_payload

    normalized = (payload.get("normalized") or {}) if isinstance(payload.get("normalized"), dict) else {}
    cache = {
        "kind": "eta_public_diamond_retune_truth_cache",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "surface": payload,
        "focus_bot": normalized.get("focus_bot"),
        "focus_issue": normalized.get("focus_issue"),
        "focus_state": normalized.get("focus_state"),
    }
    ensure_parent(output_path)
    output_path.write_text(json.dumps(cache, indent=2, default=str), encoding="utf-8")
    return output_path


def write_public_broker_close_truth_cache(
    surface: TruthSurface | dict[str, Any],
    *,
    output_path: Path = DEFAULT_PUBLIC_BROKER_CLOSE_CACHE_PATH,
) -> Path:
    payload = _surface_payload_dict(surface)
    normalized = (payload.get("normalized") or {}) if isinstance(payload.get("normalized"), dict) else {}
    cache = {
        "kind": "eta_public_broker_close_truth_cache",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "surface": payload,
        "focus_bot": normalized.get("focus_bot"),
        "focus_issue": normalized.get("focus_issue"),
        "focus_state": normalized.get("focus_state"),
        "focus_closed_trade_count": normalized.get("focus_closed_trade_count"),
        "focus_total_realized_pnl": normalized.get("focus_total_realized_pnl"),
        "focus_profit_factor": normalized.get("focus_profit_factor"),
        "broker_mtd_pnl": normalized.get("broker_mtd_pnl"),
        "today_realized_pnl": normalized.get("today_realized_pnl"),
        "total_unrealized_pnl": normalized.get("total_unrealized_pnl"),
        "open_position_count": normalized.get("open_position_count"),
        "reporting_timezone": normalized.get("reporting_timezone"),
        "close_windows": normalized.get("close_windows"),
        "focus_recent_outcomes_mtd": normalized.get("focus_recent_outcomes_mtd"),
        "mtd_pnl_map": normalized.get("mtd_pnl_map"),
    }
    ensure_parent(output_path)
    output_path.write_text(json.dumps(cache, indent=2, default=str), encoding="utf-8")
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare public and local ETA diamond retune truth.")
    parser.add_argument("--json", action="store_true", help="Print the JSON report to stdout.")
    parser.add_argument("--public-url", default=DEFAULT_PUBLIC_URL, help="Public retune status endpoint to compare.")
    parser.add_argument(
        "--public-broker-state-url",
        default=DEFAULT_PUBLIC_BROKER_STATE_URL,
        help="Public broker-state endpoint to mirror into local advisory close truth.",
    )
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S, help="HTTP timeout in seconds.")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Where to write the latest diagnostic report.",
    )
    parser.add_argument(
        "--public-cache-out",
        type=Path,
        default=DEFAULT_PUBLIC_CACHE_PATH,
        help="Where to mirror the fetched public retune truth locally.",
    )
    parser.add_argument(
        "--public-broker-close-cache-out",
        type=Path,
        default=DEFAULT_PUBLIC_BROKER_CLOSE_CACHE_PATH,
        help="Where to mirror the fetched public broker close truth locally.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_diamond_retune_truth_report(
        public_url=args.public_url,
        public_broker_state_url=args.public_broker_state_url,
        timeout_s=args.timeout_s,
    )
    write_diamond_retune_truth_report(report, output_path=args.output)
    public_surface = report.get("public_surface")
    if isinstance(public_surface, dict):
        write_public_retune_truth_cache(public_surface, output_path=args.public_cache_out)
    public_broker_close_truth = report.get("public_broker_close_truth")
    if isinstance(public_broker_close_truth, dict):
        write_public_broker_close_truth_cache(
            public_broker_close_truth,
            output_path=args.public_broker_close_cache_out,
        )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    if report["status"] == "critical":
        return 2
    if report["status"] != "healthy":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
