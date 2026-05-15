"""
Deploy // dashboard_api
=======================
Minimal FastAPI backend for the Evolutionary Trading Algo dashboard.

Reads the JSON state files written by the Avengers stack and exposes them
via a small REST API. Designed to be consumed by the React trading-dashboard
or hit directly from curl.

Run:
  uvicorn deploy.scripts.dashboard_api:app --host 127.0.0.1 --port 8000

Endpoints:
  GET  /health                       -- liveness
  GET  /api/heartbeat                -- avengers_heartbeat.json
  GET  /api/dashboard                -- dashboard_payload.json
  GET  /api/last-task                -- last_task.json
  GET  /api/kaizen                   -- kaizen_ledger.json summary
  GET  /api/state/{filename}         -- raw JSON file from state dir
  GET  /api/tasks                    -- list registered BackgroundTasks
  POST /api/tasks/{task}/fire        -- manually fire a BackgroundTask
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from zoneinfo import ZoneInfo

import portalocker
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from eta_engine.brain.jarvis_v3.sentiment_pressure import summarize_pressure, unknown_pressure
from eta_engine.deploy.scripts.dashboard_services import ensure_dir_writable, read_jsonl_tail, run_background_task

if TYPE_CHECKING:
    from collections.abc import Callable

_BOT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
CANONICAL_BOT_FLEET_TITLE = "Evolutionary Trading Algo // Bot Fleet Roster"
DASHBOARD_VERSION = "v1"
DASHBOARD_RELEASE_STAGE = "pre_beta"
DASHBOARD_LOCAL_TIME_ZONE_NAME = "America/New_York"
DASHBOARD_LOCAL_TIME_ZONE = ZoneInfo(DASHBOARD_LOCAL_TIME_ZONE_NAME)
DASHBOARD_REQUIRED_DATA = (
    "bot_fleet",
    "fleet_equity",
    "auth_session",
    "source_freshness",
)
DASHBOARD_CARD_REGISTRY = (
    {
        "id": "cc-verdict-stream",
        "title": "Live Verdict Stream",
        "source": "sse",
        "endpoint": "/api/live/stream",
        "required": True,
        "stale_after_s": 30,
    },
    {
        "id": "cc-stress-mood",
        "title": "Stress & Session",
        "source": "endpoint",
        "endpoint": "/api/jarvis/summary",
        "required": True,
        "stale_after_s": 30,
    },
    {
        "id": "cc-operator-queue",
        "title": "Operator Blockers",
        "source": "endpoint",
        "endpoint": "/api/jarvis/operator_queue",
        "required": True,
        "stale_after_s": 30,
    },
    {
        "id": "cc-paper-live-transition",
        "title": "Paper Live Transition",
        "source": "endpoint",
        "endpoint": "/api/jarvis/paper_live_transition",
        "required": True,
        "stale_after_s": 30,
    },
    {
        "id": "cc-bot-strategy-readiness",
        "title": "Bot Strategy Readiness",
        "source": "endpoint",
        "endpoint": "/api/jarvis/bot_strategy_readiness",
        "required": True,
        "stale_after_s": 60,
    },
    {
        "id": "cc-strategy-supercharge",
        "title": "Strategy Supercharge Queue",
        "source": "endpoint",
        "endpoint": "/api/jarvis/strategy_supercharge_manifest",
        "required": True,
        "stale_after_s": 60,
    },
    {
        "id": "cc-diamond-retune-status",
        "title": "Diamond Retune Status",
        "source": "endpoint",
        "endpoint": "/api/jarvis/diamond_retune_status",
        "required": True,
        "stale_after_s": 3600,
    },
    {
        "id": "cc-v22-toggle",
        "title": "V22 Modulation",
        "source": "endpoint",
        "endpoint": "/api/jarvis/sage_modulation_toggle",
        "required": True,
        "stale_after_s": 45,
    },
    {
        "id": "cc-sage-explain",
        "title": "Sage Explain",
        "source": "endpoint",
        "endpoint": "/api/jarvis/sage_explain?symbol=MNQ&side=long",
        "required": True,
        "stale_after_s": 45,
    },
    {
        "id": "cc-sage-health",
        "title": "Sage Health",
        "source": "endpoint",
        "endpoint": "/api/jarvis/health",
        "required": True,
        "stale_after_s": 45,
    },
    {
        "id": "cc-disagreement-heatmap",
        "title": "School Disagreement",
        "source": "endpoint",
        "endpoint": "/api/jarvis/sage_disagreement_heatmap?symbol=MNQ",
        "required": True,
        "stale_after_s": 45,
    },
    {
        "id": "cc-sage-registry",
        "title": "School Registry",
        "source": "endpoint",
        "endpoint": "/api/jarvis/sage_school_registry",
        "required": True,
        "stale_after_s": 300,
    },
    {
        "id": "cc-edge-leaderboard",
        "title": "Edge Leaderboard",
        "source": "endpoint",
        "endpoint": "/api/jarvis/edge_leaderboard",
        "required": True,
        "stale_after_s": 60,
    },
    {
        "id": "cc-policy-diff",
        "title": "Bandit Policy Diff",
        "source": "endpoint",
        "endpoint": "/api/jarvis/policy_diff",
        "required": True,
        "stale_after_s": 120,
    },
    {
        "id": "cc-model-tier",
        "title": "Model Tier",
        "source": "endpoint",
        "endpoint": "/api/jarvis/model_tier",
        "required": True,
        "stale_after_s": 120,
    },
    {
        "id": "cc-kaizen-latest",
        "title": "Latest Kaizen Ticket",
        "source": "endpoint",
        "endpoint": "/api/jarvis/kaizen_latest",
        "required": True,
        "stale_after_s": 300,
    },
    {
        "id": "fl-roster",
        "title": "Bot Fleet Roster",
        "source": "endpoint",
        "endpoint": "/api/bot-fleet?since_days=1&live_broker_probe=false",
        "required": True,
        "stale_after_s": 15,
    },
    {
        "id": "fl-drilldown",
        "title": "Last Trade & Drill-Down",
        "source": "endpoint",
        "endpoint": "/api/bot-fleet/{selected_bot}",
        "required": True,
        "stale_after_s": 15,
    },
    {
        "id": "fl-equity-curve",
        "title": "Fleet Equity Curve",
        "source": "endpoint",
        "endpoint": "/api/fleet-equity?range=1d&normalize=1&since_days=1",
        "required": True,
        "stale_after_s": 15,
    },
    {
        "id": "fl-drawdown",
        "title": "Drawdown vs Threshold",
        "source": "endpoint",
        "endpoint": "/api/risk_gates",
        "required": True,
        "stale_after_s": 30,
    },
    {
        "id": "fl-sage-effect",
        "title": "Sage Modulation",
        "source": "endpoint",
        "endpoint": "/api/jarvis/sage_modulation_stats",
        "required": True,
        "stale_after_s": 45,
    },
    {
        "id": "fl-correlation",
        "title": "Correlation Throttles",
        "source": "endpoint",
        "endpoint": "/api/preflight",
        "required": True,
        "stale_after_s": 45,
    },
    {
        "id": "fl-edge-per-bot",
        "title": "Per-Bot Edge",
        "source": "endpoint",
        "endpoint": "/api/jarvis/edge_leaderboard?bot={selected_bot}",
        "required": True,
        "stale_after_s": 45,
    },
    {
        "id": "fl-position-reconciler",
        "title": "Position Reconciler",
        "source": "endpoint",
        "endpoint": "/api/positions/reconciler",
        "required": True,
        "stale_after_s": 30,
    },
    {
        "id": "fl-risk-ladder",
        "title": "Risk Gate Ladder",
        "source": "endpoint",
        "endpoint": "/api/risk_gates",
        "required": True,
        "stale_after_s": 30,
    },
    {
        "id": "fl-controls",
        "title": "Lifecycle Controls",
        "source": "client",
        "endpoint": None,
        "required": True,
        "stale_after_s": None,
    },
    {
        "id": "fl-fill-quality",
        "title": "Fill Quality",
        "source": "endpoint",
        "endpoint": "/api/live/fills?limit=80",
        "required": True,
        "stale_after_s": 20,
    },
    {
        "id": "fl-risk-sim",
        "title": "Risk Simulator",
        "source": "endpoint",
        "endpoint": "/api/risk_gates",
        "required": True,
        "stale_after_s": 30,
    },
    {
        "id": "fl-performance-os",
        "title": "Performance OS",
        "source": "endpoint",
        "endpoint": "/api/bot-fleet?live_broker_probe=false",
        "required": True,
        "stale_after_s": 20,
    },
    {
        "id": "fl-health-badges",
        "title": "Bot Health Badges",
        "source": "endpoint",
        "endpoint": "/api/bot-fleet?live_broker_probe=false",
        "required": True,
        "stale_after_s": 20,
    },
)

# State/log dirs: canonical workspace paths per CLAUDE.md hard rule #1
# ("everything writes under C:\EvolutionaryTradingAlgo"). Legacy in-repo
# locations remain as read fallbacks (handled in ``_state_dir`` /
# ``_log_dir``) so the API can still surface state files persisted
# before the migration; new writes always land at the canonical paths.  # HISTORICAL-PATH-OK
_REPO_ROOT = Path(__file__).resolve().parents[2]  # .../eta_engine/
_WORKSPACE_ROOT = _REPO_ROOT.parent  # .../EvolutionaryTradingAlgo/
_DEFAULT_STATE = _WORKSPACE_ROOT / "var" / "eta_engine" / "state"
_DEFAULT_LOG = _WORKSPACE_ROOT / "logs" / "eta_engine"
# Legacy in-repo locations kept ONLY for read fallback during the
# migration window. Never used as a write target. Once a fresh canonical
# session has rolled over, a follow-up PR can delete the fallbacks.
_LEGACY_STATE = _REPO_ROOT / "state"  # HISTORICAL-PATH-OK
_LEGACY_LOG = _REPO_ROOT / "logs"
_DEFAULT_RUNTIME_STATE = _DEFAULT_STATE / "runtime_state.json"
_LEGACY_RUNTIME_STATE = (
    _WORKSPACE_ROOT / "firm_command_center" / "var" / "data" / "runtime_state.json"
)  # HISTORICAL-PATH-OK
_DEFAULT_BOT_STRATEGY_READINESS_SNAPSHOT = (
    _WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "bot_strategy_readiness_latest.json"
)
_DEFAULT_CORS_ORIGINS = (
    "https://ops.evolutionarytradingalgo.com",
    "https://jarvis.evolutionarytradingalgo.com",
    "https://app.evolutionarytradingalgo.com",
    "https://evolutionarytradingalgo.com",
    "https://www.evolutionarytradingalgo.com",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)
STATE_DIR = Path(os.environ.get("ETA_STATE_DIR", os.environ.get("ETA_STATE_DIR", str(_DEFAULT_STATE))))
LOG_DIR = Path(os.environ.get("ETA_LOG_DIR", os.environ.get("ETA_LOG_DIR", str(_DEFAULT_LOG))))
_START_TS = time.time()
API_BUILD_CAPABILITIES = (
    "command_center_watchdog",
    "eta_readiness_snapshot",
    "ibkr_futures_avg_cost_normalized",
    "sage_sentiment_pressure",
)
_VPS_OPS_HARDENING_STATUSES = {
    "PASS",
    "WARN",
    "BLOCKED",
    "GREEN_READY_FOR_SOAK",
    "RED_RUNTIME_DEGRADED",
    "YELLOW_RESTART_REQUIRED",
    "YELLOW_DURABILITY_GAP",
    "YELLOW_SAFETY_BLOCKED",
    "YELLOW_ADMIN_AI_BLOCKED",
    "YELLOW_ADMIN_AI_PENDING",
    "missing",
    "unreadable",
    "invalid",
    "unknown",
}


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _truthy_env(name: str, default: str = "0") -> bool:
    return str(os.environ.get(name, default)).strip().lower() in {"1", "true", "yes", "on", "y"}


# The Kaizen loop runs every 15 minutes. Keep a small grace window so normal
# scheduler drift does not hide an otherwise current blocked-readiness receipt.
_ETA_READINESS_SNAPSHOT_MAX_AGE_S = _positive_int_env(
    "ETA_READINESS_SNAPSHOT_MAX_AGE_S",
    20 * 60,
)
_PAPER_LIVE_TRANSITION_CACHE_MAX_AGE_S = _positive_int_env(
    "ETA_PAPER_LIVE_TRANSITION_CACHE_MAX_AGE_S",
    15 * 60,
)


def _dashboard_cors_origins() -> list[str]:
    """Return public dashboard origins plus optional comma-separated overrides."""
    configured = os.environ.get("ETA_DASHBOARD_CORS_ORIGINS", "")
    extra = [origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip()]
    return list(dict.fromkeys((*_DEFAULT_CORS_ORIGINS, *extra)))


def _dashboard_contract() -> dict:
    """Machine-readable contract shared by V1 dashboard endpoints."""
    return {
        "dashboard_version": DASHBOARD_VERSION,
        "release_stage": DASHBOARD_RELEASE_STAGE,
        "beta_launched": False,
        "required_data": list(DASHBOARD_REQUIRED_DATA),
        "operator_url": "https://ops.evolutionarytradingalgo.com",
    }


def _dashboard_card_health_payload() -> dict:
    """Static V1 card registry so the shell can detect dead/unwired panels."""
    cards: list[dict] = []
    for item in DASHBOARD_CARD_REGISTRY:
        card = dict(item)
        source = str(card.get("source") or "endpoint")
        if source == "sse":
            card["status"] = "stream_ready"
        elif source == "client":
            card["status"] = "client_ready"
        elif card.get("endpoint"):
            card["status"] = "registered"
        else:
            card["status"] = "dead"
        cards.append(card)

    dead_cards = [card for card in cards if card["status"] == "dead"]
    stale_cards: list[dict] = []
    by_source: defaultdict[str, int] = defaultdict(int)
    for card in cards:
        by_source[str(card.get("source") or "endpoint")] += 1

    return {
        **_dashboard_contract(),
        "source_of_truth": "dashboard_card_registry",
        "generated_at": time.time(),
        "cards": cards,
        "dead_cards": dead_cards,
        "stale_cards": stale_cards,
        "summary": {
            "total": len(cards),
            "registered": sum(1 for card in cards if card["status"] == "registered"),
            "client": int(by_source["client"]),
            "sse": int(by_source["sse"]),
            "dead": len(dead_cards),
            "stale": len(stale_cards),
        },
    }


def _dashboard_proxy_watchdog_payload(*, server_ts: float) -> dict:
    """Summarize the 8421 proxy bridge self-heal heartbeat for diagnostics."""
    heartbeat_path = _state_dir() / "dashboard_proxy_watchdog_heartbeat.json"
    payload = _read_json_file(heartbeat_path)
    if not payload:
        return {
            "status": "missing",
            "fresh": False,
            "heartbeat_path": str(heartbeat_path),
            "heartbeat_ts": None,
            "heartbeat_age_s": None,
            "checked_at": None,
            "action": "missing",
            "task_name": "",
            "probe_healthy": None,
            "probe_reason": "heartbeat_missing",
            "status_code": None,
            "restart_ok": None,
            "restart_reason": None,
            "summary": "dashboard proxy watchdog heartbeat missing",
        }

    decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    probe = decision.get("post_restart_probe") if isinstance(decision.get("post_restart_probe"), dict) else {}
    if not probe:
        probe = decision.get("probe") if isinstance(decision.get("probe"), dict) else {}

    heartbeat_ts = payload.get("ts")
    checked_at = decision.get("checked_at") or heartbeat_ts
    heartbeat_age_s = _iso_age_s(heartbeat_ts, server_ts=server_ts)
    checked_age_s = _iso_age_s(checked_at, server_ts=server_ts)
    age_s = checked_age_s if checked_age_s is not None else heartbeat_age_s
    action = str(decision.get("action") or "unknown")
    probe_healthy = probe.get("healthy") if isinstance(probe.get("healthy"), bool) else None
    restart_ok = decision.get("restart_ok") if isinstance(decision.get("restart_ok"), bool) else None
    probe_reason = str(probe.get("reason") or decision.get("restart_reason") or "unknown")

    if age_s is None:
        status = "unknown"
    elif action == "restart_failed" or restart_ok is False:
        status = "failed"
    elif probe_healthy is False:
        status = "degraded"
    elif age_s > 180 and probe_healthy is True:
        status = "probe_ok_watchdog_stale"
    elif age_s > 180:
        status = "stale"
    elif probe_healthy is True:
        status = "ok"
    else:
        status = "unknown"

    return {
        "status": status,
        "fresh": age_s is not None and age_s <= 180,
        "heartbeat_path": str(heartbeat_path),
        "heartbeat_ts": heartbeat_ts,
        "heartbeat_age_s": heartbeat_age_s,
        "checked_at": checked_at,
        "checked_age_s": checked_age_s,
        "action": action,
        "task_name": str(decision.get("task_name") or ""),
        "probe_healthy": probe_healthy,
        "probe_reason": probe_reason,
        "status_code": probe.get("status_code"),
        "elapsed_ms": probe.get("elapsed_ms"),
        "body_len": probe.get("body_len"),
        "restart_ok": restart_ok,
        "restart_reason": decision.get("restart_reason"),
        "summary": f"{action}: {probe_reason}",
    }


def _command_center_doctor_receipt_path() -> Path:
    """Canonical root-level Command Center doctor receipt path."""
    override = os.environ.get("ETA_COMMAND_CENTER_DOCTOR_RECEIPT_PATH", "").strip()
    if override:
        return Path(override)
    return _WORKSPACE_ROOT / "var" / "ops" / "command_center_doctor_latest.json"


def _command_center_watchdog_status_path() -> Path:
    """Canonical root-level Command Center watchdog status receipt path."""
    override = os.environ.get("ETA_COMMAND_CENTER_WATCHDOG_STATUS_PATH", "").strip()
    if override:
        return Path(override)
    return _WORKSPACE_ROOT / "var" / "ops" / "command_center_watchdog_status_latest.json"


def _eta_readiness_snapshot_status_path() -> Path:
    """Canonical root-level ETA readiness snapshot receipt path."""
    override = os.environ.get("ETA_READINESS_SNAPSHOT_STATUS_PATH", "").strip()
    if override:
        return Path(override)
    return _WORKSPACE_ROOT / "var" / "ops" / "eta_readiness_snapshot_latest.json"


def _eta_readiness_snapshot_payload(*, server_ts: float) -> dict:
    """Summarize the root readiness receipt for dashboard diagnostics."""
    status_path = _eta_readiness_snapshot_status_path()
    receipt = _read_json_file(status_path)
    if not receipt:
        return {
            "status": "missing_receipt",
            "fresh": False,
            "healthy": False,
            "status_path": str(status_path),
            "checked_at": None,
            "age_s": None,
            "summary": "ETA readiness snapshot receipt missing",
            "check_count": 0,
            "blocked_count": 0,
            "ok_count": 0,
            "primary_blocker": "",
            "primary_action": "Run .\\scripts\\eta-readiness-snapshot.ps1",
            "next_actions": ["Run .\\scripts\\eta-readiness-snapshot.ps1"],
            "closed_trade_count": 0,
            "total_realized_pnl": None,
            "win_rate_pct": None,
            "cumulative_r": None,
            "broker_missing_bracket_count": 0,
            "broker_open_position_count": 0,
            "public_fallback_reason": "",
            "public_fallback_active": False,
            "public_fallback_primary_action": "",
            "public_fallback_blocked_count": 0,
            "prop_primary_bot": "",
            "promotion_summary": "",
            "required_evidence": [],
        }

    checked_at = receipt.get("checked_at_utc") or receipt.get("checked_at")
    age_s = _iso_age_s(checked_at, server_ts=server_ts)
    fresh = age_s is not None and age_s <= _ETA_READINESS_SNAPSHOT_MAX_AGE_S
    raw_summary = str(receipt.get("summary") or "UNKNOWN")
    checks = receipt.get("checks") if isinstance(receipt.get("checks"), list) else []
    pass_statuses = {"OK", "PASS", "READY", "READY_NO_OPEN_EXPOSURE"}
    blocked_checks = [
        check
        for check in checks
        if isinstance(check, dict) and str(check.get("status") or "").upper() not in pass_statuses
    ]
    ok_count = len(checks) - len(blocked_checks)

    by_name = {str(check.get("name") or ""): check for check in checks if isinstance(check, dict) and check.get("name")}
    closed_payload = (
        by_name.get("closed_trade_ledger", {}).get("payload")
        if isinstance(by_name.get("closed_trade_ledger", {}).get("payload"), dict)
        else {}
    )
    bracket_payload = (
        by_name.get("broker_bracket_audit", {}).get("payload")
        if isinstance(by_name.get("broker_bracket_audit", {}).get("payload"), dict)
        else {}
    )
    prop_payload = (
        by_name.get("prop_live_readiness_gate", {}).get("payload")
        if isinstance(by_name.get("prop_live_readiness_gate", {}).get("payload"), dict)
        else {}
    )
    promotion_payload = (
        by_name.get("prop_strategy_promotion_audit", {}).get("payload")
        if isinstance(by_name.get("prop_strategy_promotion_audit", {}).get("payload"), dict)
        else {}
    )
    position_summary = (
        bracket_payload.get("position_summary") if isinstance(bracket_payload.get("position_summary"), dict) else {}
    )
    promotion_primary = promotion_payload.get("primary") if isinstance(promotion_payload.get("primary"), dict) else {}
    public_fallback_checks = (
        receipt.get("public_fallback_checks") if isinstance(receipt.get("public_fallback_checks"), list) else []
    )
    public_fallback_by_name = {
        str(check.get("name") or ""): check
        for check in public_fallback_checks
        if isinstance(check, dict) and check.get("name")
    }
    fallback_bracket_payload = (
        public_fallback_by_name.get("broker_bracket_audit_public_fallback", {}).get("payload")
        if isinstance(
            public_fallback_by_name.get("broker_bracket_audit_public_fallback", {}).get("payload"),
            dict,
        )
        else {}
    )
    fallback_prop_payload = (
        public_fallback_by_name.get("prop_live_readiness_gate_public_fallback", {}).get("payload")
        if isinstance(
            public_fallback_by_name.get("prop_live_readiness_gate_public_fallback", {}).get("payload"),
            dict,
        )
        else {}
    )
    fallback_position_summary = (
        fallback_bracket_payload.get("position_summary")
        if isinstance(fallback_bracket_payload.get("position_summary"), dict)
        else {}
    )
    public_fallback_blocked_checks = [
        check
        for check in public_fallback_checks
        if isinstance(check, dict) and str(check.get("status") or "").upper() not in pass_statuses
    ]
    local_fleet_truth_unavailable = (
        str(bracket_payload.get("summary") or "").upper() == "BLOCKED_FLEET_TRUTH_UNAVAILABLE"
    )
    public_fallback_active = bool(
        receipt.get("public_fallback_reason") and public_fallback_checks and local_fleet_truth_unavailable
    )

    next_actions: list[str] = []
    public_fallback_next_actions: list[str] = []
    fallback_bracket_action = str(
        fallback_bracket_payload.get("next_action") or fallback_bracket_payload.get("operator_action") or ""
    ).strip()
    if fallback_bracket_action:
        public_fallback_next_actions.append(fallback_bracket_action)
    fallback_prop_actions = fallback_prop_payload.get("next_actions")
    if isinstance(fallback_prop_actions, list):
        public_fallback_next_actions.extend(str(action) for action in fallback_prop_actions if action)
    raw_next_actions = prop_payload.get("next_actions")
    if isinstance(raw_next_actions, list):
        next_actions.extend(str(action) for action in raw_next_actions if action)
    bracket_action = str(bracket_payload.get("next_action") or bracket_payload.get("operator_action") or "").strip()
    if bracket_action and bracket_action not in next_actions:
        next_actions.append(bracket_action)
    raw_required_evidence = promotion_payload.get("required_evidence")
    required_evidence = (
        [str(item) for item in raw_required_evidence if item] if isinstance(raw_required_evidence, list) else []
    )
    for item in required_evidence:
        if item not in next_actions:
            next_actions.append(item)
    if public_fallback_active:
        for item in reversed(public_fallback_next_actions):
            if item and item not in next_actions:
                next_actions.insert(0, item)

    primary_blocker = ""
    if public_fallback_active and public_fallback_blocked_checks:
        primary_blocker = str(public_fallback_blocked_checks[0].get("name") or "")
    elif blocked_checks:
        primary_blocker = str(blocked_checks[0].get("name") or "")
    primary_action = (
        next_actions[0]
        if next_actions
        else ("Clear blocked readiness checks." if blocked_checks else "No action required.")
    )
    effective_position_summary = (
        fallback_position_summary if public_fallback_active and fallback_position_summary else position_summary
    )
    if not fresh:
        status = "stale_receipt"
    elif raw_summary.upper() == "READY" and not blocked_checks:
        status = "ready"
    elif raw_summary.upper().startswith("BLOCKED") or blocked_checks:
        status = "blocked"
    else:
        status = "unknown"

    return {
        "status": status,
        "fresh": fresh,
        "healthy": status == "ready",
        "status_path": str(status_path),
        "checked_at": checked_at,
        "age_s": age_s,
        "summary": raw_summary,
        "check_count": len(checks),
        "blocked_count": len(blocked_checks),
        "ok_count": ok_count,
        "primary_blocker": primary_blocker,
        "primary_action": primary_action,
        "next_actions": next_actions[:5],
        "closed_trade_count": int(closed_payload.get("closed_trade_count") or 0),
        "total_realized_pnl": closed_payload.get("total_realized_pnl"),
        "win_rate_pct": closed_payload.get("win_rate_pct"),
        "cumulative_r": closed_payload.get("cumulative_r"),
        "broker_missing_bracket_count": int(effective_position_summary.get("missing_bracket_count") or 0),
        "broker_open_position_count": int(effective_position_summary.get("broker_open_position_count") or 0),
        "public_fallback_reason": str(receipt.get("public_fallback_reason") or ""),
        "public_fallback_active": public_fallback_active,
        "public_fallback_primary_action": public_fallback_next_actions[0] if public_fallback_next_actions else "",
        "public_fallback_blocked_count": len(public_fallback_blocked_checks),
        "prop_primary_bot": str(
            prop_payload.get("primary_bot")
            or promotion_payload.get("primary_bot")
            or promotion_primary.get("bot_id")
            or ""
        ),
        "promotion_summary": str(promotion_payload.get("summary") or ""),
        "ready_for_prop_dry_run_review": bool(promotion_payload.get("ready_for_prop_dry_run_review")),
        "required_evidence": required_evidence[:5],
    }


def _command_center_watchdog_payload(*, server_ts: float) -> dict:
    """Summarize the root CommandCenterDoctor receipt for dashboard diagnostics."""
    receipt_path = _command_center_doctor_receipt_path()
    status_path = _command_center_watchdog_status_path()
    receipt = _read_json_file(receipt_path)
    status_receipt = _read_json_file(status_path)
    status_receipt = status_receipt if isinstance(status_receipt, dict) else {}
    if not receipt:
        return {
            "status": "missing_receipt",
            "fresh": False,
            "receipt_path": str(receipt_path),
            "status_receipt_path": str(status_path),
            "checked_at": None,
            "age_s": None,
            "healthy": False,
            "failure_class": "missing_receipt",
            "operator_contract_state": "missing_receipt",
            "recommended_action": "run_doctor",
            "next_step": "run_watchdog_now",
            "next_command": None,
            "action_plan": status_receipt.get("operator_action_plan")
            if isinstance(status_receipt.get("operator_action_plan"), list)
            else [],
            "follow_up_actions": status_receipt.get("operator_follow_up_actions")
            if isinstance(status_receipt.get("operator_follow_up_actions"), list)
            else [],
            "follow_up_count": int(status_receipt.get("operator_follow_up_count") or 0),
            "watchdog_registered": status_receipt.get("watchdog_registered"),
            "watchdog_state": status_receipt.get("watchdog_state"),
            "repair_required": True,
            "requires_elevation": True,
            "summary": "Command Center doctor receipt missing",
        }

    operator_action = receipt.get("operator_action") if isinstance(receipt.get("operator_action"), dict) else {}
    failure_summary = receipt.get("failure_summary") if isinstance(receipt.get("failure_summary"), dict) else {}
    action_plan = (
        status_receipt.get("operator_action_plan")
        if isinstance(status_receipt.get("operator_action_plan"), list)
        else []
    )
    follow_up_actions = (
        status_receipt.get("operator_follow_up_actions")
        if isinstance(status_receipt.get("operator_follow_up_actions"), list)
        else []
    )
    first_follow_up = follow_up_actions[0] if follow_up_actions and isinstance(follow_up_actions[0], dict) else {}
    watchdog_missing = status_receipt.get("watchdog_registered") is False
    checked_at = receipt.get("checked_at")
    age_s = _iso_age_s(checked_at, server_ts=server_ts)
    fresh = age_s is not None and age_s <= 900
    receipt_healthy = bool(receipt.get("healthy")) and fresh
    healthy = receipt_healthy and not watchdog_missing
    failure_class = str(receipt.get("failure_class") or "unknown")
    contract_state = str(receipt.get("operator_contract_state") or failure_class)
    recommended_action = str(receipt.get("recommended_action") or operator_action.get("step") or "unknown")
    reason = str(operator_action.get("reason") or contract_state or failure_class)
    status = "healthy" if healthy else ("stale_receipt" if not fresh else reason)
    if status == "unknown" and failure_class != "unknown":
        status = failure_class
    if watchdog_missing and fresh:
        status = "missing_watchdog"

    next_step = str(operator_action.get("step") or recommended_action)
    next_command = operator_action.get("command")
    repair_required = bool(receipt.get("repair_required"))
    requires_elevation = bool(operator_action.get("requires_elevation"))
    if not fresh or watchdog_missing:
        status_reason = str(
            status_receipt.get("operator_next_reason")
            or status_receipt.get("primary_blocker")
            or status_receipt.get("effective_status")
            or status_receipt.get("operator_issue_status")
            or ""
        ).strip()
        status_next_step = str(status_receipt.get("operator_next_step") or "").strip()
        status_next_command = status_receipt.get("operator_next_command")
        if status_next_step:
            recommended_action = status_next_step
            next_step = status_next_step
        elif watchdog_missing and (receipt_healthy or recommended_action in {"none", "unknown"}):
            recommended_action = str(first_follow_up.get("step") or "register_watchdog")
            next_step = recommended_action
        if status_next_command:
            next_command = status_next_command
        elif first_follow_up.get("command"):
            next_command = first_follow_up.get("command")
        if status_reason:
            reason = status_reason
        elif watchdog_missing and (receipt_healthy or reason in {"healthy", "unknown"}):
            reason = str(first_follow_up.get("reason") or "watchdog_missing")
        repair_required = True
        requires_elevation = bool(
            status_receipt.get("operator_next_requires_elevation")
            or first_follow_up.get("requires_elevation")
            or requires_elevation
        )

    summary = (
        "Command Center watchdog is healthy."
        if healthy
        else (
            f"Command Center watchdog receipt is stale; latest status says {reason}; next={recommended_action}."
            if not fresh
            else f"Command Center watchdog needs {recommended_action}: {reason}."
        )
    )

    return {
        "status": status,
        "fresh": fresh,
        "receipt_path": str(receipt_path),
        "status_receipt_path": str(status_path),
        "checked_at": checked_at,
        "age_s": age_s,
        "healthy": healthy,
        "failure_class": failure_class,
        "operator_contract_state": contract_state,
        "recommended_action": recommended_action,
        "next_step": next_step,
        "next_command": next_command,
        "action_plan": action_plan,
        "action_count": int(status_receipt.get("operator_action_count") or len(action_plan)),
        "follow_up_actions": follow_up_actions,
        "follow_up_count": int(status_receipt.get("operator_follow_up_count") or len(follow_up_actions)),
        "watchdog_registered": status_receipt.get("watchdog_registered"),
        "watchdog_state": status_receipt.get("watchdog_state"),
        "can_launch_from_desktop": status_receipt.get("operator_next_can_launch_from_desktop"),
        "launch_context": status_receipt.get("operator_next_launch_context"),
        "instruction": status_receipt.get("operator_next_instruction"),
        "repair_required": repair_required,
        "requires_elevation": requires_elevation,
        "failure_summary": failure_summary,
        "summary": summary,
    }


def _dashboard_diagnostics_payload() -> dict:
    """Single source-of-truth rollup for Command Center self-diagnostics."""
    server_ts = time.time()
    generated_at = datetime.fromtimestamp(server_ts, UTC).isoformat()
    cards = _dashboard_card_health_payload()
    dashboard_proxy_watchdog = _dashboard_proxy_watchdog_payload(server_ts=server_ts)
    command_center_watchdog = _command_center_watchdog_payload(server_ts=server_ts)
    eta_readiness_snapshot = _eta_readiness_snapshot_payload(server_ts=server_ts)
    vps_ops_hardening = _vps_ops_hardening_payload(server_ts=server_ts)

    try:
        roster = bot_fleet_roster(Response(), since_days=1, live_broker_probe=False)
    except Exception as exc:  # noqa: BLE001 -- diagnostics should fail soft.
        roster = {"bots": [], "confirmed_bots": 0, "summary": {}, "_error": str(exc)}

    try:
        equity = equity_curve(range="1d", normalize=True, since_days=1, response=Response())
    except Exception as exc:  # noqa: BLE001 -- diagnostics should fail soft.
        equity = {"series": [], "summary": {}, "source": "error", "_error": str(exc)}

    operator_queue = _operator_queue_payload(prefer_cache=True, server_ts=server_ts)
    paper_live_transition = _paper_live_transition_payload(refresh=False)
    readiness = _bot_strategy_readiness_payload()
    symbol_intelligence = _load_symbol_intelligence_snapshot()
    diamond_retune_status = _load_diamond_retune_status()
    live_broker_diagnostics = _live_broker_diagnostic_payload()
    roster_bots = roster.get("bots") if isinstance(roster.get("bots"), list) else []
    roster_summary = roster.get("summary") if isinstance(roster.get("summary"), dict) else {}
    equity_series = equity.get("series") if isinstance(equity.get("series"), list) else []
    equity_summary = equity.get("summary") if isinstance(equity.get("summary"), dict) else {}
    card_summary = cards.get("summary") if isinstance(cards.get("summary"), dict) else {}
    operator_summary = operator_queue.get("summary") if isinstance(operator_queue.get("summary"), dict) else {}
    top_operator_blockers = (
        operator_queue.get("top_blockers") if isinstance(operator_queue.get("top_blockers"), list) else []
    )
    top_launch_blockers = (
        operator_queue.get("top_launch_blockers") if isinstance(operator_queue.get("top_launch_blockers"), list) else []
    )
    first_operator_blocker = (
        top_operator_blockers[0] if top_operator_blockers and isinstance(top_operator_blockers[0], dict) else {}
    )
    first_launch_blocker = (
        top_launch_blockers[0] if top_launch_blockers and isinstance(top_launch_blockers[0], dict) else {}
    )
    first_operator_evidence = (
        first_operator_blocker.get("evidence") if isinstance(first_operator_blocker.get("evidence"), dict) else {}
    )
    first_operator_blocked_bots = first_operator_evidence.get("blocked_bots")
    if not isinstance(first_operator_blocked_bots, list):
        first_operator_blocked_bots = []
    first_operator_next_actions = first_operator_blocker.get("next_actions")
    if not isinstance(first_operator_next_actions, list):
        first_operator_next_actions = []
    first_failed_gate = _first_failed_gate(paper_live_transition if isinstance(paper_live_transition, dict) else {})
    readiness_summary = readiness.get("summary") if isinstance(readiness.get("summary"), dict) else {}
    readiness_lanes = readiness_summary.get("launch_lanes") if isinstance(readiness_summary, dict) else {}
    readiness_lane_counts = readiness_lanes if isinstance(readiness_lanes, dict) else {}
    readiness_blocked_data = int(
        readiness_summary.get("blocked_data") or readiness_lane_counts.get("blocked_data") or 0
    )
    transition_launch_blocked_raw = paper_live_transition.get("operator_queue_launch_blocked_count")
    if transition_launch_blocked_raw is None:
        transition_launch_blocked_raw = operator_queue.get("launch_blocked_count")
    try:
        transition_launch_blocked = int(transition_launch_blocked_raw or 0)
    except (TypeError, ValueError):
        transition_launch_blocked = 0
    transition_first_launch_blocker = ""
    transition_first_launch_next_action = ""
    if transition_launch_blocked > 0:
        transition_first_launch_blocker = str(
            paper_live_transition.get("operator_queue_first_launch_blocker_op_id")
            or paper_live_transition.get("operator_queue_first_blocker_op_id")
            or ""
        )
        transition_first_launch_next_action = str(
            paper_live_transition.get("operator_queue_first_launch_next_action")
            or paper_live_transition.get("operator_queue_first_next_action")
            or ""
        )
        fresh_operator_queue = not operator_queue.get("cache_stale")
        if fresh_operator_queue and not transition_first_launch_blocker:
            transition_first_launch_blocker = str(first_launch_blocker.get("op_id") or "")
        if fresh_operator_queue and not transition_first_launch_next_action:
            launch_actions = first_launch_blocker.get("next_actions")
            if isinstance(launch_actions, list) and launch_actions:
                transition_first_launch_next_action = str(launch_actions[0])
            else:
                transition_first_launch_next_action = str(
                    first_launch_blocker.get("detail")
                    or first_launch_blocker.get("title")
                    or transition_first_launch_next_action
                )
        if paper_live_transition.get("cache_stale") and fresh_operator_queue:
            transition_first_launch_blocker = str(first_launch_blocker.get("op_id") or "")
            transition_first_launch_next_action = str(
                first_launch_blocker.get("detail")
                or first_launch_blocker.get("title")
                or transition_first_launch_next_action
            )

    paper_live_status = str(paper_live_transition.get("status") or "unknown")
    paper_live_effective_status = str(
        roster_summary.get("paper_live_effective_status")
        or paper_live_transition.get("effective_status")
        or paper_live_status
    )
    paper_live_effective_detail = str(
        roster_summary.get("paper_live_effective_detail") or paper_live_transition.get("effective_detail") or ""
    )
    paper_live_held_by_bracket_audit = bool(
        roster_summary.get("paper_live_held_by_bracket_audit") or paper_live_transition.get("held_by_bracket_audit")
    )
    if transition_launch_blocked > 0 and paper_live_effective_status in {
        "ready",
        "ready_to_launch_paper_live",
        "green",
    }:
        paper_live_effective_status = "blocked_by_operator_queue"
        paper_live_effective_detail = (
            transition_first_launch_next_action
            or str(first_launch_blocker.get("detail") or first_launch_blocker.get("title") or "")
            or "Fresh operator queue has a launch blocker."
        )

    return {
        **_dashboard_contract(),
        "source_of_truth": "dashboard_diagnostics",
        "generated_at": generated_at,
        "server_ts": server_ts,
        "api_build": {
            "name": "eta-command-center-v1",
            "dashboard_version": DASHBOARD_VERSION,
            "release_stage": DASHBOARD_RELEASE_STAGE,
            "capabilities": list(API_BUILD_CAPABILITIES),
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "started_at": datetime.fromtimestamp(_START_TS, UTC).isoformat(),
        },
        "service": {
            "status": "ok",
            "uptime_s": round(max(0.0, server_ts - _START_TS), 3),
            "pid": os.getpid(),
        },
        "paths": {
            "repo_root": str(_REPO_ROOT),
            "workspace_root": str(_WORKSPACE_ROOT),
            "state_dir": str(_state_dir()),
            "log_dir": str(_log_dir()),
            "runtime_state_path": str(_runtime_state_path()),
        },
        "cards": {
            "summary": card_summary,
            "dead_cards": cards.get("dead_cards") if isinstance(cards.get("dead_cards"), list) else [],
            "stale_cards": cards.get("stale_cards") if isinstance(cards.get("stale_cards"), list) else [],
        },
        "bot_fleet": {
            "bot_total": int(roster_summary.get("bot_total") or len(roster_bots)),
            "confirmed_bots": int(roster.get("confirmed_bots") or roster_summary.get("confirmed_bots") or 0),
            "active_bots": int(roster_summary.get("active_bots") or roster.get("active_bots") or 0),
            "runtime_active_bots": int(
                roster_summary.get("runtime_active_bots")
                or roster.get("runtime_active_bots")
                or roster_summary.get("active_bots")
                or 0
            ),
            "running_bots": int(roster_summary.get("running_bots") or 0),
            "staged_bots": int(roster_summary.get("staged_bots") or roster.get("staged_bots") or 0),
            "truth_status": str(roster.get("truth_status") or roster_summary.get("truth_status") or "unknown"),
            "truth_summary_line": str(
                roster.get("truth_summary_line") or roster_summary.get("truth_summary_line") or "",
            ),
            "live_broker_probe_mode": str(roster_summary.get("live_broker_probe_mode") or "unknown"),
            "source_of_truth": str(roster.get("source_of_truth") or _state_dir()),
            "error": roster.get("_error"),
        },
        "equity": {
            "source": str(equity.get("source") or "unknown"),
            "session_truth_status": str(equity.get("session_truth_status") or "unknown"),
            "source_age_s": equity.get("source_age_s"),
            "point_count": len(equity_series),
            "today_pnl": equity_summary.get("today_pnl"),
            "error": equity.get("_error"),
        },
        "bot_strategy_readiness": {
            "status": str(readiness.get("status") or "unknown"),
            "blocked_data": readiness_blocked_data,
            "paper_ready": int(readiness_summary.get("can_paper_trade") or 0),
            "can_live_any": bool(readiness_summary.get("can_live_any")),
            "launch_lanes": readiness_lane_counts,
            "top_action_count": len(readiness.get("top_actions") or []),
            "error": readiness.get("error"),
        },
        "symbol_intelligence": _symbol_intelligence_diagnostic_payload(symbol_intelligence),
        "diamond_retune_status": _diamond_retune_diagnostic_payload(diamond_retune_status),
        "live_broker_state": live_broker_diagnostics,
        "operator_queue": {
            "blocked": int(operator_summary.get("BLOCKED") or 0),
            "observed": int(operator_summary.get("OBSERVED") or 0),
            "unknown": int(operator_summary.get("UNKNOWN") or 0),
            "launch_blocked": int(operator_queue.get("launch_blocked_count") or 0),
            "top_blocker_op_id": str(first_operator_blocker.get("op_id") or ""),
            "top_blocker_title": str(first_operator_blocker.get("title") or ""),
            "top_blocker_detail": str(first_operator_blocker.get("detail") or ""),
            "top_blocker_launch_blocker": bool(first_operator_evidence.get("launch_blocker")),
            "top_blocker_launch_role": str(first_operator_evidence.get("launch_role") or ""),
            "top_blocker_blocked_bots": [str(bot) for bot in first_operator_blocked_bots],
            "top_blocker_next_actions": [str(action) for action in first_operator_next_actions],
            "top_launch_blocker_op_id": str(first_launch_blocker.get("op_id") or ""),
            "top_launch_blocker_detail": str(
                first_launch_blocker.get("detail") or first_launch_blocker.get("title") or ""
            ),
            "source": str(operator_queue.get("source") or "unknown"),
            "cache_status": str(operator_queue.get("cache_status") or ""),
            "cache_age_s": operator_queue.get("cache_age_s"),
            "cache_stale": bool(operator_queue.get("cache_stale")),
            "stale_cache_age_s": operator_queue.get("stale_cache_age_s"),
            "stale_cache_path": operator_queue.get("stale_cache_path"),
            "error": operator_queue.get("error"),
        },
        "paper_live_transition": {
            "status": paper_live_status,
            "effective_status": paper_live_effective_status,
            "effective_detail": paper_live_effective_detail,
            "held_by_bracket_audit": paper_live_held_by_bracket_audit,
            "broker_bracket_missing_count": int(roster_summary.get("broker_bracket_missing_count") or 0),
            "broker_bracket_primary_symbol": str(roster_summary.get("broker_bracket_primary_symbol") or ""),
            "broker_bracket_primary_venue": str(roster_summary.get("broker_bracket_primary_venue") or ""),
            "broker_bracket_primary_sec_type": str(roster_summary.get("broker_bracket_primary_sec_type") or ""),
            "critical_ready": bool(paper_live_transition.get("critical_ready")),
            "paper_ready_bots": int(paper_live_transition.get("paper_ready_bots") or 0),
            "operator_queue_blocked_count": int(operator_summary.get("BLOCKED") or 0),
            "operator_queue_launch_blocked_count": transition_launch_blocked,
            "first_launch_blocker_op_id": transition_first_launch_blocker,
            "first_launch_next_action": transition_first_launch_next_action,
            "first_failed_gate": {
                "name": str(first_failed_gate.get("name") or ""),
                "detail": str(first_failed_gate.get("detail") or ""),
                "next_action": str(first_failed_gate.get("next_action") or ""),
            },
            "source_age_s": paper_live_transition.get("source_age_s"),
            "cache_stale": bool(paper_live_transition.get("cache_stale")),
            "error": paper_live_transition.get("error"),
        },
        "dashboard_proxy_watchdog": dashboard_proxy_watchdog,
        "command_center_watchdog": command_center_watchdog,
        "eta_readiness_snapshot": eta_readiness_snapshot,
        "vps_ops_hardening": vps_ops_hardening,
        "hardening": vps_ops_hardening,
        "checks": {
            "api_contract": True,
            "card_contract": int(card_summary.get("dead") or 0) == 0 and int(card_summary.get("stale") or 0) == 0,
            "bot_fleet_contract": isinstance(roster.get("bots"), list),
            "equity_contract": "series" in equity,
            "bot_strategy_readiness_contract": readiness.get("status") == "ready" and not readiness.get("error"),
            "symbol_intelligence_contract": bool(symbol_intelligence.get("contract_ok")),
            "diamond_retune_status_contract": bool(diamond_retune_status.get("contract_ok")),
            "live_broker_state_contract": isinstance(live_broker_diagnostics, dict)
            and "ready" in live_broker_diagnostics
            and "broker_snapshot_source" in live_broker_diagnostics,
            "operator_queue_contract": isinstance(operator_queue, dict) and "summary" in operator_queue,
            "paper_live_transition_contract": isinstance(paper_live_transition, dict)
            and "status" in paper_live_transition,
            "dashboard_proxy_watchdog_contract": dashboard_proxy_watchdog.get("status")
            in {
                "ok",
                "missing",
                "stale",
                "probe_ok_watchdog_stale",
                "failed",
                "degraded",
                "unknown",
            },
            "command_center_watchdog_contract": command_center_watchdog.get("status")
            in {
                "healthy",
                "missing_receipt",
                "missing_watchdog",
                "stale_receipt",
                "stale_service",
                "service_unreachable",
                "public_operator_drift",
                "contract_failure",
                "secret_surface",
                "unknown",
            },
            "eta_readiness_snapshot_contract": eta_readiness_snapshot.get("status")
            in {
                "ready",
                "blocked",
                "missing_receipt",
                "stale_receipt",
                "unknown",
            },
            "vps_ops_hardening_contract": vps_ops_hardening.get("status") in _VPS_OPS_HARDENING_STATUSES,
            "hardening_contract": vps_ops_hardening.get("status") in _VPS_OPS_HARDENING_STATUSES,
            "auth_contract": "auth_session" in DASHBOARD_REQUIRED_DATA,
        },
    }


def _dashboard_cross_check_payload() -> dict:
    """Compare card-health and diagnostics card summaries as a public route."""
    server_ts = time.time()
    card_health = _dashboard_card_health_payload()
    diagnostics = _dashboard_diagnostics_payload()
    card_summary = card_health.get("summary") if isinstance(card_health.get("summary"), dict) else {}
    diagnostics_cards = diagnostics.get("cards") if isinstance(diagnostics.get("cards"), dict) else {}
    diagnostics_summary = diagnostics_cards.get("summary") if isinstance(diagnostics_cards.get("summary"), dict) else {}
    findings: list[str] = []
    for key in ("total", "dead", "stale"):
        left = int(card_summary.get(key) or 0)
        right = int(diagnostics_summary.get(key) or 0)
        if left != right:
            findings.append(f"card summary {key} mismatch: card-health={left} diagnostics={right}")
    card_dead = card_health.get("dead_cards") if isinstance(card_health.get("dead_cards"), list) else []
    diagnostics_dead = (
        diagnostics_cards.get("dead_cards") if isinstance(diagnostics_cards.get("dead_cards"), list) else []
    )
    if len(card_dead) != len(diagnostics_dead):
        findings.append(f"dead_cards length mismatch: card-health={len(card_dead)} diagnostics={len(diagnostics_dead)}")
    return {
        **_dashboard_contract(),
        "source_of_truth": "dashboard_cross_check",
        "generated_at": datetime.fromtimestamp(server_ts, UTC).isoformat(),
        "server_ts": server_ts,
        "status": "ok" if not findings else "warn",
        "findings": findings,
        "checks": {
            "route_backed": True,
            "card_summary_match": not findings,
            "no_dead_cards": int(card_summary.get("dead") or 0) == 0,
            "no_stale_cards": int(card_summary.get("stale") or 0) == 0,
        },
        "card_health": {"summary": card_summary, "dead_cards": card_dead},
        "diagnostics": {"cards": diagnostics_cards},
    }


def _live_broker_diagnostic_payload() -> dict[str, object]:
    """Fast broker truth for diagnostics without opening a broker connection."""
    try:
        cached = _cached_live_broker_state_for_diagnostics()
    except Exception as exc:  # noqa: BLE001 - diagnostics must fail soft.
        return {
            "ready": False,
            "status": "error",
            "source": "cached_live_broker_state_for_diagnostics",
            "broker_snapshot_source": "",
            "broker_snapshot_state": "",
            "broker_probe_skipped": True,
            "broker_refresh_probe_failed": False,
            "error": str(exc),
        }
    cached = cached if isinstance(cached, dict) else {}
    summary = _broker_summary_fields(cached)
    snapshot_state = str(summary.get("broker_snapshot_state") or cached.get("broker_snapshot_state") or "")
    ready = bool(cached.get("ready")) and not cached.get("error")
    status = "ready" if ready else ("stale" if snapshot_state.startswith("stale") else "unavailable")
    return {
        "ready": ready,
        "status": status,
        "source": str(cached.get("source") or "cached_live_broker_state_for_diagnostics"),
        "error": cached.get("error"),
        **summary,
    }


def _compact_close_window(window: object) -> dict[str, object]:
    """Return only operator-facing close-window totals, never full trade rows."""
    if not isinstance(window, dict):
        return {}
    pnl_map = window.get("pnl_map") if isinstance(window.get("pnl_map"), dict) else {}
    return {
        "label": window.get("label"),
        "window": window.get("window"),
        "since": window.get("since"),
        "until": window.get("until"),
        "closed_outcome_count": int(window.get("closed_outcome_count") or window.get("count") or 0),
        "evaluated_outcome_count": int(window.get("evaluated_outcome_count") or 0),
        "winning_outcomes": int(window.get("winning_outcomes") or 0),
        "losing_outcomes": int(window.get("losing_outcomes") or 0),
        "win_rate": _float_value(window.get("win_rate")),
        "realized_pnl": _float_value(window.get("realized_pnl")),
        "pnl_map": {
            "limit": int(pnl_map.get("limit") or 5),
            "top_winners": pnl_map.get("top_winners") if isinstance(pnl_map.get("top_winners"), list) else [],
            "top_losers": pnl_map.get("top_losers") if isinstance(pnl_map.get("top_losers"), list) else [],
        },
    }


def _live_broker_summary_payload(*, refresh: bool = False) -> dict[str, object]:
    """Small live-broker payload for watches that do not need full trade rows."""
    live_state = (
        _last_good_broker_state_after_failed_refresh(_live_broker_state_payload())
        if refresh
        else _cached_live_broker_state_for_diagnostics()
    )
    live_state = live_state if isinstance(live_state, dict) else {}
    close_history = live_state.get("close_history") if isinstance(live_state.get("close_history"), dict) else {}
    windows = close_history.get("windows") if isinstance(close_history.get("windows"), dict) else {}
    focus_policy = live_state.get("focus_policy") if isinstance(live_state.get("focus_policy"), dict) else {}
    return {
        "source": "live_broker_summary",
        "ready": bool(live_state.get("ready")) and not live_state.get("error"),
        "refresh_requested": bool(refresh),
        "order_action_allowed": False,
        "live_money_gate_bypassed": False,
        "reporting_timezone": str(live_state.get("reporting_timezone") or DASHBOARD_LOCAL_TIME_ZONE_NAME),
        "today_start_utc": live_state.get("today_start_utc"),
        "broker": _broker_summary_fields(live_state),
        "focus_policy": {
            "active_venues": focus_policy.get("active_venues")
            if isinstance(focus_policy.get("active_venues"), list)
            else [],
            "standby_venues": focus_policy.get("standby_venues")
            if isinstance(focus_policy.get("standby_venues"), list)
            else [],
            "dormant_venues": focus_policy.get("dormant_venues")
            if isinstance(focus_policy.get("dormant_venues"), list)
            else [],
            "paused_venues": focus_policy.get("paused_venues")
            if isinstance(focus_policy.get("paused_venues"), list)
            else [],
        },
        "close_history": {
            "source": close_history.get("source"),
            "default_window": close_history.get("default_window"),
            "timezone": close_history.get("timezone") or live_state.get("reporting_timezone"),
            "day_boundary": close_history.get("day_boundary"),
            "today": _compact_close_window(windows.get("today")),
            "mtd": _compact_close_window(windows.get("mtd")),
        },
    }


def _dashboard_data_cross_check_payload() -> dict:
    """Compare direct bot/equity endpoints with the diagnostics rollup."""
    server_ts = time.time()
    try:
        bot_fleet = bot_fleet_roster(Response(), since_days=1)
    except Exception as exc:  # noqa: BLE001 -- operator route must fail soft.
        bot_fleet = {"bots": [], "summary": {}, "confirmed_bots": 0, "_error": str(exc)}
    try:
        fleet_equity = equity_curve(range="1d", normalize=True, since_days=1, response=Response())
    except Exception as exc:  # noqa: BLE001 -- operator route must fail soft.
        fleet_equity = {"series": [], "curve": [], "source": "error", "_error": str(exc)}
    diagnostics = _dashboard_diagnostics_payload()
    fleet_rows = bot_fleet.get("bots") if isinstance(bot_fleet.get("bots"), list) else []
    fleet_summary = bot_fleet.get("summary") if isinstance(bot_fleet.get("summary"), dict) else {}
    diag_fleet = diagnostics.get("bot_fleet") if isinstance(diagnostics.get("bot_fleet"), dict) else {}
    equity_series = fleet_equity.get("series") if isinstance(fleet_equity.get("series"), list) else []
    if not equity_series:
        equity_series = fleet_equity.get("curve") if isinstance(fleet_equity.get("curve"), list) else []
    diag_equity = diagnostics.get("equity") if isinstance(diagnostics.get("equity"), dict) else {}

    direct_total = int(fleet_summary.get("bot_total") or len(fleet_rows))
    direct_confirmed = int(bot_fleet.get("confirmed_bots") or fleet_summary.get("confirmed_bots") or 0)
    direct_active = int(
        fleet_summary.get("active_bots")
        or bot_fleet.get("active_bots")
        or fleet_summary.get("runtime_active_bots")
        or 0
    )
    direct_truth = str(bot_fleet.get("truth_status") or fleet_summary.get("truth_status") or "")
    diag_total = int(diag_fleet.get("bot_total") or 0)
    diag_confirmed = int(diag_fleet.get("confirmed_bots") or 0)
    diag_active = int(diag_fleet.get("active_bots") or diag_fleet.get("runtime_active_bots") or 0)
    diag_truth = str(diag_fleet.get("truth_status") or "")
    direct_points = len(equity_series)
    diag_points = int(diag_equity.get("point_count") or 0)
    direct_equity_truth = str(fleet_equity.get("session_truth_status") or "")
    diag_equity_truth = str(diag_equity.get("session_truth_status") or "")
    direct_equity_source = str(fleet_equity.get("source") or "")
    diag_equity_source = str(diag_equity.get("source") or "")

    findings: list[str] = []
    if direct_total != diag_total:
        findings.append(f"bot_fleet total mismatch: endpoint={direct_total} diagnostics={diag_total}")
    if direct_confirmed != diag_confirmed:
        findings.append(f"bot_fleet confirmed mismatch: endpoint={direct_confirmed} diagnostics={diag_confirmed}")
    if direct_active != diag_active:
        findings.append(f"bot_fleet active mismatch: endpoint={direct_active} diagnostics={diag_active}")
    if direct_truth and diag_truth and direct_truth != diag_truth:
        findings.append(f"bot_fleet truth_status mismatch: endpoint={direct_truth!r} diagnostics={diag_truth!r}")
    if direct_points != diag_points:
        findings.append(f"equity point_count mismatch: endpoint={direct_points} diagnostics={diag_points}")
    if direct_equity_truth and diag_equity_truth and direct_equity_truth != diag_equity_truth:
        findings.append(
            f"equity session_truth_status mismatch: endpoint={direct_equity_truth!r} diagnostics={diag_equity_truth!r}"
        )
    if direct_equity_source and diag_equity_source and direct_equity_source != diag_equity_source:
        findings.append(f"equity source mismatch: endpoint={direct_equity_source!r} diagnostics={diag_equity_source!r}")

    return {
        **_dashboard_contract(),
        "source_of_truth": "dashboard_data_cross_check",
        "generated_at": datetime.fromtimestamp(server_ts, UTC).isoformat(),
        "server_ts": server_ts,
        "status": "ok" if not findings else "warn",
        "findings": findings,
        "direct": {
            "bot_fleet": {
                "bot_total": direct_total,
                "confirmed_bots": direct_confirmed,
                "active_bots": direct_active,
                "truth_status": direct_truth,
                "error": bot_fleet.get("_error"),
            },
            "equity": {
                "point_count": direct_points,
                "session_truth_status": direct_equity_truth,
                "source": direct_equity_source,
                "error": fleet_equity.get("_error"),
            },
        },
        "diagnostics": {
            "bot_fleet": diag_fleet,
            "equity": diag_equity,
        },
    }


def _state_dir() -> Path:
    """Lazy state-dir resolver so tests can monkeypatch state paths.

    Resolution order (per CLAUDE.md hard rule #1):
      1. ``$ETA_STATE_DIR`` if set (test / explicit operator override)
      2. Canonical ``<workspace>/var/eta_engine/state`` if present OR
         the legacy in-repo path is missing
      3. Legacy in-repo path only as a one-shot read fallback when
         canonical does not yet exist on disk  # HISTORICAL-PATH-OK

    Writes always target the canonical path; the fallback exists so
    state files persisted before the migration remain readable.
    """
    override = os.environ.get("ETA_STATE_DIR", "").strip()
    if override:
        return Path(override)
    if _DEFAULT_STATE.exists() or not _LEGACY_STATE.exists():
        return _DEFAULT_STATE
    return _LEGACY_STATE


def _symbol_intelligence_snapshot_path() -> Path:
    override = os.environ.get("ETA_SYMBOL_INTELLIGENCE_SNAPSHOT_PATH", "").strip()
    if override:
        return Path(override)
    return _state_dir() / "symbol_intelligence_latest.json"


def _symbol_intelligence_collector_status_path() -> Path:
    override = os.environ.get("ETA_SYMBOL_INTELLIGENCE_COLLECTOR_STATUS_PATH", "").strip()
    if override:
        return Path(override)
    return _state_dir() / "symbol_intelligence_collector_latest.json"


def _sentiment_snapshot_collector_status_path() -> Path:
    override = os.environ.get("ETA_SENTIMENT_SNAPSHOT_COLLECTOR_STATUS_PATH", "").strip()
    if override:
        return Path(override)
    return _state_dir() / "sentiment_snapshot_collector_latest.json"


def _diamond_retune_status_path() -> Path:
    override = os.environ.get("ETA_DIAMOND_RETUNE_STATUS_PATH", "").strip()
    if override:
        return Path(override)
    return _state_dir() / "diamond_retune_status_latest.json"


def _diamond_retune_status_unknown(path: Path, *, reason: str) -> dict[str, object]:
    return {
        "kind": "eta_diamond_retune_status",
        "source": reason,
        "path": str(path),
        "source_path": str(path),
        "status": "missing",
        "ready": False,
        "contract_ok": False,
        "safe_to_mutate_live": False,
        "summary": {
            "n_targets": 0,
            "n_attempted_bots": 0,
            "n_unattempted_targets": 0,
            "n_research_backlog_targets": 0,
            "n_low_sample_keep_collecting": 0,
            "n_near_miss_keep_tuning": 0,
            "n_unstable_positive_keep_tuning": 0,
            "n_research_passed_broker_proof_required": 0,
            "n_stuck_research_failing": 0,
            "n_timeout_retry": 0,
            "safe_to_mutate_live": False,
        },
        "bots": [],
        "research_backlog": [],
        "notes": ["diamond retune status has not been generated"],
    }


def _load_diamond_retune_status() -> dict[str, object]:
    path = _diamond_retune_status_path()
    payload = _read_json_file(path)
    if not payload:
        return _diamond_retune_status_unknown(path, reason="missing_snapshot")

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    bots = payload.get("bots") if isinstance(payload.get("bots"), list) else []
    research_backlog = payload.get("research_backlog") if isinstance(payload.get("research_backlog"), list) else []
    kind_ok = payload.get("kind") == "eta_diamond_retune_status"
    contract_ok = kind_ok and isinstance(summary, dict) and isinstance(bots, list)
    status = str(payload.get("status") or ("ready" if contract_ok else "invalid"))
    normalized_summary = {
        "n_targets": int(summary.get("n_targets") or len(bots)),
        "n_attempted_bots": int(summary.get("n_attempted_bots") or 0),
        "n_unattempted_targets": int(summary.get("n_unattempted_targets") or 0),
        "n_research_backlog_targets": int(summary.get("n_research_backlog_targets") or len(research_backlog)),
        "n_low_sample_keep_collecting": int(summary.get("n_low_sample_keep_collecting") or 0),
        "n_near_miss_keep_tuning": int(summary.get("n_near_miss_keep_tuning") or 0),
        "n_unstable_positive_keep_tuning": int(summary.get("n_unstable_positive_keep_tuning") or 0),
        "n_research_passed_broker_proof_required": int(
            summary.get("n_research_passed_broker_proof_required") or 0
        ),
        "n_stuck_research_failing": int(summary.get("n_stuck_research_failing") or 0),
        "n_timeout_retry": int(summary.get("n_timeout_retry") or 0),
        "safe_to_mutate_live": False,
    }
    normalized: dict[str, object] = dict(payload)
    normalized.update(
        {
            "kind": str(payload.get("kind") or "eta_diamond_retune_status"),
            "source": str(payload.get("source") or "diamond_retune_status_latest"),
            "path": str(path),
            "source_path": str(path),
            "status": status,
            "ready": contract_ok,
            "contract_ok": contract_ok,
            "safe_to_mutate_live": False,
            "writes_live_routing": False,
            "summary": normalized_summary,
            "bots": bots,
            "research_backlog": research_backlog,
        }
    )
    return normalized


def _diamond_retune_diagnostic_payload(snapshot: dict[str, Any]) -> dict[str, object]:
    path = Path(str(snapshot.get("path") or _diamond_retune_status_path()))
    mtime = _safe_mtime(path)
    summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
    bots = snapshot.get("bots") if isinstance(snapshot.get("bots"), list) else []
    first_bot = bots[0] if bots and isinstance(bots[0], dict) else {}
    return {
        "status": str(snapshot.get("status") or "missing"),
        "ready": bool(snapshot.get("ready")),
        "contract_ok": bool(snapshot.get("contract_ok")),
        "n_targets": int(summary.get("n_targets") or len(bots)),
        "n_attempted_bots": int(summary.get("n_attempted_bots") or 0),
        "n_unattempted_targets": int(summary.get("n_unattempted_targets") or 0),
        "n_research_backlog_targets": int(summary.get("n_research_backlog_targets") or 0),
        "n_low_sample_keep_collecting": int(summary.get("n_low_sample_keep_collecting") or 0),
        "n_near_miss_keep_tuning": int(summary.get("n_near_miss_keep_tuning") or 0),
        "n_unstable_positive_keep_tuning": int(summary.get("n_unstable_positive_keep_tuning") or 0),
        "n_research_passed_broker_proof_required": int(
            summary.get("n_research_passed_broker_proof_required") or 0
        ),
        "n_stuck_research_failing": int(summary.get("n_stuck_research_failing") or 0),
        "n_timeout_retry": int(summary.get("n_timeout_retry") or 0),
        "safe_to_mutate_live": bool(summary.get("safe_to_mutate_live") is True),
        "top_bot_id": str(first_bot.get("bot_id") or ""),
        "top_retune_state": str(first_bot.get("retune_state") or ""),
        "top_next_action": str(first_bot.get("next_action") or ""),
        "path": str(path),
        "source": str(snapshot.get("source") or "diamond_retune_status_latest"),
        "updated_at": datetime.fromtimestamp(mtime, UTC).isoformat() if mtime is not None else None,
        "age_s": max(0, int(time.time() - mtime)) if mtime is not None else None,
    }


def _symbol_intelligence_collector_unknown(path: Path, *, reason: str) -> dict[str, object]:
    return {
        "kind": "eta_symbol_intelligence_collector",
        "source": reason,
        "path": str(path),
        "status": "missing",
        "ready": False,
        "audit_status": "unknown",
        "news_records": 0,
        "book_records": 0,
        "sentiment_snapshot_count": 0,
        "duration_seconds": None,
        "updated_at": None,
        "age_s": None,
    }


def _load_symbol_intelligence_collector_status() -> dict[str, object]:
    path = _symbol_intelligence_collector_status_path()
    payload = _read_json_file(path)
    if not payload:
        return _symbol_intelligence_collector_unknown(path, reason="missing_snapshot")

    bootstrap = payload.get("bootstrap_counts") if isinstance(payload.get("bootstrap_counts"), dict) else {}
    audit = payload.get("audit") if isinstance(payload.get("audit"), dict) else {}
    mtime = _safe_mtime(path)
    status = str(payload.get("status") or "unknown")
    normalized: dict[str, object] = dict(payload)
    normalized.update(
        {
            "kind": str(payload.get("kind") or "eta_symbol_intelligence_collector"),
            "source": str(payload.get("source") or "symbol_intelligence_collector_latest"),
            "path": str(path),
            "status": status,
            "ready": status == "ok",
            "audit_status": str(audit.get("overall_status") or "unknown"),
            "news_records": int(bootstrap.get("news") or 0),
            "book_records": int(bootstrap.get("book") or 0),
            "sentiment_snapshot_count": int(bootstrap.get("sentiment_snapshots") or 0),
            "duration_seconds": _float_value(payload.get("duration_seconds")),
            "updated_at": datetime.fromtimestamp(mtime, UTC).isoformat() if mtime is not None else None,
            "age_s": max(0, int(time.time() - mtime)) if mtime is not None else None,
        }
    )
    return normalized


def _sentiment_snapshot_unknown(path: Path, *, reason: str) -> dict[str, object]:
    return {
        "kind": "eta_sentiment_snapshot_collector",
        "source": reason,
        "path": str(path),
        "status": "missing",
        "ready": False,
        "asset_count": 0,
        "ok_count": 0,
        "ok_assets": [],
        "sources": [],
        "active_topics": [],
        "lead_asset": "",
        "lead_social_volume_z": None,
        "asset_summaries": [],
        "macro_headlines": [],
        "lead_headlines": [],
        "pressure": unknown_pressure(),
        "updated_at": None,
        "age_s": None,
        "results": {},
    }


def _load_sentiment_snapshot_status() -> dict[str, object]:
    path = _sentiment_snapshot_collector_status_path()
    payload = _read_json_file(path)
    if not payload:
        return _sentiment_snapshot_unknown(path, reason="missing_snapshot")

    requested_assets_raw = payload.get("requested_assets") if isinstance(payload.get("requested_assets"), list) else []
    requested_assets = [str(asset).strip() for asset in requested_assets_raw if str(asset).strip()]
    results = payload.get("results") if isinstance(payload.get("results"), dict) else {}
    ok_assets: list[str] = []
    sources: list[str] = []
    active_topics: list[str] = []
    asset_summaries: list[dict[str, object]] = []
    macro_headlines: list[dict[str, object]] = []
    lead_asset = ""
    lead_social_volume_z: float | None = None
    lead_score = -1.0
    ordered_assets = [asset for asset in requested_assets if asset in results]
    ordered_assets.extend(asset for asset in results if asset not in ordered_assets)

    for asset in ordered_assets:
        row = results.get(asset)
        if not isinstance(row, dict):
            continue
        asset_name = str(asset).strip()
        if row.get("ok") is True and asset_name:
            ok_assets.append(asset_name)
        raw_source = str(row.get("raw_source") or "").strip()
        if raw_source and raw_source not in sources:
            sources.append(raw_source)
        topic_flags = row.get("topic_flags") if isinstance(row.get("topic_flags"), dict) else {}
        enabled_topics: list[str] = []
        for topic, enabled in topic_flags.items():
            topic_name = str(topic).strip()
            if not enabled or not topic_name:
                continue
            enabled_topics.append(topic_name)
            if topic_name not in active_topics:
                active_topics.append(topic_name)
        social_volume_z = _float_value(row.get("social_volume_z"))
        if social_volume_z is not None and abs(social_volume_z) > lead_score:
            lead_score = abs(social_volume_z)
            lead_asset = asset_name
            lead_social_volume_z = round(social_volume_z, 4)
        headlines_raw = row.get("headlines") if isinstance(row.get("headlines"), list) else []
        headlines: list[dict[str, object]] = []
        for item in headlines_raw[:3]:
            if not isinstance(item, dict):
                continue
            headlines.append(
                {
                    "headline": str(item.get("headline") or ""),
                    "publisher": str(item.get("publisher") or ""),
                    "published_at_utc": str(item.get("published_at_utc") or ""),
                    "url": str(item.get("url") or ""),
                }
            )
        if asset_name.lower() == "macro":
            macro_headlines = headlines
        asset_summaries.append(
            {
                "asset": asset_name,
                "ok": row.get("ok") is True,
                "source": raw_source,
                "fear_greed": _float_value(row.get("fear_greed")),
                "social_volume_z": social_volume_z,
                "headline_count": int(row.get("headline_count") or 0),
                "active_topics": enabled_topics,
                "headlines": headlines,
                "query": str(row.get("query") or ""),
            }
        )

    lead_headlines: list[dict[str, object]] = []
    for summary in asset_summaries:
        if str(summary.get("asset") or "") == lead_asset:
            lead_headlines = summary.get("headlines") if isinstance(summary.get("headlines"), list) else []
            break

    ok_count = int(payload.get("ok_count") or len(ok_assets))
    status = str(payload.get("status") or ("ok" if ok_count >= len(requested_assets) else "partial"))
    mtime = _safe_mtime(path)
    normalized: dict[str, object] = dict(payload)
    pressure = summarize_pressure(asset_summaries)
    normalized.update(
        {
            "kind": str(payload.get("kind") or "eta_sentiment_snapshot_collector"),
            "source": str(payload.get("source") or "sentiment_snapshot_collector_latest"),
            "path": str(path),
            "status": status,
            "ready": status == "ok" and ok_count >= len(requested_assets),
            "asset_count": len(requested_assets),
            "ok_count": ok_count,
            "ok_assets": ok_assets,
            "sources": sources,
            "active_topics": active_topics[:6],
            "lead_asset": lead_asset,
            "lead_social_volume_z": lead_social_volume_z,
            "asset_summaries": asset_summaries,
            "macro_headlines": macro_headlines,
            "lead_headlines": lead_headlines,
            "pressure": pressure,
            "updated_at": datetime.fromtimestamp(mtime, UTC).isoformat() if mtime is not None else None,
            "age_s": max(0, int(time.time() - mtime)) if mtime is not None else None,
            "results": results,
        }
    )
    return normalized


def _symbol_intelligence_unknown(path: Path, *, reason: str) -> dict[str, object]:
    return {
        "schema": "eta.symbol_intelligence.audit.v1",
        "kind": "eta_symbol_intelligence_audit",
        "source": reason,
        "path": str(path),
        "status": "UNKNOWN",
        "overall_status": "unknown",
        "ready": False,
        "contract_ok": False,
        "average_score_pct": None,
        "symbol_count": 0,
        "status_counts": {"green": 0, "amber": 0, "red": 0, "unknown": 0},
        "required_gap_count": 0,
        "optional_gap_count": 0,
        "optional_component_symbol_counts": {"news": 0, "book": 0},
        "news_ready_symbols": 0,
        "book_ready_symbols": 0,
        "symbols": [],
        "notes": ["symbol intelligence snapshot has not been generated"],
    }


def _symbol_intelligence_diagnostic_payload(snapshot: dict[str, Any]) -> dict[str, object]:
    path = Path(str(snapshot.get("path") or _symbol_intelligence_snapshot_path()))
    mtime = _safe_mtime(path)
    optional_counts = (
        snapshot.get("optional_component_symbol_counts")
        if isinstance(snapshot.get("optional_component_symbol_counts"), dict)
        else {}
    )
    collector = snapshot.get("collector") if isinstance(snapshot.get("collector"), dict) else {}
    sentiment = snapshot.get("sentiment") if isinstance(snapshot.get("sentiment"), dict) else {}
    news_ready_symbols = int(optional_counts.get("news") or 0)
    book_ready_symbols = int(optional_counts.get("book") or 0)
    collector_news_records = int(collector.get("news_records") or 0)
    collector_book_records = int(collector.get("book_records") or 0)
    sentiment_ok_assets = sentiment.get("ok_assets") if isinstance(sentiment.get("ok_assets"), list) else []
    sentiment_sources = sentiment.get("sources") if isinstance(sentiment.get("sources"), list) else []
    sentiment_active_topics = (
        sentiment.get("active_topics") if isinstance(sentiment.get("active_topics"), list) else []
    )
    sentiment_asset_summaries = (
        sentiment.get("asset_summaries") if isinstance(sentiment.get("asset_summaries"), list) else []
    )
    sentiment_macro_headlines = (
        sentiment.get("macro_headlines") if isinstance(sentiment.get("macro_headlines"), list) else []
    )
    sentiment_lead_headlines = (
        sentiment.get("lead_headlines") if isinstance(sentiment.get("lead_headlines"), list) else []
    )
    sentiment_pressure = sentiment.get("pressure") if isinstance(sentiment.get("pressure"), dict) else {}
    return {
        "status": str(snapshot.get("status") or "UNKNOWN").upper(),
        "ready": bool(snapshot.get("ready")),
        "contract_ok": bool(snapshot.get("contract_ok")),
        "average_score_pct": snapshot.get("average_score_pct"),
        "symbol_count": int(snapshot.get("symbol_count") or 0),
        "status_counts": snapshot.get("status_counts") if isinstance(snapshot.get("status_counts"), dict) else {},
        "required_gap_count": int(snapshot.get("required_gap_count") or 0),
        "optional_gap_count": int(snapshot.get("optional_gap_count") or 0),
        "optional_component_symbol_counts": optional_counts,
        "news_ready_symbols": news_ready_symbols,
        "book_ready_symbols": book_ready_symbols,
        "collector": {
            "status": str(collector.get("status") or "missing"),
            "ready": bool(collector.get("ready")),
            "audit_status": str(collector.get("audit_status") or "unknown"),
            "news_records": collector_news_records,
            "book_records": collector_book_records,
            "news_records_added_last_run": collector_news_records,
            "book_records_added_last_run": collector_book_records,
            "news_ready_symbols": news_ready_symbols,
            "book_ready_symbols": book_ready_symbols,
            "news_flowing": news_ready_symbols > 0,
            "book_flowing": book_ready_symbols > 0,
            "sentiment_snapshot_count": int(collector.get("sentiment_snapshot_count") or 0),
            "updated_at": collector.get("updated_at"),
            "age_s": collector.get("age_s"),
        },
        "sentiment": {
            "status": str(sentiment.get("status") or "missing"),
            "ready": bool(sentiment.get("ready")),
            "asset_count": int(sentiment.get("asset_count") or 0),
            "ok_count": int(sentiment.get("ok_count") or 0),
            "ok_assets": sentiment_ok_assets,
            "sources": sentiment_sources,
            "active_topics": sentiment_active_topics,
            "lead_asset": str(sentiment.get("lead_asset") or ""),
            "lead_social_volume_z": sentiment.get("lead_social_volume_z"),
            "asset_summaries": sentiment_asset_summaries,
            "macro_headlines": sentiment_macro_headlines,
            "lead_headlines": sentiment_lead_headlines,
            "pressure": sentiment_pressure,
            "updated_at": sentiment.get("updated_at"),
            "age_s": sentiment.get("age_s"),
        },
        "path": str(path),
        "source": str(snapshot.get("source") or "symbol_intelligence_latest"),
        "updated_at": datetime.fromtimestamp(mtime, UTC).isoformat() if mtime is not None else None,
        "age_s": max(0, int(time.time() - mtime)) if mtime is not None else None,
    }


def _load_symbol_intelligence_snapshot() -> dict[str, object]:
    path = _symbol_intelligence_snapshot_path()
    payload = _read_json_file(path)
    collector_status = _load_symbol_intelligence_collector_status()
    sentiment_status = _load_sentiment_snapshot_status()
    if not payload:
        normalized = _symbol_intelligence_unknown(path, reason="missing_snapshot")
        normalized["collector"] = collector_status
        normalized["sentiment"] = sentiment_status
        return normalized

    symbols = payload.get("symbols") if isinstance(payload.get("symbols"), list) else []
    counts = {"green": 0, "amber": 0, "red": 0, "unknown": 0}
    required_gap_count = 0
    optional_gap_count = 0
    optional_component_names_raw = payload.get("optional_components") if isinstance(payload.get("optional_components"), list) else []
    optional_component_names = [str(name).strip() for name in optional_component_names_raw if str(name).strip()]
    if not optional_component_names:
        optional_component_names = ["news", "book"]
    optional_component_symbol_counts = {name: 0 for name in optional_component_names}
    for row in symbols:
        if not isinstance(row, dict):
            counts["unknown"] += 1
            continue
        row_status = str(row.get("status") or row.get("overall_status") or "unknown").strip().lower()
        if row_status not in counts:
            row_status = "unknown"
        counts[row_status] += 1
        required = row.get("missing_required") if isinstance(row.get("missing_required"), list) else []
        optional = row.get("missing_optional") if isinstance(row.get("missing_optional"), list) else []
        optional_components = row.get("optional_components") if isinstance(row.get("optional_components"), dict) else {}
        required_gap_count += len(required)
        optional_gap_count += len(optional)
        for component_name in optional_component_symbol_counts:
            if optional_components.get(component_name) is True:
                optional_component_symbol_counts[component_name] += 1

    status = str(payload.get("status") or payload.get("overall_status") or "UNKNOWN").strip().upper()
    if status not in {"GREEN", "AMBER", "RED", "UNKNOWN"}:
        status = "UNKNOWN"
    score_raw = payload.get("average_score_pct")
    if score_raw is None:
        score_raw = payload.get("average_score")
    score = _float_value(score_raw)
    normalized: dict[str, object] = dict(payload)
    normalized.update(
        {
            "schema": str(payload.get("schema") or "eta.symbol_intelligence.audit.v1"),
            "kind": str(payload.get("kind") or "eta_symbol_intelligence_audit"),
            "source": str(payload.get("source") or "symbol_intelligence_latest"),
            "path": str(path),
            "status": status,
            "overall_status": status.lower(),
            "ready": status == "GREEN",
            "contract_ok": payload.get("schema") == "eta.symbol_intelligence.audit.v1" and isinstance(symbols, list),
            "average_score_pct": round(score, 2) if score is not None else None,
            "symbol_count": len(symbols),
            "status_counts": counts,
            "required_gap_count": required_gap_count,
            "optional_gap_count": optional_gap_count,
            "optional_component_symbol_counts": optional_component_symbol_counts,
            "news_ready_symbols": int(optional_component_symbol_counts.get("news") or 0),
            "book_ready_symbols": int(optional_component_symbol_counts.get("book") or 0),
            "collector": collector_status,
            "sentiment": sentiment_status,
            "symbols": symbols,
        }
    )
    return normalized


def _log_dir() -> Path:
    """Lazy log-dir resolver so tests can monkeypatch log paths.

    Same canonical-first / legacy-fallback resolution as
    :func:`_state_dir`.
    """
    override = os.environ.get("ETA_LOG_DIR", "").strip()
    if override:
        return Path(override)
    if _DEFAULT_LOG.exists() or not _LEGACY_LOG.exists():
        return _DEFAULT_LOG
    return _LEGACY_LOG


def _runtime_state_path() -> Path:
    """Canonical runtime heartbeat path for ETA service-level truth."""
    override = os.environ.get("ETA_RUNTIME_STATE_PATH", "").strip()
    if override:
        return Path(override)
    return _state_dir() / _DEFAULT_RUNTIME_STATE.name


def _runtime_state_candidates() -> list[Path]:
    """Runtime-state read order with canonical path first and legacy as fallback only."""
    primary = _runtime_state_path()
    candidates = [primary]
    if not os.environ.get("ETA_RUNTIME_STATE_PATH", "").strip():
        legacy = _LEGACY_RUNTIME_STATE
        if legacy != primary and not primary.exists() and legacy.exists():
            candidates.append(legacy)
    return candidates


def _bot_strategy_readiness_snapshot_path() -> Path:
    """Per-bot readiness snapshot path scoped to the active state root."""
    explicit = os.environ.get("ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH")
    if explicit:
        return Path(explicit)
    return _state_dir() / _DEFAULT_BOT_STRATEGY_READINESS_SNAPSHOT.name


def _vps_ops_hardening_audit_path() -> Path:
    """Latest read-only VPS hardening audit surfaced into diagnostics."""
    explicit = os.environ.get("ETA_VPS_OPS_HARDENING_AUDIT_PATH")
    if explicit:
        return Path(explicit)
    return _state_dir() / "vps_ops_hardening_latest.json"


def _iso_age_s(value: object, *, server_ts: float) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, server_ts - parsed.timestamp())


def _vps_ops_hardening_payload(*, server_ts: float) -> dict:
    """Fail-soft summary of VPS hardening gates for the dashboard."""
    path = _vps_ops_hardening_audit_path()
    if not path.exists():
        return {
            "status": "missing",
            "ready": False,
            "path": str(path),
            "summary": {},
            "jarvis_hermes_admin_ai": {"status": "missing", "ready": False, "next_actions": []},
            "next_actions": ["Run eta_engine.scripts.vps_ops_hardening_audit --json-out"],
            "age_s": None,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "unreadable",
            "ready": False,
            "path": str(path),
            "summary": {},
            "jarvis_hermes_admin_ai": {
                "status": "unreadable",
                "ready": False,
                "next_actions": [str(exc)],
            },
            "next_actions": [f"Refresh unreadable VPS hardening audit: {exc}"],
            "age_s": None,
        }
    if not isinstance(payload, dict):
        return {
            "status": "invalid",
            "ready": False,
            "path": str(path),
            "summary": {},
            "jarvis_hermes_admin_ai": {"status": "invalid", "ready": False, "next_actions": []},
            "next_actions": ["Refresh invalid VPS hardening audit JSON"],
            "age_s": None,
        }
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    gates = payload.get("safety_gates") if isinstance(payload.get("safety_gates"), dict) else {}
    admin_gate = gates.get("jarvis_hermes_admin_ai")
    if not isinstance(admin_gate, dict):
        admin_gate = {
            "status": str(summary.get("admin_ai_status") or "unknown"),
            "ready": bool(summary.get("admin_ai_ready")),
            "next_actions": [],
        }
    supervisor_reconcile_gate = gates.get("supervisor_reconcile")
    if not isinstance(supervisor_reconcile_gate, dict):
        supervisor_reconcile_gate = {
            "status": str(summary.get("supervisor_reconcile_status") or "unknown"),
            "ready": bool(summary.get("supervisor_reconcile_ready")),
            "broker_only_symbols": [],
            "supervisor_only_symbols": [],
            "divergent_symbols": [],
            "mismatch_count": 0,
        }
    generated_at = payload.get("generated_at_utc") or payload.get("generated_at")
    return {
        "status": str(summary.get("status") or payload.get("status") or "unknown"),
        "ready": bool(
            summary.get("runtime_ready")
            and summary.get("dashboard_durable")
            and summary.get("trading_gate_ready")
            and summary.get("admin_ai_ready")
        ),
        "path": str(path),
        "generated_at": generated_at,
        "age_s": _iso_age_s(generated_at, server_ts=server_ts),
        "summary": {
            "runtime_ready": bool(summary.get("runtime_ready")),
            "dashboard_durable": bool(summary.get("dashboard_durable")),
            "paper_live_gate_ready": bool(summary.get("paper_live_gate_ready")),
            "paper_live_status": str(summary.get("paper_live_status") or "unknown"),
            "trading_gate_ready": bool(summary.get("trading_gate_ready")),
            "prop_promotion_gate_ready": bool(summary.get("prop_promotion_gate_ready")),
            "live_promotion_blocked": bool(summary.get("live_promotion_blocked")),
            "admin_ai_ready": bool(summary.get("admin_ai_ready")),
            "admin_ai_status": str(summary.get("admin_ai_status") or "unknown"),
            "supervisor_reconcile_ready": bool(summary.get("supervisor_reconcile_ready")),
            "promotion_allowed": bool(summary.get("promotion_allowed")),
            "order_action_allowed": bool(summary.get("order_action_allowed")),
        },
        "jarvis_hermes_admin_ai": {
            "status": str(admin_gate.get("status") or "unknown"),
            "ready": bool(admin_gate.get("ready")),
            "blocked": int(admin_gate.get("blocked") or 0),
            "warned": int(admin_gate.get("warned") or 0),
            "next_actions": admin_gate.get("next_actions") if isinstance(admin_gate.get("next_actions"), list) else [],
        },
        "supervisor_reconcile": {
            "status": str(supervisor_reconcile_gate.get("status") or "unknown"),
            "ready": bool(supervisor_reconcile_gate.get("ready")),
            "source": str(supervisor_reconcile_gate.get("source") or ""),
            "checked_at": supervisor_reconcile_gate.get("checked_at"),
            "heartbeat_ts": supervisor_reconcile_gate.get("heartbeat_ts"),
            "broker_state_source": supervisor_reconcile_gate.get("broker_state_source"),
            "age_s": supervisor_reconcile_gate.get("age_s"),
            "max_age_s": supervisor_reconcile_gate.get("max_age_s"),
            "broker_only_symbols": (
                supervisor_reconcile_gate.get("broker_only_symbols")
                if isinstance(supervisor_reconcile_gate.get("broker_only_symbols"), list)
                else []
            ),
            "supervisor_only_symbols": (
                supervisor_reconcile_gate.get("supervisor_only_symbols")
                if isinstance(supervisor_reconcile_gate.get("supervisor_only_symbols"), list)
                else []
            ),
            "divergent_symbols": (
                supervisor_reconcile_gate.get("divergent_symbols")
                if isinstance(supervisor_reconcile_gate.get("divergent_symbols"), list)
                else []
            ),
            "mismatch_count": int(supervisor_reconcile_gate.get("mismatch_count") or 0),
            "brokers_queried": (
                supervisor_reconcile_gate.get("brokers_queried")
                if isinstance(supervisor_reconcile_gate.get("brokers_queried"), list)
                else []
            ),
        },
        "next_actions": payload.get("next_actions") if isinstance(payload.get("next_actions"), list) else [],
    }


def _bot_strategy_readiness_rows_by_bot() -> dict[str, dict]:
    """Load per-bot readiness rows by bot id, failing soft when the snapshot is absent."""
    path = _bot_strategy_readiness_snapshot_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        copy = dict(row)
        for key in ("bot_id", "id", "name"):
            bot_id = str(copy.get(key) or "").strip()
            if bot_id:
                out[bot_id] = copy
    return out


def _lookup_bot_strategy_readiness(
    readiness_rows: dict[str, dict],
    status: dict,
    fallback_bot_id: str,
) -> dict:
    """Find the readiness row that best matches a dashboard status row."""
    for key in (fallback_bot_id, status.get("bot_id"), status.get("id"), status.get("name")):
        if isinstance(key, str) and key in readiness_rows:
            return readiness_rows[key]
    return {}


def _apply_bot_strategy_readiness(status: dict, readiness: dict) -> dict:
    """Attach launch-lane readiness without overwriting live supervisor fields."""
    if not isinstance(status, dict):
        return {}
    existing = status.get("strategy_readiness") if isinstance(status.get("strategy_readiness"), dict) else {}
    if readiness:
        strategy_readiness = {**dict(readiness), **existing}
        status["strategy_readiness"] = strategy_readiness
    elif existing:
        strategy_readiness = existing
    else:
        return status

    if not status.get("launch_lane"):
        status["launch_lane"] = str(strategy_readiness.get("launch_lane") or "")
    if readiness or "can_paper_trade" not in status:
        status["can_paper_trade"] = bool(strategy_readiness.get("can_paper_trade"))
    if readiness or "can_live_trade" not in status:
        status["can_live_trade"] = bool(strategy_readiness.get("can_live_trade"))
    if not status.get("readiness_next_action"):
        status["readiness_next_action"] = str(
            strategy_readiness.get("next_action") or strategy_readiness.get("next_promotion_step") or "",
        )
    return status


def _readiness_only_roster_row(readiness: dict, *, now_ts: float) -> dict:
    """Build a roster-compatible row for a bot known only by the readiness snapshot."""
    bot_id = str(readiness.get("bot_id") or readiness.get("id") or readiness.get("name") or "").strip()
    row = {
        "id": bot_id,
        "bot_id": bot_id,
        "name": bot_id,
        "symbol": str(readiness.get("symbol") or ""),
        "tier": str(readiness.get("strategy_kind") or readiness.get("strategy_id") or ""),
        "venue": "readiness-snapshot",
        "status": "readiness_only",
        "todays_pnl": 0.0,
        "todays_pnl_source": "not_live",
        "last_trade_ts": None,
        "last_trade_age_s": None,
        "last_trade_side": None,
        "last_trade_r": None,
        "last_trade_qty": None,
        "last_signal_ts": None,
        "last_signal_age_s": None,
        "last_signal_side": None,
        "last_activity_ts": None,
        "last_activity_age_s": None,
        "last_activity_side": None,
        "last_activity_type": None,
        "data_ts": now_ts,
        "data_age_s": 0.0,
        "heartbeat_age_s": None,
        "source": "bot_strategy_readiness_snapshot",
        "confirmed": False,
        "mode": "readiness_snapshot",
        "last_jarvis_verdict": "",
    }
    _apply_bot_strategy_readiness(row, readiness)
    return row


def _registry_active_by_bot() -> dict[str, bool]:
    """Return per-bot registry activation truth for display filtering."""
    try:
        from eta_engine.strategies.per_bot_registry import ASSIGNMENTS, is_active
    except Exception:  # noqa: BLE001 -- dashboard must fail soft.
        return {}
    active_by_bot: dict[str, bool] = {}
    for assignment in ASSIGNMENTS:
        bot_id = str(getattr(assignment, "bot_id", "") or "").strip()
        if not bot_id:
            continue
        with contextlib.suppress(Exception):
            active_by_bot[bot_id] = bool(is_active(assignment))
    return active_by_bot


def _row_has_open_exposure(row: dict) -> bool:
    """Avoid hiding retired bots if they still have position risk attached."""
    with contextlib.suppress(TypeError, ValueError):
        if float(row.get("open_positions") or 0) > 0:
            return True
    open_position = row.get("open_position")
    if isinstance(open_position, dict) and open_position:
        return True
    position_state = row.get("position_state")
    return isinstance(position_state, dict) and str(position_state.get("state") or "").lower() == "open"


_HIDDEN_BOT_ROW_VALUES = {
    "deactivated",
    "disabled",
    "removed",
    "retired",
    "inactive",
}


def _is_hidden_bot_row(row: dict) -> bool:
    """Return True when a bot was intentionally removed from display surfaces."""
    if not isinstance(row, dict):
        return False
    readiness = row.get("strategy_readiness")
    readiness = readiness if isinstance(readiness, dict) else {}
    values = (
        row.get("status"),
        row.get("mode"),
        row.get("launch_lane"),
        row.get("promotion_status"),
        row.get("data_status"),
        readiness.get("status"),
        readiness.get("launch_lane"),
        readiness.get("promotion_status"),
        readiness.get("data_status"),
    )
    if any(str(value or "").strip().lower() in _HIDDEN_BOT_ROW_VALUES for value in values):
        return True
    if row.get("registry_deactivated") is True and not _row_has_open_exposure(row):
        return True
    # Readiness snapshots mark old/retired rows with active=false. Do not
    # hide a live supervisor row that somehow still has exposure attached.
    return readiness.get("active") is False and not _row_has_open_exposure(row)


def _is_runtime_active_bot_row(row: dict) -> bool:
    """Mirror the status-page active-bot rule for API summaries and probes."""
    if not isinstance(row, dict):
        return False
    status = str(row.get("status") or "").strip().lower()
    if not status or status in {"idle", "readiness_only", "unknown", "stale", "delayed"}:
        return False
    if status in {"running", "connected", "active", "live"}:
        return True
    return row.get("confirmed") is True or row.get("source") == "jarvis_strategy_supervisor"


def _read_runtime_state() -> dict:
    """Read runtime state without letting service diagnostics break the dashboard."""
    primary = _runtime_state_path()
    for path in _runtime_state_candidates():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            continue
        except OSError:
            continue
        except json.JSONDecodeError:
            return {"_warning": "invalid_runtime_state", "_path": str(path)}
        return data if isinstance(data, dict) else {"_warning": "invalid_runtime_state", "_path": str(path)}
    return {"_warning": "missing_runtime_state", "_path": str(primary)}


def _ibgateway_reauth_snapshot() -> dict | None:
    """Return the Gateway recovery-controller state when present."""
    env_path = os.environ.get("ETA_IBGATEWAY_REAUTH_PATH")
    candidates = [
        Path(env_path) if env_path else None,
        _state_dir() / "ibgateway_reauth.json",
        _WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "ibgateway_reauth.json",
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if not candidate.exists():
            continue
        data = _read_json_file(candidate)
        status = str(data.get("status") or "").strip()
        if not status:
            continue
        return {
            "status": status,
            "action": str(data.get("action") or ""),
            "operator_action_required": data.get("operator_action_required") is True,
            "operator_action": str(data.get("operator_action") or ""),
            "restart_attempts": int(data.get("restart_attempts") or 0),
            "last_restart_at": data.get("last_restart_at") or "",
            "last_start_at": data.get("last_start_at") or "",
            "last_task_name": data.get("last_task_name") or "",
            "generated_at_utc": data.get("generated_at_utc") or "",
            "source_path": key,
        }
    return None


def _ibgateway_install_snapshot() -> dict | None:
    """Return the Gateway installer/download audit when present."""
    env_path = os.environ.get("ETA_IBGATEWAY_INSTALL_PATH")
    candidates = [
        Path(env_path) if env_path else None,
        _state_dir() / "ibgateway_install.json",
        _WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "ibgateway_install.json",
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if not candidate.exists():
            continue
        data = _read_json_file(candidate)
        installer_path = str(data.get("installer_path") or "").strip()
        if not installer_path:
            continue
        return {
            "downloaded": data.get("downloaded") is True,
            "installed": data.get("installed") is True,
            "install_requested": data.get("install_requested") is True,
            "install_attempted": data.get("install_attempted") is True,
            "installer_path": installer_path,
            "installer_length": int(data.get("installer_length") or 0),
            "installer_sha256": str(data.get("installer_sha256") or ""),
            "authenticode_status": str(data.get("authenticode_status") or ""),
            "operator_action_required": data.get("operator_action_required") is True,
            "operator_action": str(data.get("operator_action") or ""),
            "generated_at_utc": data.get("generated_at_utc") or "",
            "source_path": key,
        }
    return None


def _ibgateway_repair_snapshot() -> dict | None:
    """Return the latest Gateway 10.46 repair/config audit when present."""
    env_path = os.environ.get("ETA_IBGATEWAY_REPAIR_PATH")
    candidates = [
        Path(env_path) if env_path else None,
        _state_dir() / "ibgateway_repair.json",
        _WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "ibgateway_repair.json",
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        data = _read_json_file(candidate)
        gateway_config = data.get("gateway_config") if isinstance(data.get("gateway_config"), dict) else {}
        if not gateway_config:
            continue
        single_source = data.get("single_source") if isinstance(data.get("single_source"), dict) else {}
        return {
            "generated_at_utc": data.get("generated_at_utc") or "",
            "jts_ini": gateway_config.get("jts_ini") or {},
            "vmoptions": gateway_config.get("vmoptions") or {},
            "single_source": {
                "gateway_task_canonical": single_source.get("gateway_task_canonical") is True,
                "port_listeners": single_source.get("port_listeners") or [],
                "non_canonical_installs": single_source.get("non_canonical_installs") or [],
            },
            "source_path": key,
        }
    return None


def _tws_watchdog_candidates() -> list[Path]:
    env_path = os.environ.get("ETA_TWS_WATCHDOG_PATH")
    return [
        Path(env_path) if env_path else None,
        _state_dir() / "tws_watchdog.json",
        _WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "tws_watchdog.json",
    ]


def _load_tws_watchdog_payload() -> tuple[dict, str] | tuple[None, None]:
    seen: set[str] = set()
    for candidate in _tws_watchdog_candidates():
        if candidate is None:
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data, key
    return None, None


def _broker_gateway_snapshot() -> dict:
    """Return broker execution gateway health, separate from bot heartbeat liveness."""
    data, key = _load_tws_watchdog_payload()
    if data is not None and key is not None:
        details = data.get("details") if isinstance(data.get("details"), dict) else {}
        crash = details.get("gateway_crash") if isinstance(details.get("gateway_crash"), dict) else None
        process = details.get("gateway_process") if isinstance(details.get("gateway_process"), dict) else None
        account_snapshot = details.get("account_snapshot") if isinstance(details.get("account_snapshot"), dict) else {}
        account_summary = account_snapshot.get("summary") if isinstance(account_snapshot.get("summary"), dict) else None
        healthy = data.get("healthy") is True
        detail = str(details.get("handshake_detail") or "")
        if process and process.get("running") and not healthy:
            process_detail = "gateway process running; API not ready"
            detail = f"{process_detail}; {detail}" if detail else process_detail
        elif process and process.get("running") is False and not healthy:
            process_detail = "gateway process not running"
            detail = f"{process_detail}; {detail}" if detail else process_detail
        config = _ibgateway_repair_snapshot()
        if config:
            jts_configured = bool((config.get("jts_ini") or {}).get("configured"))
            vm_configured = bool((config.get("vmoptions") or {}).get("configured"))
            single_source = config.get("single_source") or {}
            task_canonical = bool(single_source.get("gateway_task_canonical"))
            if jts_configured and vm_configured and task_canonical:
                verified_detail = "gateway config verified"
                detail = f"{detail}; {verified_detail}" if detail else verified_detail
        if crash and crash.get("summary"):
            detail = f"{detail}; latest crash: {crash['summary']}" if detail else str(crash["summary"])
        install = _ibgateway_install_snapshot()
        if install:
            if install.get("downloaded"):
                installer_detail = f"installer downloaded ({install.get('authenticode_status') or 'signature unknown'})"
                detail = f"{detail}; {installer_detail}" if detail else installer_detail
            if install.get("install_attempted") and not install.get("installed"):
                install_detail = "installer ran but 10.46 is not installed"
                detail = f"{detail}; {install_detail}" if detail else install_detail
            if install.get("operator_action_required"):
                detail = f"{detail}; installer action required" if detail else "installer action required"
        recovery = _ibgateway_reauth_snapshot()
        if recovery and recovery.get("status"):
            detail = f"{detail}; recovery: {recovery['status']}" if detail else f"recovery: {recovery['status']}"
            if recovery.get("operator_action_required"):
                detail = f"{detail}; operator action required"
        ibkr = {
            "status": "connected" if healthy else "down",
            "healthy": healthy,
            "checked_at": data.get("checked_at"),
            "last_healthy_at": data.get("last_healthy_at"),
            "consecutive_failures": int(data.get("consecutive_failures") or 0),
            "host": details.get("host") or "127.0.0.1",
            "port": int(details.get("port") or 4002),
            "socket_ok": details.get("socket_ok") is True,
            "handshake_ok": details.get("handshake_ok") is True,
            "detail": detail,
            "crash": crash,
            "process": process,
            "config": config,
            "install": install,
            "recovery": recovery,
            "account_summary": account_summary,
            "source_path": key,
        }
        return {
            "status": ibkr["status"],
            "healthy": ibkr["healthy"],
            "detail": ibkr["detail"],
            "checked_at": ibkr["checked_at"],
            "ibkr": ibkr,
        }
    ibkr = {
        "status": "unknown",
        "healthy": None,
        "checked_at": None,
        "last_healthy_at": None,
        "consecutive_failures": 0,
        "host": "127.0.0.1",
        "port": 4002,
        "socket_ok": None,
        "handshake_ok": None,
        "detail": "missing tws_watchdog.json",
        "crash": None,
        "process": None,
        "source_path": None,
    }
    return {
        "status": ibkr["status"],
        "healthy": ibkr["healthy"],
        "detail": ibkr["detail"],
        "checked_at": ibkr["checked_at"],
        "ibkr": ibkr,
    }


def _append_detail_once(detail: object, extra: str) -> str:
    """Append an operator detail fragment without duplicating it."""
    base = str(detail or "").strip()
    extra = str(extra or "").strip()
    if not extra:
        return base
    if extra in base:
        return base
    if not base:
        return extra
    separator = " " if base.endswith((".", "!", "?")) else "; "
    return f"{base}{separator}{extra}"


def _live_ibkr_exposure_for_gateway(live_broker_state: dict | None) -> dict:
    """Summarize live IBKR positions for Gateway card reconciliation."""
    live_broker_state = live_broker_state if isinstance(live_broker_state, dict) else {}
    ibkr = live_broker_state.get("ibkr") if isinstance(live_broker_state.get("ibkr"), dict) else {}
    positions = [
        position
        for position in _normalized_live_open_positions({"ibkr": ibkr})
        if str(position.get("venue") or "").lower() == "ibkr"
    ]
    raw_count = _float_value(ibkr.get("open_position_count"))
    count = len(positions) if positions else int(raw_count or 0)
    symbols = sorted({str(position.get("symbol") or "") for position in positions if position.get("symbol")})
    return {
        "observed": bool(ibkr),
        "open_position_count": count,
        "symbols": symbols,
        "source": "live_broker_state",
    }


def _reconcile_broker_gateway_with_live_state(
    broker_gateway: dict,
    live_broker_state: dict | None,
) -> dict:
    """Keep Gateway health detail aligned with fresher live broker exposure."""
    out = dict(broker_gateway) if isinstance(broker_gateway, dict) else {}
    ibkr = out.get("ibkr") if isinstance(out.get("ibkr"), dict) else {}
    ibkr = dict(ibkr)
    exposure = _live_ibkr_exposure_for_gateway(live_broker_state)
    if exposure.get("observed"):
        count = int(exposure.get("open_position_count") or 0)
        symbols = list(exposure.get("symbols") or [])
        ibkr["live_broker_open_position_count"] = count
        ibkr["live_broker_open_symbols"] = symbols
        ibkr["live_broker_position_source"] = exposure.get("source")
        if count > 0:
            symbol_text = f" ({', '.join(symbols[:5])})" if symbols else ""
            detail = _append_detail_once(
                ibkr.get("detail") or out.get("detail"),
                f"live broker exposure: {count} IBKR open{symbol_text}",
            )
            ibkr["detail"] = detail
            out["detail"] = detail
    out["ibkr"] = ibkr
    return out


def _read_json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _vps_root_reconciliation_plan_path() -> Path:
    """Review-only plan generated from the live VPS root dirty inventory."""
    return _state_dir() / "vps_root_reconciliation_plan.json"


def _vps_root_dirty_inventory_path() -> Path:
    """Read-only root dirty inventory; never used to mutate the workspace."""
    return _state_dir() / "vps_root_dirty_inventory.json"


def _vps_root_reconciliation_payload() -> dict[str, object]:
    """Expose the VPS root reconciliation artifacts without taking action."""

    def _as_int(value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _file_snapshot(path: Path, *, now_ts: float) -> dict[str, object]:
        mtime = _safe_mtime(path)
        if mtime is None:
            return {"updated_at": None, "age_s": None}
        return {
            "updated_at": datetime.fromtimestamp(mtime, UTC).isoformat(),
            "age_s": max(0, int(now_ts - mtime)),
        }

    plan_path = _vps_root_reconciliation_plan_path()
    inventory_path = _vps_root_dirty_inventory_path()
    plan = _read_json_file(plan_path)
    inventory = _read_json_file(inventory_path)
    now_ts = time.time()
    plan_snapshot = _file_snapshot(plan_path, now_ts=now_ts)
    inventory_snapshot = _file_snapshot(inventory_path, now_ts=now_ts)
    if not plan and not inventory:
        return {
            "status": "missing",
            "source": "missing",
            "plan_path": str(plan_path),
            "inventory_path": str(inventory_path),
            "plan_updated_at": plan_snapshot["updated_at"],
            "plan_age_s": plan_snapshot["age_s"],
            "inventory_updated_at": inventory_snapshot["updated_at"],
            "inventory_age_s": inventory_snapshot["age_s"],
            "artifact_stale": False,
            "risk_level": "unknown",
            "cleanup_allowed": False,
            "destructive_actions_performed": False,
            "counts": {},
            "summary": {},
            "steps": [],
            "recommended_action": "run inspect_vps_root_dirty.ps1 and plan_vps_root_reconciliation.ps1 on the VPS",
        }

    counts = plan.get("counts") if isinstance(plan.get("counts"), dict) else {}
    summary = plan.get("summary") if isinstance(plan.get("summary"), dict) else {}
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    if not counts and isinstance(inventory.get("counts"), dict):
        counts = inventory["counts"]
    if not summary and isinstance(inventory.get("summary"), dict):
        summary = inventory["summary"]

    risk_level = str(plan.get("risk_level") or inventory.get("risk_level") or "unknown").lower()
    cleanup_allowed = bool(plan.get("cleanup_allowed") is True)
    destructive_actions_performed = bool(plan.get("destructive_actions_performed") is True)
    source_deleted = _as_int(summary.get("source_or_governance_deleted"))
    unknown_deleted = _as_int(summary.get("unknown_deleted"))
    source_untracked = _as_int(summary.get("source_or_governance_untracked"))
    submodule_drift = _as_int(summary.get("submodule_drift") or counts.get("submodule_drift"))
    status_rows = _as_int(counts.get("status"))
    generated_untracked = _as_int(summary.get("generated_untracked") or counts.get("generated_untracked"))
    dirty_companion_repos = _as_int(summary.get("dirty_companion_repos") or counts.get("dirty_companion_repos"))
    manual_review_required = (
        risk_level in {"high", "medium"}
        or source_deleted > 0
        or unknown_deleted > 0
        or source_untracked > 0
        or submodule_drift > 0
        or status_rows > 0
        or generated_untracked > 0
        or dirty_companion_repos > 0
    )
    recommended_action = str(
        plan.get("recommended_action") or "review VPS root reconciliation plan before any root cleanup"
    )
    if (
        recommended_action == "review VPS root reconciliation plan before any root cleanup"
        and steps
        and isinstance(steps[0], dict)
    ):
        recommended_action = str(steps[0].get("action") or recommended_action)

    source_age_s = plan_snapshot["age_s"] if plan else inventory_snapshot["age_s"]
    artifact_stale = isinstance(source_age_s, int) and source_age_s > 7200
    if artifact_stale:
        status = "stale_review_required" if manual_review_required else "stale"
    else:
        status = "review_required" if manual_review_required else "ready_for_review"

    return {
        "status": status,
        "source": "vps_root_reconciliation_plan" if plan else "vps_root_dirty_inventory",
        "plan_status": plan.get("status") or "missing",
        "plan_path": str(plan_path),
        "inventory_path": str(inventory_path),
        "plan_updated_at": plan_snapshot["updated_at"],
        "plan_age_s": plan_snapshot["age_s"],
        "inventory_updated_at": inventory_snapshot["updated_at"],
        "inventory_age_s": inventory_snapshot["age_s"],
        "artifact_stale": artifact_stale,
        "risk_level": risk_level,
        "cleanup_allowed": cleanup_allowed,
        "destructive_actions_performed": destructive_actions_performed,
        "counts": counts,
        "summary": summary,
        "steps": steps,
        "recommended_action": recommended_action,
    }


def _count_matching_files(path: Path, pattern: str) -> int:
    try:
        return sum(1 for item in path.glob(pattern) if item.is_file())
    except OSError:
        return 0


def _files_newest_first(path: Path, pattern: str, *, limit: int = 100) -> list[Path]:
    try:
        files = [item for item in path.glob(pattern) if item.is_file()]
    except OSError:
        return []
    return sorted(
        files,
        key=lambda item: _safe_mtime(item) or 0.0,
        reverse=True,
    )[:limit]


def _broker_router_state_root() -> Path:
    """Locate broker-router state without falling back to legacy paths."""
    env_root = os.environ.get("ETA_BROKER_ROUTER_STATE_ROOT")
    candidates = [
        Path(env_root) if env_root else None,
        _state_dir() / "router",
        _WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "router",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            return candidate
    return candidates[0] or (_WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "router")


def _order_entry_hold_snapshot(heartbeat: dict) -> dict:
    """Return the canonical order-entry hold, with router heartbeat as a fallback."""
    hold_path = _state_dir() / "order_entry_hold.json"
    file_hold = _read_json_file(hold_path)
    if file_hold:
        hold = dict(file_hold)
        hold["active"] = bool(file_hold.get("active", True))
        hold["reason"] = str(file_hold.get("reason") or "")
        hold["source"] = "order_entry_hold_file"
        hold["path"] = str(hold_path)
        return hold

    heartbeat_hold = heartbeat.get("order_entry_hold")
    if isinstance(heartbeat_hold, dict) and heartbeat_hold:
        hold = dict(heartbeat_hold)
        hold.setdefault("source", "broker_router_heartbeat")
        return hold
    return {}


_IBKR_ROUTER_SLEEVES = frozenset(
    {
        "equity_index_futures",
        "commodities",
        "rates_fx",
        "crypto_futures",
    }
)
_BROKER_BRACKET_REQUIRED_VENUES = frozenset({"ibkr", "tasty", "tastytrade"})
_BROKER_BRACKET_REQUIRED_SEC_TYPES = frozenset({"FUT", "FOP"})
_FUTURES_AVG_COST_MULTIPLIERS = {
    "6E": 125000.0,
    "CL": 1000.0,
    "ES": 50.0,
    "GC": 100.0,
    "M2K": 5.0,
    "MBT": 0.1,
    "MCL": 100.0,
    "MES": 5.0,
    "MET": 0.1,
    "MGC": 10.0,
    "MNQ": 2.0,
    "MYM": 0.5,
    "NG": 10000.0,
    "NQ": 20.0,
    "RTY": 50.0,
    "YM": 5.0,
    "ZN": 1000.0,
}


def _router_active_order_summary(path: Path, *, lane: str) -> dict:
    """Return a small, safe summary of an active router file."""
    payload = _read_json_file(path)
    bot_id = path.name[: -len(".pending_order.json")] if path.name.endswith(".pending_order.json") else path.stem
    symbol = str(payload.get("symbol") or "")
    venue = str(payload.get("venue") or payload.get("target_venue") or payload.get("broker") or "").strip().lower()
    symbol_root = _portfolio_symbol_root(symbol)
    sleeve = _portfolio_sleeve_for_symbol(symbol)
    requires_ibkr = venue == "ibkr" or (not venue and sleeve in _IBKR_ROUTER_SLEEVES)
    return {
        "lane": lane,
        "path": str(path),
        "bot_id": bot_id,
        "signal_id": str(payload.get("signal_id") or ""),
        "symbol": symbol,
        "symbol_root": symbol_root,
        "sleeve": sleeve,
        "venue": venue or ("ibkr" if requires_ibkr else ""),
        "requires_ibkr": requires_ibkr,
    }


def _active_ibkr_router_orders(pending_dir: Path, processing_dir: Path) -> list[dict]:
    """Active pending/processing files that need the IBKR API surface."""
    orders: list[dict] = []
    for lane, directory in (("pending", pending_dir), ("processing", processing_dir)):
        for path in _files_newest_first(directory, "*.pending_order.json", limit=50):
            row = _router_active_order_summary(path, lane=lane)
            if row.get("requires_ibkr"):
                orders.append(row)
    return orders


def _broker_gateway_router_blocker(active_ibkr_orders: list[dict]) -> dict:
    """Correlate active IBKR router work with Gateway health."""
    gateway = _broker_gateway_snapshot()
    ibkr = gateway.get("ibkr") if isinstance(gateway.get("ibkr"), dict) else {}
    status = str(ibkr.get("status") or gateway.get("status") or "unknown").lower()
    recovery = ibkr.get("recovery") if isinstance(ibkr.get("recovery"), dict) else {}
    gateway_down = status == "down" or ibkr.get("healthy") is False
    active = bool(active_ibkr_orders) and gateway_down
    return {
        "active": active,
        "venue": "ibkr",
        "gateway_status": status,
        "gateway_detail": str(ibkr.get("detail") or gateway.get("detail") or ""),
        "checked_at": ibkr.get("checked_at") or gateway.get("checked_at"),
        "recovery_status": str(recovery.get("status") or ""),
        "operator_action_required": bool(recovery.get("operator_action_required") is True),
        "active_ibkr_order_count": len(active_ibkr_orders),
        "active_ibkr_orders": active_ibkr_orders[:10],
    }


def _filled_summary_from_ib_statuses(raw: dict) -> tuple[float | None, float | None]:
    statuses = raw.get("ib_statuses")
    if not isinstance(statuses, list):
        return None, None
    filled_qty = 0.0
    weighted_notional = 0.0
    for item in statuses:
        if not isinstance(item, dict):
            continue
        try:
            item_filled = float(item.get("filled") or 0.0)
            item_price = float(item.get("avg_fill_price") or 0.0)
        except (TypeError, ValueError):
            continue
        if item_filled <= 0.0:
            continue
        filled_qty += item_filled
        weighted_notional += item_filled * item_price
    if filled_qty <= 0.0:
        return None, None
    return filled_qty, weighted_notional / filled_qty


def _truthful_router_fill_fields(result: dict, raw: dict) -> tuple[object, object]:
    filled_qty = result.get("filled_qty")
    avg_price = result.get("avg_price")
    try:
        top_filled = float(filled_qty or 0.0)
    except (TypeError, ValueError):
        top_filled = 0.0
    if top_filled > 0.0:
        return filled_qty, avg_price
    raw_filled, raw_avg_price = _filled_summary_from_ib_statuses(raw)
    if raw_filled is None:
        return filled_qty, avg_price
    return raw_filled, raw_avg_price


def _normalize_router_result(path: Path, payload: dict) -> dict:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    status = result.get("status") or payload.get("status")
    raw = result.get("raw") if isinstance(result.get("raw"), dict) else {}
    reason = (
        result.get("error_message") or result.get("reason") or raw.get("error") or raw.get("note") or raw.get("detail")
    )
    filled_qty, avg_price = _truthful_router_fill_fields(result, raw)
    return {
        "signal_id": payload.get("signal_id") or request.get("client_order_id"),
        "bot_id": payload.get("bot_id") or request.get("bot_id"),
        "venue": payload.get("venue") or raw.get("venue"),
        "status": str(status).upper() if status else None,
        "order_id": result.get("order_id"),
        "filled_qty": filled_qty,
        "avg_price": avg_price,
        "ts": payload.get("ts") or payload.get("submitted_at"),
        "reason": reason,
        "source_path": str(path),
    }


def _latest_router_failure(failed_dir: Path) -> dict | None:
    for path in _files_newest_first(failed_dir, "*.retry_meta.json", limit=1):
        payload = _read_json_file(path)
        if not payload:
            continue
        return {
            "attempts": int(payload.get("attempts") or 0),
            "last_attempt_ts": payload.get("last_attempt_ts"),
            "last_reject_reason": payload.get("last_reject_reason"),
            "source_path": str(path),
        }
    return None


def _broker_router_snapshot() -> dict:
    """Return execution-router health, separate from strategy signal liveness."""
    state_root = _broker_router_state_root()
    heartbeat = _read_json_file(state_root / "broker_router_heartbeat.json")
    pending_dir_raw = (
        heartbeat.get("pending_dir") or os.environ.get("ETA_BROKER_ROUTER_PENDING_DIR") or str(state_root / "pending")
    )
    pending_dir = Path(str(pending_dir_raw))
    processing_dir = state_root / "processing"
    failed_dir = state_root / "failed"
    blocked_dir = state_root / "blocked"
    quarantine_dir = state_root / "quarantine"
    result_dir = state_root / "fill_results"

    result_status_counts: dict[str, int] = defaultdict(int)
    latest_result: dict | None = None
    for path in _files_newest_first(result_dir, "*_result.json"):
        normalized = _normalize_router_result(path, _read_json_file(path))
        status = str(normalized.get("status") or "UNKNOWN").upper()
        result_status_counts[status] += 1
        if latest_result is None:
            latest_result = normalized

    pending_count = _count_matching_files(pending_dir, "*.pending_order.json")
    processing_count = _count_matching_files(processing_dir, "*.pending_order.json")
    failed_count = _count_matching_files(failed_dir, "*.pending_order.json")
    blocked_count = _count_matching_files(blocked_dir, "*.pending_order.json")
    quarantine_count = _count_matching_files(quarantine_dir, "*.pending_order.json")
    rejected_count = int(result_status_counts.get("REJECTED", 0))
    terminal_count = sum(result_status_counts.values())
    active_blocker_count = pending_count + processing_count
    historical_reasons: list[str] = []
    if failed_count:
        historical_reasons.append("historical_failed_orders")
    if blocked_count:
        historical_reasons.append("historical_blocked_orders")
    if rejected_count:
        historical_reasons.append("historical_rejected_results")
    if quarantine_count:
        historical_reasons.append("quarantined_orders")

    degraded_reasons: list[str] = []
    hold = _order_entry_hold_snapshot(heartbeat)
    hold_active = bool(hold.get("active"))
    if hold_active:
        degraded_reasons.append("order_entry_hold")
    active_ibkr_orders = _active_ibkr_router_orders(pending_dir, processing_dir)
    gateway_blocker = _broker_gateway_router_blocker(active_ibkr_orders)
    if gateway_blocker.get("active"):
        degraded_reasons.append("ibkr_gateway_down")

    if hold_active:
        status = "held"
    elif gateway_blocker.get("active"):
        status = "blocked"
    elif pending_count or processing_count:
        status = "processing"
    elif degraded_reasons:
        status = "degraded"
    elif heartbeat:
        status = "ok" if terminal_count else "idle"
    elif state_root.exists():
        status = "idle"
    else:
        status = "unknown"

    heartbeat_ts = heartbeat.get("last_poll_ts") or heartbeat.get("ts")
    heartbeat_dt = _parse_fill_dt(heartbeat_ts)
    heartbeat_age_s = (
        max(0, int((datetime.now(UTC) - heartbeat_dt).total_seconds())) if heartbeat_dt is not None else None
    )
    counts = heartbeat.get("counts") if isinstance(heartbeat.get("counts"), dict) else {}
    events = heartbeat.get("recent_events") if isinstance(heartbeat.get("recent_events"), list) else []

    return {
        "status": status,
        "state_root": str(state_root),
        "pending_dir": str(pending_dir),
        "heartbeat_path": str(state_root / "broker_router_heartbeat.json"),
        "heartbeat_ts": heartbeat_ts,
        "heartbeat_age_s": heartbeat_age_s,
        "pending_count": pending_count,
        "processing_count": processing_count,
        "failed_count": failed_count,
        "blocked_count": blocked_count,
        "quarantine_count": quarantine_count,
        "active_blocker_count": active_blocker_count,
        "degraded_reasons": degraded_reasons,
        "historical_reasons": historical_reasons,
        "order_entry_hold": hold,
        "gateway_blocker": gateway_blocker,
        "fill_results_count": terminal_count,
        "result_status_counts": dict(sorted(result_status_counts.items())),
        "counts": counts,
        "recent_events": events[-10:],
        "latest_result": latest_result,
        "latest_failure": _latest_router_failure(failed_dir),
    }


def _iso_age_s(value: object, *, server_ts: float) -> int | None:
    dt = _parse_fill_dt(value)
    if dt is None:
        return None
    return max(0, int((datetime.fromtimestamp(server_ts, UTC) - dt).total_seconds()))


def _watchdog_policy_thresholds() -> dict[str, float]:
    """Return the same stale-position thresholds used by AutopilotWatchdog."""
    try:
        from eta_engine.obs.autopilot_watchdog import WatchdogPolicy  # noqa: PLC0415

        policy = WatchdogPolicy()
        policy.validate_ordering()
        return {
            "ack_ttl_sec": float(policy.ack_ttl_sec),
            "tighten_after_sec": float(policy.tighten_after_sec),
            "max_age_sec": float(policy.max_age_sec),
        }
    except Exception:  # noqa: BLE001 - dashboard must stay readable during partial deploys.
        return {
            "ack_ttl_sec": 1800.0,
            "tighten_after_sec": 3600.0,
            "max_age_sec": 7200.0,
        }


def _position_opened_age_s(row: dict, *, server_ts: float) -> int | None:
    state = row.get("position_state") if isinstance(row.get("position_state"), dict) else {}
    open_pos = row.get("open_position") if isinstance(row.get("open_position"), dict) else {}
    opened_at = state.get("opened_at") or open_pos.get("entry_ts") or open_pos.get("opened_at") or open_pos.get("ts")
    return _iso_age_s(opened_at, server_ts=server_ts)


def _position_watchdog_snapshot(row: dict, *, server_ts: float) -> dict:
    """Read-only projection of the stale-position watchdog SLA for one row."""
    thresholds = _watchdog_policy_thresholds()
    age_s = _position_opened_age_s(row, server_ts=server_ts)
    state = row.get("position_state") if isinstance(row.get("position_state"), dict) else {}
    open_pos = row.get("open_position") if isinstance(row.get("open_position"), dict) else {}
    stale_tightened_at = open_pos.get("stale_tighten_applied_at") or state.get("stale_tighten_applied_at")
    stale_tightened = _parse_fill_dt(stale_tightened_at) is not None
    if age_s is None:
        return {
            "status": "unknown_age",
            "level": None,
            "age_s": None,
            "seconds_to_next_action": None,
            "next_action": "surface_position_opened_at",
            "stale_tighten_applied_at": stale_tightened_at or None,
            "policy": thresholds,
        }

    ack_ttl = thresholds["ack_ttl_sec"]
    tighten_after = thresholds["tighten_after_sec"]
    max_age = thresholds["max_age_sec"]
    if age_s >= max_age:
        status = "force_flatten_due"
        level = "FORCE_FLATTEN"
        next_action = "force_flatten_position"
        seconds_to_next_action = 0
    elif age_s >= tighten_after and stale_tightened:
        status = "tightened_watch"
        level = "TIGHTEN_STOP_APPLIED"
        next_action = "continue_watch_until_force_flatten"
        seconds_to_next_action = max(0, int(max_age - age_s))
    elif age_s >= tighten_after:
        status = "tighten_stop_due"
        level = "TIGHTEN_STOP"
        next_action = "tighten_stop_or_ack"
        seconds_to_next_action = max(0, int(max_age - age_s))
    elif age_s >= ack_ttl:
        status = "require_ack"
        level = "REQUIRE_ACK"
        next_action = "operator_ack_or_review"
        seconds_to_next_action = max(0, int(tighten_after - age_s))
    else:
        status = "fresh"
        level = "ACTIVE"
        next_action = "continue_watch"
        seconds_to_next_action = max(0, int(ack_ttl - age_s))

    return {
        "status": status,
        "level": level,
        "age_s": int(age_s),
        "seconds_to_next_action": seconds_to_next_action,
        "next_action": next_action,
        "stale_tighten_applied_at": stale_tightened_at or None,
        "policy": thresholds,
    }


def _position_staleness_summary(rows: list[dict], *, server_ts: float) -> dict:
    """Aggregate open-position stale/never-close risk for operator cards."""
    thresholds = _watchdog_policy_thresholds()
    open_items: list[dict] = []
    unknown_age_count = 0
    for row in rows:
        if not isinstance(row, dict) or not _row_has_open_exposure(row):
            continue
        watchdog = _position_watchdog_snapshot(row, server_ts=server_ts)
        if watchdog["age_s"] is None:
            unknown_age_count += 1
        open_items.append(
            {
                "bot": str(row.get("name") or row.get("id") or row.get("bot_id") or ""),
                "symbol": str(row.get("symbol") or ""),
                **watchdog,
            }
        )

    force_flatten = [item for item in open_items if item.get("status") == "force_flatten_due"]
    tighten = [item for item in open_items if item.get("status") == "tighten_stop_due"]
    require_ack = [item for item in open_items if item.get("status") == "require_ack"]
    tightened_watch = [item for item in open_items if item.get("status") == "tightened_watch"]
    if force_flatten:
        status = "force_flatten_due"
    elif tighten:
        status = "tighten_stop_due"
    elif require_ack:
        status = "require_ack"
    elif tightened_watch:
        status = "tightened_watch"
    elif unknown_age_count:
        status = "unknown_age"
    elif open_items:
        status = "fresh"
    else:
        status = "flat"

    age_items = [item for item in open_items if item.get("age_s") is not None]
    oldest = max(age_items, key=lambda item: int(item["age_s"])) if age_items else None
    watched = sorted(
        open_items,
        key=lambda item: (
            -1 if item.get("age_s") is None else -int(item["age_s"]),
            str(item.get("bot") or ""),
        ),
    )
    return {
        "status": status,
        "open_position_count": len(open_items),
        "unknown_age_count": unknown_age_count,
        "require_ack_count": len(require_ack),
        "tighten_stop_due_count": len(tighten),
        "tightened_watch_count": len(tightened_watch),
        "force_flatten_due_count": len(force_flatten),
        "oldest_position": oldest,
        "watchlist": watched[:8],
        "policy": thresholds,
    }


def _signal_cadence_summary(rows: list[dict], *, server_ts: float) -> dict:
    """Explain whether supervisor signal timestamps are staggered or clustered."""
    signal_buckets: defaultdict[str, list[str]] = defaultdict(list)
    latest_dt: datetime | None = None
    latest_ts = ""
    latest_bar_dt: datetime | None = None
    latest_bar_ts = ""
    open_position_count = 0
    for row in rows:
        if float(row.get("open_positions") or 0) > 0:
            open_position_count += 1
        bar_ts = row.get("last_bar_ts")
        bar_dt = _parse_fill_dt(bar_ts)
        if bar_dt is not None and (latest_bar_dt is None or bar_dt > latest_bar_dt):
            latest_bar_dt = bar_dt
            latest_bar_ts = str(bar_ts or "")
        signal_ts = row.get("last_signal_ts")
        signal_dt = _parse_fill_dt(signal_ts)
        if signal_dt is None:
            continue
        if latest_dt is None or signal_dt > latest_dt:
            latest_dt = signal_dt
            latest_ts = str(signal_ts or "")
        bucket = signal_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        bot_name = str(row.get("name") or row.get("id") or row.get("bot_id") or row.get("symbol") or "?")
        signal_buckets[bucket].append(bot_name)

    signal_count = sum(len(names) for names in signal_buckets.values())
    unique_seconds = len(signal_buckets)
    top_second = ""
    top_bots: list[str] = []
    if signal_buckets:
        top_second, top_bots = max(signal_buckets.items(), key=lambda item: (len(item[1]), item[0]))
    synchronized_seconds = {second: names for second, names in signal_buckets.items() if len(names) > 1}
    max_same_second = len(top_bots)
    same_second_ratio = round(max_same_second / signal_count, 3) if signal_count else 0.0
    latest_signal_age_s = _iso_age_s(latest_ts, server_ts=server_ts) if latest_ts else None
    latest_bar_age_s = _iso_age_s(latest_bar_ts, server_ts=server_ts) if latest_bar_ts else None
    freshness_status = "unknown"
    if not signal_count:
        status = "no_signals"
        detail = "no signal timestamps in visible roster"
        freshness_status = "no_signal_history"
    elif max_same_second >= max(3, int(signal_count * 0.5)):
        status = "clustered"
        detail = f"{max_same_second}/{signal_count} visible signals share one second"
        freshness_status = "clustered"
    elif synchronized_seconds:
        status = "mixed"
        detail = f"{len(synchronized_seconds)} same-second cluster(s); cadence otherwise staggered"
        freshness_status = "staggered"
    else:
        status = "staggered"
        detail = f"{signal_count} visible signals across {unique_seconds} timestamp second(s)"
        freshness_status = "staggered"

    if (
        signal_count
        and latest_signal_age_s is not None
        and latest_signal_age_s > 3600
        and latest_bar_age_s is not None
        and latest_bar_age_s <= 300
        and open_position_count > 0
        and status in {"staggered", "mixed"}
    ):
        status = "watching"
        freshness_status = "watching_fresh_bars"
        detail = (
            f"{detail}; latest entry signal is {int(latest_signal_age_s)}s old, "
            f"but {open_position_count} paper position(s) are being watched "
            f"on fresh bars ({int(latest_bar_age_s)}s)"
        )

    return {
        "status": status,
        "detail": detail,
        "signal_update_count": signal_count,
        "unique_signal_seconds": unique_seconds,
        "latest_signal_ts": latest_ts or None,
        "latest_signal_age_s": latest_signal_age_s,
        "latest_bar_ts": latest_bar_ts or None,
        "latest_bar_age_s": latest_bar_age_s,
        "freshness_status": freshness_status,
        "open_position_count": open_position_count,
        "max_same_second": max_same_second,
        "same_second_ratio": same_second_ratio,
        "top_signal_second": top_second or None,
        "top_signal_bots": sorted(top_bots)[:8],
        "synchronized_signal_seconds": len(synchronized_seconds),
        "synchronized_signal_bots": sum(len(names) for names in synchronized_seconds.values()),
    }


def _supervisor_liveness_snapshot(state_dir: Path, *, server_ts: float) -> dict:
    """Return main-loop and keepalive freshness for the JARVIS supervisor."""
    supervisor_dir = state_dir / "jarvis_intel" / "supervisor"
    main_path = supervisor_dir / "heartbeat.json"
    keepalive_path = supervisor_dir / "heartbeat_keepalive.json"
    main_payload = _read_json_file(main_path)
    keepalive_payload = _read_json_file(keepalive_path)
    main_ts = main_payload.get("ts")
    keepalive_ts = keepalive_payload.get("keepalive_ts") or keepalive_payload.get("ts")
    main_age_s = _iso_age_s(main_ts, server_ts=server_ts)
    keepalive_age_s = _iso_age_s(keepalive_ts, server_ts=server_ts)
    keepalive_fresh = keepalive_age_s is not None and keepalive_age_s <= 90
    main_fresh = main_age_s is not None and main_age_s <= 300
    return {
        "main_heartbeat_path": str(main_path),
        "main_heartbeat_ts": main_ts,
        "main_heartbeat_age_s": main_age_s,
        "main_heartbeat_fresh": main_fresh,
        "keepalive_path": str(keepalive_path),
        "keepalive_ts": keepalive_ts,
        "keepalive_age_s": keepalive_age_s,
        "keepalive_fresh": keepalive_fresh,
    }


def _truth_snapshot(rows: list[dict], *, server_ts: float) -> dict:
    """Explain exactly why the roster is live, stale, stopped, or empty."""
    runtime = _read_runtime_state()
    state_dir = _state_dir()
    bots_dir = state_dir / "bots"
    supervisor_hb = state_dir / "jarvis_intel" / "supervisor" / "heartbeat.json"
    supervisor_liveness = _supervisor_liveness_snapshot(state_dir, server_ts=server_ts)
    order_entry_hold = _order_entry_hold_snapshot({})
    order_entry_hold_active = bool(order_entry_hold.get("active"))
    order_entry_hold_reason = str(order_entry_hold.get("reason") or "order_entry_hold")
    warnings: list[str] = []

    if not supervisor_hb.exists():
        warnings.append(f"missing JARVIS supervisor heartbeat: {supervisor_hb}")
    if order_entry_hold_active:
        warnings.append(f"order_entry_hold: {order_entry_hold_reason}")

    fresh_rows = [
        row for row in rows if row.get("heartbeat_age_s") is not None and float(row.get("heartbeat_age_s") or 0) <= 300
    ]
    if runtime.get("_warning") and fresh_rows:
        runtime = {
            "mode": "running",
            "detail": "fresh_supervisor_heartbeats",
            "updated_at": str(
                supervisor_liveness.get("main_heartbeat_ts") or supervisor_liveness.get("keepalive_ts") or ""
            ),
            "source": "derived_from_supervisor_heartbeats",
            "bot_count": len(fresh_rows),
            "runtime_state_path": str(_runtime_state_path()),
        }

    mode = str(runtime.get("mode") or "").strip()
    detail = str(runtime.get("detail") or "").strip()
    updated_at = str(runtime.get("updated_at") or "").strip()

    if runtime.get("_warning") and not fresh_rows:
        warnings.append(str(runtime["_warning"]))
    if (mode or detail) and not fresh_rows:
        warnings.append(f"runtime reports {mode or 'unknown'} / {detail or 'no_detail'}")
    if not bots_dir.exists() and not fresh_rows:
        warnings.append(f"missing bot status directory: {bots_dir}")
    if fresh_rows:
        status = "live"
        line = f"Live ETA truth: {len(fresh_rows)}/{len(rows)} bot heartbeat(s) are fresh."
    elif rows and supervisor_liveness["keepalive_fresh"]:
        status = "working"
        main_age = supervisor_liveness.get("main_heartbeat_age_s")
        if main_age is not None:
            line = (
                "JARVIS supervisor process is alive; main bot snapshot is "
                f"{main_age}s old while the current tick is still working."
            )
        else:
            line = "JARVIS supervisor process is alive; waiting for the first main bot snapshot."
    elif rows:
        status = "stale"
        line = f"ETA roster has {len(rows)} bot row(s), but none have a fresh heartbeat."
    elif mode == "manage_only" and detail in {"already_stopped", "stopped", "idle"}:
        status = "runtime_stopped"
        line = "ETA runtime is in manage-only mode and reports no active bot fleet."
    else:
        status = "empty"
        line = "No live ETA bot roster is publishing into the canonical state directory."

    if order_entry_hold_active:
        line = f"Paper-live execution is held: {order_entry_hold_reason}. {line}"

    return {
        "title": CANONICAL_BOT_FLEET_TITLE,
        **_dashboard_contract(),
        "legacy_dashboard_retired": True,
        "source_of_truth": str(state_dir),
        "runtime_state_path": str(_runtime_state_path()),
        "runtime": runtime,
        "runtime_mode": mode,
        "runtime_detail": detail,
        "runtime_updated_at": updated_at,
        "supervisor_liveness": supervisor_liveness,
        "truth_status": status,
        "truth_summary_line": line,
        "truth_execution_hold": order_entry_hold if order_entry_hold_active else {},
        "truth_warnings": warnings,
        "truth_checked_at": server_ts,
    }


app = FastAPI(
    title="Evolutionary Trading Algo Dashboard",
    description="Read-only state surface for the JARVIS + Avengers stack",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_dashboard_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

_REQ_COUNTS: defaultdict[str, int] = defaultdict(int)
_REQ_ERRORS: defaultdict[str, int] = defaultdict(int)
_REQ_LAT_MS: defaultdict[str, deque[float]] = defaultdict(lambda: deque(maxlen=100))

_API_PUBLIC_PATHS = frozenset(
    {
        "/api/auth/session",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/step-up",
    }
)


def _check_session_token(session_token: str | None) -> dict | None:
    if not session_token:
        return None
    from eta_engine.deploy.scripts.dashboard_auth import get_session

    return get_session(_sessions_path(), session_token)


@app.middleware("http")
async def telemetry_and_api_auth_middleware(request: Request, call_next: Callable) -> Response:
    path = request.url.path
    started = time.perf_counter()

    _mutating = request.method in {"POST", "PUT", "PATCH", "DELETE"}
    if (
        _mutating
        and path.startswith("/api/")
        and path not in _API_PUBLIC_PATHS
        and _check_session_token(request.cookies.get("session")) is None
    ):
        return JSONResponse(status_code=401, content={"detail": {"error_code": "no_session"}})

    response = None
    try:
        response = await call_next(request)
        return response
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        _REQ_COUNTS[path] += 1
        _REQ_LAT_MS[path].append(elapsed_ms)
        status = getattr(response, "status_code", 500)
        if status >= 400:
            _REQ_ERRORS[path] += 1


def _read_json(name: str) -> dict:
    path = _state_dir() / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{name} not found in {_state_dir()}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"parse error: {e}") from e


def _operator_queue_cache_payload(*, server_ts: float | None = None) -> dict | None:
    """Return cached operator-queue truth for fast diagnostics when present."""
    path = _state_dir() / "operator_queue_snapshot.json"
    if not path.exists():
        return None
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(snapshot, dict):
        return None
    queue = snapshot.get("operator_queue")
    if not isinstance(queue, dict):
        return None
    payload = dict(queue)
    generated_at = snapshot.get("generated_at")
    age_s = _iso_age_s(generated_at, server_ts=server_ts or time.time())
    payload["source"] = "operator_queue_snapshot_cache"
    payload["cache_status"] = "hit"
    payload["cache_path"] = str(path)
    payload["snapshot_status"] = str(snapshot.get("status") or "unknown")
    payload["snapshot_generated_at"] = generated_at
    payload["cache_age_s"] = age_s
    payload["cache_stale"] = age_s is None or age_s > 900
    payload.setdefault("summary", {})
    payload.setdefault("top_blockers", [])
    payload.setdefault("top_launch_blockers", [])
    if "launch_blocked_count" not in payload:
        payload["launch_blocked_count"] = int(snapshot.get("launch_blocked_count") or 0)
    return payload


def _operator_queue_payload(*, prefer_cache: bool = False, server_ts: float | None = None) -> dict:
    """Return JARVIS/operator blockers without letting status probes break the dashboard."""
    stale_cached: dict | None = None
    if prefer_cache:
        cached = _operator_queue_cache_payload(server_ts=server_ts)
        if cached is not None:
            if not cached.get("cache_stale"):
                return cached
            stale_cached = cached
    try:
        from eta_engine.scripts.jarvis_status import build_operator_queue_summary

        payload = build_operator_queue_summary()
    except Exception as exc:  # noqa: BLE001 -- dashboard should render degraded state
        if stale_cached is not None:
            fallback = dict(stale_cached)
            fallback["cache_status"] = "stale_fallback_error"
            fallback["cache_stale"] = True
            fallback["error"] = str(exc)
            return fallback
        return {
            "source": "jarvis_status",
            "error": str(exc),
            "summary": {},
            "top_blockers": [],
        }
    if isinstance(payload, dict):
        if stale_cached is not None:
            payload = dict(payload)
            payload["cache_status"] = "stale_fallback"
            payload["cache_stale"] = False
            payload["stale_cache_age_s"] = stale_cached.get("cache_age_s")
            payload["stale_cache_path"] = stale_cached.get("cache_path")
        return payload
    if stale_cached is not None:
        fallback = dict(stale_cached)
        fallback["cache_status"] = "stale_fallback_error"
        fallback["cache_stale"] = True
        fallback["error"] = "operator queue summary returned a non-object payload"
        return fallback
    return {
        "source": "jarvis_status",
        "error": "operator queue summary returned a non-object payload",
        "summary": {},
        "top_blockers": [],
    }


def _paper_live_transition_cache_payload() -> dict:
    """Return the latest paper-live transition artifact without running probes."""
    path = _state_dir() / "paper_live_transition_check.json"
    if not path.exists():
        return {
            "source": "paper_live_transition_check_cache",
            "cache_status": "missing",
            "cache_stale": True,
            "cache_path": str(path),
            "error": "paper-live transition cache missing; run python -m eta_engine.scripts.paper_live_transition_check",
            "status": "unreadable",
            "critical_ready": False,
            "launch_command": "",
            "operator_queue_blocked_count": 0,
            "operator_queue_first_blocker_op_id": None,
            "operator_queue_first_next_action": None,
            "paper_ready_bots": 0,
            "gates": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 -- dashboard should fail soft
        return {
            "source": "paper_live_transition_check_cache",
            "cache_status": "unreadable",
            "cache_stale": True,
            "cache_path": str(path),
            "error": str(exc),
            "status": "unreadable",
            "critical_ready": False,
            "launch_command": "",
            "operator_queue_blocked_count": 0,
            "operator_queue_first_blocker_op_id": None,
            "operator_queue_first_next_action": None,
            "paper_ready_bots": 0,
            "gates": [],
        }
    if not isinstance(payload, dict):
        return {
            "source": "paper_live_transition_check_cache",
            "cache_status": "unreadable",
            "cache_stale": True,
            "cache_path": str(path),
            "error": "paper-live transition cache returned a non-object payload",
            "status": "unreadable",
            "critical_ready": False,
            "launch_command": "",
            "operator_queue_blocked_count": 0,
            "operator_queue_first_blocker_op_id": None,
            "operator_queue_first_next_action": None,
            "paper_ready_bots": 0,
            "gates": [],
        }

    generated_at = payload.get("generated_at")
    source_age_s = None
    if isinstance(generated_at, str) and generated_at:
        with contextlib.suppress(ValueError):
            source_dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            source_age_s = max(0.0, datetime.now(UTC).timestamp() - source_dt.timestamp())
    payload.setdefault("source", "paper_live_transition_check_cache")
    payload["cache_status"] = "hit"
    payload["cache_path"] = str(path)
    payload["cache_stale"] = source_age_s is None or source_age_s > _PAPER_LIVE_TRANSITION_CACHE_MAX_AGE_S
    if source_age_s is not None:
        payload["source_age_s"] = round(source_age_s, 3)
    return payload


def _paper_live_transition_payload(*, refresh: bool = False) -> dict:
    """Return paper-live launch readiness without blocking the Command Center."""
    if not refresh:
        return _paper_live_transition_cache_payload()

    try:
        from eta_engine.scripts.paper_live_transition_check import build_transition_check

        payload = build_transition_check()
    except Exception as exc:  # noqa: BLE001 -- broker probes must fail soft in UI
        return {
            "source": "paper_live_transition_check",
            "error": str(exc),
            "status": "unreadable",
            "critical_ready": False,
            "launch_command": "",
            "operator_queue_blocked_count": 0,
            "operator_queue_first_blocker_op_id": None,
            "operator_queue_first_next_action": None,
            "paper_ready_bots": 0,
            "gates": [],
        }
    if isinstance(payload, dict):
        payload.setdefault("source", "paper_live_transition_check")
        return payload
    return {
        "source": "paper_live_transition_check",
        "error": "paper-live transition check returned a non-object payload",
        "status": "unreadable",
        "critical_ready": False,
        "launch_command": "",
        "operator_queue_blocked_count": 0,
        "operator_queue_first_blocker_op_id": None,
        "operator_queue_first_next_action": None,
        "paper_ready_bots": 0,
        "gates": [],
    }


def _first_failed_gate(payload: dict) -> dict:
    """Return the first failed gate from a paper-live transition payload."""
    gates = payload.get("gates")
    if not isinstance(gates, list):
        return {}
    for gate in gates:
        if isinstance(gate, dict) and gate.get("passed") is False:
            return gate
    return {}


def _bot_strategy_readiness_payload() -> dict:
    """Return bot strategy/data readiness without letting snapshot probes break the dashboard."""
    try:
        from eta_engine.scripts.jarvis_status import build_bot_strategy_readiness_summary

        payload = build_bot_strategy_readiness_summary(path=_bot_strategy_readiness_snapshot_path())
    except Exception as exc:  # noqa: BLE001 -- dashboard should render degraded state
        return {
            "source": "jarvis_status",
            "error": str(exc),
            "status": "unreadable",
            "summary": {},
            "row_count": 0,
            "rows": [],
            "rows_by_bot": {},
            "top_actions": [],
        }
    return (
        payload
        if isinstance(payload, dict)
        else {
            "source": "jarvis_status",
            "error": "bot strategy readiness summary returned a non-object payload",
            "status": "unreadable",
            "summary": {},
            "row_count": 0,
            "rows": [],
            "rows_by_bot": {},
            "top_actions": [],
        }
    )


def _bot_strategy_readiness_bot_payload(bot_id: str) -> dict:
    """Return one bot's readiness row without forcing clients to scan the full roster."""
    payload = _bot_strategy_readiness_payload()
    rows_by_bot = payload.get("rows_by_bot") if isinstance(payload.get("rows_by_bot"), dict) else {}
    if not rows_by_bot:
        rows = payload.get("rows")
        if isinstance(rows, list):
            rows_by_bot = {
                key: row
                for row in rows
                if isinstance(row, dict)
                if (key := str(row.get("bot_id") or row.get("id") or row.get("name") or "").strip())
            }
    row = rows_by_bot.get(bot_id)
    if not isinstance(row, dict):
        row = {}
    return {
        "source": payload.get("source") or "bot_strategy_readiness",
        "status": payload.get("status") or "unknown",
        "path": payload.get("path"),
        "schema_version": payload.get("schema_version"),
        "generated_at": payload.get("generated_at"),
        "summary": payload.get("summary") if isinstance(payload.get("summary"), dict) else {},
        "bot_id": bot_id,
        "found": bool(row),
        "row": row,
        "available_bots": sorted(str(key) for key in rows_by_bot),
        "launch_lane": str(row.get("launch_lane") or ""),
        "can_paper_trade": bool(row.get("can_paper_trade")),
        "can_live_trade": bool(row.get("can_live_trade")),
        "readiness_next_action": str(row.get("next_action") or row.get("next_promotion_step") or ""),
    }


def _strategy_supercharge_scorecard_payload() -> dict:
    """Return conservative strategy-supercharge targets without breaking the dashboard."""
    try:
        from eta_engine.scripts.strategy_supercharge_scorecard import build_scorecard

        payload = build_scorecard()
    except Exception as exc:  # noqa: BLE001 -- dashboard should render degraded state
        return {
            "source": "strategy_supercharge_scorecard",
            "status": "unreadable",
            "error": str(exc),
            "summary": {},
            "rows": [],
            "rows_by_bot": {},
            "next_targets": [],
            "b_later": [],
            "hold": [],
        }
    return (
        payload
        if isinstance(payload, dict)
        else {
            "source": "strategy_supercharge_scorecard",
            "status": "unreadable",
            "error": "strategy supercharge scorecard returned a non-object payload",
            "summary": {},
            "rows": [],
            "rows_by_bot": {},
            "next_targets": [],
            "b_later": [],
            "hold": [],
        }
    )


def _strategy_supercharge_manifest_payload() -> dict:
    """Return the executable A+C retest manifest without breaking the dashboard."""
    try:
        from eta_engine.scripts.strategy_supercharge_manifest import build_manifest

        payload = build_manifest()
    except Exception as exc:  # noqa: BLE001 -- dashboard should render degraded state
        return {
            "source": "strategy_supercharge_manifest",
            "status": "unreadable",
            "error": str(exc),
            "summary": {},
            "rows": [],
            "rows_by_bot": {},
            "next_batch": [],
            "b_later": [],
            "hold": [],
            "commands": [],
        }
    return (
        payload
        if isinstance(payload, dict)
        else {
            "source": "strategy_supercharge_manifest",
            "status": "unreadable",
            "error": "strategy supercharge manifest returned a non-object payload",
            "summary": {},
            "rows": [],
            "rows_by_bot": {},
            "next_batch": [],
            "b_later": [],
            "hold": [],
            "commands": [],
        }
    )


def _strategy_supercharge_results_payload() -> dict:
    """Return latest A+C retest evidence without breaking the dashboard."""
    try:
        from eta_engine.scripts.strategy_supercharge_results import build_results

        payload = build_results()
    except Exception as exc:  # noqa: BLE001 -- dashboard should render degraded state
        return {
            "source": "strategy_supercharge_results",
            "status": "unreadable",
            "error": str(exc),
            "summary": {},
            "rows": [],
            "rows_by_bot": {},
            "tested": [],
            "passed": [],
            "failed": [],
            "near_misses": [],
            "retune_queue": [],
            "pending": [],
        }
    return (
        payload
        if isinstance(payload, dict)
        else {
            "source": "strategy_supercharge_results",
            "status": "unreadable",
            "error": "strategy supercharge results returned a non-object payload",
            "summary": {},
            "rows": [],
            "rows_by_bot": {},
            "tested": [],
            "passed": [],
            "failed": [],
            "near_misses": [],
            "retune_queue": [],
            "pending": [],
        }
    )


def _append_dashboard_event(event: str, payload: dict) -> None:
    """Best-effort append to local dashboard event log."""
    row = {"ts": time.time(), "event": event, **payload}
    try:
        events_log = _state_dir() / "dashboard_events.jsonl"
        events_log.parent.mkdir(parents=True, exist_ok=True)
        with events_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Auth (Wave-7, 2026-04-27)
# ---------------------------------------------------------------------------


def _users_path() -> Path:
    """Resolve users.json path at call time so env-var monkeypatching works."""
    return Path(
        os.environ.get(
            "ETA_DASHBOARD_USERS_PATH",
            str(STATE_DIR / "auth" / "users.json"),
        )
    )


def _sessions_path() -> Path:
    """Resolve sessions.json path at call time so env-var monkeypatching works."""
    return Path(
        os.environ.get(
            "ETA_DASHBOARD_SESSIONS_PATH",
            str(STATE_DIR / "auth" / "sessions.json"),
        )
    )


class LoginRequest(BaseModel):
    username: str
    password: str


class StepUpRequest(BaseModel):
    pin: str


# ─── Login rate-limit (per (username, IP) token bucket) ─────────────
_LOGIN_WINDOW_SECONDS = 60
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_FAILURES_MAX_ENTRIES = 10_000
# {(username, client_ip): deque[float] of failed-attempt timestamps}
_LOGIN_FAILURES: dict[tuple[str, str], deque] = defaultdict(
    lambda: deque(maxlen=_LOGIN_MAX_ATTEMPTS + 1),
)


def _login_allowed(username: str, client_ip: str) -> tuple[bool, int]:
    """Check whether (username, ip) can attempt login.

    Returns (allowed, retry_after_seconds). When allowed=False, the
    caller should return 429 with Retry-After header.
    """
    key = (username, client_ip)
    now = time.time()
    fails = _LOGIN_FAILURES[key]
    # Drop entries older than the window
    while fails and fails[0] < now - _LOGIN_WINDOW_SECONDS:
        fails.popleft()
    # If this bucket emptied, drop the key entirely
    if not fails:
        _LOGIN_FAILURES.pop(key, None)
        # Also opportunistically GC the dict if it's grown large
        if len(_LOGIN_FAILURES) > _LOGIN_FAILURES_MAX_ENTRIES:
            # Drop oldest 10% (insertion-order via dict iteration)
            n_drop = len(_LOGIN_FAILURES) // 10
            for k in list(_LOGIN_FAILURES.keys())[:n_drop]:
                _LOGIN_FAILURES.pop(k, None)
        return True, 0
    if len(fails) >= _LOGIN_MAX_ATTEMPTS:
        retry_after = int(_LOGIN_WINDOW_SECONDS - (now - fails[0])) + 1
        return False, max(retry_after, 1)
    return True, 0


def _record_login_failure(username: str, client_ip: str) -> None:
    _LOGIN_FAILURES[(username, client_ip)].append(time.time())
    # On the 5th failure within window, log a warning (best-effort).
    fails = _LOGIN_FAILURES[(username, client_ip)]
    if len(fails) >= _LOGIN_MAX_ATTEMPTS:
        try:
            log_line = json.dumps(
                {
                    "ts": time.time(),
                    "level": "WARNING",
                    "event": "login_rate_limit_tripped",
                    "username": username,
                    "client_ip": client_ip,
                    "failures_in_window": len(fails),
                    "window_seconds": _LOGIN_WINDOW_SECONDS,
                }
            )
            log_path = LOG_DIR / "dashboard.jsonl"
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(log_line + "\n")
            except OSError:
                # Fall back to stderr if dashboard log isn't wired
                print(log_line, file=sys.stderr)
        except Exception:  # noqa: BLE001 -- never let logging break auth
            pass


def _reset_login_failures(username: str, client_ip: str) -> None:
    _LOGIN_FAILURES.pop((username, client_ip), None)


def require_session(session: str | None = Cookie(default=None)) -> dict:
    """FastAPI dependency: returns session row or raises 401."""
    from eta_engine.deploy.scripts.dashboard_auth import get_session

    if session is None:
        raise HTTPException(status_code=401, detail={"error_code": "no_session"})
    s = get_session(_sessions_path(), session)
    if s is None:
        raise HTTPException(status_code=401, detail={"error_code": "session_expired"})
    return s


def require_step_up(session: str | None = Cookie(default=None)) -> dict:
    """FastAPI dependency: requires fresh step-up auth."""
    from eta_engine.deploy.scripts.dashboard_auth import is_stepped_up

    s = require_session(session)
    if not is_stepped_up(_sessions_path(), session):
        raise HTTPException(status_code=403, detail={"error_code": "step_up_required"})
    return s


@app.get("/api/auth/session")
def auth_session(session: str | None = Cookie(default=None)) -> dict:
    from eta_engine.deploy.scripts.dashboard_auth import get_session, is_stepped_up

    if session is None:
        return {"authenticated": False}
    s = get_session(_sessions_path(), session)
    if s is None:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "user": s["user"],
        "stepped_up": is_stepped_up(_sessions_path(), session),
    }


@app.post("/api/auth/login")
def auth_login(req: LoginRequest, request: Request, response: Response) -> dict:
    from eta_engine.deploy.scripts.dashboard_auth import (
        create_session,
        verify_password,
    )

    client_ip = request.client.host if request.client else "unknown"

    # Rate-limit check (per (username, IP))
    allowed, retry_after = _login_allowed(req.username, client_ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={"error_code": "rate_limited"},
            headers={"Retry-After": str(retry_after)},
        )

    if not verify_password(_users_path(), req.username, req.password):
        _record_login_failure(req.username, client_ip)
        _append_dashboard_event("auth_login_failed", {"username": req.username, "client_ip": client_ip})
        raise HTTPException(status_code=401, detail={"error_code": "bad_credentials"})

    # Successful login -> reset counter
    _reset_login_failures(req.username, client_ip)

    token = create_session(_sessions_path(), user=req.username)
    secure = os.environ.get("ETA_DASHBOARD_COOKIE_SECURE", "false").strip().lower() in ("1", "true", "yes", "on", "y")
    response.set_cookie(
        key="session",
        value=token,
        path="/",
        httponly=True,
        samesite="strict",
        secure=secure,
        max_age=24 * 3600,
    )
    _append_dashboard_event("auth_login_ok", {"username": req.username, "client_ip": client_ip})
    return {"authenticated": True, "user": req.username}


@app.post("/api/auth/logout")
def auth_logout(
    response: Response,
    session: str | None = Cookie(default=None),
) -> dict:
    from eta_engine.deploy.scripts.dashboard_auth import revoke_session

    if session is not None:
        revoke_session(_sessions_path(), session)
    secure = os.environ.get("ETA_DASHBOARD_COOKIE_SECURE", "false").strip().lower() in ("1", "true", "yes", "on", "y")
    response.delete_cookie(
        key="session",
        path="/",
        httponly=True,
        samesite="strict",
        secure=secure,
    )
    _append_dashboard_event("auth_logout", {})
    return {"authenticated": False}


@app.post("/api/auth/step-up")
def auth_step_up(
    req: StepUpRequest,
    session: str | None = Cookie(default=None),
) -> dict:
    from eta_engine.deploy.scripts.dashboard_auth import mark_step_up

    if session is None:
        raise HTTPException(status_code=401, detail={"error_code": "no_session"})
    pin = os.environ.get("ETA_DASHBOARD_STEP_UP_PIN", "")
    if not pin:
        raise HTTPException(status_code=503, detail={"error_code": "step_up_not_configured"})
    if not secrets.compare_digest(req.pin, pin):
        _append_dashboard_event("auth_step_up_failed", {})
        raise HTTPException(status_code=403, detail={"error_code": "bad_pin"})
    mark_step_up(_sessions_path(), session)
    _append_dashboard_event("auth_step_up_ok", {})
    return {"stepped_up": True}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

_STATUS_PAGE = Path(__file__).resolve().parent.parent / "status_page" / "index.html"
_SCORECARD_PAGE = Path(__file__).resolve().parent.parent / "status_page" / "scorecard.html"
_PROP_PAGE = Path(__file__).resolve().parent.parent / "status_page" / "prop.html"
_SERVICE_WORKER_CLEANUP_JS = """
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil(self.registration.unregister());
});
""".strip()


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    """Serve the status page at the root URL."""
    if _STATUS_PAGE.exists():
        return HTMLResponse(
            _STATUS_PAGE.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )
    return HTMLResponse(
        "<h1>Evolutionary Trading Algo</h1><p>Status page not bundled. See /health or /api/dashboard.</p>",
    )


@app.get("/status", response_class=HTMLResponse)
def status_page() -> HTMLResponse:
    """Alias for /."""
    return root()


@app.get("/scorecard", response_class=HTMLResponse)
def scorecard_page() -> HTMLResponse:
    """Serve the firm benchmark scorecard page."""
    if _SCORECARD_PAGE.exists():
        return HTMLResponse(_SCORECARD_PAGE.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Scorecard</h1><p>Scorecard page not bundled.</p>")


@app.get("/prop", response_class=HTMLResponse)
def prop_page() -> HTMLResponse:
    """Serve the prop-firm-account dashboard.

    Operator-facing view of every registered prop account (BluSky,
    Apex, Topstep, etc.) with daily-loss headroom, trailing-DD
    headroom, profit-target progress, and recent trade activity.
    Distinct from the main dashboard, which mixes paper-testing
    and prop-routed trades.
    """
    if _PROP_PAGE.exists():
        return HTMLResponse(
            _PROP_PAGE.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )
    return HTMLResponse(
        "<h1>Prop Firm Dashboard</h1><p>Page not bundled. See /api/prop/snapshot for raw JSON.</p>",
    )


@app.get("/api/prop/snapshot")
def api_prop_snapshot_all(include_inactive: bool = False) -> dict:
    """Return snapshots for prop accounts visible on the operator dashboard.

    By default returns only ``ACTIVE_ACCOUNTS`` (paper-test +
    blusky-50K-launch). Pass ``?include_inactive=true`` to see every
    registered account — useful for ops audits and for previewing what
    the dashboard would look like once a dormant account gets
    reintroduced.

    Each snapshot includes the rule set, computed state (day PnL,
    high-water mark, open contracts), and headroom values for every
    breach rule. Sorted by severity descending so the most-stressed
    account shows first.
    """
    try:
        from eta_engine.brain.jarvis_v3 import prop_firm_guardrails
    except ImportError as exc:
        return {"error": f"prop_firm_guardrails import failed: {exc}", "accounts": []}
    snaps = prop_firm_guardrails.aggregate_status(include_inactive=include_inactive)
    return {
        "asof": datetime.now(UTC).isoformat(),
        "accounts": [s.to_dict() for s in snaps],
        "n_accounts": len(snaps),
        "include_inactive": include_inactive,
        "schema_version": 1,
    }


@app.get("/api/prop/snapshot/{account_id}")
def api_prop_snapshot_one(account_id: str) -> dict:
    """Return a single account's snapshot, or 404-ish ``{error}``
    payload if the account_id is not in the registry.
    """
    try:
        from eta_engine.brain.jarvis_v3 import prop_firm_guardrails
    except ImportError as exc:
        return {"error": f"prop_firm_guardrails import failed: {exc}"}
    snap = prop_firm_guardrails.snapshot_one(account_id)
    if snap is None:
        return {"error": f"unknown account_id: {account_id}", "account_id": account_id}
    return {
        "asof": datetime.now(UTC).isoformat(),
        "snapshot": snap.to_dict(),
        "schema_version": 1,
    }


@app.get("/api/data/status")
def api_data_status() -> dict:
    """Snapshot of the market data pipeline: catalog freshness, live signal
    coverage, capture task health.

    Three slices:
      1. ``catalog`` — read from var/eta_engine/state/data_inventory.json,
         counts STALE / FRESH / MISSING per requirement.
      2. ``live_signals`` — last-6h verdicts.jsonl per subsystem; non-zero
         means the supervisor IS receiving bars for that bot/symbol via the
         in-memory feed, regardless of whether the historical file cache is
         stale.
      3. ``capture_tasks`` — scheduled-task health (last result, next run).

    This is the operator-facing answer to "is market data flowing?" — the
    catalog and the live signals can diverge (historical files stale but
    live IBKR feed working), and the dashboard makes that distinction
    explicit so the operator doesn't have to grep two files.
    """
    out: dict = {
        "asof": datetime.now(UTC).isoformat(),
        "catalog": {},
        "live_signals": {},
        "capture_tasks": [],
        "schema_version": 1,
    }

    # 1. Catalog
    catalog_path = Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\data_inventory.json")
    if catalog_path.exists():
        try:
            inv = json.loads(catalog_path.read_text(encoding="utf-8"))
            by_dataset = inv.get("by_dataset") or {}
            summary = inv.get("summary") or {}
            # Build per-symbol freshness table for the active fleet symbols
            active_symbols = {
                "MNQ1", "M2K1", "MYM1", "MES1", "NQ1",
                "GC", "MGC", "CL", "MCL", "NG",
                "6E1", "EUR", "ZN",
                "MBT1", "MET1",
            }
            per_symbol_rows = []
            for key in sorted(by_dataset.keys()):
                # keys look like "bars:MNQ1/5m" — extract symbol
                if ":" not in key:
                    continue
                kind, rest = key.split(":", 1)
                if kind != "bars":
                    continue
                if "/" not in rest:
                    continue
                sym, tf = rest.split("/", 1)
                if sym not in active_symbols:
                    continue
                v = by_dataset.get(key) or {}
                if not isinstance(v, dict):
                    continue
                per_symbol_rows.append({
                    "key": key,
                    "symbol": sym,
                    "timeframe": tf,
                    "status": v.get("status", "?"),
                    "end": v.get("end"),
                    "rows": v.get("rows"),
                    "path": v.get("path", ""),
                })
            out["catalog"] = {
                "ts": inv.get("ts"),
                "schema_version": inv.get("schema_version"),
                "summary": summary,
                "n_datasets": len(by_dataset),
                "active_symbol_rows": per_symbol_rows,
            }
        except (OSError, ValueError) as exc:
            out["catalog"] = {"error": f"inventory read failed: {exc}"}
    else:
        out["catalog"] = {"error": "no data_inventory.json on disk"}

    # 2. Live signals (last 6h verdicts per subsystem)
    verdicts_path = Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\verdicts.jsonl")
    if verdicts_path.exists():
        try:
            from collections import defaultdict
            from datetime import timedelta as _td
            cutoff = datetime.now(UTC) - _td(hours=6)
            per_subsys: dict[str, int] = defaultdict(int)
            most_recent_ts: dict[str, str] = {}
            with verdicts_path.open(encoding="utf-8") as fh:
                for raw in fh:
                    if not raw.strip():
                        continue
                    try:
                        rec = json.loads(raw)
                    except Exception:
                        continue
                    ts_str = str(rec.get("ts", ""))
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    if ts < cutoff:
                        continue
                    subsys = rec.get("subsystem", "?")
                    per_subsys[subsys] += 1
                    if ts_str > most_recent_ts.get(subsys, ""):
                        most_recent_ts[subsys] = ts_str
            out["live_signals"] = {
                "window_hours": 6,
                "n_verdicts_total": sum(per_subsys.values()),
                "per_subsystem": [
                    {
                        "subsystem": s,
                        "n_verdicts_6h": n,
                        "most_recent_ts": most_recent_ts.get(s, ""),
                    }
                    for s, n in sorted(per_subsys.items(), key=lambda x: x[1], reverse=True)
                ],
            }
        except OSError as exc:
            out["live_signals"] = {"error": f"verdicts read failed: {exc}"}
    else:
        out["live_signals"] = {"error": "no verdicts.jsonl on disk"}

    # 3. Capture tasks — query schtasks for the data feeds
    out["capture_tasks"] = _data_capture_task_status()
    out["symbol_intelligence"] = _symbol_intelligence_diagnostic_payload(_load_symbol_intelligence_snapshot())

    return out


@app.get("/api/data/symbol-intelligence")
def api_symbol_intelligence_status(response: Response) -> dict[str, object]:
    """Latest symbol-intelligence audit snapshot for the data pipeline card."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return _load_symbol_intelligence_snapshot()


@app.get("/api/jarvis/diamond_retune_status")
def api_diamond_retune_status(response: Response) -> dict[str, object]:
    """Latest paper-only retune campaign progress for the ops dashboard."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return _load_diamond_retune_status()


def _data_capture_task_status() -> list[dict]:
    """Return Windows scheduled-task state for the data-capture tasks
    (ETA-CaptureDepth, ETA-CaptureTicks, ETA-Data-Inventory,
    VpsIbkrBbo1mCapture). Lazy import so a non-Windows test harness
    doesn't import subprocess at module load."""
    import subprocess  # noqa: PLC0415
    targets = (
        "ETA-CaptureDepth",
        "ETA-CaptureTicks",
        "ETA-Data-Inventory",
        "VpsIbkrBbo1mCapture",
    )
    out: list[dict] = []
    for name in targets:
        row: dict = {"name": name, "exists": False}
        try:
            proc = subprocess.run(
                ["schtasks", "/query", "/tn", name, "/v", "/fo", "list"],
                check=False, capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0:
                row["error"] = "task not found"
                out.append(row)
                continue
            row["exists"] = True
            for line in proc.stdout.splitlines():
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                k = k.strip()
                v = v.strip()
                if k == "Status":
                    row["status"] = v
                elif k == "Last Run Time":
                    row["last_run"] = v
                elif k == "Last Result":
                    row["last_result"] = v
                elif k == "Next Run Time":
                    row["next_run"] = v
        except Exception as exc:  # noqa: BLE001
            row["error"] = str(exc)
        out.append(row)
    return out


@app.get("/api/prop/accounts")
def api_prop_accounts(include_inactive: bool = False) -> dict:
    """Return registered account_ids + their firms.

    Honors ``ACTIVE_ACCOUNTS`` by default — the 4 dormant Apex/Topstep/
    ETF accounts stay hidden until the operator reintroduces them by
    editing the ``ACTIVE_ACCOUNTS`` set in
    ``prop_firm_guardrails.py``. Pass ``?include_inactive=true`` to
    surface every registered rule profile.
    """
    try:
        from eta_engine.brain.jarvis_v3 import prop_firm_guardrails
    except ImportError as exc:
        return {"error": f"prop_firm_guardrails import failed: {exc}", "accounts": []}
    accts = []
    for aid in prop_firm_guardrails.list_known_accounts(include_inactive=include_inactive):
        rules = prop_firm_guardrails.REGISTRY.get(aid)
        if rules is None:
            continue
        accts.append({
            "account_id": aid,
            "firm": rules.firm,
            "size": rules.size,
            "starting_balance": rules.starting_balance,
            "daily_loss_limit": rules.daily_loss_limit,
            "trailing_drawdown": rules.trailing_drawdown,
            "profit_target": rules.profit_target,
            "automation_allowed": rules.automation_allowed,
            "active": prop_firm_guardrails.is_account_active(aid),
        })
    return {
        "asof": datetime.now(UTC).isoformat(),
        "accounts": accts,
        "n_accounts": len(accts),
        "include_inactive": include_inactive,
    }


@app.get("/favicon.ico", response_model=None)
def favicon() -> FileResponse | HTMLResponse:
    fav = _STATUS_PAGE.parent / "favicon.ico"
    if fav.exists():
        return FileResponse(str(fav))
    return HTMLResponse(status_code=204)


@app.get("/service-worker.js", response_class=PlainTextResponse)
def service_worker_cleanup() -> PlainTextResponse:
    """Unregister stale service workers left by older dashboard builds."""
    return PlainTextResponse(
        _SERVICE_WORKER_CLEANUP_JS,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/theme.css", response_class=PlainTextResponse)
def serve_theme_css() -> PlainTextResponse:
    """Serve the dashboard CSS theme."""
    css = _STATUS_PAGE.parent / "theme.css"
    if not css.exists():
        return PlainTextResponse("/* theme.css missing */", media_type="text/css")
    return PlainTextResponse(
        css.read_text(encoding="utf-8"),
        media_type="text/css",
    )


@app.get("/js/{filename}", response_class=PlainTextResponse)
def serve_js_module(filename: str) -> PlainTextResponse:
    """Serve a JS module from deploy/status_page/js/. Path-traversal-safe."""
    if "/" in filename or "\\" in filename or filename.startswith(".") or "\x00" in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    # Resolve js_dir lazily so monkeypatching _STATUS_PAGE in tests works.
    js_dir = _STATUS_PAGE.parent / "js"
    js_path = js_dir / filename
    try:
        resolved = js_path.resolve()
        js_dir_resolved = js_dir.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(js_dir_resolved):
            raise HTTPException(status_code=404, detail=f"{filename} not found")
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail=f"{filename} not found") from None
    return PlainTextResponse(
        resolved.read_text(encoding="utf-8"),
        media_type="text/javascript",
    )


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics() -> PlainTextResponse:
    """Prometheus OpenMetrics endpoint. Reads the textfile written by
    PROMETHEUS_EXPORT task. Scrape with Prometheus / Grafana / UptimeKuma."""
    prom_file = STATE_DIR / "prometheus" / "avengers.prom"
    if not prom_file.exists():
        return PlainTextResponse(
            "# no metrics file yet -- PROMETHEUS_EXPORT task has not run\neta_up 0\n",
            media_type="text/plain; version=0.0.4",
        )
    return PlainTextResponse(
        prom_file.read_text(encoding="utf-8"),
        media_type="text/plain; version=0.0.4",
    )


@app.get("/health")
def health() -> dict:
    """Liveness probe."""
    state_dir = _state_dir()
    log_dir = _log_dir()
    state_writable = ensure_dir_writable(state_dir)
    return {
        "status": "ok",
        **_dashboard_contract(),
        "state_dir": str(state_dir),
        "log_dir": str(log_dir),
        "state_dir_exists": state_dir.exists(),
        "state_dir_writable": state_writable,
        "auth_store_exists": _users_path().exists(),
        "session_store_exists": _sessions_path().exists(),
        "uptime_s": round(time.time() - _START_TS, 2),
    }


@app.get("/api/heartbeat")
def heartbeat() -> dict:
    """Latest Avengers daemon heartbeat."""
    return _read_json("avengers_heartbeat.json")


@app.get("/api/dashboard")
def dashboard_payload(response: Response) -> dict:
    """Dashboard payload assembled by ROBIN every minute."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    payload = dict(read_json_safe(_state_dir() / "dashboard_payload.json"))
    payload["operator_queue"] = _operator_queue_payload()
    payload["paper_live_transition"] = _paper_live_transition_payload(refresh=False)
    payload["bot_strategy_readiness"] = _bot_strategy_readiness_payload()
    payload["symbol_intelligence"] = _symbol_intelligence_diagnostic_payload(_load_symbol_intelligence_snapshot())
    symbol_intelligence = (
        payload["symbol_intelligence"] if isinstance(payload.get("symbol_intelligence"), dict) else {}
    )
    collector = symbol_intelligence.get("collector") if isinstance(symbol_intelligence.get("collector"), dict) else {}
    sentiment = symbol_intelligence.get("sentiment") if isinstance(symbol_intelligence.get("sentiment"), dict) else {}
    payload["symbol_intelligence_status"] = str(symbol_intelligence.get("status") or "UNKNOWN")
    payload["symbol_intelligence_required_gap_count"] = int(symbol_intelligence.get("required_gap_count") or 0)
    payload["symbol_intelligence_optional_gap_count"] = int(symbol_intelligence.get("optional_gap_count") or 0)
    payload["news_ready_symbols"] = int(symbol_intelligence.get("news_ready_symbols") or 0)
    payload["book_ready_symbols"] = int(symbol_intelligence.get("book_ready_symbols") or 0)
    payload["symbol_intelligence_news_flowing"] = bool(collector.get("news_flowing"))
    payload["symbol_intelligence_book_flowing"] = bool(collector.get("book_flowing"))
    payload["sentiment_asset_count"] = int(sentiment.get("asset_count") or 0)
    payload["sentiment_ok_count"] = int(sentiment.get("ok_count") or 0)
    payload["sentiment_active_topics"] = [str(topic) for topic in sentiment.get("active_topics") or []]
    payload["sentiment_lead_asset"] = str(sentiment.get("lead_asset") or "")
    payload["sentiment_lead_social_volume_z"] = sentiment.get("lead_social_volume_z")
    payload["sentiment_assets"] = sentiment.get("asset_summaries") if isinstance(sentiment.get("asset_summaries"), list) else []
    payload["sentiment_macro_headlines"] = (
        sentiment.get("macro_headlines") if isinstance(sentiment.get("macro_headlines"), list) else []
    )
    payload["sentiment_lead_headlines"] = (
        sentiment.get("lead_headlines") if isinstance(sentiment.get("lead_headlines"), list) else []
    )
    payload["sentiment_pressure"] = sentiment.get("pressure") if isinstance(sentiment.get("pressure"), dict) else {}
    pressure = payload["sentiment_pressure"] if isinstance(payload.get("sentiment_pressure"), dict) else {}
    payload["sentiment_pressure_status"] = str(pressure.get("status") or "unknown")
    payload["sentiment_pressure_score"] = pressure.get("score")
    payload["sentiment_pressure_summary"] = str(pressure.get("summary_line") or "")
    payload["diamond_retune_status"] = _load_diamond_retune_status()
    # Additive: cached broker reality for first paint. Fresh IBKR probes can
    # stall when Gateway is wedged, so /api/dashboard must not block on them.
    # A degraded cached read must never tank the front-page bootstrap.
    try:
        payload["live_broker_state"] = _cached_live_broker_state_for_diagnostics()
    except Exception as exc:  # noqa: BLE001
        payload["live_broker_state"] = {
            "ready": False,
            "error": f"live_broker_state_failed: {exc}",
            "probe_skipped": True,
            "broker_snapshot_source": "cached_live_broker_state_failed",
            "today_actual_fills": 0,
            "today_realized_pnl": 0.0,
            "total_unrealized_pnl": 0.0,
            "open_position_count": 0,
            "win_rate_30d": None,
            "server_ts": time.time(),
        }
    return payload


@app.get("/api/dashboard/card-health")
def dashboard_card_health(response: Response) -> dict:
    """V1 rendered-card source contract for dead/stale card detection."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return _dashboard_card_health_payload()


@app.get("/api/dashboard/diagnostics")
def dashboard_diagnostics(response: Response) -> dict:
    """V1 live-source diagnostic rollup for the Command Center."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return _dashboard_diagnostics_payload()


@app.get("/api/dashboard/cross-check")
def dashboard_cross_check(response: Response) -> dict:
    """V1 route-backed card-health vs diagnostics consistency check."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return _dashboard_cross_check_payload()


@app.get("/api/dashboard/data-cross-check")
def dashboard_data_cross_check(response: Response) -> dict:
    """V1 route-backed direct endpoint vs diagnostics data consistency check."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return _dashboard_data_cross_check_payload()


@app.get("/api/jarvis/operator_queue")
def jarvis_operator_queue(response: Response) -> dict:
    """Current operator blockers, prioritized for dashboard rendering."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return _operator_queue_payload()


@app.get("/api/jarvis/paper_live_transition")
def jarvis_paper_live_transition(response: Response, refresh: bool = False) -> dict:
    """Current paper-live transition verdict for dashboard rendering."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return _paper_live_transition_payload(refresh=refresh)


@app.get("/api/jarvis/bot_strategy_readiness")
def jarvis_bot_strategy_readiness(response: Response) -> dict:
    """Current bot strategy/data readiness snapshot for dashboard rendering."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return _bot_strategy_readiness_payload()


@app.get("/api/jarvis/bot_strategy_readiness/{bot_id}")
def jarvis_bot_strategy_readiness_bot(bot_id: str, response: Response) -> dict:
    """Current strategy/data readiness for a single bot id."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return _bot_strategy_readiness_bot_payload(bot_id)


@app.get("/api/jarvis/strategy_supercharge_scorecard")
def jarvis_strategy_supercharge_scorecard(response: Response) -> dict:
    """Current conservative strategy-supercharge target scorecard."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return _strategy_supercharge_scorecard_payload()


@app.get("/api/jarvis/strategy_supercharge_manifest")
def jarvis_strategy_supercharge_manifest(response: Response) -> dict:
    """Current executable A+C strategy-supercharge retest manifest."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return _strategy_supercharge_manifest_payload()


@app.get("/api/jarvis/strategy_supercharge_results")
def jarvis_strategy_supercharge_results(response: Response) -> dict:
    """Current A+C strategy-supercharge retest evidence."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return _strategy_supercharge_results_payload()


@app.get("/api/last-task")
def last_task() -> dict:
    """Result of the most recent BackgroundTask invocation."""
    return _read_json("last_task.json")


# ─── JARVIS verdict-stream panel (Tier-2 #6, 2026-04-27) ────────────
@app.get("/api/jarvis/today_verdicts")
def jarvis_today_verdicts() -> dict:
    """Aggregated JARVIS audit records for today.

    Powers the dashboard panel showing by-bot verdict counts, top denial
    reasons, average size_cap_mult on CONDITIONAL verdicts, hourly
    timeline, and the policy versions seen today.

    Lazy-imports the aggregator so this endpoint stays alive even if
    eta_engine.obs.jarvis_today_verdicts is broken at import time.
    """
    try:
        from eta_engine.obs.jarvis_today_verdicts import aggregate_today

        return aggregate_today()
    except ImportError as exc:
        return {
            "ts": None,
            "error_code": "eta_engine_unavailable",
            "error_detail": str(exc),
            "totals": {},
            "by_subsystem": {},
            "top_denial_reasons": [],
            "avg_conditional_cap": 1.0,
            "hourly_timeline": [],
            "policy_versions_seen": [],
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"aggregate failed: {exc}") from exc


@app.get("/api/jarvis/health")
def jarvis_router_health() -> dict:
    """Liveness and authority probe for JARVIS/Sage/Quantum."""
    issues = []
    sage_snapshot = {}
    try:
        from eta_engine.brain.jarvis_v3.sage.health import default_monitor

        monitor = default_monitor()
        issues = [issue.__dict__ for issue in monitor.check_health()]
        sage_snapshot = monitor.snapshot()
    except Exception as exc:  # noqa: BLE001
        issues.append(
            {
                "school": "sage_health",
                "neutral_rate": 0.0,
                "n_consultations": 0,
                "severity": "warn",
                "detail": f"sage health monitor unavailable: {exc}",
            }
        )

    quantum_jobs = []
    for raw in read_jsonl_tail(_state_dir() / "quantum" / "jobs.jsonl", limit=25):
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            quantum_jobs.append(row)
    quantum_last = quantum_jobs[0] if quantum_jobs else {}
    quantum_fallbacks = sum(1 for row in quantum_jobs if row.get("fell_back_to_classical"))
    quantum_cost = sum(float(row.get("cost_estimate_usd") or 0.0) for row in quantum_jobs)
    try:
        from eta_engine.brain.feature_flags import ETA_FLAGS

        flags = ETA_FLAGS.snapshot()
    except Exception:  # noqa: BLE001
        flags = {}

    return {
        "status": "ok" if not any(i.get("severity") == "critical" for i in issues) else "degraded",
        "router": "jarvis",
        "policy_authority": "JARVIS",
        "sage": {
            "issues": issues,
            "schools": sage_snapshot,
            "n_schools_observed": len(sage_snapshot),
        },
        "issues": issues,
        "quantum": {
            "status": "idle" if not quantum_jobs else "observed",
            "last_job": quantum_last,
            "recent_jobs": len(quantum_jobs),
            "recent_fallbacks": quantum_fallbacks,
            "recent_cost_estimate_usd": round(quantum_cost, 4),
        },
        "flags": {
            "ONLINE_LEARNING": flags.get("ONLINE_LEARNING", False),
            "V22_SAGE_MODULATION": flags.get("V22_SAGE_MODULATION", False),
            "JARVIS_V3_FLEET_AWARE": flags.get("JARVIS_V3_FLEET_AWARE", False),
            "JARVIS_V3_ADVANCED": flags.get("JARVIS_V3_ADVANCED", False),
            "BANDIT_LIVE_ROUTING": flags.get("BANDIT_LIVE_ROUTING", False),
        },
    }


@app.get("/api/jarvis/sharpe_drift")
def jarvis_sharpe_drift() -> dict:
    """Live-vs-lab sharpe drift table (v27 input surface).

    For each active bot, compares the registry's ``lab_audit_*`` exp_R
    stamp against live-realized PnL/n_exits from the supervisor heartbeat.
    Surfaces any bot where live performance has drifted from lab claim
    BEFORE the v27 layer has to size-cap automatically.

    Returns:
      {
        "rows": [{ bot_id, lab_exp_r, live_per_exit, ratio, n_exits,
                   instrument_class, drift_state }],
        "drifted_count": int,
        "ts": iso8601,
      }
    """
    rows = []
    drifted = 0
    healthy = 0
    insufficient = 0
    untested = 0

    # Read supervisor heartbeat for live state
    sup_hb_path = _state_dir() / "jarvis_intel" / "supervisor" / "heartbeat.json"
    bots_state: list[dict] = []
    if sup_hb_path.exists():
        try:
            sup_hb = json.loads(sup_hb_path.read_text(encoding="utf-8"))
            if isinstance(sup_hb, dict):
                bots_state = sup_hb.get("bots") or []
        except (OSError, json.JSONDecodeError):
            bots_state = []

    # Cross-ref against registry for lab_audit
    try:
        from eta_engine.brain.jarvis_v3.policies.v23_fleet_aware import (
            _INSTRUMENT_CLASS_TO_BROAD,
        )
        from eta_engine.strategies.per_bot_registry import ASSIGNMENTS, is_active
    except Exception:  # noqa: BLE001
        return {"rows": [], "drifted_count": 0, "ts": datetime.now(UTC).isoformat(), "error": "registry import failed"}

    for a in ASSIGNMENTS:
        if not is_active(a):
            continue
        # Find lab_audit stamp
        lab_exp = None
        lab_sharpe = None
        for k, v in (a.extras or {}).items():
            if isinstance(v, dict) and (k.startswith("lab_audit_") or k.startswith("lab_promotion_")):
                lab_exp = v.get("exp_R") if v.get("exp_R") is not None else lab_exp
                lab_sharpe = v.get("sharpe") if v.get("sharpe") is not None else lab_sharpe
        # Find live state
        live = next(
            (bs for bs in bots_state if isinstance(bs, dict) and bs.get("bot_id") == a.bot_id),
            None,
        )
        n_exits = int(live.get("n_exits") or 0) if live else 0
        realized_pnl = float(live.get("realized_pnl") or 0.0) if live else 0.0
        live_per_exit = (realized_pnl / n_exits) if n_exits > 0 else 0.0
        # Determine drift state
        if lab_exp is None:
            state = "untested"
            untested += 1
            ratio = None
        elif n_exits < 10:
            state = "insufficient_sample"
            insufficient += 1
            ratio = None
        else:
            try:
                ratio = live_per_exit / max(float(lab_exp), 1e-9) if float(lab_exp) > 0 else None
            except (TypeError, ValueError, ZeroDivisionError):
                ratio = None
            if ratio is None:
                state = "untested"
                untested += 1
            elif ratio < 0.30:
                state = "drifted"
                drifted += 1
            elif ratio < 0.70:
                state = "soft_drift"
                healthy += 1
            else:
                state = "healthy"
                healthy += 1

        cls = _INSTRUMENT_CLASS_TO_BROAD.get(str((a.extras or {}).get("instrument_class", "")).strip().lower(), "")
        rows.append(
            {
                "bot_id": a.bot_id,
                "symbol": a.symbol,
                "instrument_class": cls,
                "promotion_status": (a.extras or {}).get("promotion_status", ""),
                "lab_exp_r": lab_exp,
                "lab_sharpe": lab_sharpe,
                "live_per_exit": round(live_per_exit, 4),
                "n_exits": n_exits,
                "realized_pnl": round(realized_pnl, 2),
                "ratio": round(ratio, 3) if ratio is not None else None,
                "drift_state": state,
            }
        )

    rows.sort(key=lambda r: (r["drift_state"] != "drifted", -(r.get("n_exits") or 0)))
    return {
        "rows": rows,
        "summary": {
            "drifted": drifted,
            "soft_drift": sum(1 for r in rows if r["drift_state"] == "soft_drift"),
            "healthy": sum(1 for r in rows if r["drift_state"] == "healthy"),
            "insufficient_sample": insufficient,
            "untested": untested,
        },
        "drifted_count": drifted,
        "ts": datetime.now(UTC).isoformat(),
    }


# ─── Sage explain (Wave-6 #3, 2026-04-27) ──────────────────────────
@app.get("/api/jarvis/sage_explain")
def sage_explain_endpoint(symbol: str = "MNQ", side: str = "long") -> dict:
    ...
    try:
        bars_file = _state_dir() / "raw_state" / f"{symbol}_bars.json"
        bars: list = []
        if bars_file and bars_file.exists():
            import json as _json

            bars = _json.loads(bars_file.read_text(encoding="utf-8"))
        if not bars or len(bars) < 30:
            return {
                "symbol": symbol,
                "side": side,
                "narrative": f"No recent bars for {symbol} (need >= 30). "
                f"Drop bars at state/raw_state/{symbol}_bars.json.",
                "status": "no_bars",
            }
        from eta_engine.brain.jarvis_v3.sage import MarketContext, consult_sage
        from eta_engine.brain.jarvis_v3.sage.narrative import explain_sage

        ctx = MarketContext(bars=bars[-200:], side=side, symbol=symbol)
        report = consult_sage(ctx, parallel=True, use_cache=True)
        last_ts = bars[-1].get("ts") or bars[-1].get("timestamp") or ""
        narrative = explain_sage(report, symbol=symbol, bar_ts_key=str(last_ts))
        return {
            "symbol": symbol,
            "side": side,
            "narrative": narrative,
            "summary_line": report.summary_line(),
            "composite_bias": report.composite_bias.value,
            "conviction": report.conviction,
            "alignment_score": report.alignment_score,
            "schools_consulted": report.schools_consulted,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error_code": "sage_explain_failed", "error_detail": str(exc)}


# ─── Sage timeline (Wave-6 #4, 2026-04-27) ─────────────────────────
@app.get("/api/jarvis/sage_timeline")
def sage_timeline_endpoint(symbol: str = "MNQ", hours: int = 24, side: str = "long") -> dict:
    """Per-bar sage report (composite + conviction + alignment) over
    the last ``hours`` of bars. Used by the dashboard timeline panel.

    Returns ``{ts, composite_bias, conviction, alignment_score}`` array.
    """
    try:
        from pathlib import Path

        bars_file = Path(STATE_DIR) / "raw_state" / f"{symbol}_bars.json" if "STATE_DIR" in globals() else None
        bars: list = []
        if bars_file and bars_file.exists():
            import json as _json

            bars = _json.loads(bars_file.read_text(encoding="utf-8"))
        if not bars or len(bars) < 60:
            return {"symbol": symbol, "timeline": [], "status": "no_bars"}

        from eta_engine.brain.jarvis_v3.sage import MarketContext, consult_sage
        from eta_engine.brain.jarvis_v3.sage.consultation import clear_sage_cache

        clear_sage_cache()  # don't get same-key collisions across bars

        # Sample every Nth bar to keep response under a reasonable size
        approx_bars_per_hour = 12  # ~5min bars
        target_pts = min(60, hours)
        all_window = bars[-(hours * approx_bars_per_hour) :]
        step = max(1, len(all_window) // target_pts)

        timeline = []
        for end_idx in range(50, len(all_window), step):
            window = all_window[max(0, end_idx - 50) : end_idx]
            if len(window) < 30:
                continue
            ctx = MarketContext(bars=window, side=side, symbol=symbol)
            r = consult_sage(ctx, parallel=False, use_cache=False, apply_edge_weights=False)
            ts = window[-1].get("ts") or window[-1].get("timestamp") or ""
            timeline.append(
                {
                    "ts": str(ts),
                    "composite_bias": r.composite_bias.value,
                    "conviction": round(r.conviction, 4),
                    "alignment_score": round(r.alignment_score, 4),
                }
            )
        return {
            "symbol": symbol,
            "side": side,
            "hours": hours,
            "n_points": len(timeline),
            "timeline": timeline,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error_code": "sage_timeline_failed", "error_detail": str(exc)}


# ─── Sage disagreement heatmap (Wave-6 #5, 2026-04-27) ─────────────
@app.get("/api/jarvis/sage_school_registry")
def sage_school_registry_endpoint() -> dict:
    """All 23 schools with metadata, weight, edge health, and activation status."""
    from eta_engine.brain.jarvis_v3.sage import SCHOOLS
    from eta_engine.brain.jarvis_v3.sage.edge_tracker import default_tracker
    from eta_engine.brain.jarvis_v3.sage.health import default_monitor

    tracker = default_tracker()
    edges = tracker.snapshot()
    monitor = default_monitor()
    issues = {i.school: i for i in monitor.check_health()}
    schools = []
    for name, s in SCHOOLS.items():
        edge = edges.get(name, {})
        issue = issues.get(name)
        schools.append(
            {
                "name": name,
                "weight": s.WEIGHT,
                "knowledge": s.KNOWLEDGE[:120] + "..." if len(s.KNOWLEDGE) > 120 else s.KNOWLEDGE,
                "instruments": list(s.INSTRUMENTS) if s.INSTRUMENTS else ["all"],
                "regimes": list(s.REGIMES) if s.REGIMES else ["all"],
                "multi_timeframe": s.MULTI_TIMEFRAME,
                "edge_hit_rate": edge.get("hit_rate", 0.0),
                "edge_expectancy": edge.get("expectancy", 0.0),
                "edge_weight_modifier": edge.get("weight_modifier", 1.0),
                "edge_n_obs": edge.get("n_obs", 0),
                "health_issue": {"severity": issue.severity, "detail": issue.detail} if issue else None,
            }
        )
    return {
        "n_schools": len(schools),
        "schools": sorted(schools, key=lambda s: s["edge_expectancy"], reverse=True),
    }


@app.get("/api/jarvis/sage_disagreement_heatmap")
def sage_disagreement_heatmap_endpoint(symbol: str = "MNQ") -> dict:
    ...
    try:
        bars_file = _state_dir() / "raw_state" / f"{symbol}_bars.json"
        if not bars_file or not bars_file.exists():
            return {"symbol": symbol, "status": "no_bars", "heatmap": {}}
        import json as _json

        bars = _json.loads(bars_file.read_text(encoding="utf-8"))
        if not bars or len(bars) < 30:
            return {"symbol": symbol, "status": "no_bars", "heatmap": {}}
        from eta_engine.brain.jarvis_v3.sage import MarketContext, consult_sage
        from eta_engine.brain.jarvis_v3.sage.disagreement import detect_clashes

        ctx = MarketContext(bars=bars[-200:], side="long", symbol=symbol)
        report = consult_sage(ctx)
        clashes = detect_clashes(report)
        # Per-school: bias vs composite, enriched with edge tracker data
        from eta_engine.brain.jarvis_v3.sage import SCHOOLS
        from eta_engine.brain.jarvis_v3.sage.edge_tracker import default_tracker

        tracker = default_tracker()
        edges = tracker.snapshot()
        per_school_disagree = {
            name: {
                "bias": v.bias.value,
                "aligned_with_composite": v.bias == report.composite_bias,
                "conviction": round(v.conviction, 4),
                "weight": SCHOOLS.get(name, type("_", (), {"WEIGHT": 1.0})()).WEIGHT,
                "edge_expectancy": edges.get(name, {}).get("expectancy", 0.0),
                "edge_hit_rate": edges.get(name, {}).get("hit_rate", 0.5),
                "edge_weight_modifier": edges.get(name, {}).get("weight_modifier", 1.0),
            }
            for name, v in report.per_school.items()
        }
        return {
            "symbol": symbol,
            "composite_bias": report.composite_bias.value,
            "conviction": report.conviction,
            "per_school": per_school_disagree,
            "named_clashes": [
                {
                    "name": c.name,
                    "interpretation": c.interpretation,
                    "modifier": c.verdict_modifier,
                    "cap_mult": c.cap_mult,
                }
                for c in clashes
            ],
        }
    except Exception as exc:  # noqa: BLE001
        return {"error_code": "heatmap_failed", "error_detail": str(exc)}


@app.get("/api/jarvis/governor")
def jarvis_governor() -> dict:
    """Governor snapshot from state/jarvis_governor.json."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    return read_json_safe(_state_dir() / "jarvis_governor.json")


@app.get("/api/jarvis/edge_leaderboard")
def jarvis_edge_leaderboard(bot: str | None = None, limit: int = 5) -> dict:
    """Top + bottom schools by expectancy. Optional ?bot=<id> for per-bot."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    if bot is not None and not _BOT_ID_RE.match(bot):
        raise HTTPException(status_code=400, detail={"error_code": "invalid_bot_id"})
    edge_path = _state_dir() / "sage" / "edge_tracker.json"
    if bot:
        edge_path = _state_dir() / "sage" / f"edge_tracker_{bot}.json"
    data = read_json_safe(edge_path)
    schools = data.get("schools") or {}
    rows = []
    for name, e in schools.items():
        n = e.get("n_aligned_wins", 0) + e.get("n_aligned_losses", 0)
        avg_r = (e.get("sum_r", 0.0) / n) if n > 0 else 0.0
        rows.append(
            {
                "school": name,
                "n_obs": e.get("n_obs", 0),
                "n_aligned": n,
                "avg_r": round(avg_r, 4),
                "sum_r": e.get("sum_r", 0.0),
            }
        )
    rows.sort(key=lambda r: r["avg_r"], reverse=True)
    return {
        "top": rows[:limit],
        "bottom": list(reversed(rows[-limit:])) if rows else [],
    }


@app.get("/api/jarvis/model_tier")
def jarvis_model_tier() -> dict:
    """Most recent LLM tier routing decision from today's audit log."""
    from datetime import UTC, datetime

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    audit = _state_dir() / "jarvis_audit" / f"{today}.jsonl"
    if not audit.exists():
        return {"_warning": "no_data", "_path": str(audit)}
    last_llm: dict | None = None
    try:
        for line in audit.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("request", {}).get("action") == "LLM_INVOCATION":
                last_llm = row
    except (json.JSONDecodeError, OSError) as exc:
        return {"_error_code": "audit_parse_failed", "_error_detail": str(exc)}
    if last_llm is None:
        return {"_warning": "no_llm_invocation_today"}
    return {
        "tier": last_llm.get("response", {}).get("selected_model"),
        "ts": last_llm.get("ts"),
        "subsystem": last_llm.get("request", {}).get("subsystem"),
        "task_category": last_llm.get("request", {}).get("payload", {}).get("task_category"),
    }


@app.get("/api/jarvis/kaizen_latest")
def jarvis_kaizen_latest() -> dict:
    """Latest kaizen result from the active loop, with legacy ticket fallback."""
    latest_path = _state_dir() / "kaizen_latest.json"
    if latest_path.exists():
        try:
            payload = json.loads(latest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "_warning": "invalid_latest_json",
                "_path": str(latest_path),
                "error": str(exc),
            }
        if isinstance(payload, dict):
            started_at = str(payload.get("started_at") or "")
            action_counts = payload.get("action_counts") if isinstance(payload.get("action_counts"), dict) else {}
            title = f"Kaizen loop {started_at}".strip() if started_at else "Kaizen loop latest"
            return {
                **payload,
                "title": title,
                "filename": latest_path.name,
                "source": "kaizen_latest_json",
                "summary": {
                    "applied": bool(payload.get("applied")),
                    "applied_count": int(payload.get("applied_count") or 0),
                    "held_count": int(payload.get("held_count") or 0),
                    "n_bots": int(payload.get("n_bots") or 0),
                    "action_counts": action_counts,
                },
            }
        return {
            "_warning": "invalid_latest_shape",
            "_path": str(latest_path),
        }

    tickets_dir = _state_dir() / "kaizen" / "tickets"
    if not tickets_dir.exists():
        return {"_warning": "no_data", "_path": str(tickets_dir)}
    files = sorted(tickets_dir.glob("*.md"))
    if not files:
        return {"_warning": "no_tickets"}
    latest = files[-1]
    md = latest.read_text(encoding="utf-8")
    title = md.splitlines()[0].lstrip("# ").strip() if md else latest.stem
    return {
        "title": title,
        "filename": latest.name,
        "markdown": md,
        "source": "legacy_ticket_markdown",
    }


def _pct_distance(distance: float | None, mark_price: float | None) -> float | None:
    if distance is None or mark_price in (None, 0):
        return None
    return round((distance / abs(mark_price)) * 100.0, 4)


def _supervisor_exit_visibility(
    *,
    side: str,
    entry_price: float | None,
    mark_price: float | None,
    bracket_stop: float | None,
    bracket_target: float | None,
    last_bar_high: float | None,
    last_bar_low: float | None,
    broker_bracket: bool,
) -> dict:
    side_upper = side.upper()
    is_short = side_upper in {"SELL", "SHORT"}
    target_distance = None
    stop_distance = None
    if mark_price is not None:
        if bracket_target is not None:
            target_distance = round(
                mark_price - bracket_target if is_short else bracket_target - mark_price,
                8,
            )
        if bracket_stop is not None:
            stop_distance = round(
                bracket_stop - mark_price if is_short else mark_price - bracket_stop,
                8,
            )

    target_progress_pct = None
    if entry_price is not None and mark_price is not None and bracket_target is not None:
        target_span = abs(bracket_target - entry_price)
        if target_span > 0:
            target_progress = entry_price - mark_price if is_short else mark_price - entry_price
            target_progress_pct = round((target_progress / target_span) * 100.0, 2)

    stop_cushion_pct = None
    if entry_price is not None and mark_price is not None and bracket_stop is not None:
        stop_span = abs(entry_price - bracket_stop)
        if stop_span > 0:
            stop_cushion = bracket_stop - mark_price if is_short else mark_price - bracket_stop
            stop_cushion_pct = round((stop_cushion / stop_span) * 100.0, 2)

    target_touched = False
    if bracket_target is not None:
        target_touched = (
            last_bar_low is not None and last_bar_low <= bracket_target
            if is_short
            else last_bar_high is not None and last_bar_high >= bracket_target
        )
    stop_touched = False
    if bracket_stop is not None:
        stop_touched = (
            last_bar_high is not None and last_bar_high >= bracket_stop
            if is_short
            else last_bar_low is not None and last_bar_low <= bracket_stop
        )

    if target_touched:
        status = "target_touched_still_open"
    elif stop_touched:
        status = "stop_touched_still_open"
    elif broker_bracket:
        status = "broker_bracket_watch"
    elif mark_price is None:
        status = "mark_missing"
    elif bracket_stop is None or bracket_target is None:
        status = "bracket_missing"
    else:
        status = "watching"

    return {
        "status": status,
        "owner": "broker" if broker_bracket else "supervisor",
        "target_distance_points": target_distance,
        "target_distance_pct": _pct_distance(target_distance, mark_price),
        "target_progress_pct": target_progress_pct,
        "stop_distance_points": stop_distance,
        "stop_distance_pct": _pct_distance(stop_distance, mark_price),
        "stop_cushion_pct": stop_cushion_pct,
        "target_touched_latest_bar": target_touched,
        "stop_touched_latest_bar": stop_touched,
    }


def _target_exit_summary(
    rows: list[dict],
    *,
    broker_open_position_count: int | None = None,
    broker_bracket_required_position_count: int | None = None,
    broker_open_order_verified_bracket_count: int | None = None,
    server_ts: float | None = None,
) -> dict:
    """Summarize open-position target/stop supervision for operator cards."""
    server_ts = time.time() if server_ts is None else server_ts
    open_count = 0
    watching_count = 0
    supervisor_watch_count = 0
    broker_bracket_count = 0
    missing_bracket_count = 0
    target_touched_count = 0
    stop_touched_count = 0
    nearest_target: dict | None = None
    nearest_stop: dict | None = None

    def _candidate(row: dict, distance: float, distance_pct: float | None) -> dict:
        return {
            "bot": str(row.get("name") or row.get("id") or row.get("bot_id") or ""),
            "symbol": str(row.get("symbol") or ""),
            "distance_points": distance,
            "distance_pct": distance_pct,
        }

    for row in rows:
        if not isinstance(row, dict) or not _row_has_open_exposure(row):
            continue
        open_count += 1
        state = row.get("position_state") if isinstance(row.get("position_state"), dict) else {}
        visibility = (
            state.get("target_exit_visibility") if isinstance(state.get("target_exit_visibility"), dict) else {}
        )
        status = str(visibility.get("status") or "").strip().lower()
        owner = str(visibility.get("owner") or "").strip().lower()
        broker_bracket = bool(row.get("broker_bracket") or state.get("broker_bracket") or owner == "broker")
        if broker_bracket:
            broker_bracket_count += 1
        else:
            supervisor_watch_count += 1
        if status in {"watching", "broker_bracket_watch"}:
            watching_count += 1
        if status == "target_touched_still_open":
            target_touched_count += 1
        if status == "stop_touched_still_open":
            stop_touched_count += 1

        target_level = _float_value(state.get("bracket_target") or row.get("bracket_target"))
        stop_level = _float_value(state.get("bracket_stop") or row.get("bracket_stop"))
        if target_level is None or stop_level is None or status == "bracket_missing":
            missing_bracket_count += 1

        target_distance = _float_value(
            visibility.get("target_distance_points")
            if "target_distance_points" in visibility
            else state.get("target_distance_points")
        )
        target_distance_pct = _float_value(
            visibility.get("target_distance_pct")
            if "target_distance_pct" in visibility
            else state.get("target_distance_pct")
        )
        if target_distance is not None:
            candidate = _candidate(row, target_distance, target_distance_pct)
            if nearest_target is None or abs(target_distance) < abs(float(nearest_target["distance_points"])):
                nearest_target = candidate

        stop_distance = _float_value(
            visibility.get("stop_distance_points")
            if "stop_distance_points" in visibility
            else state.get("stop_distance_points")
        )
        stop_distance_pct = _float_value(
            visibility.get("stop_distance_pct") if "stop_distance_pct" in visibility else state.get("stop_distance_pct")
        )
        if stop_distance is not None:
            candidate = _candidate(row, stop_distance, stop_distance_pct)
            if nearest_stop is None or abs(stop_distance) < abs(float(nearest_stop["distance_points"])):
                nearest_stop = candidate

    touched_count = target_touched_count + stop_touched_count
    position_staleness = _position_staleness_summary(rows, server_ts=server_ts)
    supervisor_local_count = max(0, open_count - broker_bracket_count)
    effective_broker_open_count = int(broker_open_position_count) if broker_open_position_count is not None else 0
    if broker_bracket_required_position_count is None:
        broker_bracket_required_count = effective_broker_open_count
    else:
        broker_bracket_required_count = max(
            0,
            min(int(broker_bracket_required_position_count), effective_broker_open_count),
        )
    broker_supervisor_managed_count = max(
        0,
        effective_broker_open_count - broker_bracket_required_count,
    )
    supervisor_reported_broker_bracket_count = broker_bracket_count
    broker_open_order_verified_count = (
        max(
            0,
            min(
                int(broker_open_order_verified_bracket_count),
                broker_bracket_required_count,
            ),
        )
        if broker_open_order_verified_bracket_count is not None
        else 0
    )
    broker_bracket_count = max(
        broker_bracket_count,
        broker_open_order_verified_count,
    )
    broker_unbracketed_count = max(0, broker_bracket_required_count - broker_bracket_count)
    total_missing_bracket_count = missing_bracket_count + broker_unbracketed_count
    if touched_count > 0:
        status = "alert"
    elif total_missing_bracket_count > 0:
        status = "missing_brackets"
    elif open_count == 0 and effective_broker_open_count == 0:
        status = "flat"
    elif effective_broker_open_count == 0 and supervisor_local_count > 0:
        status = "paper_watching"
    elif (
        watching_count > 0 or supervisor_watch_count > 0 or broker_bracket_count > 0 or effective_broker_open_count > 0
    ):
        status = "watching"
    else:
        status = "unknown"

    if open_count == 0 and effective_broker_open_count == 0:
        summary_line = "flat; no open positions need target/stop supervision"
    else:
        nearest_text = (
            f"; nearest target {nearest_target['bot']} {float(nearest_target['distance_points']):.2f} pts"
            if nearest_target
            else "; nearest target n/a"
        )
        if broker_open_position_count is not None:
            required_text = (
                f" ({broker_bracket_required_count} broker bracket-required)"
                if broker_bracket_required_count != effective_broker_open_count
                else ""
            )
            summary_line = (
                f"{effective_broker_open_count} broker open{required_text}; "
                f"{supervisor_local_count} supervisor paper-local open; "
                f"{supervisor_watch_count} supervisor watcher(s); "
                f"{broker_bracket_count} broker bracket(s); {total_missing_bracket_count} missing bracket(s)"
                f"{nearest_text}"
            )
        elif supervisor_local_count > 0:
            summary_line = (
                f"0 broker open; "
                f"{supervisor_local_count} supervisor paper-local open; "
                f"{supervisor_watch_count} supervisor watcher(s); "
                f"{broker_bracket_count} broker bracket(s); {total_missing_bracket_count} missing bracket(s)"
                f"{nearest_text}"
            )
        else:
            summary_line = (
                f"{open_count} open; {supervisor_watch_count} supervisor watcher(s); "
                f"{broker_bracket_count} broker bracket(s); {total_missing_bracket_count} missing bracket(s)"
                f"{nearest_text}"
            )

    return {
        "status": status,
        "summary_line": summary_line,
        "open_position_count": open_count,
        "broker_open_position_count": effective_broker_open_count,
        "broker_open_position_count_observed": broker_open_position_count is not None,
        "broker_bracket_required_position_count": broker_bracket_required_count,
        "broker_supervisor_managed_position_count": broker_supervisor_managed_count,
        "supervisor_local_position_count": supervisor_local_count,
        "watching_count": watching_count,
        "supervisor_watch_count": supervisor_watch_count,
        "broker_bracket_count": broker_bracket_count,
        "supervisor_reported_broker_bracket_count": supervisor_reported_broker_bracket_count,
        "broker_open_order_verified_bracket_count": broker_open_order_verified_count,
        "broker_unbracketed_count": broker_unbracketed_count,
        "missing_bracket_count": total_missing_bracket_count,
        "supervisor_missing_bracket_count": missing_bracket_count,
        "target_touched_count": target_touched_count,
        "stop_touched_count": stop_touched_count,
        "stale_position_status": position_staleness["status"],
        "position_staleness": position_staleness,
        "nearest_target": nearest_target,
        "nearest_stop": nearest_stop,
        "nearest_target_bot": nearest_target.get("bot") if nearest_target else None,
        "nearest_target_distance_points": (nearest_target.get("distance_points") if nearest_target else None),
        "nearest_target_distance_pct": (nearest_target.get("distance_pct") if nearest_target else None),
        "nearest_stop_bot": nearest_stop.get("bot") if nearest_stop else None,
        "nearest_stop_distance_points": (nearest_stop.get("distance_points") if nearest_stop else None),
        "nearest_stop_distance_pct": (nearest_stop.get("distance_pct") if nearest_stop else None),
    }


def _sup_bot_to_roster_row(sup: dict, now_ts: float) -> dict:
    """Convert a jarvis_supervisor_bot_accounts() row into /api/bot-fleet roster shape."""
    from datetime import UTC, datetime

    def _age_seconds(value: str) -> int | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return max(0, int(now_ts - dt.timestamp()))
        except (ValueError, OSError):
            return None

    today = sup.get("today") or {}
    heartbeat_at = str(sup.get("heartbeat_ts") or sup.get("updated_at") or "")
    signal_at = str(sup.get("last_signal_ts") or sup.get("last_signal_at") or "")
    heartbeat_age_s = _age_seconds(heartbeat_at)
    open_pos_raw = sup.get("open_position") or {}
    open_pos = open_pos_raw if isinstance(open_pos_raw, dict) else {}
    position_opened_at = str(
        open_pos.get("entry_ts") or open_pos.get("opened_at") or open_pos.get("ts") or "",
    )
    if not signal_at and position_opened_at:
        signal_at = position_opened_at
    last_signal_age_s = _age_seconds(signal_at)
    position_age_s = _age_seconds(position_opened_at) if position_opened_at else None
    bracket_stop = _float_value(
        open_pos.get("bracket_stop") or open_pos.get("stop_price"),
    )
    bracket_target = _float_value(
        open_pos.get("bracket_target") or open_pos.get("target_price"),
    )
    broker_bracket = bool(open_pos.get("broker_bracket"))
    bracket_src = str(open_pos.get("bracket_src") or open_pos.get("exit_src") or "")
    open_positions = 1 if open_pos else 0
    side = str(open_pos.get("side") or "").upper()
    qty = _float_value(open_pos.get("qty") or open_pos.get("quantity"))
    entry_price = _float_value(open_pos.get("entry_price"))
    mark_price = _float_value(open_pos.get("mark_price") or open_pos.get("last_price"))
    last_bar_high = _float_value(_first_present(open_pos, ("last_bar_high", "bar_high", "high")))
    if last_bar_high is None:
        last_bar_high = _float_value(sup.get("last_bar_high"))
    last_bar_low = _float_value(_first_present(open_pos, ("last_bar_low", "bar_low", "low")))
    if last_bar_low is None:
        last_bar_low = _float_value(sup.get("last_bar_low"))
    last_bar_ts = open_pos.get("last_bar_ts") or open_pos.get("mark_ts") or sup.get("last_bar_ts")
    target_exit_visibility = (
        _supervisor_exit_visibility(
            side=side,
            entry_price=entry_price,
            mark_price=mark_price,
            bracket_stop=bracket_stop,
            bracket_target=bracket_target,
            last_bar_high=last_bar_high,
            last_bar_low=last_bar_low,
            broker_bracket=broker_bracket,
        )
        if open_pos
        else {"status": "flat", "owner": "none"}
    )
    position_state = {"state": "flat", "open": False}
    if open_pos:
        position_state = {
            "state": "open",
            "open": True,
            "side": side,
            "qty": qty,
            "age_s": position_age_s,
            "entry_price": entry_price,
            "mark_price": mark_price,
            "bracket_stop": bracket_stop,
            "bracket_target": bracket_target,
            "target_distance_points": target_exit_visibility.get("target_distance_points"),
            "target_distance_pct": target_exit_visibility.get("target_distance_pct"),
            "target_progress_pct": target_exit_visibility.get("target_progress_pct"),
            "stop_distance_points": target_exit_visibility.get("stop_distance_points"),
            "stop_distance_pct": target_exit_visibility.get("stop_distance_pct"),
            "stop_cushion_pct": target_exit_visibility.get("stop_cushion_pct"),
            "broker_bracket": broker_bracket,
            "bracket_src": bracket_src,
            "signal_id": str(open_pos.get("signal_id") or ""),
            "opened_at": position_opened_at or None,
            "last_bar_ts": last_bar_ts,
            "last_bar_high": last_bar_high,
            "last_bar_low": last_bar_low,
            "target_exit_visibility": target_exit_visibility,
            "position_watchdog": _position_watchdog_snapshot(
                {"position_state": {"opened_at": position_opened_at}, "open_positions": 1},
                server_ts=now_ts,
            ),
        }
    last_side: str | None = None
    if open_pos.get("side"):
        last_side = str(open_pos["side"])
    elif sup.get("direction"):
        last_side = str(sup["direction"]).upper()
    strategy_readiness = sup.get("strategy_readiness") if isinstance(sup.get("strategy_readiness"), dict) else {}
    bar_at = str(sup.get("last_bar_ts") or "")
    if signal_at:
        activity_ts = signal_at
        activity_age_s = last_signal_age_s
        activity_type = "signal"
        activity_side = last_side
    elif position_opened_at:
        activity_ts = position_opened_at
        activity_age_s = _age_seconds(position_opened_at)
        activity_type = "position"
        activity_side = last_side
    elif bar_at:
        activity_ts = bar_at
        activity_age_s = _age_seconds(bar_at)
        activity_type = "bar"
        activity_side = None
    elif heartbeat_at:
        activity_ts = heartbeat_at
        activity_age_s = heartbeat_age_s
        activity_type = "heartbeat"
        activity_side = None
    else:
        activity_ts = ""
        activity_age_s = None
        activity_type = None
        activity_side = None
    return {
        "id": str(sup.get("id") or ""),
        "name": str(sup.get("name") or ""),
        "symbol": str(sup.get("symbol") or ""),
        "tier": str(sup.get("strategy") or ""),
        "venue": str(sup.get("broker") or "paper-sim"),
        "status": str(sup.get("status") or "unknown"),
        "todays_pnl": float(today.get("pnl") or 0.0),
        "todays_pnl_source": "supervisor_heartbeat",
        "last_trade_ts": None,
        "last_trade_age_s": None,
        "last_trade_side": None,
        "last_trade_r": None,
        "last_trade_qty": None,
        "last_signal_ts": signal_at or None,
        "last_signal_age_s": last_signal_age_s,
        "last_signal_side": last_side if signal_at else None,
        "last_activity_ts": activity_ts or None,
        "last_activity_age_s": activity_age_s,
        "last_activity_side": activity_side,
        "last_activity_type": activity_type,
        "last_bar_ts": bar_at or None,
        "data_ts": now_ts,
        "data_age_s": 0.0,
        "heartbeat_ts": heartbeat_at or None,
        "heartbeat_age_s": heartbeat_age_s,
        "source": "jarvis_strategy_supervisor",
        "confirmed": True,
        "mode": str(sup.get("mode") or ""),
        "last_jarvis_verdict": str(sup.get("last_jarvis_verdict") or ""),
        "strategy_readiness": strategy_readiness,
        "open_position": open_pos,
        "open_positions": open_positions,
        "position_state": position_state,
        "position_age_s": position_age_s,
        "bracket_stop": bracket_stop,
        "bracket_target": bracket_target,
        "broker_bracket": broker_bracket,
        "bracket_src": bracket_src,
        "launch_lane": str(sup.get("launch_lane") or strategy_readiness.get("launch_lane") or ""),
        "can_paper_trade": bool(sup.get("can_paper_trade") or strategy_readiness.get("can_paper_trade")),
        "can_live_trade": bool(sup.get("can_live_trade") or strategy_readiness.get("can_live_trade")),
        "readiness_next_action": str(
            sup.get("readiness_next_action") or strategy_readiness.get("next_action") or "",
        ),
    }


def _supervisor_roster_rows(now_ts: float, bot: str | None = None) -> list[dict]:
    """Load live supervisor heartbeat rows in the same shape as /api/bot-fleet."""
    try:
        from eta_engine.scripts.jarvis_supervisor_bridge import (
            jarvis_supervisor_bot_accounts,
        )

        sup_hb = _state_dir() / "jarvis_intel" / "supervisor" / "heartbeat.json"
        sup_accounts = jarvis_supervisor_bot_accounts(heartbeat_path=sup_hb)
    except Exception:
        return []

    rows = [_sup_bot_to_roster_row(s, now_ts) for s in sup_accounts]
    if bot is not None:
        rows = [row for row in rows if str(row.get("name") or row.get("id") or "") == bot]
    return rows


@app.get("/api/bot-fleet")
def bot_fleet_roster(
    response: Response,
    bot: str | None = None,
    since_days: int = 1,
    include_disabled: bool = False,
    live_broker_probe: bool = False,
) -> dict:
    """Roster: scan bot state and use cached broker truth unless a live probe is requested."""
    from datetime import UTC, datetime

    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    bots_dir = _state_dir() / "bots"

    def _parse_ts(value: object) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        ts = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt

    if bot is not None and not _BOT_ID_RE.match(bot):
        raise HTTPException(status_code=400, detail={"error_code": "invalid_bot_id"})
    since_days = max(0, min(int(since_days), 7))
    fill_r_totals, live_latest, fills_mtime = _fill_r_by_bot_since_days(since_days)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    rows = []
    fills_stats = _fills_activity_snapshot()
    now_ts = time.time()
    readiness_rows = _bot_strategy_readiness_rows_by_bot()
    for bot_dir in sorted(bots_dir.iterdir()) if bots_dir.exists() else []:
        if not bot_dir.is_dir():
            continue
        if bot and bot_dir.name != bot:
            continue
        status = read_json_safe(bot_dir / "status.json")
        if "_warning" in status:
            continue
        recent_fills = read_json_safe(bot_dir / "recent_fills.json")
        fills_list = recent_fills if isinstance(recent_fills, list) else []
        local_last_fill = fills_list[0] if fills_list else None
        live_last_fill = live_latest.get(bot_dir.name)
        local_ts = _parse_ts(local_last_fill.get("ts")) if isinstance(local_last_fill, dict) else None
        live_ts = _parse_ts(live_last_fill.get("ts")) if isinstance(live_last_fill, dict) else None
        last_fill = local_last_fill
        if isinstance(live_last_fill, dict) and (local_ts is None or (live_ts is not None and live_ts >= local_ts)):
            last_fill = live_last_fill
        if last_fill:
            status["last_trade_ts"] = last_fill.get("ts")
            status["last_trade_side"] = last_fill.get("side")
            status["last_trade_qty"] = last_fill.get("qty")
            status["last_trade_r"] = last_fill.get("realized_r")
            status["last_activity_ts"] = status["last_trade_ts"]
            status["last_activity_side"] = status["last_trade_side"]
            status["last_activity_type"] = "trade"
            duration_s = (
                last_fill.get("hold_seconds") or last_fill.get("duration_s") or last_fill.get("time_in_trade_s")
            )
            if duration_s in (None, "", 0, 0.0):
                open_positions = float(status.get("open_positions") or 0)
                open_ts = _parse_ts(status.get("last_signal_ts"))
                if open_positions > 0 and open_ts is not None:
                    duration_s = max(0, int((datetime.now(UTC) - open_ts).total_seconds()))
            status["last_trade_duration_s"] = duration_s
        else:
            status["last_trade_ts"] = None
            status["last_trade_side"] = None
            status["last_trade_qty"] = None
            status["last_trade_r"] = None
            status["last_trade_duration_s"] = None
            status["last_activity_ts"] = status.get("last_signal_ts") or None
            status["last_activity_side"] = status.get("last_signal_side") or status.get("last_signal") or None
            status["last_activity_type"] = "signal" if status.get("last_signal_ts") else None
        last_trade_dt = _parse_fill_dt(status.get("last_trade_ts"))
        status["last_trade_age_s"] = (
            max(0, int((datetime.now(UTC) - last_trade_dt).total_seconds())) if last_trade_dt is not None else None
        )
        status["last_activity_age_s"] = (
            status["last_trade_age_s"] if status.get("last_activity_type") == "trade" else None
        )
        # Keep roster PnL live by deriving today's delta from the bot curve when available.
        eq_curve = read_json_safe(bot_dir / "equity_curve.json")
        curve_mtime = _safe_mtime(bot_dir / "equity_curve.json") or 0.0
        if isinstance(eq_curve, dict):
            today_curve = eq_curve.get("today")
            if isinstance(today_curve, list) and len(today_curve) >= 2:
                try:
                    start_eq = float(today_curve[0].get("equity"))
                    end_eq = float(today_curve[-1].get("equity"))
                    status["todays_pnl"] = round(end_eq - start_eq, 2)
                except (TypeError, ValueError):
                    pass
            summary = eq_curve.get("summary")
            if isinstance(summary, dict) and summary.get("today_pnl") is not None:
                with contextlib.suppress(TypeError, ValueError):
                    status["todays_pnl"] = round(float(summary.get("today_pnl")), 2)
        # Keep day PnL strictly live by sourcing from today's fills.
        status["todays_pnl"] = round(float(fill_r_totals.get(bot_dir.name, 0.0)), 2)
        status["todays_pnl_source"] = "fills_realized_r"

        status_mtime = _safe_mtime(bot_dir / "status.json") or 0.0
        status["data_ts"] = max(status_mtime, curve_mtime)
        status["data_age_s"] = round(max(0.0, now_ts - float(status["data_ts"])), 1)
        hb_dt = _parse_fill_dt(status.get("heartbeat_ts"))
        hb_age = max(0, int((datetime.now(UTC) - hb_dt).total_seconds())) if hb_dt is not None else None
        status["heartbeat_age_s"] = hb_age
        if not status.get("status"):
            if hb_age is None:
                status["status"] = "unknown"
            elif hb_age <= 90:
                status["status"] = "running"
            elif hb_age <= 300:
                status["status"] = "delayed"
            else:
                status["status"] = "stale"
        _apply_bot_strategy_readiness(
            status,
            _lookup_bot_strategy_readiness(readiness_rows, status, bot_dir.name),
        )
        rows.append(status)
    # --- Supervisor merge ---------------------------------------------------
    # The JARVIS strategy supervisor writes its 16-bot roster to the heartbeat.
    # Those bots never appear in state/bots/, so we merge them in here.
    # Supervisor rows win on name collision (they carry live session data).
    sup_rows = _supervisor_roster_rows(now_ts, bot=bot)
    if sup_rows:
        for sup_row in sup_rows:
            _apply_bot_strategy_readiness(
                sup_row,
                _lookup_bot_strategy_readiness(
                    readiness_rows,
                    sup_row,
                    str(sup_row.get("id") or sup_row.get("name") or ""),
                ),
            )
        sup_ids = {str(s.get("id") or "") for s in sup_rows}
        rows = [r for r in rows if str(r.get("name") or r.get("id") or "") not in sup_ids]
        rows.extend(sup_rows)
    existing_ids = {
        str(value) for row in rows for value in (row.get("bot_id"), row.get("id"), row.get("name")) if value
    }
    readiness_seen: set[str] = set()
    for readiness in readiness_rows.values():
        bot_id = str(readiness.get("bot_id") or readiness.get("id") or readiness.get("name") or "").strip()
        if not bot_id or bot_id in readiness_seen or bot_id in existing_ids:
            continue
        if bot is not None and bot_id != bot:
            continue
        rows.append(_readiness_only_roster_row(readiness, now_ts=now_ts))
        readiness_seen.add(bot_id)

    registry_active = _registry_active_by_bot()
    for row in rows:
        row_bot_id = str(row.get("name") or row.get("id") or row.get("bot_id") or "").strip()
        if row_bot_id in registry_active:
            row["registry_active"] = registry_active[row_bot_id]
            row["registry_deactivated"] = registry_active[row_bot_id] is False

    hidden_disabled_count = sum(1 for row in rows if _is_hidden_bot_row(row))
    if not include_disabled:
        rows = [row for row in rows if not _is_hidden_bot_row(row)]

    confirmed_bots = sum(
        1 for r in rows if r.get("source") == "jarvis_strategy_supervisor" or r.get("confirmed") is True
    )
    active_bots = sum(1 for r in rows if _is_runtime_active_bot_row(r))
    staged_bots = max(0, len(rows) - active_bots)
    running_bots = sum(1 for r in rows if str(r.get("status") or "").lower() == "running")
    mnq_rows = [r for r in rows if str(r.get("symbol") or "").upper().startswith("MNQ")]

    def _is_readiness_only_runtime_inventory(row: dict) -> bool:
        return (
            str(row.get("status") or "").lower() == "readiness_only"
            or str(row.get("mode") or "").lower() == "readiness_snapshot"
        )

    mnq_readiness_only = [r for r in mnq_rows if _is_readiness_only_runtime_inventory(r)]
    mnq_runtime_rows = [r for r in mnq_rows if not _is_readiness_only_runtime_inventory(r)]
    truth = _truth_snapshot(rows, server_ts=now_ts)
    signal_cadence = _signal_cadence_summary(rows, server_ts=now_ts)
    if live_broker_probe:
        try:
            live_broker_state = _last_good_broker_state_after_failed_refresh(_live_broker_state_payload())
        except Exception as exc:  # noqa: BLE001
            try:
                cached_broker_state = _cached_live_broker_state_for_diagnostics()
            except Exception:  # noqa: BLE001
                cached_broker_state = {}
            if (
                isinstance(cached_broker_state, dict)
                and cached_broker_state.get("ready") is True
                and not cached_broker_state.get("error")
            ):
                live_broker_state = dict(cached_broker_state)
                live_broker_state["refresh_probe_failed"] = True
                live_broker_state["refresh_probe_error"] = f"live_broker_state_failed: {exc}"
                live_broker_state["refresh_probe_source"] = "bot_fleet_live_probe_exception"
            else:
                live_broker_state = {
                    "ready": False,
                    "error": f"live_broker_state_failed: {exc}",
                    "today_actual_fills": 0,
                    "today_realized_pnl": 0.0,
                    "total_unrealized_pnl": 0.0,
                    "open_position_count": 0,
                    "win_rate_30d": None,
                    "server_ts": time.time(),
                }
    else:
        live_broker_state = _cached_live_broker_state_for_diagnostics()
    broker_open_position_count = _float_value(live_broker_state.get("open_position_count"))
    broker_bracket_required_position_count = _broker_bracket_required_position_count(live_broker_state)
    broker_oco_evidence = _broker_oco_evidence_payload(live_broker_state)
    if isinstance(live_broker_state, dict):
        live_broker_state["broker_oco_evidence"] = broker_oco_evidence
    target_exit_summary = _target_exit_summary(
        rows,
        broker_open_position_count=(
            int(broker_open_position_count) if broker_open_position_count is not None else None
        ),
        broker_bracket_required_position_count=broker_bracket_required_position_count,
        broker_open_order_verified_bracket_count=int(broker_oco_evidence.get("verified_count") or 0),
        server_ts=now_ts,
    )
    target_exit_summary["broker_position_scope"] = "futures_focus"
    target_exit_summary["broker_position_scope_detail"] = (
        "/api/bot-fleet main cards count active futures-focus venues; Alpaca/spot is retained under cellar evidence."
    )
    position_staleness = (
        target_exit_summary.get("position_staleness")
        if isinstance(target_exit_summary.get("position_staleness"), dict)
        else {}
    )
    oldest_stale_position = (
        position_staleness.get("oldest_position") if isinstance(position_staleness.get("oldest_position"), dict) else {}
    )
    if isinstance(live_broker_state, dict):
        live_broker_state["position_exposure"] = _position_exposure_payload(
            live_broker_state,
            close_history=(
                live_broker_state.get("close_history")
                if isinstance(live_broker_state.get("close_history"), dict)
                else None
            ),
            target_exit_summary=target_exit_summary,
        )
    close_history = (
        live_broker_state.get("close_history") if isinstance(live_broker_state.get("close_history"), dict) else {}
    )
    close_history = _limit_close_history_recent_rows(_normalize_close_history_count_alias(close_history))
    live_broker_state["close_history"] = close_history
    close_windows = close_history.get("windows") if isinstance(close_history.get("windows"), dict) else {}
    default_close_history_window = str(close_history.get("default_window") or "mtd")
    history_window_pnl = {}
    for window_key in ("today", "wtd", "mtd", "ytd", "all"):
        window = close_windows.get(window_key)
        if not isinstance(window, dict):
            continue
        history_window_pnl[window_key] = {
            "label": window.get("label") or window_key.upper(),
            "pnl": _float_value(window.get("realized_pnl")),
            "count": _close_window_count(window),
            "closed_outcome_count": int(window.get("closed_outcome_count") or 0),
            "evaluated_outcome_count": int(window.get("evaluated_outcome_count") or 0),
            "win_rate": _float_value(window.get("win_rate")),
            "since": window.get("since"),
            "until": window.get("until"),
            "source": window.get("source") or close_history.get("source") or "trade_close_ledger",
        }
    default_close_window_payload = (
        close_windows.get(default_close_history_window)
        if isinstance(close_windows.get(default_close_history_window), dict)
        else {}
    )
    if not default_close_window_payload and isinstance(close_windows.get("mtd"), dict):
        default_close_history_window = "mtd"
        default_close_window_payload = close_windows["mtd"]
    default_close_rows = (
        default_close_window_payload.get("recent_outcomes")
        if isinstance(default_close_window_payload.get("recent_outcomes"), list)
        else []
    )
    default_close_rows = default_close_rows[:_DASHBOARD_POSITION_EXPOSURE_CLOSE_ROW_LIMIT]
    close_history_window = {
        "window": default_close_history_window,
        "label": default_close_window_payload.get("label") or default_close_history_window.upper(),
        "realized_pnl": _float_value(default_close_window_payload.get("realized_pnl")),
        "count": _close_window_count(default_close_window_payload),
        "closed_outcome_count": int(default_close_window_payload.get("closed_outcome_count") or 0),
        "evaluated_outcome_count": int(default_close_window_payload.get("evaluated_outcome_count") or 0),
        "winning_outcomes": int(default_close_window_payload.get("winning_outcomes") or 0),
        "losing_outcomes": int(default_close_window_payload.get("losing_outcomes") or 0),
        "win_rate": _float_value(default_close_window_payload.get("win_rate")),
        "since": default_close_window_payload.get("since"),
        "until": default_close_window_payload.get("until"),
        "source": default_close_window_payload.get("source") or close_history.get("source") or "trade_close_ledger",
        "recent_outcomes": default_close_rows,
    }
    broker_summary = _broker_summary_fields(live_broker_state)
    portfolio_summary = _portfolio_summary_payload(
        rows,
        live_broker_state,
        hidden_disabled_count=hidden_disabled_count,
        close_history=close_history,
    )
    broker_gateway = _reconcile_broker_gateway_with_live_state(
        _broker_gateway_snapshot(),
        live_broker_state,
    )
    broker_bracket_audit = _broker_bracket_audit_payload(
        target_exit_summary=target_exit_summary,
        live_broker_state=live_broker_state,
    )
    broker_bracket_position_summary = (
        broker_bracket_audit.get("position_summary")
        if isinstance(broker_bracket_audit.get("position_summary"), dict)
        else {}
    )
    broker_bracket_unprotected_symbols = (
        broker_bracket_position_summary.get("unprotected_symbols")
        if isinstance(broker_bracket_position_summary.get("unprotected_symbols"), list)
        else []
    )
    broker_bracket_primary = (
        broker_bracket_audit.get("primary_unprotected_position")
        if isinstance(broker_bracket_audit.get("primary_unprotected_position"), dict)
        else {}
    )
    broker_bracket_actions = (
        broker_bracket_audit.get("operator_actions")
        if isinstance(broker_bracket_audit.get("operator_actions"), list)
        else []
    )
    broker_bracket_action_ids = [
        str(action.get("id") or "")
        for action in broker_bracket_actions
        if isinstance(action, dict) and action.get("id")
    ]
    broker_bracket_action_labels = [
        str(action.get("label") or "")
        for action in broker_bracket_actions
        if isinstance(action, dict) and action.get("label")
    ]
    broker_bracket_manual_action_count = sum(
        1 for action in broker_bracket_actions if isinstance(action, dict) and action.get("manual") is True
    )
    broker_bracket_order_actions = [
        action for action in broker_bracket_actions if isinstance(action, dict) and action.get("order_action") is True
    ]
    broker_bracket_primary_action = (
        broker_bracket_actions[0] if broker_bracket_actions and isinstance(broker_bracket_actions[0], dict) else {}
    )
    broker_bracket_order_action = broker_bracket_order_actions[0] if broker_bracket_order_actions else {}
    broker_bracket_prop_dry_run_blocked = bool(broker_bracket_audit.get("operator_action_required")) and not bool(
        broker_bracket_audit.get("ready_for_prop_dry_run")
    )
    paper_live_transition = _paper_live_transition_payload(refresh=False)
    paper_live_status = str(paper_live_transition.get("status") or "unknown")
    paper_live_critical_ready = bool(paper_live_transition.get("critical_ready"))
    paper_live_held_by_bracket_audit = broker_bracket_prop_dry_run_blocked and paper_live_critical_ready
    paper_live_effective_status = "held_by_bracket_audit" if paper_live_held_by_bracket_audit else paper_live_status
    paper_live_effective_detail = ""
    if paper_live_held_by_bracket_audit:
        paper_live_effective_detail = (
            f"held by Bracket Audit: {' or '.join(broker_bracket_action_labels)}"
            if broker_bracket_action_labels
            else "held by Bracket Audit"
        )
    vps_root_reconciliation = _vps_root_reconciliation_payload()
    vps_root_summary = (
        vps_root_reconciliation.get("summary") if isinstance(vps_root_reconciliation.get("summary"), dict) else {}
    )
    vps_root_counts = (
        vps_root_reconciliation.get("counts") if isinstance(vps_root_reconciliation.get("counts"), dict) else {}
    )
    vps_root_steps = (
        vps_root_reconciliation.get("steps") if isinstance(vps_root_reconciliation.get("steps"), list) else []
    )
    vps_root_top_step = vps_root_steps[0] if vps_root_steps and isinstance(vps_root_steps[0], dict) else {}
    vps_root_companion_step = next(
        (step for step in vps_root_steps if isinstance(step, dict) and step.get("id") == "align-submodules"),
        {},
    )
    vps_root_top_step_evidence = (
        vps_root_top_step.get("evidence") if isinstance(vps_root_top_step.get("evidence"), list) else []
    )
    vps_root_companion_step_evidence = (
        vps_root_companion_step.get("evidence") if isinstance(vps_root_companion_step.get("evidence"), list) else []
    )
    ibkr_gateway = broker_gateway.get("ibkr") if isinstance(broker_gateway.get("ibkr"), dict) else {}
    return {
        "bots": rows,
        "confirmed_bots": confirmed_bots,
        "summary": {
            "bot_total": len(rows),
            "confirmed_bots": confirmed_bots,
            "active_bots": active_bots,
            "active_bot_count": active_bots,
            "runtime_active_bots": active_bots,
            "running_bots": running_bots,
            "staged_bots": staged_bots,
            "readiness_staged_bots": staged_bots,
            "live_broker_probe_mode": "live" if live_broker_probe else "cached_diagnostics",
            "mnq_total": len(mnq_runtime_rows),
            "mnq_runtime_total": len(mnq_runtime_rows),
            "mnq_inventory_total": len(mnq_rows),
            "mnq_readiness_only": len(mnq_readiness_only),
            "mnq_running": sum(1 for r in mnq_runtime_rows if str(r.get("status") or "").lower() == "running"),
            "truth_status": truth["truth_status"],
            "truth_summary_line": truth["truth_summary_line"],
            "latest_signal_ts": signal_cadence["latest_signal_ts"],
            "signal_cadence_status": signal_cadence["status"],
            "signal_update_count": signal_cadence["signal_update_count"],
            "unique_signal_seconds": signal_cadence["unique_signal_seconds"],
            "max_same_second": signal_cadence["max_same_second"],
            "target_exit_status": target_exit_summary["status"],
            "stale_position_status": target_exit_summary["stale_position_status"],
            "unknown_position_age_count": int(position_staleness.get("unknown_age_count") or 0),
            "require_ack_count": int(position_staleness.get("require_ack_count") or 0),
            "tighten_stop_due_count": int(position_staleness.get("tighten_stop_due_count") or 0),
            "tightened_watch_count": int(position_staleness.get("tightened_watch_count") or 0),
            "force_flatten_due_count": int(position_staleness.get("force_flatten_due_count") or 0),
            "stale_position_oldest_bot": str(oldest_stale_position.get("bot") or ""),
            "stale_position_oldest_symbol": str(oldest_stale_position.get("symbol") or ""),
            "stale_position_oldest_age_s": oldest_stale_position.get("age_s"),
            "stale_position_oldest_next_action": str(oldest_stale_position.get("next_action") or ""),
            "stale_position_seconds_to_next_action": oldest_stale_position.get("seconds_to_next_action"),
            "open_position_count_visible": target_exit_summary["open_position_count"],
            "target_exit_broker_position_scope": str(target_exit_summary.get("broker_position_scope") or ""),
            "target_exit_broker_position_scope_detail": str(
                target_exit_summary.get("broker_position_scope_detail") or ""
            ),
            "supervisor_exit_watch_count": target_exit_summary["supervisor_watch_count"],
            "close_history_window": close_history_window["window"],
            "close_history_label": close_history_window["label"],
            "close_history_realized_pnl": close_history_window["realized_pnl"],
            "close_history_closed_outcome_count": close_history_window["closed_outcome_count"],
            "close_history_evaluated_outcome_count": close_history_window["evaluated_outcome_count"],
            "close_history_win_rate": close_history_window["win_rate"],
            "broker_bracket_audit_status": broker_bracket_audit.get("summary"),
            "broker_bracket_audit_ready": bool(broker_bracket_audit.get("ready_for_prop_dry_run")),
            "broker_bracket_operator_action_required": bool(
                broker_bracket_audit.get("operator_action_required"),
            ),
            "broker_bracket_prop_dry_run_blocked": broker_bracket_prop_dry_run_blocked,
            "broker_bracket_missing_count": int(broker_bracket_position_summary.get("missing_bracket_count") or 0),
            "broker_bracket_unprotected_symbols": broker_bracket_unprotected_symbols,
            "broker_bracket_operator_action_count": len(broker_bracket_action_ids),
            "broker_bracket_operator_action_ids": broker_bracket_action_ids,
            "broker_bracket_operator_action_labels": broker_bracket_action_labels,
            "broker_bracket_manual_action_count": broker_bracket_manual_action_count,
            "broker_bracket_order_action_count": len(broker_bracket_order_actions),
            "broker_bracket_primary_action_label": str(broker_bracket_primary_action.get("label") or ""),
            "broker_bracket_primary_action_detail": str(broker_bracket_primary_action.get("detail") or ""),
            "broker_bracket_order_action_label": str(broker_bracket_order_action.get("label") or ""),
            "broker_bracket_order_action_detail": str(broker_bracket_order_action.get("detail") or ""),
            "broker_bracket_next_action": str(broker_bracket_audit.get("next_action") or ""),
            "broker_bracket_primary_symbol": str(broker_bracket_primary.get("symbol") or ""),
            "broker_bracket_primary_venue": str(broker_bracket_primary.get("venue") or ""),
            "broker_bracket_primary_sec_type": str(broker_bracket_primary.get("sec_type") or ""),
            "broker_bracket_primary_side": str(broker_bracket_primary.get("side") or ""),
            "broker_bracket_primary_qty": broker_bracket_primary.get("qty"),
            "broker_bracket_primary_market_value": broker_bracket_primary.get("market_value"),
            "broker_bracket_primary_unrealized_pnl": broker_bracket_primary.get("unrealized_pnl"),
            "broker_bracket_primary_coverage_status": str(broker_bracket_primary.get("coverage_status") or ""),
            "paper_live_status": paper_live_status,
            "paper_live_effective_status": paper_live_effective_status,
            "paper_live_effective_detail": paper_live_effective_detail,
            "paper_live_held_by_bracket_audit": paper_live_held_by_bracket_audit,
            "paper_live_critical_ready": paper_live_critical_ready,
            "paper_live_ready_bots": int(paper_live_transition.get("paper_ready_bots") or 0),
            "paper_live_launch_blocked_count": int(
                paper_live_transition.get("operator_queue_launch_blocked_count") or 0
            ),
            "paper_live_source_age_s": paper_live_transition.get("source_age_s"),
            "vps_root_reconciliation_status": str(vps_root_reconciliation.get("status") or "unknown"),
            "vps_root_risk_level": str(vps_root_reconciliation.get("risk_level") or "unknown"),
            "vps_root_cleanup_allowed": bool(vps_root_reconciliation.get("cleanup_allowed")),
            "vps_root_artifact_stale": bool(vps_root_reconciliation.get("artifact_stale")),
            "vps_root_source_deleted_count": int(vps_root_summary.get("source_or_governance_deleted") or 0),
            "vps_root_submodule_drift": int(
                vps_root_summary.get("submodule_drift") or vps_root_counts.get("submodule_drift") or 0
            ),
            "vps_root_submodule_uninitialized": int(
                vps_root_summary.get("submodule_uninitialized") or vps_root_counts.get("submodule_uninitialized") or 0
            ),
            "vps_root_generated_untracked": int(
                vps_root_summary.get("generated_untracked") or vps_root_counts.get("generated_untracked") or 0
            ),
            "vps_root_status_rows": int(vps_root_counts.get("status") or 0),
            "vps_root_dirty_companion_repos": int(
                vps_root_summary.get("dirty_companion_repos") or vps_root_counts.get("dirty_companion_repos") or 0
            ),
            "vps_root_recommended_action": str(vps_root_reconciliation.get("recommended_action") or ""),
            "vps_root_review_step_count": len(vps_root_steps),
            "vps_root_top_step_id": str(vps_root_top_step.get("id") or ""),
            "vps_root_top_step_title": str(vps_root_top_step.get("title") or ""),
            "vps_root_top_step_risk": str(vps_root_top_step.get("risk") or ""),
            "vps_root_top_step_decision": str(vps_root_top_step.get("decision") or ""),
            "vps_root_top_step_action": str(vps_root_top_step.get("action") or ""),
            "vps_root_top_step_evidence_count": len(vps_root_top_step_evidence),
            "vps_root_top_step_evidence": vps_root_top_step_evidence,
            "vps_root_companion_step_id": str(vps_root_companion_step.get("id") or ""),
            "vps_root_companion_step_title": str(vps_root_companion_step.get("title") or ""),
            "vps_root_companion_step_risk": str(vps_root_companion_step.get("risk") or ""),
            "vps_root_companion_step_decision": str(
                vps_root_companion_step.get("decision") or "",
            ),
            "vps_root_companion_step_action": str(vps_root_companion_step.get("action") or ""),
            "vps_root_companion_step_evidence_count": len(vps_root_companion_step_evidence),
            "vps_root_companion_step_evidence": vps_root_companion_step_evidence,
            "portfolio_hidden_disabled_count": portfolio_summary["hidden_disabled_count"],
            "ibkr_gateway_status": ibkr_gateway.get("status") or broker_gateway.get("status"),
            "ibkr_gateway_detail": ibkr_gateway.get("detail") or broker_gateway.get("detail"),
            **broker_summary,
        },
        "active_bots": active_bots,
        "runtime_active_bots": active_bots,
        "staged_bots": staged_bots,
        "portfolio_summary": portfolio_summary,
        "close_history": close_history,
        "close_history_window": close_history_window,
        "close_history_rows": default_close_rows,
        "close_history_row_count": len(default_close_rows),
        "default_close_history_window": default_close_history_window,
        "history_window_pnl": history_window_pnl,
        "latest_signal_ts": signal_cadence["latest_signal_ts"],
        "signal_cadence": signal_cadence,
        "target_exit_summary": target_exit_summary,
        "server_ts": now_ts,
        "live": fills_stats,
        "live_broker_state": live_broker_state,
        "broker_gateway": broker_gateway,
        "broker_router": _broker_router_snapshot(),
        "broker_bracket_audit": broker_bracket_audit,
        "paper_live_transition": paper_live_transition,
        "vps_root_reconciliation": vps_root_reconciliation,
        "window_since_days": since_days,
        **truth,
    }


@app.get("/api/dashboard/live-summary")
def dashboard_live_summary(
    response: Response,
    since_days: int = 1,
) -> dict:
    """Fast first-paint dashboard payload using cached broker truth only.

    The public ops page should not wait on a fresh broker probe before it can
    display the current book. This route keeps the same top-level shape as
    ``/api/bot-fleet`` but forces the broker path through the cached diagnostic
    snapshot and compact close-history rows.
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    payload = bot_fleet_roster(Response(), since_days=since_days, live_broker_probe=False)
    payload["source"] = "dashboard_live_summary_cached_broker"
    payload["fast_summary"] = True
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    summary["dashboard_payload_tier"] = "live_summary"
    summary["live_broker_probe_mode"] = "cached_diagnostics"
    payload["summary"] = summary
    live_broker_state = payload.get("live_broker_state")
    if isinstance(live_broker_state, dict):
        live_broker_state["fast_summary"] = True
        live_broker_state.setdefault("probe_skipped", True)
        live_broker_state["close_history"] = _limit_close_history_recent_rows(
            _normalize_close_history_count_alias(live_broker_state.get("close_history") or {}),
        )
        position_exposure = live_broker_state.get("position_exposure")
        if isinstance(position_exposure, dict):
            position_exposure["recent_closes"] = (
                position_exposure.get("recent_closes")
                if isinstance(position_exposure.get("recent_closes"), list)
                else []
            )[:_DASHBOARD_POSITION_EXPOSURE_CLOSE_ROW_LIMIT]
            position_exposure["close_history"] = _limit_close_history_recent_rows(
                _normalize_close_history_count_alias(position_exposure.get("close_history") or {}),
            )
    return payload


@app.get("/api/dashboard/close-history")
def dashboard_close_history(
    response: Response,
    window: str = "mtd",
    limit: int = 6,
) -> dict:
    """Lazy close-history rows for expanded dashboard panels."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    window = str(window or "mtd").lower()
    if window not in {"today", "wtd", "mtd", "ytd", "all"}:
        raise HTTPException(status_code=400, detail={"error_code": "invalid_close_history_window"})
    limit = max(1, min(int(limit), _DASHBOARD_LAZY_CLOSE_HISTORY_MAX_LIMIT))
    now_utc = datetime.now(UTC)
    all_trade_closes = _recent_trade_closes(limit=5000)
    focus_trade_closes = [row for row in all_trade_closes if not _trade_close_is_cellar(row)]
    close_history = _normalize_close_history_count_alias(_close_history_windows(focus_trade_closes, now=now_utc))
    windows = close_history.get("windows") if isinstance(close_history.get("windows"), dict) else {}
    selected = windows.get(window) if isinstance(windows.get(window), dict) else {}
    selected = dict(selected)
    rows = selected.get("recent_outcomes") if isinstance(selected.get("recent_outcomes"), list) else []
    selected["recent_outcomes"] = rows[:limit]
    selected["count"] = _close_window_count(selected)
    return {
        "source": "trade_close_ledger",
        "window": window,
        "timezone": DASHBOARD_LOCAL_TIME_ZONE_NAME,
        "server_ts": time.time(),
        "close_history": close_history,
        "close_history_window": selected,
        "close_history_rows": selected["recent_outcomes"],
        "close_history_row_count": len(selected["recent_outcomes"]),
        "history_window_pnl": {
            "label": selected.get("label") or window.upper(),
            "pnl": _float_value(selected.get("realized_pnl")),
            "count": _close_window_count(selected),
            "closed_outcome_count": int(selected.get("closed_outcome_count") or 0),
            "evaluated_outcome_count": int(selected.get("evaluated_outcome_count") or 0),
            "win_rate": _float_value(selected.get("win_rate")),
            "source": selected.get("source") or "trade_close_ledger",
        },
    }


@app.get("/api/bot-fleet/{bot_id}")
def bot_fleet_drilldown(bot_id: str) -> dict:
    """Per-bot drill: status + recent fills + recent verdicts + sage effects."""
    if not _BOT_ID_RE.match(bot_id):
        raise HTTPException(status_code=400, detail={"error_code": "invalid_bot_id"})
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    bot_dir = _state_dir() / "bots" / bot_id
    readiness_rows = _bot_strategy_readiness_rows_by_bot()
    supervisor_statuses = _supervisor_roster_rows(time.time(), bot=bot_id)
    supervisor_status = supervisor_statuses[0] if supervisor_statuses else None
    supervisor_overlay_keys = (
        "status",
        "todays_pnl",
        "todays_pnl_source",
        "last_signal_ts",
        "last_signal_age_s",
        "last_signal_side",
        "last_activity_ts",
        "last_activity_age_s",
        "last_activity_side",
        "last_activity_type",
        "open_position",
        "open_positions",
        "position_state",
        "bracket_stop",
        "bracket_target",
        "broker_bracket",
        "bracket_src",
        "strategy_readiness",
        "launch_lane",
        "can_paper_trade",
        "can_live_trade",
        "readiness_next_action",
        "mode",
        "last_jarvis_verdict",
        "heartbeat_age_s",
        "source",
    )
    if supervisor_status is not None:
        _apply_bot_strategy_readiness(
            supervisor_status,
            _lookup_bot_strategy_readiness(readiness_rows, supervisor_status, bot_id),
        )
        strategy_readiness = supervisor_status.get("strategy_readiness")
        if not isinstance(strategy_readiness, dict):
            strategy_readiness = {}
    else:
        strategy_readiness = {}
    if not bot_dir.exists():
        if supervisor_status is not None:
            return {
                "status": supervisor_status,
                "recent_fills": [],
                "recent_verdicts": [],
                "sage_effects": {},
                "strategy_readiness": strategy_readiness,
                "launch_lane": supervisor_status.get("launch_lane") or strategy_readiness.get("launch_lane") or "",
                "can_paper_trade": bool(
                    supervisor_status.get("can_paper_trade") or strategy_readiness.get("can_paper_trade")
                ),
                "can_live_trade": bool(
                    supervisor_status.get("can_live_trade") or strategy_readiness.get("can_live_trade")
                ),
                "readiness_next_action": str(
                    supervisor_status.get("readiness_next_action") or strategy_readiness.get("next_action") or "",
                ),
            }
        readiness = _lookup_bot_strategy_readiness(readiness_rows, {}, bot_id)
        if readiness:
            status = _readiness_only_roster_row(readiness, now_ts=time.time())
            strategy_readiness = (
                status.get("strategy_readiness") if isinstance(status.get("strategy_readiness"), dict) else {}
            )
            return {
                "status": status,
                "recent_fills": [],
                "recent_verdicts": [],
                "sage_effects": {},
                "strategy_readiness": strategy_readiness,
                "launch_lane": status.get("launch_lane") or strategy_readiness.get("launch_lane") or "",
                "can_paper_trade": bool(status.get("can_paper_trade") or strategy_readiness.get("can_paper_trade")),
                "can_live_trade": bool(status.get("can_live_trade") or strategy_readiness.get("can_live_trade")),
                "readiness_next_action": str(
                    status.get("readiness_next_action") or strategy_readiness.get("next_action") or "",
                ),
            }
        return {
            "_warning": "no_data",
            "status": {"_warning": "no_data"},
            "recent_fills": [],
            "recent_verdicts": [],
            "sage_effects": {"_warning": "no_data"},
        }
    status = read_json_safe(bot_dir / "status.json")
    if supervisor_status is not None:
        for key in supervisor_overlay_keys:
            if key in supervisor_status:
                status[key] = supervisor_status[key]
        strategy_readiness = status.get("strategy_readiness")
        if not isinstance(strategy_readiness, dict):
            strategy_readiness = {}
    _apply_bot_strategy_readiness(
        status,
        _lookup_bot_strategy_readiness(readiness_rows, status, bot_id),
    )
    strategy_readiness = status.get("strategy_readiness")
    if not isinstance(strategy_readiness, dict):
        strategy_readiness = {}
    recent_fills = read_json_safe(bot_dir / "recent_fills.json")
    local_fills = recent_fills if isinstance(recent_fills, list) else []
    merged_fills: list[dict] = []
    dedup_keys: set[tuple] = set()
    for row in local_fills:
        if not isinstance(row, dict):
            continue
        key = (row.get("ts"), row.get("side"), row.get("price"), row.get("qty"))
        if key in dedup_keys:
            continue
        dedup_keys.add(key)
        merged_fills.append(row)
    for row in _recent_live_fill_rows(bot=bot_id, limit=80):
        key = (row.get("ts"), row.get("side"), row.get("price"), row.get("qty"))
        if key in dedup_keys:
            continue
        dedup_keys.add(key)
        merged_fills.append(row)
    merged_fills.sort(
        key=lambda x: str(x.get("ts") or ""),
        reverse=True,
    )
    return {
        "status": status,
        "recent_fills": merged_fills[:50],
        "recent_verdicts": read_json_safe(bot_dir / "recent_verdicts.json"),
        "sage_effects": read_json_safe(bot_dir / "sage_effects.json"),
        "strategy_readiness": strategy_readiness,
        "launch_lane": status.get("launch_lane") or strategy_readiness.get("launch_lane") or "",
        "can_paper_trade": bool(status.get("can_paper_trade") or strategy_readiness.get("can_paper_trade")),
        "can_live_trade": bool(status.get("can_live_trade") or strategy_readiness.get("can_live_trade")),
        "readiness_next_action": str(
            status.get("readiness_next_action") or strategy_readiness.get("next_action") or "",
        ),
    }


@app.get("/api/live/fills")
def live_fills(limit: int = 30, response: Response = None) -> dict:
    """Latest fills for tape bootstrap/fallback rendering."""
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    limit = max(1, min(limit, 100))
    rows = _recent_live_fill_rows(limit=limit)
    return {"fills": rows, "server_ts": time.time()}


@app.get("/api/risk_gates")
def risk_gates() -> dict:
    """Per-bot kill latch + DD + cap state + fleet aggregate."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    latches = read_json_safe(_state_dir() / "safety" / "kill_switch_latch.json")
    fleet_agg = read_json_safe(_state_dir() / "safety" / "fleet_risk_gate_state.json")
    bots = []
    if "_warning" not in latches:
        for bot_id, row in latches.items():
            if not isinstance(row, dict):
                continue
            row_out = {"bot_id": bot_id, **row}
            bots.append(row_out)
    return {"bots": bots, "fleet_aggregate": fleet_agg}


@app.get("/api/positions/reconciler")
def positions_reconciler() -> dict:
    """Latest position reconciler snapshot."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    return read_json_safe(_state_dir() / "safety" / "position_reconciler_latest.json")


class SageModulationToggleRequest(BaseModel):
    enabled: bool


def _supervisor_equity_payload(
    *,
    bot: str | None,
    range_key: str,
    range_label: str,
    since_days: int,
    normalize: bool,
) -> dict | None:
    """Build a truthful live equity payload from the JARVIS supervisor heartbeat."""
    from datetime import UTC, datetime

    now_ts = time.time()
    rows = _supervisor_roster_rows(now_ts, bot=bot)
    if not rows:
        return None

    total_pnl = 0.0
    latest_ts = ""
    for row in rows:
        with contextlib.suppress(TypeError, ValueError):
            total_pnl += float(row.get("todays_pnl") or 0.0)
        row_ts = str(
            row.get("last_trade_ts") or row.get("last_activity_ts") or row.get("last_signal_ts") or "",
        )
        if row_ts > latest_ts:
            latest_ts = row_ts

    source_dt = None
    if latest_ts:
        with contextlib.suppress(ValueError, OSError):
            source_dt = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
            if source_dt.tzinfo is None:
                source_dt = source_dt.replace(tzinfo=UTC)
    source_epoch = source_dt.timestamp() if source_dt is not None else now_ts
    source_age_s = max(0, int(now_ts - source_epoch))
    baseline = 5000.0 if bot is not None else float(max(1, len(rows)) * 5000)
    current_equity = round(baseline + total_pnl, 2)
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    point_ts = latest_ts or datetime.fromtimestamp(now_ts, UTC).isoformat()
    rounded_pnl = round(total_pnl, 2)
    series = [
        {"ts": today_start.isoformat().replace("+00:00", "Z"), "equity": round(baseline, 2)},
        {"ts": point_ts, "equity": current_equity},
    ]
    summary = {
        "current_equity": current_equity,
        "today_pnl": rounded_pnl,
        "week_pnl": rounded_pnl,
        "month_pnl": rounded_pnl,
        "total_pnl": rounded_pnl,
        "total_pnl_is_lifetime": False,
        "total_pnl_source": "supervisor_session_fallback",
        "lifetime_ledger_attached": False,
        "lifetime_total_pnl": None,
    }
    truth = _truth_snapshot(rows, server_ts=now_ts)
    return {
        "bot_id": bot,
        "range": range_label,
        "series": series,
        "curve": series,
        "summary": summary,
        "lifetime_ledger_attached": False,
        "lifetime_total_pnl": None,
        "baseline_equity": baseline if normalize else None,
        "server_ts": now_ts,
        "data_ts": source_epoch,
        "data_age_s": source_age_s,
        "source_updated_at": latest_ts or point_ts,
        "source_age_s": source_age_s,
        "source_heartbeat_count": len(rows),
        "source": "supervisor_heartbeat",
        "since_days": since_days,
        "live": _fills_activity_snapshot(bot=bot),
        "pnl": rounded_pnl,
        "session_cum_pnl": rounded_pnl,
        "session_truth_status": truth["truth_status"],
        "session_truth_line": truth["truth_summary_line"],
        range_key: series,
        **truth,
    }


@app.get("/api/equity")
def equity_curve(
    bot: str | None = None,
    range: str = "1d",
    normalize: bool = False,
    since_days: int = 1,
    response: Response = None,
) -> dict:
    """Equity curve + P&L summary for fleet or a specific bot.

    Params:
      bot:   optional bot_id (e.g. "mnq"); None = fleet aggregate
      range: "1d" | "1w" | "1m" | "all" (default 1d)
    """
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    if bot is not None and not _BOT_ID_RE.match(bot):
        raise HTTPException(status_code=400, detail={"error_code": "invalid_bot_id"})

    if range not in ("1d", "1w", "1m", "all"):
        raise HTTPException(status_code=400, detail={"error_code": "invalid_range"})
    since_days = max(0, min(int(since_days), 7))

    # Resolve source file
    if bot:
        source = _state_dir() / "bots" / bot / "equity_curve.json"
    else:
        source = _state_dir() / "blotter" / "equity_curve.json"
    source_mtime = _safe_mtime(source)
    _fill_totals, _fill_latest, fills_mtime = _fill_r_by_bot_since_days(since_days)

    data = read_json_safe(source)
    if "_warning" in data:
        series_key = {"1d": "today", "1w": "week", "1m": "month", "all": "all_time"}[range]
        supervisor_payload = _supervisor_equity_payload(
            bot=bot,
            range_key=series_key,
            range_label=range,
            since_days=since_days,
            normalize=normalize,
        )
        if supervisor_payload is not None:
            return supervisor_payload
        truth = _truth_snapshot([], server_ts=time.time())
        return {
            "bot_id": bot,
            "range": range,
            "series": [],
            "curve": [],
            "summary": {
                "current_equity": None,
                "today_pnl": None,
                "week_pnl": None,
                "month_pnl": None,
                "total_pnl": None,
            },
            "_warning": "no_data",
            "session_cum_pnl": 0.0,
            "session_truth_status": truth["truth_status"],
            "session_truth_line": truth["truth_summary_line"],
            "server_ts": truth["truth_checked_at"],
            "source": "canonical_state_empty",
            **truth,
        }

    # Pick the right series for the requested range
    series_key = {"1d": "today", "1w": "week", "1m": "month", "all": "all_time"}[range]
    # If the file uses old `thirty_day` key, fall back to it for backwards-compat
    series = data.get(series_key) or data.get("thirty_day") or []
    series_source = "blotter_curve" if bot is None else "bot_curve"

    if bot is None:
        # If fleet blotter curve is stale/missing, aggregate from per-bot curves.
        agg_series, agg_mtime = _aggregate_fleet_curve_from_bots(series_key)
        prefer_agg = bool(agg_series) and (
            not series or source_mtime is None or (agg_mtime is not None and agg_mtime > source_mtime)
        )
        if prefer_agg:
            series = agg_series
            data = dict(data)
            data[series_key] = agg_series
            # Keep compatibility keys coherent for summary math.
            if series_key == "today":
                data["today"] = agg_series
            if series_key == "week":
                data["week"] = agg_series
            if series_key == "month":
                data["month"] = agg_series
            if series_key == "all_time":
                data["all_time"] = agg_series
            source_mtime = agg_mtime or source_mtime
            series_source = "aggregated_bot_curves"

    # For intraday view, always synthesize directly from today's fills.
    if range == "1d":
        live_baseline = 5000.0
        if bot is None:
            bots_dir = _state_dir() / "bots"
            bot_count = sum(1 for p in bots_dir.iterdir() if p.is_dir()) if bots_dir.exists() else 7
            live_baseline = float(max(1, bot_count) * 5000)
        series_live = _intraday_equity_from_fills(bot, live_baseline, since_days)
        if series_live:
            series = series_live
            data = dict(data)
            data["today"] = series_live
            source_mtime = fills_mtime
            series_source = "fills_intraday"

    baseline = None
    if normalize:
        # Rebase to clean paper baselines:
        # - per bot: 5,000 starting equity
        # - fleet aggregate: 5,000 x active bot count (default 7 => 35,000)
        baseline = 5000.0
        if bot is None:
            bots_dir = _state_dir() / "bots"
            bot_count = sum(1 for p in bots_dir.iterdir() if p.is_dir()) if bots_dir.exists() else 7
            baseline = float(max(1, bot_count) * 5000)
        rebased_data = dict(data)
        for k in ("today", "week", "month", "all_time", "thirty_day"):
            if isinstance(rebased_data.get(k), list):
                rebased_data[k] = _rebase_series(rebased_data[k], baseline)
        series = _rebase_series(series, baseline)
        summary = _compute_pnl_summary(rebased_data)
    else:
        summary = data.get("summary") or _compute_pnl_summary(data)

    server_ts = time.time()
    source_age_s = max(0, int(server_ts - source_mtime)) if source_mtime is not None else None
    source_updated_at = datetime.fromtimestamp(source_mtime, UTC).isoformat() if source_mtime is not None else None
    # Preserve legacy keys (today/thirty_day) for backwards-compat with old test
    out = {
        "bot_id": bot,
        "range": range,
        **_dashboard_contract(),
        "series": series,
        "curve": series,
        "summary": summary,
        "baseline_equity": baseline,
        "server_ts": server_ts,
        "data_ts": source_mtime,
        "data_age_s": source_age_s,
        "source_updated_at": source_updated_at,
        "source_age_s": source_age_s,
        "source": series_source,
        "since_days": since_days,
        "live": _fills_activity_snapshot(bot=bot),
        "session_cum_pnl": float(summary.get("today_pnl") or 0.0),
        "session_truth_status": "live" if series else "no_data",
    }
    # Carry through legacy keys so existing consumers (and the
    # `test_equity_returns_curve` test) continue to work.
    for legacy_key in ("today", "thirty_day", "week", "month", "all_time"):
        if legacy_key in data:
            out[legacy_key] = data[legacy_key]
    return out


@app.get("/api/fleet-equity")
def fleet_equity_curve(
    bot: str | None = None,
    range: str = "1d",
    normalize: bool = False,
    since_days: int = 1,
    response: Response = None,
) -> dict:
    """Compatibility alias for the public fleet equity widget."""
    return equity_curve(
        bot=bot,
        range=range,
        normalize=normalize,
        since_days=since_days,
        response=response,
    )


def _compute_pnl_summary(data: dict) -> dict:
    """Compute P&L summary from a curve file.

    Falls back to today/thirty_day if explicit week/month series are missing.
    """
    today = data.get("today") or []
    week = data.get("week") or data.get("thirty_day") or []
    month = data.get("month") or data.get("thirty_day") or []
    all_time = data.get("all_time") or month or week or today

    def first_eq(s: list) -> float | None:
        return s[0]["equity"] if s else None

    def last_eq(s: list) -> float | None:
        return s[-1]["equity"] if s else None

    def diff(s: list) -> float | None:
        f = first_eq(s)
        last = last_eq(s)
        return None if (f is None or last is None) else round(last - f, 2)

    current = last_eq(today) or last_eq(week) or last_eq(month) or last_eq(all_time)

    return {
        "current_equity": current,
        "today_pnl": diff(today),
        "week_pnl": diff(week),
        "month_pnl": diff(month),
        "total_pnl": diff(all_time),
    }


def _rebase_series(series: list[dict], baseline: float) -> list[dict]:
    """Rebase an equity curve to a clean baseline while preserving PnL deltas."""
    if not series:
        return []
    first = series[0].get("equity")
    if first is None:
        return series
    try:
        anchor = float(first)
    except (TypeError, ValueError):
        return series
    rebased: list[dict] = []
    for point in series:
        p = dict(point)
        try:
            eq = float(point.get("equity"))
            p["equity"] = round(baseline + (eq - anchor), 2)
        except (TypeError, ValueError):
            pass
        rebased.append(p)
    return rebased


def _safe_mtime(path: Path) -> float | None:
    try:
        if path.exists():
            return float(path.stat().st_mtime)
    except OSError:
        return None
    return None


def _aggregate_fleet_curve_from_bots(range_key: str) -> tuple[list[dict], float | None]:
    """Build fleet curve by summing per-bot curves by timestamp."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    bots_dir = _state_dir() / "bots"
    if not bots_dir.exists():
        return ([], None)

    by_ts: dict[str, float] = defaultdict(float)
    latest_source_ts: float | None = None
    for bot_dir in bots_dir.iterdir():
        if not bot_dir.is_dir():
            continue
        curve_path = bot_dir / "equity_curve.json"
        curve = read_json_safe(curve_path)
        if not isinstance(curve, dict):
            continue
        series = curve.get(range_key) or curve.get("thirty_day") or []
        if not isinstance(series, list):
            continue
        for point in series:
            if not isinstance(point, dict):
                continue
            ts = str(point.get("ts") or "")
            eq = point.get("equity")
            if not ts:
                continue
            try:
                by_ts[ts] += float(eq)
            except (TypeError, ValueError):
                continue
        mts = _safe_mtime(curve_path)
        if mts is not None:
            latest_source_ts = mts if latest_source_ts is None else max(latest_source_ts, mts)

    if not by_ts:
        return ([], latest_source_ts)

    merged = [{"ts": ts, "equity": round(eq, 2)} for ts, eq in sorted(by_ts.items(), key=lambda item: item[0])]
    return (merged, latest_source_ts)


def _fill_r_by_bot_since_days(since_days: int = 0) -> tuple[dict[str, float], dict[str, dict], float | None]:
    """Return per-bot realized-R totals since UTC day cutoff and latest fill row."""
    fills_path = _state_dir() / "blotter" / "fills.jsonl"
    totals: dict[str, float] = defaultdict(float)
    latest: dict[str, dict] = {}
    latest_mtime = _safe_mtime(fills_path)
    if not fills_path.exists():
        return (totals, latest, latest_mtime)
    since_days = max(0, min(int(since_days), 7))
    now_utc = datetime.now(UTC)
    cutoff_dt = (now_utc - timedelta(days=since_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        for raw in fills_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            bot = str(row.get("bot") or "").strip()
            if not bot:
                continue
            ts_dt = _parse_fill_dt(row.get("ts"))
            if ts_dt is not None and ts_dt >= cutoff_dt:
                with contextlib.suppress(TypeError, ValueError):
                    # Sanitize so r=69-style tick leaks don't poison rollups
                    r_clean = _sanitize_trade_close_r(row)
                    if r_clean is not None:
                        totals[bot] += float(r_clean)
            curr = latest.get(bot)
            if curr is None or str(row.get("ts") or "") >= str(curr.get("ts") or ""):
                latest[bot] = row
    except OSError:
        pass
    return (totals, latest, latest_mtime)


def _intraday_equity_from_fills(bot: str | None, baseline: float, since_days: int = 0) -> list[dict]:
    """Build an intraday equity-like curve from realized-R fills since UTC cutoff."""
    fills_path = _state_dir() / "blotter" / "fills.jsonl"
    if not fills_path.exists():
        return []
    since_days = max(0, min(int(since_days), 7))
    now_utc = datetime.now(UTC)
    cutoff_dt = (now_utc - timedelta(days=since_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows: list[dict] = []
    try:
        for raw in fills_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_dt = _parse_fill_dt(row.get("ts"))
            if ts_dt is None or ts_dt < cutoff_dt:
                continue
            if bot and str(row.get("bot") or "") != bot:
                continue
            rows.append(row)
    except OSError:
        return []
    rows.sort(key=lambda r: str(r.get("ts") or ""))
    eq = baseline
    out: list[dict] = []
    for row in rows:
        with contextlib.suppress(TypeError, ValueError):
            # Sanitize each step so equity curve doesn't get a +69R cliff
            r_clean = _sanitize_trade_close_r(row)
            if r_clean is not None:
                eq += float(r_clean)
        out.append({"ts": str(row.get("ts") or ""), "equity": round(eq, 2)})
    return out


def _parse_fill_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    ts = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


_LIVE_FILL_STATUSES = {"FILLED", "PARTIAL", "PARTIALLY_FILLED", "EXECUTED"}
_NON_FILL_STATUSES = {
    "CANCELLED",
    "CANCELED",
    "INACTIVE",
    "PENDING",
    "PENDINGSUBMIT",
    "PRESUBMITTED",
    "REJECTED",
    "SUBMITTED",
}


def _first_present(row: dict, keys: tuple[str, ...]) -> object:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _float_value(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _derive_ibkr_today_realized_pnl(snapshot: dict) -> float:
    """Best-effort split of IBKR's intraday futures PnL into realized PnL.

    IBKR account summary exposes ``FuturesPNL`` as the current-day futures
    bucket. When positions are still open, subtract the live unrealized PnL so
    the dashboard keeps the open exposure in the unrealized bucket. Once the
    book is flat, the full futures bucket should show up as realized.
    """

    futures_pnl = _float_value(snapshot.get("futures_pnl"))
    unrealized = _float_value(snapshot.get("unrealized_pnl")) or 0.0
    if futures_pnl is not None:
        realized = futures_pnl - unrealized
        if abs(realized) < 0.005:
            return 0.0
        return round(realized, 2)
    for key in ("req_pnl_realized_pnl", "account_summary_realized_pnl"):
        fallback = _float_value(snapshot.get(key))
        if fallback is not None:
            if abs(fallback) < 0.005:
                return 0.0
            return round(fallback, 2)
    return 0.0


def _ibkr_client_portal_base_url() -> str:
    raw = str(
        os.environ.get("ETA_IBKR_CLIENT_PORTAL_BASE_URL")
        or os.environ.get("ETA_IBKR_CP_BASE_URL")
        or "https://127.0.0.1:5000/v1/api"
    ).strip()
    return (raw or "https://127.0.0.1:5000/v1/api").rstrip("/")


def _ibkr_client_portal_request(
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout_s: float | None = None,
) -> dict | list:
    """Best-effort JSON request helper for the local IBKR Client Portal gateway."""

    url = f"{_ibkr_client_portal_base_url()}/{path.lstrip('/')}"
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib_request.Request(
        url,
        data=body,
        method=method.upper(),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        resolved_timeout = float(timeout_s or os.environ.get("ETA_IBKR_CP_TIMEOUT_S", "8"))
    except ValueError:
        resolved_timeout = 8.0
    kwargs: dict[str, Any] = {"timeout": max(1.0, resolved_timeout)}
    if url.lower().startswith(("https://127.0.0.1", "https://localhost")):
        kwargs["context"] = ssl._create_unverified_context()
    try:
        with urllib_request.urlopen(request, **kwargs) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        detail = detail[:240] if detail else ""
        msg = f"http_{exc.code}"
        if detail:
            msg = f"{msg}: {detail}"
        raise RuntimeError(msg) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"request_failed:{type(exc).__name__}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        preview = raw[:160].replace("\n", " ").strip()
        raise RuntimeError(f"invalid_json: {preview}") from exc


def _series_last_numeric(values: Any) -> float | None:
    if not isinstance(values, list):
        return None
    for item in reversed(values):
        value = _float_value(item)
        if value is not None:
            return value
    return None


def _ibkr_extract_mtd_performance(payload: dict, *, account_id: str | None = None) -> dict:
    """Normalize IBKR Client Portal MTD performance payloads into one shape."""

    out = {
        "ready": False,
        "period": "MTD",
        "account_id": account_id,
        "source": "ibkr_client_portal_pa_performance_mtd",
        "mtd_pnl": None,
        "mtd_return_pct": None,
        "start_nav": None,
        "end_nav": None,
        "dates": [],
    }
    if not isinstance(payload, dict):
        out["error"] = "invalid_payload"
        return out

    period_payload: dict[str, Any] | None = None
    matched_account = account_id
    candidate_accounts: list[str] = []
    if account_id:
        candidate_accounts.append(account_id)
    candidate_accounts.extend(
        key
        for key, value in payload.items()
        if isinstance(key, str) and key not in candidate_accounts and isinstance(value, dict)
    )
    for candidate in candidate_accounts:
        account_payload = payload.get(candidate)
        if isinstance(account_payload, dict) and isinstance(account_payload.get("MTD"), dict):
            period_payload = dict(account_payload["MTD"])
            matched_account = candidate
            break

    if period_payload is None:
        nav_section = payload.get("nav") if isinstance(payload.get("nav"), dict) else {}
        cps_section = payload.get("cps") if isinstance(payload.get("cps"), dict) else {}
        nav_rows = nav_section.get("data") if isinstance(nav_section.get("data"), list) else []
        cps_rows = cps_section.get("data") if isinstance(cps_section.get("data"), list) else []
        nav_row = next(
            (
                row
                for row in nav_rows
                if isinstance(row, dict) and (not account_id or str(row.get("id") or "") == account_id)
            ),
            None,
        )
        if nav_row is None and len(nav_rows) == 1 and isinstance(nav_rows[0], dict):
            nav_row = nav_rows[0]
        cps_row = next(
            (
                row
                for row in cps_rows
                if isinstance(row, dict) and (not account_id or str(row.get("id") or "") == account_id)
            ),
            None,
        )
        if cps_row is None and len(cps_rows) == 1 and isinstance(cps_rows[0], dict):
            cps_row = cps_rows[0]
        if nav_row or cps_row:
            period_payload = {}
            if isinstance(nav_row, dict):
                period_payload["nav"] = (
                    nav_row.get("navs") if isinstance(nav_row.get("navs"), list) else nav_row.get("nav")
                )
                if isinstance(nav_row.get("dates"), list):
                    period_payload["dates"] = nav_row.get("dates")
                matched_account = str(nav_row.get("id") or matched_account or "")
            if isinstance(cps_row, dict):
                period_payload["cps"] = (
                    cps_row.get("returns") if isinstance(cps_row.get("returns"), list) else cps_row.get("cps")
                )
                if not period_payload.get("dates") and isinstance(cps_row.get("dates"), list):
                    period_payload["dates"] = cps_row.get("dates")
                matched_account = str(cps_row.get("id") or matched_account or "")
            if isinstance(payload.get("startNAV"), dict):
                period_payload["startNAV"] = payload.get("startNAV")
            elif isinstance(nav_section.get("startNAV"), dict):
                period_payload["startNAV"] = nav_section.get("startNAV")

    if not isinstance(period_payload, dict):
        out["error"] = "mtd_payload_missing"
        return out

    start_nav = None
    if isinstance(period_payload.get("startNAV"), dict):
        start_nav = _float_value(
            period_payload["startNAV"].get("val")
            or period_payload["startNAV"].get("value")
            or period_payload["startNAV"].get("nav")
        )
    if start_nav is None:
        start_nav = _float_value(period_payload.get("start_nav") or period_payload.get("startNAV"))
    nav_series = period_payload.get("nav") if isinstance(period_payload.get("nav"), list) else []
    end_nav = _series_last_numeric(nav_series)
    cps_series = period_payload.get("cps") if isinstance(period_payload.get("cps"), list) else []
    return_value = _series_last_numeric(cps_series)
    out.update(
        {
            "account_id": matched_account or account_id,
            "start_nav": start_nav,
            "end_nav": end_nav,
            "dates": period_payload.get("dates") if isinstance(period_payload.get("dates"), list) else [],
            "mtd_return_pct": round(return_value * 100, 2) if return_value is not None else None,
        }
    )
    if start_nav is not None and end_nav is not None:
        out["mtd_pnl"] = round(end_nav - start_nav, 2)
        out["ready"] = True
    else:
        out["error"] = "mtd_nav_missing"
    return out


def _ibkr_client_portal_mtd_snapshot(
    *,
    managed_accounts: list[str] | None = None,
    preferred_account: str | None = None,
) -> dict:
    """Fetch month-to-date account performance from the local IBKR Client Portal."""

    enabled = str(os.environ.get("ETA_IBKR_CP_MTD_ENABLED", "1")).strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return {
            "ready": False,
            "period": "MTD",
            "account_id": preferred_account,
            "source": "ibkr_client_portal_pa_performance_mtd",
            "error": "disabled",
        }

    requested_accounts: list[str] = []
    for account in [preferred_account, *(managed_accounts or [])]:
        text = str(account or "").strip()
        if text and text != "All" and text not in requested_accounts:
            requested_accounts.append(text)
    if not requested_accounts:
        return {
            "ready": False,
            "period": "MTD",
            "account_id": preferred_account,
            "source": "ibkr_client_portal_pa_performance_mtd",
            "error": "account_missing",
        }

    try:
        payload = _ibkr_client_portal_request(
            "/pa/performance",
            method="POST",
            payload={"acctIds": requested_accounts, "period": "MTD"},
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ready": False,
            "period": "MTD",
            "account_id": requested_accounts[0],
            "source": "ibkr_client_portal_pa_performance_mtd",
            "requested_accounts": requested_accounts,
            "error": str(exc),
        }
    extracted = _ibkr_extract_mtd_performance(payload, account_id=requested_accounts[0])
    extracted["requested_accounts"] = requested_accounts
    return extracted


def _ibkr_mtd_tracker_state_path() -> Path:
    """Canonical state file for IBKR month-baseline net-liq tracking."""
    return _state_dir() / "broker_mtd" / "ibkr_net_liq_month_tracker.json"


def _ibkr_mtd_override_state_path() -> Path:
    """Canonical state file for operator-seeded IBKR month-baseline overrides."""
    return _state_dir() / "broker_mtd" / "ibkr_net_liq_month_overrides.json"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write ``payload`` to ``path`` using a temp file + replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp_name, str(path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _utc_month_key(now_utc: datetime | None = None) -> str:
    """Return the current UTC month key as ``YYYY-MM``."""
    ts = now_utc.astimezone(UTC) if now_utc is not None else datetime.now(UTC)
    return ts.strftime("%Y-%m")


def _ibkr_mtd_manual_override(account_id: str, month_key: str) -> dict[str, Any]:
    """Return an operator-seeded IBKR MTD baseline override when present."""
    override_path = _ibkr_mtd_override_state_path()
    if not override_path.exists():
        return {}
    try:
        loaded = json.loads(override_path.read_text(encoding="utf-8").lstrip("\ufeff"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    accounts = loaded.get("accounts")
    if not isinstance(accounts, dict):
        return {}
    for account_key in (account_id, "*", "default"):
        account_state = accounts.get(account_key)
        if not isinstance(account_state, dict):
            continue
        month_state = account_state.get(month_key)
        if not isinstance(month_state, dict):
            continue
        baseline_net_liq = _float_value(month_state.get("baseline_net_liquidation"))
        if baseline_net_liq is None:
            continue
        baseline_set_at = str(month_state.get("baseline_set_at") or f"{month_key}-01T00:00:00+00:00").strip()
        return {
            "baseline_net_liquidation": round(float(baseline_net_liq), 2),
            "baseline_set_at": baseline_set_at,
            "source": str(month_state.get("source") or "manual_override").strip() or "manual_override",
            "note": str(month_state.get("note") or "").strip(),
        }
    return {}


def _ibkr_net_liquidation_mtd_snapshot(
    *,
    account_id: str | None,
    net_liquidation: float | None,
    checked_at: str | None = None,
    now_utc: datetime | None = None,
) -> dict:
    """Track IBKR paper-account MTD from net liquidation when CP is unavailable.

    The dashboard prefers Client Portal ``/pa/performance`` because it exposes
    true portfolio MTD. When that sidecar is down, we fall back to a canonical
    month baseline persisted under ``var/eta_engine/state`` and compute:

      current_net_liq - first_seen_net_liq_for_this_utc_month

    This makes Broker MTD durable and self-healing across dashboard restarts
    without writing outside the canonical ETA workspace.
    """
    out = {
        "ready": False,
        "period": "MTD",
        "account_id": str(account_id or "").strip() or None,
        "source": "ibkr_net_liquidation_month_tracker",
        "error": "",
    }
    account_text = str(account_id or "").strip()
    current_net_liq = _float_value(net_liquidation)
    if not account_text:
        out["error"] = "account_missing"
        return out
    if current_net_liq is None:
        out["error"] = "net_liquidation_missing"
        return out

    current_net_liq = round(float(current_net_liq), 2)
    ts_utc = now_utc.astimezone(UTC) if now_utc is not None else datetime.now(UTC)
    checked_iso = str(checked_at or ts_utc.isoformat())
    month_key = _utc_month_key(ts_utc)
    tracker_path = _ibkr_mtd_tracker_state_path()
    tracker_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = tracker_path.with_suffix(".lock")
    manual_override = _ibkr_mtd_manual_override(account_text, month_key)

    with portalocker.Lock(str(lock_path), mode="a", timeout=5, flags=portalocker.LOCK_EX):
        tracker_state: dict[str, Any] = {}
        if tracker_path.exists():
            with contextlib.suppress(OSError, json.JSONDecodeError):
                loaded = json.loads(tracker_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    tracker_state = loaded
        accounts = tracker_state.setdefault("accounts", {})
        if not isinstance(accounts, dict):
            accounts = {}
            tracker_state["accounts"] = accounts
        account_state = accounts.setdefault(account_text, {})
        if not isinstance(account_state, dict):
            account_state = {}
            accounts[account_text] = account_state
        month_state = account_state.get(month_key)
        if not isinstance(month_state, dict):
            month_state = {}
            account_state[month_key] = month_state

        baseline_net_liq = _float_value(month_state.get("baseline_net_liquidation"))
        baseline_set_at = str(month_state.get("baseline_set_at") or "").strip()
        baseline_origin = str(month_state.get("baseline_origin") or "").strip().lower()
        baseline_note = str(month_state.get("baseline_note") or "").strip()
        baseline_initialized = baseline_net_liq is None
        manual_baseline_net_liq = _float_value(manual_override.get("baseline_net_liquidation"))
        if manual_baseline_net_liq is not None:
            baseline_net_liq = round(float(manual_baseline_net_liq), 2)
            baseline_set_at = str(
                manual_override.get("baseline_set_at") or baseline_set_at or f"{month_key}-01T00:00:00+00:00"
            ).strip()
            baseline_origin = "manual_override"
            baseline_note = str(manual_override.get("note") or baseline_note).strip()
            baseline_initialized = False
        elif baseline_origin == "manual_override" and baseline_net_liq is not None:
            if not baseline_set_at:
                baseline_set_at = f"{month_key}-01T00:00:00+00:00"
            baseline_initialized = False
        else:
            if baseline_net_liq is None:
                baseline_net_liq = current_net_liq
            if not baseline_set_at:
                baseline_set_at = checked_iso
            baseline_origin = "tracker"
            baseline_note = ""

        month_state.update(
            {
                "month": month_key,
                "baseline_net_liquidation": round(float(baseline_net_liq), 2),
                "baseline_set_at": baseline_set_at,
                "baseline_origin": baseline_origin,
                "baseline_note": baseline_note,
                "last_net_liquidation": current_net_liq,
                "last_seen_at": checked_iso,
            }
        )
        tracker_state["schema_version"] = 1
        tracker_state["updated_at"] = checked_iso
        _write_json_atomic(tracker_path, tracker_state)

    baseline_net_liq = round(float(baseline_net_liq), 2)
    mtd_pnl = round(current_net_liq - baseline_net_liq, 2)
    out.update(
        {
            "ready": True,
            "month": month_key,
            "source": (
                "ibkr_net_liquidation_month_manual_override"
                if baseline_origin == "manual_override"
                else (
                    "ibkr_net_liquidation_month_tracker_bootstrap"
                    if baseline_initialized
                    else "ibkr_net_liquidation_month_tracker"
                )
            ),
            "mtd_pnl": mtd_pnl,
            "start_nav": baseline_net_liq,
            "end_nav": current_net_liq,
            "baseline_set_at": baseline_set_at,
            "baseline_initialized": baseline_initialized,
            "baseline_origin": baseline_origin,
            "baseline_note": baseline_note,
        }
    )
    if baseline_net_liq not in {0.0, -0.0}:
        out["mtd_return_pct"] = round(((current_net_liq / baseline_net_liq) - 1.0) * 100.0, 2)
    return out


def _ibkr_cached_mtd_tracker_snapshot(now_utc: datetime | None = None) -> dict[str, Any]:
    """Read persisted IBKR MTD without opening a broker session."""
    tracker_path = _ibkr_mtd_tracker_state_path()
    if not tracker_path.exists():
        return {}
    now_utc = now_utc.astimezone(UTC) if now_utc is not None else datetime.now(UTC)
    month_key = _utc_month_key(now_utc)
    try:
        payload = json.loads(tracker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    accounts = payload.get("accounts") if isinstance(payload, dict) else {}
    if not isinstance(accounts, dict):
        return {}

    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for account_id, account_state in accounts.items():
        if not isinstance(account_state, dict):
            continue
        month_state = account_state.get(month_key)
        if not isinstance(month_state, dict):
            continue
        baseline = _float_value(month_state.get("baseline_net_liquidation"))
        last_net_liq = _float_value(month_state.get("last_net_liquidation"))
        if baseline is None or last_net_liq is None:
            continue
        baseline_origin = str(month_state.get("baseline_origin") or "").lower()
        source = (
            "ibkr_net_liquidation_month_manual_override"
            if baseline_origin == "manual_override"
            else "ibkr_net_liquidation_month_tracker"
        )
        checked_at = str(month_state.get("last_seen_at") or payload.get("updated_at") or "").strip()
        checked_dt = _parse_fill_dt(checked_at) or datetime.min.replace(tzinfo=UTC)
        mtd_pnl = round(float(last_net_liq) - float(baseline), 2)
        snapshot: dict[str, Any] = {
            "ready": False,
            "account_id": str(account_id),
            "net_liquidation": round(float(last_net_liq), 2),
            "account_mtd_pnl": mtd_pnl,
            "account_mtd_return_pct": (
                round(((float(last_net_liq) / float(baseline)) - 1.0) * 100.0, 2)
                if float(baseline) not in {0.0, -0.0}
                else None
            ),
            "account_mtd_source": source,
            "account_mtd_baseline_set_at": str(month_state.get("baseline_set_at") or ""),
            "account_mtd_checked_at": checked_at,
            "account_mtd_error": "",
            "source": "ibkr_net_liquidation_month_tracker_cache",
        }
        candidates.append((checked_dt, snapshot))
    if not candidates:
        return {}
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _recent_trade_closes(limit: int = 25) -> list[dict]:
    """Return the newest Jarvis trade-close ledger rows without heavy reads.

    Every row is run through the trade_close_sanitizer so downstream
    consumers (the open-book view, equity rollups, recent-trades list)
    see CLEAN realized_r values — never the r=69 tick-leak or the
    raw-USD-in-r-field bug from older bots. The original value is
    preserved in ``realized_r_raw`` for audit + a flag
    ``realized_r_sanitized`` marks rows that were touched.
    """
    path = _state_dir() / "jarvis_intel" / "trade_closes.jsonl"
    if not path.exists():
        return []
    try:
        max_bytes = int(os.environ.get("ETA_DASHBOARD_TRADE_CLOSE_TAIL_BYTES", "1048576"))
    except ValueError:
        max_bytes = 1_048_576
    max_bytes = max(8192, min(max_bytes, 8_388_608))
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            raw_lines = fh.read().decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return []

    out: list[dict] = []
    for raw in reversed(raw_lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            # Apply the sanitizer in place: any tick-leak or USD-leak in
            # realized_r is corrected before the row leaves this function.
            # Source-of-truth on disk stays untouched (preserved as
            # realized_r_raw on the in-memory copy for audit).
            _apply_sanitizer_inplace(row)
            out.append(row)
        if len(out) >= limit:
            break
    return out


def _apply_sanitizer_inplace(row: dict) -> None:
    """Mutate ``row`` so realized_r is sanitized + raw is preserved.

    Schema after:
      * ``realized_r``           — clean value (or None for unrecoverable)
      * ``realized_r_raw``       — original bot-written value
      * ``realized_r_sanitized`` — True when sanitizer changed the value
    """
    try:
        from eta_engine.brain.jarvis_v3.trade_close_sanitizer import sanitize_r

        raw_val = row.get("realized_r")
        try:
            raw_float = float(raw_val) if raw_val is not None else None
        except (TypeError, ValueError):
            raw_float = None
        cleaned = sanitize_r(row)
        row["realized_r_raw"] = raw_float
        row["realized_r_sanitized"] = cleaned != raw_float
        row["realized_r"] = cleaned
    except Exception:  # noqa: BLE001 — fail-soft, leave row untouched
        pass


def _normalize_live_position(row: dict, *, venue: str) -> dict | None:
    """Normalize Alpaca/IBKR position rows into one operator-facing shape."""
    if not isinstance(row, dict):
        return None
    symbol = row.get("symbol")
    if symbol is None:
        return None
    qty = _float_value(row.get("qty") if venue == "alpaca" else row.get("position"))
    raw_side = str(row.get("side") or "").strip().lower()
    if raw_side not in {"long", "short"}:
        if qty is not None and qty < 0:
            raw_side = "short"
        elif qty is not None and qty > 0:
            raw_side = "long"
        else:
            raw_side = "unknown"
    sec_type = row.get("secType") or row.get("sec_type")
    current_price = _float_value(row.get("current_price") if venue == "alpaca" else row.get("market_price"))
    avg_entry_price = _normalize_live_avg_entry_price(
        row,
        venue=venue,
        symbol=str(symbol),
        sec_type=sec_type,
        raw_avg_entry_price=_float_value(row.get("avg_entry_price") if venue == "alpaca" else row.get("avg_cost")),
        current_price=current_price,
    )
    normalized = {
        "venue": venue,
        "symbol": str(symbol),
        "side": raw_side,
        "qty": qty,
        "avg_entry_price": avg_entry_price,
        "current_price": current_price,
        "market_value": _float_value(row.get("market_value")),
        "unrealized_pnl": _float_value(row.get("unrealized_pl") if venue == "alpaca" else row.get("unrealized_pnl")),
        "unrealized_pct": _float_value(row.get("unrealized_plpc")),
        "sec_type": sec_type,
        "exchange": row.get("exchange"),
    }
    normalized["broker_bracket_required"] = _position_requires_broker_bracket(normalized)
    return normalized


def _live_position_contract_multiplier(row: dict, symbol: str) -> float | None:
    multiplier = _float_value(row.get("multiplier") or row.get("contract_multiplier") or row.get("contractMultiplier"))
    if multiplier is not None and multiplier > 0:
        return multiplier
    symbol_key = symbol.strip().upper()
    for root, value in sorted(
        _FUTURES_AVG_COST_MULTIPLIERS.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if symbol_key.startswith(root):
            return value
    return None


def _normalize_live_avg_entry_price(
    row: dict,
    *,
    venue: str,
    symbol: str,
    sec_type: object,
    raw_avg_entry_price: float | None,
    current_price: float | None,
) -> float | None:
    """Normalize broker-reported futures average cost into price points."""
    if raw_avg_entry_price is None:
        return None
    if venue not in _BROKER_BRACKET_REQUIRED_VENUES:
        return raw_avg_entry_price
    if str(sec_type or "").strip().upper() not in _BROKER_BRACKET_REQUIRED_SEC_TYPES:
        return raw_avg_entry_price
    multiplier = _live_position_contract_multiplier(row, symbol)
    if multiplier is None or multiplier <= 0 or current_price is None:
        return raw_avg_entry_price
    candidate = raw_avg_entry_price / multiplier
    if abs(candidate - current_price) < abs(raw_avg_entry_price - current_price):
        return candidate
    return raw_avg_entry_price


def _position_requires_broker_bracket(position: dict) -> bool:
    """True for broker-open instruments that should have broker-side OCO protection."""
    venue = str(position.get("venue") or "").strip().lower()
    if venue not in _BROKER_BRACKET_REQUIRED_VENUES:
        return False
    sec_type = str(position.get("sec_type") or position.get("secType") or "").strip().upper()
    if sec_type in _BROKER_BRACKET_REQUIRED_SEC_TYPES:
        return True
    return _portfolio_sleeve_for_symbol(position.get("symbol")) in _IBKR_ROUTER_SLEEVES


def _normalize_trade_close(row: dict) -> dict | None:
    """Normalize Jarvis close-ledger rows for dashboard close evidence."""
    if not isinstance(row, dict):
        return None
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    layers_updated = row.get("layers_updated")
    layer_errors = row.get("layer_errors")
    qty_value = _first_present(extra, ("qty",))
    if qty_value is None:
        qty_value = _first_present(row, ("qty", "quantity"))
    fill_value = _first_present(extra, ("fill_price", "price"))
    if fill_value is None:
        fill_value = _first_present(row, ("fill_price", "price"))
    pnl_value = _first_present(extra, ("realized_pnl", "pnl"))
    if pnl_value is None:
        pnl_value = _first_present(row, ("realized_pnl", "pnl"))
    # Sanitize the per-trade realized_r so the open-book view doesn't
    # display the r=69 tick-leak bug. If the row was already sanitized
    # by ``_recent_trade_closes`` upstream, ``realized_r_raw`` is already
    # set — preserve it. Otherwise compute now.
    if "realized_r_raw" in row:
        raw_for_audit = row.get("realized_r_raw")
        sanitized_r = _float_value(row.get("realized_r"))
        was_sanitized = bool(row.get("realized_r_sanitized"))
    else:
        raw_for_audit = _float_value(row.get("realized_r"))
        sanitized_r = _sanitize_trade_close_r(row)
        was_sanitized = sanitized_r != raw_for_audit
    return {
        "ts": row.get("ts") or extra.get("close_ts"),
        "close_ts": extra.get("close_ts") or row.get("ts"),
        "bot_id": row.get("bot_id"),
        "symbol": extra.get("symbol") or row.get("symbol"),
        "side": extra.get("side") or row.get("direction"),
        "qty": _float_value(qty_value),
        "fill_price": _float_value(fill_value),
        "realized_pnl": _float_value(pnl_value),
        "realized_r": sanitized_r,
        "realized_r_raw": raw_for_audit,
        "realized_r_sanitized": was_sanitized,
        "action_taken": row.get("action_taken"),
        "layers_updated": layers_updated if isinstance(layers_updated, list) else [],
        "layer_errors": layer_errors if isinstance(layer_errors, list) else [],
    }


def _sanitize_trade_close_r(row: dict) -> float | None:
    """Run a trade_closes row through the canonical sanitizer.

    Defends the open-book view against the r=69 tick-leak bug (and the
    older raw-USD-in-r-field bug). The sanitizer recovers the true R
    when ``extra.realized_pnl`` + ``extra.symbol`` are present and the
    symbol root has a known dollar-per-R; otherwise returns the original
    value if clean, or None if it's an unrecoverable suspect.
    """
    try:
        from eta_engine.brain.jarvis_v3.trade_close_sanitizer import sanitize_r

        return sanitize_r(row)
    except Exception:  # noqa: BLE001 — fail-soft to original value
        try:
            return float(row.get("realized_r"))
        except (TypeError, ValueError):
            return None


def _closed_outcomes_from_filled_orders(orders: list[dict]) -> dict:
    """Derive same-day closed outcomes from broker filled-order pairs.

    This is intentionally conservative and only pairs fills from the order
    payload already returned by the broker. It does not claim lifetime or 30d
    performance; it gives the dashboard a truthful same-day outcome rate when
    the close ledger has not yet written realized PnL rows.
    """
    lots_by_symbol: dict[str, list[dict[str, float | str]]] = defaultdict(list)
    outcomes: list[dict[str, object]] = []

    def _order_dt(row: dict) -> datetime:
        parsed = _parse_fill_dt(row.get("filled_at") or row.get("ts") or row.get("submitted_at"))
        return parsed or datetime.min.replace(tzinfo=UTC)

    for order in sorted((o for o in orders if isinstance(o, dict)), key=_order_dt):
        symbol = str(order.get("symbol") or "").strip()
        side = str(order.get("side") or "").strip().lower()
        qty = _float_value(order.get("filled_qty") or order.get("qty"))
        price = _float_value(order.get("filled_avg_price") or order.get("price"))
        if not symbol or side not in {"buy", "sell"} or qty is None or qty <= 0 or price is None:
            continue
        remaining = qty
        opposite = "sell" if side == "buy" else "buy"
        lots = lots_by_symbol[symbol]
        while remaining > 1e-12 and lots and lots[0]["side"] == opposite:
            lot = lots[0]
            lot_qty = float(lot["qty"])
            close_qty = min(remaining, lot_qty)
            entry_price = float(lot["price"])
            pnl = (price - entry_price) * close_qty if lot["side"] == "buy" else (entry_price - price) * close_qty
            outcomes.append(
                {
                    "symbol": symbol,
                    "closed_side": "long" if lot["side"] == "buy" else "short",
                    "qty": round(close_qty, 8),
                    "entry_price": round(entry_price, 8),
                    "exit_price": round(price, 8),
                    "realized_pnl": round(pnl, 6),
                    "closed_at": order.get("filled_at") or order.get("ts"),
                },
            )
            remaining -= close_qty
            lot["qty"] = round(lot_qty - close_qty, 12)
            if float(lot["qty"]) <= 1e-12:
                lots.pop(0)
        if remaining > 1e-12:
            lots.append({"side": side, "qty": round(remaining, 12), "price": price})

    wins = sum(1 for row in outcomes if float(row.get("realized_pnl") or 0.0) > 0)
    losses = sum(1 for row in outcomes if float(row.get("realized_pnl") or 0.0) < 0)
    evaluated = wins + losses
    win_rate = round(wins / evaluated, 4) if evaluated else None
    return {
        "closed_outcome_count": len(outcomes),
        "evaluated_outcome_count": evaluated,
        "winning_outcomes": wins,
        "losing_outcomes": losses,
        "win_rate": win_rate,
        "recent_outcomes": outcomes[-20:][::-1],
    }


def _close_outcome_pnl_map(outcomes: list[dict[str, object]], *, limit: int = 5) -> dict[str, object]:
    """Aggregate closed outcomes into distinct winner/loser PnL impact rows."""
    grouped: dict[str, dict[str, object]] = {}
    for row in outcomes:
        if not isinstance(row, dict):
            continue
        pnl = _float_value(row.get("realized_pnl"))
        if pnl is None or pnl == 0:
            continue
        bot_id = str(row.get("bot_id") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        key_value = bot_id or symbol
        if not key_value:
            continue
        key = f"{'bot' if bot_id else 'symbol'}:{key_value.lower()}"
        bucket = grouped.setdefault(
            key,
            {
                "bot_id": bot_id or None,
                "symbol": symbol,
                "sleeve": _portfolio_sleeve_for_symbol(symbol),
                "closes": 0,
                "realized_pnl": 0.0,
            },
        )
        bucket["closes"] = int(bucket.get("closes") or 0) + 1
        bucket["realized_pnl"] = float(bucket.get("realized_pnl") or 0.0) + pnl
        if symbol and not bucket.get("symbol"):
            bucket["symbol"] = symbol
            bucket["sleeve"] = _portfolio_sleeve_for_symbol(symbol)

    rows: list[dict[str, object]] = []
    for row in grouped.values():
        realized = round(float(row.get("realized_pnl") or 0.0), 2)
        rows.append(
            {
                **row,
                "realized_pnl": realized,
                "impact_value": round(abs(realized), 2),
                "source": "trade_close_ledger",
            }
        )
    winners = sorted(
        (row for row in rows if float(row.get("realized_pnl") or 0.0) > 0),
        key=lambda row: (-float(row.get("realized_pnl") or 0.0), str(row.get("bot_id") or row.get("symbol") or "")),
    )[:limit]
    losers = sorted(
        (row for row in rows if float(row.get("realized_pnl") or 0.0) < 0),
        key=lambda row: (float(row.get("realized_pnl") or 0.0), str(row.get("bot_id") or row.get("symbol") or "")),
    )[:limit]
    return {
        "limit": limit,
        "top_winners": winners,
        "top_losers": losers,
    }


def _closed_outcomes_from_trade_closes(
    closes: list[dict],
    *,
    since: datetime | None = None,
    row_limit: int | None = 20,
) -> dict:
    """Derive realized W/L truth from the Jarvis trade-close ledger."""
    outcomes: list[dict[str, object]] = []
    wins = 0
    losses = 0
    realized_total = 0.0

    for row in closes:
        normalized = _normalize_trade_close(row)
        if normalized is None:
            continue
        ts_dt = _parse_fill_dt(normalized.get("close_ts") or normalized.get("ts"))
        if since is not None and (ts_dt is None or ts_dt < since):
            continue
        pnl = _float_value(normalized.get("realized_pnl"))
        if pnl is None:
            continue
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        realized_total += pnl
        outcomes.append(normalized)

    evaluated = wins + losses
    count = len(outcomes)
    return {
        "closed_outcome_count": count,
        "count": count,
        "evaluated_outcome_count": evaluated,
        "winning_outcomes": wins,
        "losing_outcomes": losses,
        "win_rate": round(wins / evaluated, 4) if evaluated else None,
        "realized_pnl": round(realized_total, 2),
        "pnl_map": _close_outcome_pnl_map(outcomes, limit=5),
        "recent_outcomes": outcomes[:row_limit] if row_limit is not None else outcomes,
    }


def _close_window_count(window: dict) -> int:
    """Closed-outcome count alias used by dashboards and lightweight probes."""
    count = _float_value(window.get("count"))
    if count is None:
        count = _float_value(window.get("closed_outcome_count"))
    if count is None and isinstance(window.get("recent_outcomes"), list):
        count = len(window["recent_outcomes"])
    return int(count or 0)


def _normalize_close_history_count_alias(close_history: dict) -> dict:
    """Return close-history payload with stable ``count`` aliases per window."""
    if not isinstance(close_history, dict):
        return {}
    out = dict(close_history)
    windows = close_history.get("windows") if isinstance(close_history.get("windows"), dict) else {}
    normalized_windows: dict[str, dict] = {}
    for key, window in windows.items():
        if not isinstance(window, dict):
            continue
        normalized = dict(window)
        count = _close_window_count(normalized)
        normalized["closed_outcome_count"] = int(normalized.get("closed_outcome_count") or count)
        normalized["count"] = count
        normalized_windows[str(key)] = normalized
    out["windows"] = normalized_windows
    default_window = str(out.get("default_window") or "mtd")
    out["default_window"] = default_window
    default_payload = normalized_windows.get(default_window)
    if not isinstance(default_payload, dict):
        default_payload = normalized_windows.get("mtd") if isinstance(normalized_windows.get("mtd"), dict) else {}
        if default_payload:
            out["default_window"] = "mtd"
            default_window = "mtd"
    out["default_label"] = (
        out.get("default_label")
        or (default_payload.get("label") if isinstance(default_payload, dict) else None)
        or default_window.upper()
    )
    return out


def _dashboard_local_window_starts_utc(now: datetime | None = None) -> dict[str, datetime]:
    """Return dashboard reporting windows as UTC instants anchored to Atlanta time."""
    ts = now or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    local_now = ts.astimezone(DASHBOARD_LOCAL_TIME_ZONE)
    today_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start_local = today_start_local - timedelta(days=today_start_local.weekday())
    month_start_local = today_start_local.replace(day=1)
    year_start_local = today_start_local.replace(month=1, day=1)
    return {
        "today": today_start_local.astimezone(UTC),
        "wtd": week_start_local.astimezone(UTC),
        "mtd": month_start_local.astimezone(UTC),
        "ytd": year_start_local.astimezone(UTC),
    }


def _dashboard_local_day_start_utc(now: datetime | None = None) -> datetime:
    return _dashboard_local_window_starts_utc(now)["today"]


def _limit_close_history_recent_rows(
    close_history: dict,
    *,
    row_limit: int | None = None,
) -> dict:
    """Keep close-window totals intact while capping browser-facing row lists."""
    if not isinstance(close_history, dict):
        return {}
    if row_limit is None:
        row_limit = _DASHBOARD_CLOSE_HISTORY_RECENT_ROW_LIMIT
    windows = close_history.get("windows") if isinstance(close_history.get("windows"), dict) else {}
    for window in windows.values():
        if not isinstance(window, dict):
            continue
        rows = window.get("recent_outcomes")
        if isinstance(rows, list) and len(rows) > row_limit:
            window["recent_outcomes"] = rows[:row_limit]
    return close_history


def _close_history_windows(
    closes: list[dict],
    *,
    now: datetime | None = None,
) -> dict:
    """Build operator-facing close-ledger windows for dashboard history controls."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    window_starts = _dashboard_local_window_starts_utc(now)
    today_start = window_starts["today"]
    week_start = window_starts["wtd"]
    month_start = window_starts["mtd"]
    year_start = window_starts["ytd"]
    windows = [
        ("today", "Today", today_start, _DASHBOARD_CLOSE_HISTORY_RECENT_ROW_LIMIT),
        ("wtd", "WTD", week_start, _DASHBOARD_CLOSE_HISTORY_RECENT_ROW_LIMIT),
        ("mtd", "MTD", month_start, _DASHBOARD_CLOSE_HISTORY_RECENT_ROW_LIMIT),
        ("ytd", "YTD", year_start, _DASHBOARD_CLOSE_HISTORY_RECENT_ROW_LIMIT),
        ("all", "All", None, _DASHBOARD_CLOSE_HISTORY_RECENT_ROW_LIMIT),
    ]
    out: dict[str, object] = {
        "source": "trade_close_ledger",
        "default_window": "mtd",
        "default_label": "MTD",
        "timezone": DASHBOARD_LOCAL_TIME_ZONE_NAME,
        "day_boundary": "local_midnight",
        "windows": {},
    }
    for key, label, since, row_limit in windows:
        summary = _closed_outcomes_from_trade_closes(
            closes,
            since=since,
            row_limit=row_limit,
        )
        summary.update(
            {
                "window": key,
                "label": label,
                "since": since.isoformat() if since is not None else None,
                "until": now.isoformat(),
                "source": "trade_close_ledger",
                "timezone": DASHBOARD_LOCAL_TIME_ZONE_NAME,
                "day_boundary": "local_midnight",
            },
        )
        out["windows"][key] = summary
    return _limit_close_history_recent_rows(out)


def _broker_summary_fields(live_broker_state: dict) -> dict:
    """Broker-backed rollup fields for /api/bot-fleet.summary.

    These fields are deliberately named as broker/session truth instead of
    ``total_pnl``. A missing lifetime ledger must not be rendered as a fake
    zero-dollar lifetime result by downstream dashboards.
    """
    if not isinstance(live_broker_state, dict) or live_broker_state.get("error"):
        return {}
    realized = _float_value(live_broker_state.get("today_realized_pnl"))
    unrealized = _float_value(live_broker_state.get("total_unrealized_pnl"))
    broker_mtd = _float_value(live_broker_state.get("broker_mtd_pnl"))
    broker_mtd_return_pct = _float_value(live_broker_state.get("broker_mtd_return_pct"))
    fills = _float_value(live_broker_state.get("today_actual_fills"))
    open_positions = _float_value(live_broker_state.get("open_position_count"))
    snapshot_age_s = _float_value(live_broker_state.get("broker_snapshot_age_s"))
    win_rate = _float_value(live_broker_state.get("win_rate_30d"))
    win_rate_today = _float_value(live_broker_state.get("win_rate_today"))
    closed_outcomes_today = _float_value(live_broker_state.get("closed_outcome_count_today"))
    recent_close_count_30d = _float_value(live_broker_state.get("recent_close_count_30d"))
    recent_close_realized_pnl_30d = _float_value(live_broker_state.get("recent_close_realized_pnl_30d"))
    out: dict[str, object] = {
        "pnl_summary_source": "live_broker_state",
        "broker_ready": bool(live_broker_state.get("ready")),
        "broker_probe_skipped": bool(live_broker_state.get("probe_skipped")),
        "broker_refresh_probe_failed": bool(live_broker_state.get("refresh_probe_failed")),
        "broker_snapshot_source": str(live_broker_state.get("broker_snapshot_source") or live_broker_state.get("source") or ""),
        "broker_snapshot_state": str(live_broker_state.get("broker_snapshot_state") or ""),
    }
    if snapshot_age_s is not None:
        out["broker_snapshot_age_s"] = round(snapshot_age_s, 1)
    if live_broker_state.get("refresh_probe_error"):
        out["broker_refresh_probe_error"] = str(live_broker_state.get("refresh_probe_error") or "")
    if live_broker_state.get("refresh_probe_source"):
        out["broker_refresh_probe_source"] = str(live_broker_state.get("refresh_probe_source") or "")
    if realized is not None:
        out["broker_today_realized_pnl"] = round(realized, 2)
    if unrealized is not None:
        out["broker_total_unrealized_pnl"] = round(unrealized, 2)
    if realized is not None or unrealized is not None:
        out["broker_net_pnl"] = round((realized or 0.0) + (unrealized or 0.0), 2)
    if broker_mtd is not None:
        sources = live_broker_state.get("sources") if isinstance(live_broker_state.get("sources"), dict) else {}
        out["broker_mtd_pnl"] = round(broker_mtd, 2)
        out["broker_mtd_source"] = str(sources.get("broker_mtd_pnl") or "")
    if broker_mtd_return_pct is not None:
        out["broker_mtd_return_pct"] = broker_mtd_return_pct
    if fills is not None:
        out["broker_today_actual_fills"] = int(fills)
    if open_positions is not None:
        out["broker_open_position_count"] = int(open_positions)
    if win_rate is not None:
        out["broker_win_rate_30d"] = win_rate
        out["broker_win_rate_30d_source"] = str(live_broker_state.get("win_rate_30d_source") or "")
    if win_rate_today is not None:
        out["broker_win_rate_today"] = win_rate_today
        out["broker_win_rate_source"] = str(live_broker_state.get("win_rate_source") or "")
    if closed_outcomes_today is not None:
        out["broker_closed_outcomes_today"] = int(closed_outcomes_today)
    if recent_close_count_30d is not None:
        out["broker_recent_close_count_30d"] = int(recent_close_count_30d)
    if recent_close_realized_pnl_30d is not None:
        out["broker_recent_close_realized_pnl_30d"] = round(recent_close_realized_pnl_30d, 2)
    return out


_PORTFOLIO_SLEEVE_ROOTS: dict[str, str] = {
    "BTC": "crypto",
    "ETH": "crypto",
    "SOL": "crypto",
    "XRP": "crypto",
    "ADA": "crypto",
    "AVAX": "crypto",
    "DOGE": "crypto",
    "DOT": "crypto",
    "LINK": "crypto",
    "MBT": "crypto_futures",
    "MET": "crypto_futures",
    "MNQ": "equity_index_futures",
    "NQ": "equity_index_futures",
    "ES": "equity_index_futures",
    "MES": "equity_index_futures",
    "M2K": "equity_index_futures",
    "RTY": "equity_index_futures",
    "MYM": "equity_index_futures",
    "YM": "equity_index_futures",
    "CL": "commodities",
    "MCL": "commodities",
    "NG": "commodities",
    "GC": "commodities",
    "MGC": "commodities",
    "6E": "rates_fx",
    "M6E": "rates_fx",
    "ZN": "rates_fx",
    "ZB": "rates_fx",
    "ZF": "rates_fx",
    "ZT": "rates_fx",
}
_PORTFOLIO_FUTURES_MONTH_CODES = "FGHJKMNQUVXZ"
_PORTFOLIO_ROOTS_BY_LENGTH = tuple(
    sorted(_PORTFOLIO_SLEEVE_ROOTS, key=len, reverse=True),
)


def _portfolio_symbol_root(symbol: object) -> str:
    """Normalize display symbols and dated futures contracts to a tradable root."""
    raw = str(symbol or "").upper().replace("/", "").replace("-", "").strip()
    root = re.sub(r"(USD|USDT)$", "", raw)
    if root in _PORTFOLIO_SLEEVE_ROOTS:
        return root

    continuous_root = re.sub(r"\d+$", "", root)
    if continuous_root in _PORTFOLIO_SLEEVE_ROOTS:
        return continuous_root

    for known_root in _PORTFOLIO_ROOTS_BY_LENGTH:
        if re.fullmatch(
            rf"{re.escape(known_root)}[{_PORTFOLIO_FUTURES_MONTH_CODES}]\d{{1,2}}",
            root,
        ):
            return known_root
    return continuous_root or root


def _portfolio_sleeve_for_symbol(symbol: object) -> str:
    """Group symbols into dashboard-ready portfolio sleeves."""
    return _PORTFOLIO_SLEEVE_ROOTS.get(_portfolio_symbol_root(symbol), "other")


_FOCUS_PORTFOLIO_SLEEVES = frozenset(
    {
        "equity_index_futures",
        "commodities",
        "rates_fx",
        "crypto_futures",
    }
)
_CELLAR_PORTFOLIO_SLEEVES = frozenset({"crypto"})
_PRIMARY_FOCUS_BROKERS = ("ibkr",)
_STANDBY_FOCUS_BROKERS = ("tastytrade",)
_ACTIVE_FOCUS_BROKERS = _PRIMARY_FOCUS_BROKERS + _STANDBY_FOCUS_BROKERS
_PENDING_FOCUS_BROKERS: tuple[str, ...] = ()
_DORMANT_FOCUS_BROKERS = ("tradovate",)
_PAUSED_CELLAR_BROKERS = ("alpaca",)
_DASHBOARD_CLOSE_HISTORY_RECENT_ROW_LIMIT = 20
_DASHBOARD_POSITION_EXPOSURE_CLOSE_ROW_LIMIT = 12
_DASHBOARD_LAZY_CLOSE_HISTORY_MAX_LIMIT = 100


def _tradovate_auth_status_path() -> Path:
    """Canonical Tradovate auth-status receipt used for read-only dashboard truth."""
    return _state_dir() / "tradovate_auth_status.json"


def _tradovate_dashboard_status_payload() -> dict[str, object]:
    """Return the current Tradovate paper-portfolio posture for dashboard wording.

    The operator policy keeps Tradovate dormant unless explicitly reactivated.
    This helper does not claim live PnL or route status; it only surfaces the
    current read-only posture and the latest auth receipt when present.
    """
    enabled = str(os.environ.get("ETA_TRADOVATE_ENABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "y",
    }
    auth_path = _tradovate_auth_status_path()
    auth = _read_json_file(auth_path)
    result = str(auth.get("result") or "missing").strip().lower() if auth else "missing"
    raw_reason = str(auth.get("reason") or "").strip() if auth else ""
    reason = raw_reason if len(raw_reason) <= 180 else raw_reason[:177].rstrip() + "..."

    if enabled and result == "success":
        status = "paper_enabled"
        detail = "Tradovate paper auth is live; direct portfolio rollup is not attached yet."
    elif enabled and result == "failed":
        status = "auth_failed"
        detail = reason or "Tradovate paper auth failed."
    elif enabled:
        status = "awaiting_auth"
        detail = "Tradovate is enabled, but no auth receipt is available yet."
    elif result == "failed":
        status = "dormant_auth_failed"
        detail = "Tradovate is dormant; latest auth receipt failed."
    else:
        status = "dormant"
        detail = "Tradovate paper portfolios are dormant until explicitly reactivated."

    return {
        "enabled": enabled,
        "ready": status == "paper_enabled",
        "status": status,
        "detail": detail,
        "auth_path": str(auth_path),
        "has_auth_receipt": bool(auth),
        "auth_result": result,
        "demo": bool(auth.get("demo")) if auth else None,
        "endpoint": auth.get("endpoint") if auth else None,
        "has_all_creds": bool(auth.get("has_all_creds")) if auth else False,
    }


def _dashboard_focus_policy_payload() -> dict[str, object]:
    """Current operator focus policy for dashboard/API consumers."""
    return {
        "mode": "futures_focus",
        "active_venues": list(_PRIMARY_FOCUS_BROKERS),
        "standby_venues": list(_STANDBY_FOCUS_BROKERS),
        "pending_venues": list(_PENDING_FOCUS_BROKERS),
        "dormant_venues": list(_DORMANT_FOCUS_BROKERS),
        "paused_venues": list(_PAUSED_CELLAR_BROKERS),
        "focus_sleeves": sorted(_FOCUS_PORTFOLIO_SLEEVES),
        "cellar_sleeves": sorted(_CELLAR_PORTFOLIO_SLEEVES),
        "pnl_sources": {
            "session": "ibkr_live_broker_state",
            "mtd_closed": "trade_close_ledger",
            "tradovate_portfolio": "dormant_until_enabled",
        },
        "note": (
            "IBKR is the live futures truth path, Tastytrade is the standby lane, "
            "Tradovate stays dormant, and Alpaca/spot stay on the backburner."
        ),
    }


def _portfolio_symbol_is_cellar(symbol: object) -> bool:
    """True when a symbol belongs to the paused spot/Alpaca cellar."""
    return _portfolio_sleeve_for_symbol(symbol) in _CELLAR_PORTFOLIO_SLEEVES


def _portfolio_position_is_cellar(position: dict) -> bool:
    """True when a broker position should be hidden from focus cards."""
    venue = str(position.get("venue") or "").strip().lower()
    return venue in _PAUSED_CELLAR_BROKERS or _portfolio_symbol_is_cellar(position.get("symbol"))


def _mark_position_policy(position: dict) -> dict:
    """Attach focus/cellar policy metadata to a normalized position row."""
    out = dict(position)
    if _portfolio_position_is_cellar(out):
        out["policy_status"] = "paused_cellar"
        out["policy_reason"] = "Alpaca/spot paused by operator focus policy."
    else:
        out["policy_status"] = "focus"
        out["policy_reason"] = "Regulated futures focus lane."
    return out


def _trade_close_symbol(row: dict) -> object:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    return _first_present(extra, ("symbol", "local_symbol", "contract_symbol")) or _first_present(
        row,
        ("symbol", "local_symbol", "contract_symbol"),
    )


def _trade_close_is_cellar(row: dict) -> bool:
    return _portfolio_symbol_is_cellar(_trade_close_symbol(row))


def _contributor_pnl_value(row: dict[str, Any]) -> float | None:
    """Return the signed PnL field from a raw contributor row."""
    return _float_value(
        row.get("pnl")
        if row.get("pnl") is not None
        else row.get("unrealized_pnl")
        if row.get("unrealized_pnl") is not None
        else row.get("realized_pnl")
        if row.get("realized_pnl") is not None
        else row.get("today_pnl"),
    )


def _aggregate_portfolio_contributors(contributors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse raw contributor rows into distinct strategy/ticker impacts."""
    grouped: dict[str, dict[str, Any]] = {}
    for row in contributors:
        if not isinstance(row, dict):
            continue
        bot_id = str(row.get("bot_id") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        symbol_root = str(row.get("symbol_root") or _portfolio_symbol_root(symbol) or "").strip()
        key_value = bot_id or symbol_root or symbol
        if not key_value:
            continue
        aggregation = "strategy" if bot_id else "ticker"
        group_key = f"{aggregation}:{key_value.lower()}"
        pnl = _contributor_pnl_value(row)
        exposure = abs(
            _float_value(row.get("market_value"))
            or _float_value(row.get("exposure"))
            or _float_value(row.get("notional"))
            or 0.0
        )
        source_type = str(row.get("type") or "").strip().lower()
        venue = str(row.get("venue") or "").strip().lower()
        ownership_status = str(row.get("ownership_status") or "").strip()

        bucket = grouped.setdefault(
            group_key,
            {
                "type": "aggregated_contributor",
                "aggregation": aggregation,
                "aggregation_key": key_value,
                "bot_id": bot_id or None,
                "symbol": symbol or None,
                "symbol_root": symbol_root or None,
                "sleeve": str(row.get("sleeve") or _portfolio_sleeve_for_symbol(symbol) or "other"),
                "ownership_status": ownership_status,
                "venues": set(),
                "sources": set(),
                "pnl": 0.0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "market_value": 0.0,
                "close_count": 0,
                "open_count": 0,
            },
        )
        if bot_id and not bucket.get("bot_id"):
            bucket["bot_id"] = bot_id
        if symbol and not bucket.get("symbol"):
            bucket["symbol"] = symbol
        if symbol_root and not bucket.get("symbol_root"):
            bucket["symbol_root"] = symbol_root
        if venue:
            bucket["venues"].add(venue)
        if row.get("source"):
            bucket["sources"].add(str(row.get("source")))
        if ownership_status:
            existing_ownership = str(bucket.get("ownership_status") or "").strip()
            if not existing_ownership or existing_ownership == "managed_symbol":
                bucket["ownership_status"] = ownership_status
        if pnl is not None:
            bucket["pnl"] += float(pnl)
            if source_type == "recent_close_realized":
                bucket["realized_pnl"] += float(pnl)
            else:
                bucket["unrealized_pnl"] += float(pnl)
        if exposure:
            bucket["market_value"] += float(exposure)
        if source_type == "recent_close_realized":
            bucket["close_count"] += 1
        else:
            bucket["open_count"] += 1

    out: list[dict[str, Any]] = []
    for bucket in grouped.values():
        venues = sorted(bucket.pop("venues"))
        sources = sorted(bucket.pop("sources"))
        total_pnl = round(float(bucket.get("pnl") or 0.0), 2)
        realized_pnl = round(float(bucket.get("realized_pnl") or 0.0), 2)
        unrealized_pnl = round(float(bucket.get("unrealized_pnl") or 0.0), 2)
        market_value = round(float(bucket.get("market_value") or 0.0), 2)
        out.append(
            {
                **bucket,
                "venue": venues[0] if len(venues) == 1 else ("multi" if venues else ""),
                "source": sources[0] if len(sources) == 1 else ("multiple_sources" if sources else ""),
                "venues": venues,
                "sources": sources,
                "pnl": total_pnl,
                "realized_pnl": realized_pnl if bucket["close_count"] else None,
                "unrealized_pnl": unrealized_pnl if bucket["open_count"] else None,
                "market_value": market_value if market_value else None,
                "impact_value": round(abs(total_pnl), 2),
            }
        )
    out.sort(
        key=lambda row: (
            -abs(float(row.get("pnl") or 0.0)),
            -abs(float(row.get("market_value") or 0.0)),
            str(row.get("bot_id") or row.get("symbol") or row.get("aggregation_key") or ""),
        ),
    )
    return out


def _portfolio_summary_payload(
    rows: list[dict],
    live_broker_state: dict,
    *,
    hidden_disabled_count: int = 0,
    close_history: dict | None = None,
) -> dict:
    """API-level allocation and PnL truth for premium dashboard graphs."""
    rows = [row for row in rows if isinstance(row, dict)]
    live_broker_state = live_broker_state if isinstance(live_broker_state, dict) else {}
    close_history = close_history if isinstance(close_history, dict) else {}
    broker_summary = _broker_summary_fields(live_broker_state)
    exposure = (
        live_broker_state.get("position_exposure")
        if isinstance(live_broker_state.get("position_exposure"), dict)
        else {}
    )
    focus_rows = [row for row in rows if not _portfolio_symbol_is_cellar(row.get("symbol"))]
    cellar_rows = [row for row in rows if _portfolio_symbol_is_cellar(row.get("symbol"))]
    cellar_symbols: set[str] = {str(row.get("symbol")) for row in cellar_rows if row.get("symbol")}

    sleeve_map: dict[str, dict[str, Any]] = {}
    for row in focus_rows:
        sleeve = _portfolio_sleeve_for_symbol(row.get("symbol"))
        bucket = sleeve_map.setdefault(
            sleeve,
            {
                "sleeve": sleeve,
                "bot_count": 0,
                "open_position_count": 0,
                "paper_ready_count": 0,
                "symbols": set(),
                "today_bot_pnl": 0.0,
            },
        )
        bucket["bot_count"] += 1
        if bool(row.get("can_paper_trade")):
            bucket["paper_ready_count"] += 1
        if _float_value(row.get("open_positions")) and float(row.get("open_positions") or 0) > 0:
            bucket["open_position_count"] += 1
        if row.get("symbol"):
            bucket["symbols"].add(str(row.get("symbol")))
        pnl = _float_value(row.get("todays_pnl"))
        if pnl is not None:
            bucket["today_bot_pnl"] += pnl

    allocation_sleeves = []
    for bucket in sleeve_map.values():
        allocation_sleeves.append(
            {
                **bucket,
                "symbols": sorted(bucket["symbols"]),
                "today_bot_pnl": round(float(bucket["today_bot_pnl"]), 2),
            }
        )
    allocation_sleeves.sort(
        key=lambda row: (-int(row["open_position_count"]), -int(row["bot_count"]), row["sleeve"]),
    )

    active_symbol_roots = {_portfolio_symbol_root(row.get("symbol")) for row in focus_rows if row.get("symbol")}
    contributors: list[dict[str, Any]] = []
    unassigned_broker_symbols: set[str] = set()
    for pos in exposure.get("open_positions") or []:
        if not isinstance(pos, dict):
            continue
        unrealized = _float_value(pos.get("unrealized_pnl"))
        symbol = str(pos.get("symbol") or "")
        symbol_root = _portfolio_symbol_root(symbol)
        ownership_status = (
            "managed_symbol" if symbol_root and symbol_root in active_symbol_roots else "unassigned_broker_position"
        )
        if ownership_status == "unassigned_broker_position" and symbol:
            unassigned_broker_symbols.add(symbol)
        contributors.append(
            {
                "type": "open_position_unrealized",
                "venue": str(pos.get("venue") or ""),
                "symbol": symbol,
                "symbol_root": symbol_root,
                "sleeve": _portfolio_sleeve_for_symbol(symbol),
                "ownership_status": ownership_status,
                "side": pos.get("side"),
                "qty": _float_value(pos.get("qty")),
                "market_value": _float_value(pos.get("market_value")),
                "unrealized_pnl": round(unrealized, 2) if unrealized is not None else None,
                "source": "live_broker_state.position_exposure",
            }
        )
    cellar_positions = [pos for pos in (exposure.get("cellar_open_positions") or []) if isinstance(pos, dict)]
    for pos in cellar_positions:
        symbol = str(pos.get("symbol") or "")
        if symbol:
            cellar_symbols.add(symbol)
    for close in exposure.get("recent_closes") or []:
        if not isinstance(close, dict):
            continue
        realized = _float_value(close.get("realized_pnl"))
        if realized in (None, 0.0):
            continue
        contributors.append(
            {
                "type": "recent_close_realized",
                "venue": "",
                "bot_id": close.get("bot_id"),
                "symbol": str(close.get("symbol") or ""),
                "sleeve": _portfolio_sleeve_for_symbol(close.get("symbol")),
                "realized_pnl": round(realized, 2),
                "source": "live_broker_state.position_exposure",
            }
        )
    contributors = _aggregate_portfolio_contributors(contributors)
    focus_exposure = sum(
        abs(_float_value(pos.get("market_value")) or 0.0)
        for pos in exposure.get("open_positions") or []
        if isinstance(pos, dict)
    )
    focus_unrealized = sum(
        _float_value(pos.get("unrealized_pnl")) or 0.0
        for pos in exposure.get("open_positions") or []
        if isinstance(pos, dict)
    )
    close_windows = close_history.get("windows") if isinstance(close_history.get("windows"), dict) else {}
    today_window = close_windows.get("today") if isinstance(close_windows.get("today"), dict) else {}
    today_pnl_map = today_window.get("pnl_map") if isinstance(today_window.get("pnl_map"), dict) else {}

    return {
        "schema_version": 1,
        "source": "live_broker_state" if broker_summary else "bot_rows",
        "focus_policy": _dashboard_focus_policy_payload(),
        "allocation_sleeves": allocation_sleeves,
        "pnl_contributors": contributors[:12],
        "pnl_map": {
            "window": "today",
            "label": today_window.get("label") or "Today",
            "source": today_window.get("source") or close_history.get("source") or "trade_close_ledger",
            "closed_outcome_count": int(today_window.get("closed_outcome_count") or 0),
            "realized_pnl": _float_value(today_window.get("realized_pnl")),
            "top_winners": today_pnl_map.get("top_winners") if isinstance(today_pnl_map.get("top_winners"), list) else [],
            "top_losers": today_pnl_map.get("top_losers") if isinstance(today_pnl_map.get("top_losers"), list) else [],
        },
        "hidden_disabled_count": int(hidden_disabled_count),
        "unassigned_broker_position_count": len(unassigned_broker_symbols),
        "unassigned_broker_symbols": sorted(unassigned_broker_symbols),
        "broker_net_pnl": broker_summary.get("broker_net_pnl"),
        "broker_today_realized_pnl": broker_summary.get("broker_today_realized_pnl"),
        "broker_total_unrealized_pnl": broker_summary.get("broker_total_unrealized_pnl"),
        "open_position_count": int(exposure.get("open_position_count") or 0),
        "focus_open_position_count": int(exposure.get("open_position_count") or 0),
        "all_venue_open_position_count": int(
            live_broker_state.get("all_venue_open_position_count")
            or exposure.get("all_venue_open_position_count")
            or exposure.get("open_position_count")
            or 0
        ),
        "total_exposure": round(float(focus_exposure), 2),
        "focus_total_exposure": round(float(focus_exposure), 2),
        "focus_unrealized_pnl": round(float(focus_unrealized), 2),
        "bot_count": len(focus_rows),
        "all_bot_count": len(rows),
        "cellar_summary": {
            "policy_status": "paused_cellar",
            "hidden_bot_count": len(cellar_rows),
            "hidden_position_count": len(cellar_positions),
            "hidden_symbols": sorted(cellar_symbols),
            "paused_venues": list(_PAUSED_CELLAR_BROKERS),
            "paused_sleeves": sorted(_CELLAR_PORTFOLIO_SLEEVES),
            "note": "Alpaca/spot rows are hidden from main focus cards but retained as paused evidence.",
        },
    }


def _all_normalized_live_open_positions(live_broker_state: dict) -> list[dict]:
    """Normalize all broker open positions across venues with policy metadata."""
    live_broker_state = live_broker_state if isinstance(live_broker_state, dict) else {}
    open_positions: list[dict] = []
    for venue in ("alpaca", "ibkr"):
        venue_state = live_broker_state.get(venue) if isinstance(live_broker_state.get(venue), dict) else {}
        for row in venue_state.get("open_positions") or []:
            normalized = _normalize_live_position(row, venue=venue)
            if normalized:
                open_positions.append(_mark_position_policy(normalized))
    return open_positions


def _normalized_live_open_positions(live_broker_state: dict) -> list[dict]:
    """Normalize focus-lane broker open positions across venues."""
    return [
        position
        for position in _all_normalized_live_open_positions(live_broker_state)
        if not _portfolio_position_is_cellar(position)
    ]


def _cellar_live_open_positions(live_broker_state: dict) -> list[dict]:
    """Normalize paused Alpaca/spot positions for cellar evidence."""
    return [
        position
        for position in _all_normalized_live_open_positions(live_broker_state)
        if _portfolio_position_is_cellar(position)
    ]


def _broker_bracket_required_position_count(live_broker_state: dict) -> int | None:
    """Count broker positions that should have broker-native brackets.

    Returns ``None`` when the broker reported an open-position count but did
    not include per-position detail; callers can then preserve legacy
    fail-closed behavior.
    """
    positions = _normalized_live_open_positions(live_broker_state)
    if not positions and (_float_value(live_broker_state.get("open_position_count")) or 0) > 0:
        return None
    return sum(1 for position in positions if position.get("broker_bracket_required") is True)


def _broker_oco_evidence_payload(live_broker_state: dict) -> dict:
    """Build read-only broker OCO evidence from live positions and open orders."""
    live_broker_state = live_broker_state if isinstance(live_broker_state, dict) else {}
    positions = [
        position
        for position in _normalized_live_open_positions(live_broker_state)
        if position.get("broker_bracket_required") is True
    ]
    ibkr_state = live_broker_state.get("ibkr") if isinstance(live_broker_state.get("ibkr"), dict) else {}
    open_orders = ibkr_state.get("open_orders") if isinstance(ibkr_state, dict) else []
    try:
        from eta_engine.scripts import broker_bracket_audit  # noqa: PLC0415

        return broker_bracket_audit.build_broker_oco_evidence(
            positions,
            open_orders if isinstance(open_orders, list) else [],
        )
    except Exception as exc:  # noqa: BLE001 - status must fail soft.
        return {
            "kind": "eta_broker_oco_evidence",
            "schema_version": 1,
            "source": "broker_open_orders",
            "error": str(exc),
            "verified_count": 0,
            "verified_symbols": [],
            "positions": [],
        }


def _cached_live_broker_state_for_gateway_reconcile() -> dict:
    """Return cached IBKR live state for lightweight status-card reconciliation."""
    now_ts = time.time()
    warm_state: dict | None = None
    with _IBKR_PROBE_LOCK:
        cached = _IBKR_PROBE_CACHE.get("snapshot")
        cached_ts = float(_IBKR_PROBE_CACHE.get("ts") or 0.0)
        age_s = max(0.0, now_ts - cached_ts) if cached_ts > 0 else None
        if isinstance(cached, dict) and age_s is not None and age_s < (_IBKR_PROBE_CACHE_TTL_S * 2):
            ibkr = dict(cached)
            ibkr.setdefault("cache_ts", cached_ts)
            ibkr.setdefault("cache_age_s", round(age_s, 1))
            ibkr.setdefault("last_known", True)
            warm_state = {
                "ibkr": ibkr,
                "ibkr_cache_state": "warm",
                "ibkr_cache_ts": cached_ts,
                "ibkr_cache_age_s": round(age_s, 1),
            }
    if warm_state:
        ibkr = warm_state.get("ibkr") if isinstance(warm_state.get("ibkr"), dict) else {}
        if ibkr.get("ready") is True and not ibkr.get("error"):
            return warm_state
        persisted = _load_persisted_ibkr_probe_cache(now_ts=now_ts)
        if persisted:
            persisted_ibkr = persisted.get("ibkr") if isinstance(persisted.get("ibkr"), dict) else {}
            if ibkr.get("error"):
                persisted_ibkr["last_probe_error"] = ibkr.get("error")
            return persisted
        return warm_state
    persisted = _load_persisted_ibkr_probe_cache(now_ts=now_ts)
    if persisted:
        return persisted
    return {}


def _cached_live_broker_state_for_diagnostics() -> dict:
    """Fast broker state for diagnostics; never opens a broker connection."""
    state = _cached_live_broker_state_for_gateway_reconcile()
    if not state:
        state = _load_persisted_ibkr_probe_cache(include_stale=True)
    ibkr = state.get("ibkr") if isinstance(state.get("ibkr"), dict) else {}
    cached_mtd = _ibkr_cached_mtd_tracker_snapshot()
    if cached_mtd:
        ibkr = {**cached_mtd, **ibkr}
        if _float_value(ibkr.get("account_mtd_pnl")) is None:
            ibkr["account_mtd_pnl"] = cached_mtd.get("account_mtd_pnl")
        if _float_value(ibkr.get("account_mtd_return_pct")) is None:
            ibkr["account_mtd_return_pct"] = cached_mtd.get("account_mtd_return_pct")
        if not ibkr.get("account_mtd_source"):
            ibkr["account_mtd_source"] = cached_mtd.get("account_mtd_source")
        if not ibkr.get("account_mtd_baseline_set_at"):
            ibkr["account_mtd_baseline_set_at"] = cached_mtd.get("account_mtd_baseline_set_at")
        state = {**state, "ibkr": ibkr}
    open_position_count = int(_float_value(ibkr.get("open_position_count")) or 0)
    unrealized = round(float(_float_value(ibkr.get("unrealized_pnl")) or 0.0), 2)
    today_realized = round(float(_float_value(ibkr.get("today_realized_pnl")) or 0.0), 2)
    today_fills = int(_float_value(ibkr.get("today_executions")) or 0)
    broker_mtd_pnl = _float_value(ibkr.get("account_mtd_pnl"))
    broker_mtd_return_pct = _float_value(ibkr.get("account_mtd_return_pct"))
    now_utc = datetime.now(UTC)
    today_start_utc = _dashboard_local_day_start_utc(now_utc)
    today_start_iso = today_start_utc.isoformat().replace("+00:00", "Z")
    trade_closes = _recent_trade_closes(limit=5000)
    focus_trade_closes = [row for row in trade_closes if not _trade_close_is_cellar(row)]
    close_history = _close_history_windows(focus_trade_closes, now=now_utc)
    close_windows = close_history.get("windows") if isinstance(close_history.get("windows"), dict) else {}
    today_window = close_windows.get("today") if isinstance(close_windows.get("today"), dict) else {}
    closed_outcome_count_today = int(today_window.get("closed_outcome_count") or 0)
    evaluated_outcome_count_today = int(today_window.get("evaluated_outcome_count") or 0)
    win_rate_today = _float_value(today_window.get("win_rate"))
    win_rate_source = "trade_close_ledger_today" if evaluated_outcome_count_today > 0 else ""
    cache_age_s = _float_value(state.get("ibkr_cache_age_s") or ibkr.get("cache_age_s"))
    ibkr_cache_state = str(state.get("ibkr_cache_state") or "missing")
    ibkr_snapshot_stale = ibkr_cache_state.startswith("stale")
    ibkr_snapshot_ready = bool(
        not ibkr_snapshot_stale and (ibkr_cache_state == "warm" or ibkr.get("ready") is True)
    )
    return {
        **state,
        "ready": ibkr_snapshot_ready and "error" not in ibkr,
        "source": "cached_live_broker_state_for_diagnostics",
        "probe_skipped": True,
        "broker_snapshot_source": "ibkr_probe_cache" if ibkr else "missing_ibkr_probe_cache",
        "broker_snapshot_age_s": cache_age_s,
        "broker_snapshot_state": ibkr_cache_state,
        "server_ts": time.time(),
        "reporting_timezone": DASHBOARD_LOCAL_TIME_ZONE_NAME,
        "today_start_utc": today_start_iso,
        "today_day_boundary": "local_midnight",
        "today_actual_fills": today_fills,
        "today_realized_pnl": today_realized,
        "broker_mtd_pnl": broker_mtd_pnl,
        "broker_mtd_return_pct": broker_mtd_return_pct,
        "total_unrealized_pnl": unrealized,
        "open_position_count": open_position_count,
        "win_rate_today": win_rate_today,
        "closed_outcome_count_today": closed_outcome_count_today,
        "evaluated_outcome_count_today": evaluated_outcome_count_today,
        "win_rate_source": win_rate_source,
        "all_venue_today_actual_fills": today_fills,
        "all_venue_today_realized_pnl": today_realized,
        "all_venue_total_unrealized_pnl": unrealized,
        "all_venue_open_position_count": open_position_count,
        "cellar_today_actual_fills": 0,
        "cellar_today_realized_pnl": 0.0,
        "cellar_total_unrealized_pnl": 0.0,
        "cellar_open_position_count": 0,
        "win_rate_30d": None,
        "win_rate_30d_source": "",
        "close_history": close_history,
        "all_venue_close_history": close_history,
        "focus_policy": _dashboard_focus_policy_payload(),
        "sources": {
            "session_pnl": "ibkr_probe_cache",
            "broker_mtd_pnl": str(ibkr.get("account_mtd_source") or "unavailable"),
            "focus_mtd_closed_pnl": "trade_close_ledger",
        },
    }


def _target_exit_summary_for_master_status() -> dict:
    """Lightweight exit-protection rollup for master status cards.

    This intentionally uses cached live broker state so the master status route
    does not trigger another broker probe, but it still shows bracket risk after
    the bot-fleet or live-broker path has observed open IBKR exposure.
    """
    now_ts = time.time()
    try:
        rows = _supervisor_roster_rows(now_ts)
    except Exception as exc:  # noqa: BLE001 - status must fail soft.
        return {
            "status": "unknown",
            "summary_line": f"target exit summary unavailable: {exc}",
            "open_position_count": 0,
            "broker_open_position_count": 0,
            "broker_open_position_count_observed": False,
            "broker_bracket_required_position_count": 0,
            "missing_bracket_count": 0,
            "position_staleness": {"status": "unknown", "force_flatten_due_count": 0},
            "stale_position_status": "unknown",
            "source": "supervisor_roster_error",
        }

    live_broker_state = _cached_live_broker_state_for_gateway_reconcile()
    broker_oco_evidence = _broker_oco_evidence_payload(live_broker_state)
    if isinstance(live_broker_state, dict):
        live_broker_state["broker_oco_evidence"] = broker_oco_evidence
    exposure = _live_ibkr_exposure_for_gateway(live_broker_state)
    broker_open_count: int | None = None
    broker_bracket_required_count: int | None = None
    if exposure.get("observed"):
        broker_open_count = int(exposure.get("open_position_count") or 0)
        broker_bracket_required_count = _broker_bracket_required_position_count(live_broker_state)

    summary = _target_exit_summary(
        rows,
        broker_open_position_count=broker_open_count,
        broker_bracket_required_position_count=broker_bracket_required_count,
        broker_open_order_verified_bracket_count=int(broker_oco_evidence.get("verified_count") or 0),
        server_ts=now_ts,
    )
    summary["source"] = "supervisor_roster_cached_live_broker_state"
    summary["broker_position_source"] = exposure.get("source") if exposure.get("observed") else "not_observed"
    if exposure.get("observed"):
        summary["broker_position_scope"] = "ibkr_cached"
        summary["broker_position_scope_detail"] = (
            "Master status uses cached IBKR exposure only; /api/bot-fleet carries futures-focus venues plus cellar evidence."
        )
        broker_count = int(summary.get("broker_open_position_count") or 0)
        generic_phrase = f"{broker_count} broker open"
        scoped_phrase = f"{broker_count} IBKR cached broker open"
        line = str(summary.get("summary_line") or "")
        if broker_count > 0 and generic_phrase in line:
            summary["summary_line"] = line.replace(generic_phrase, scoped_phrase, 1)
    return summary


def _target_exit_card_status(summary: dict) -> str:
    """Map target-exit supervision truth to operator card color."""
    status = str(summary.get("status") or "unknown").lower()
    staleness = str(summary.get("stale_position_status") or "").lower()
    position_staleness = (
        summary.get("position_staleness") if isinstance(summary.get("position_staleness"), dict) else {}
    )
    force_flatten_due = int(position_staleness.get("force_flatten_due_count") or 0)
    missing_brackets = int(summary.get("missing_bracket_count") or 0)
    if status == "alert" or force_flatten_due > 0:
        return "RED"
    if (
        status in {"missing_brackets", "unknown"}
        or missing_brackets > 0
        or staleness in {"ack_due", "tighten_stop_due", "tightened_watch"}
    ):
        return "YELLOW"
    if status in {"flat", "paper_watching", "watching"}:
        return "GREEN"
    return "YELLOW"


def _audit_open_position_payload(position: dict) -> dict:
    """Compact, read-only broker position descriptor for bracket audit cards."""
    return {
        "venue": str(position.get("venue") or ""),
        "symbol": str(position.get("symbol") or ""),
        "side": str(position.get("side") or ""),
        "qty": _float_value(position.get("qty")),
        "sec_type": position.get("sec_type") or position.get("secType"),
        "exchange": position.get("exchange"),
        "avg_entry_price": _float_value(position.get("avg_entry_price")),
        "current_price": _float_value(position.get("current_price") or position.get("market_price")),
        "market_value": _float_value(position.get("market_value")),
        "unrealized_pnl": _float_value(position.get("unrealized_pnl")),
        "unrealized_pct": _float_value(position.get("unrealized_pct")),
        "broker_bracket_required": bool(position.get("broker_bracket_required")),
        "coverage_status": "requires_manual_oco_verification",
    }


def _broker_bracket_unprotected_positions(
    live_broker_state: dict | None,
    target_exit_summary: dict | None,
) -> list[dict]:
    """Best-effort list of broker positions driving a missing-OCO blocker."""
    summary = target_exit_summary if isinstance(target_exit_summary, dict) else {}
    missing_count = int(summary.get("missing_bracket_count") or 0)
    if missing_count <= 0:
        return []
    positions = [
        position
        for position in _normalized_live_open_positions(live_broker_state or {})
        if position.get("broker_bracket_required") is True
    ]
    if not positions:
        return []
    return [_audit_open_position_payload(position) for position in positions[:missing_count]]


def _position_audit_descriptor(position: dict) -> str:
    symbol = str(position.get("symbol") or "position").strip()
    venue = str(position.get("venue") or "broker").strip().upper()
    sec_type = str(position.get("sec_type") or "").strip().upper()
    return " ".join(part for part in (symbol, venue, sec_type) if part)


def _broker_bracket_operator_actions(report: dict, positions: list[dict]) -> list[dict]:
    """Structured manual choices for a broker-native bracket blocker."""
    summary = str(report.get("summary") or "").upper()
    if summary != "BLOCKED_UNBRACKETED_EXPOSURE":
        return []
    primary = positions[0] if positions else {}
    descriptor = _position_audit_descriptor(primary) if primary else "current broker exposure"
    symbol = str(primary.get("symbol") or "").strip() or None
    return [
        {
            "id": "verify_manual_broker_oco",
            "label": "Verify broker OCO coverage",
            "manual": True,
            "order_action": False,
            "blocks_prop_dry_run": True,
            "symbol": symbol,
            "detail": f"Confirm {descriptor} has broker-native TP/SL OCO attached outside ETA.",
        },
        {
            "id": "flatten_unprotected_paper_exposure",
            "label": "Flatten unprotected paper exposure",
            "manual": True,
            "order_action": True,
            "blocks_prop_dry_run": True,
            "symbol": symbol,
            "detail": f"Alternative: flatten {descriptor} before prop dry-run if no OCO exists.",
        },
    ]


def _enrich_broker_bracket_audit_with_positions(
    report: dict,
    *,
    live_broker_state: dict | None,
    target_exit_summary: dict | None,
) -> dict:
    """Attach the exact broker exposure driving the bracket blocker when known."""
    out = dict(report) if isinstance(report, dict) else {}
    positions = _broker_bracket_unprotected_positions(live_broker_state, target_exit_summary)
    if not positions:
        out.setdefault("unprotected_positions", [])
        out.setdefault("primary_unprotected_position", None)
        out["operator_action_required"] = not bool(out.get("ready_for_prop_dry_run"))
        out["operator_action"] = str(out.get("next_action") or "")
        out.setdefault("operator_actions", _broker_bracket_operator_actions(out, []))
        return out
    position_summary = dict(out.get("position_summary")) if isinstance(out.get("position_summary"), dict) else {}
    symbols = sorted({str(position.get("symbol") or "") for position in positions if position.get("symbol")})
    position_summary["unprotected_symbols"] = symbols
    out["position_summary"] = position_summary
    out["unprotected_positions"] = positions
    out["primary_unprotected_position"] = positions[0]
    descriptor = _position_audit_descriptor(positions[0])
    out["next_action"] = _append_detail_once(
        out.get("next_action"),
        f"{descriptor} missing broker-native OCO",
    )
    out["operator_action_required"] = not bool(out.get("ready_for_prop_dry_run"))
    out["operator_action"] = str(out.get("next_action") or "")
    out["operator_actions"] = _broker_bracket_operator_actions(out, positions)
    return out


def _broker_bracket_audit_payload(
    *,
    target_exit_summary: dict | None = None,
    live_broker_state: dict | None = None,
) -> dict:
    """Build the read-only broker bracket audit without writing artifacts."""
    try:
        from eta_engine.scripts import broker_bracket_audit  # noqa: PLC0415

        report = broker_bracket_audit.build_bracket_audit(
            fleet={
                "target_exit_summary": target_exit_summary or {},
                "live_broker_state": live_broker_state or {},
            },
        )
        return _enrich_broker_bracket_audit_with_positions(
            report,
            live_broker_state=live_broker_state,
            target_exit_summary=target_exit_summary,
        )
    except Exception as exc:  # noqa: BLE001 - dashboard status must fail soft.
        return {
            "kind": "eta_broker_bracket_audit",
            "schema_version": 1,
            "summary": "AUDIT_UNAVAILABLE",
            "ready_for_prop_dry_run": False,
            "target_exit_status": (
                target_exit_summary.get("status") if isinstance(target_exit_summary, dict) else None
            ),
            "position_summary": {
                "broker_open_position_count": 0,
                "broker_bracket_required_position_count": 0,
                "broker_bracket_count": 0,
                "missing_bracket_count": 0,
                "supervisor_local_position_count": 0,
            },
            "next_action": f"broker bracket audit unavailable: {exc}",
        }


def _broker_bracket_audit_card_status(report: dict) -> str:
    """Map prop/bracket audit truth to master-status card severity."""
    summary = str(report.get("summary") or "AUDIT_UNAVAILABLE").upper()
    if bool(report.get("ready_for_prop_dry_run")):
        return "GREEN"
    if summary == "BLOCKED_ADAPTER_SUPPORT":
        return "RED"
    return "YELLOW"


def _broker_bracket_audit_endpoint_payload() -> dict:
    """Read-only broker bracket audit payload for direct operator probes."""
    now_ts = time.time()
    live_broker_state = _live_broker_state_payload()
    broker_oco_evidence = _broker_oco_evidence_payload(live_broker_state)
    if isinstance(live_broker_state, dict):
        live_broker_state = dict(live_broker_state)
        live_broker_state["broker_oco_evidence"] = broker_oco_evidence

    try:
        rows = _supervisor_roster_rows(now_ts)
    except Exception:  # noqa: BLE001 - direct broker safety endpoint must fail soft.
        rows = []

    broker_open_count: int | None = None
    if isinstance(live_broker_state, dict):
        observed_count = _float_value(live_broker_state.get("open_position_count"))
        if observed_count is not None:
            broker_open_count = int(observed_count)
    target_exit_summary = _target_exit_summary(
        rows,
        broker_open_position_count=broker_open_count,
        broker_bracket_required_position_count=_broker_bracket_required_position_count(
            live_broker_state,
        ),
        broker_open_order_verified_bracket_count=int(
            broker_oco_evidence.get("verified_count") or 0,
        ),
        server_ts=now_ts,
    )
    payload = _broker_bracket_audit_payload(
        target_exit_summary=target_exit_summary,
        live_broker_state=live_broker_state,
    )
    payload["source"] = "dashboard_api_direct_broker_bracket_audit"
    payload["target_exit_summary"] = target_exit_summary
    payload["broker_oco_evidence"] = broker_oco_evidence
    return payload


def _worst_card_status(*statuses: str) -> str:
    """Return the highest-severity card status."""
    severity = {"GREEN": 0, "YELLOW": 1, "RED": 2}
    normalized = [str(status or "YELLOW").upper() for status in statuses]
    return max(normalized, key=lambda status: severity.get(status, 1))


def _position_exposure_payload(
    live_broker_state: dict,
    *,
    recent_closes: list[dict] | None = None,
    close_history: dict | None = None,
    target_exit_summary: dict | None = None,
) -> dict:
    """Read-only open-position and close-evidence rollup for the dashboard."""
    live_broker_state = live_broker_state if isinstance(live_broker_state, dict) else {}
    target_exit_summary = target_exit_summary if isinstance(target_exit_summary, dict) else {}
    alpaca = live_broker_state.get("alpaca") if isinstance(live_broker_state.get("alpaca"), dict) else {}
    ibkr = live_broker_state.get("ibkr") if isinstance(live_broker_state.get("ibkr"), dict) else {}

    open_positions = _normalized_live_open_positions(live_broker_state)
    cellar_open_positions = _cellar_live_open_positions(live_broker_state)

    if close_history is None:
        close_history = _close_history_windows(_recent_trade_closes(limit=5000))
    close_history = _limit_close_history_recent_rows(_normalize_close_history_count_alias(close_history))
    default_close_window = str(close_history.get("default_window") or "mtd")
    close_windows = close_history.get("windows") if isinstance(close_history.get("windows"), dict) else {}
    default_window_payload = (
        close_windows.get(default_close_window) if isinstance(close_windows.get(default_close_window), dict) else {}
    )
    if recent_closes is None:
        default_window_rows = default_window_payload.get("recent_outcomes")
        recent_closes = default_window_rows if isinstance(default_window_rows, list) else _recent_trade_closes(limit=25)
    recent_closes = recent_closes[:_DASHBOARD_POSITION_EXPOSURE_CLOSE_ROW_LIMIT]
    normalized_closes: list[dict] = []
    cellar_closes: list[dict] = []
    for row in recent_closes:
        normalized = _normalize_trade_close(row)
        if normalized:
            if _trade_close_is_cellar(row) or _portfolio_symbol_is_cellar(normalized.get("symbol")):
                normalized["policy_status"] = "paused_cellar"
                cellar_closes.append(normalized)
            else:
                normalized["policy_status"] = "focus"
                normalized_closes.append(normalized)

    open_position_count = len(open_positions)
    bracket_required_count = sum(1 for position in open_positions if position.get("broker_bracket_required") is True)
    supervisor_local_count = int(target_exit_summary.get("supervisor_local_position_count") or 0)
    supervisor_watch_count = int(target_exit_summary.get("supervisor_watch_count") or 0)
    if open_position_count:
        target_status = "open_positions_detected"
        target_detail = (
            "Broker open positions are visible; supervisor/router remain the "
            "authority for stop, target, and flatten actions."
        )
    elif supervisor_local_count:
        target_status = str(target_exit_summary.get("status") or "paper_watching")
        target_detail = str(
            target_exit_summary.get("summary_line")
            or "No broker open positions; supervisor is watching paper-local targets/stops."
        )
    elif alpaca or ibkr:
        target_status = "flat"
        target_detail = "No broker open positions detected in the current snapshot."
    else:
        target_status = "broker_snapshot_unavailable"
        target_detail = "No broker position snapshot is available for this request."

    return {
        "ready": "error" not in live_broker_state,
        "source": "live_broker_rest+trade_closes",
        "position_scope": "futures_focus",
        "focus_policy": _dashboard_focus_policy_payload(),
        "server_ts": time.time(),
        "open_position_count": open_position_count,
        "broker_open_position_count": open_position_count,
        "all_venue_open_position_count": open_position_count + len(cellar_open_positions),
        "cellar_open_position_count": len(cellar_open_positions),
        "broker_bracket_required_position_count": bracket_required_count,
        "broker_supervisor_managed_position_count": max(0, open_position_count - bracket_required_count),
        "supervisor_local_position_count": supervisor_local_count,
        "supervisor_watch_count": supervisor_watch_count,
        "symbols_open": sorted({p["symbol"] for p in open_positions if p.get("symbol")}),
        "cellar_symbols_open": sorted({p["symbol"] for p in cellar_open_positions if p.get("symbol")}),
        "open_positions": open_positions,
        "cellar_open_positions": cellar_open_positions,
        "recent_closes": normalized_closes,
        "recent_close_count": len(normalized_closes),
        "cellar_recent_closes": cellar_closes,
        "cellar_recent_close_count": len(cellar_closes),
        "close_history": close_history,
        "default_close_history_window": default_close_window,
        "target_exit_summary": target_exit_summary,
        "target_exit_visibility": {
            "status": target_status,
            "detail": target_detail,
        },
    }


def _live_fill_paths() -> list[tuple[Path, str]]:
    """Known live execution ledgers; router rows are filtered to true fills."""
    state = _state_dir()
    candidates = [
        (state / "blotter" / "fills.jsonl", "blotter"),
        (state / "broker_router_fills.jsonl", "broker_router"),
        (state / "router" / "broker_router_fills.jsonl", "broker_router"),
    ]
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path, source in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append((path, source))
    return out


def _router_fill_results_rows() -> list[dict]:
    """Scan broker_router fill_results/ directory for completed fills.

    Wave-25c rev7 (2026-05-13): the broker_router writes one JSON file
    per submission outcome under ``state/router/fill_results/``. The
    legacy ledger scanner only looks for jsonl files, so today's
    paper-live submissions never surfaced in the dashboard's
    ``supervisor.live`` block. This helper flattens each fill_result
    file into the same normalized row shape and stamps source=
    ``broker_router`` so the rest of the pipeline picks it up.

    Only files with a positive filled_qty are returned — rejection
    rows (qty=0, reason="rounds to zero" etc) are excluded so the
    live-fills counter only reflects real fills, not rejections.
    """
    state = _state_dir()
    result_dir = state / "router" / "fill_results"
    if not result_dir.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(result_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
        try:
            filled_qty = float(result.get("filled_qty") or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        if filled_qty <= 0:
            continue
        try:
            avg_price = float(result.get("avg_price") or 0)
        except (TypeError, ValueError):
            avg_price = 0.0
        ts_value = (
            result.get("filled_at")
            or result.get("ts")
            or payload.get("ts")
            or datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
        )
        flattened = {
            "ts": ts_value,
            "status": "FILLED",
            "bot": str(payload.get("bot_id") or request.get("bot_id") or ""),
            "symbol": str(request.get("symbol") or ""),
            "side": str(request.get("side") or ""),
            "qty": filled_qty,
            "price": avg_price,
            "order_id": str(request.get("client_order_id") or result.get("order_id") or ""),
            "source": "broker_router",
            "source_path": str(path),
        }
        normalized = _normalize_live_fill_row(
            flattened,
            source="broker_router",
            source_path=str(path),
        )
        if normalized is not None:
            rows.append(normalized)
    return rows


def _normalize_live_fill_row(row: dict, *, source: str, source_path: str | None = None) -> dict | None:
    if not isinstance(row, dict):
        return None
    status = (
        str(_first_present(row, ("status", "order_status", "orderStatus", "event", "result_status")) or "")
        .replace("_", "")
        .replace(" ", "")
        .upper()
    )
    qty = _float_value(_first_present(row, ("qty", "quantity", "shares", "filled_qty", "filledQty")))
    is_positive_fill = qty is not None and abs(qty) > 0
    is_fill_status = status in _LIVE_FILL_STATUSES
    is_denied_status = status in _NON_FILL_STATUSES
    if is_denied_status and not is_fill_status:
        return None
    if source != "blotter" and source != "ibkr_execution" and not (is_fill_status or is_positive_fill):
        return None

    ts_raw = _first_present(row, ("ts", "time", "filled_at", "execution_time", "submitted_at"))
    ts_dt = _parse_fill_dt(ts_raw)
    if ts_dt is None:
        return None

    normalized = dict(row)
    normalized["ts"] = str(ts_raw)
    normalized.setdefault("source", source)
    if source_path:
        normalized.setdefault("source_path", source_path)
    bot = _first_present(normalized, ("bot", "bot_id", "strategy_id", "order_ref"))
    if bot is not None:
        normalized["bot"] = str(bot)
    symbol = _first_present(normalized, ("symbol", "local_symbol", "contract_symbol"))
    if symbol is not None:
        normalized["symbol"] = str(symbol)
    if qty is not None:
        normalized["qty"] = qty
    price = _float_value(_first_present(normalized, ("price", "avg_price", "avgFillPrice", "average_price")))
    if price is not None:
        normalized["price"] = price
    return normalized


def _ibkr_execution_rows() -> list[dict]:
    payload, source_path = _load_tws_watchdog_payload()
    if not isinstance(payload, dict):
        return []
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    snapshot = details.get("account_snapshot") if isinstance(details.get("account_snapshot"), dict) else {}
    executions = snapshot.get("executions") if isinstance(snapshot.get("executions"), list) else []
    rows: list[dict] = []
    for row in executions:
        normalized = _normalize_live_fill_row(
            row,
            source="ibkr_execution",
            source_path=source_path,
        )
        if normalized is not None:
            rows.append(normalized)
    return rows


def _recent_live_fill_rows(*, bot: str | None = None, limit: int | None = None) -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple] = set()
    for path, source in _live_fill_paths():
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            normalized = _normalize_live_fill_row(payload, source=source, source_path=str(path))
            if normalized is None:
                continue
            row_bot = str(normalized.get("bot") or "")
            if bot and row_bot != bot:
                continue
            key = (
                normalized.get("ts"),
                normalized.get("source"),
                normalized.get("exec_id") or normalized.get("execution_id"),
                normalized.get("order_id"),
                normalized.get("bot"),
                normalized.get("side"),
                normalized.get("qty"),
                normalized.get("price"),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(normalized)
    # Wave-25c rev7: pull in broker_router/fill_results/*.json fills so
    # the supervisor.live block reflects current paper-live execution
    # via the broker_router instead of only the legacy supervisor
    # journal (which stopped being the source of truth after the
    # paper_live routing fix).
    for normalized in _router_fill_results_rows():
        row_bot = str(normalized.get("bot") or "")
        if bot and row_bot != bot:
            continue
        key = (
            normalized.get("ts"),
            normalized.get("source"),
            normalized.get("exec_id") or normalized.get("execution_id"),
            normalized.get("order_id"),
            normalized.get("bot"),
            normalized.get("side"),
            normalized.get("qty"),
            normalized.get("price"),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(normalized)
    for normalized in _ibkr_execution_rows():
        row_bot = str(normalized.get("bot") or "")
        if bot and row_bot != bot:
            continue
        key = (
            normalized.get("ts"),
            normalized.get("source"),
            normalized.get("exec_id") or normalized.get("execution_id"),
            normalized.get("order_id"),
            normalized.get("bot"),
            normalized.get("side"),
            normalized.get("qty"),
            normalized.get("price"),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(normalized)
    rows.sort(
        key=lambda row: _parse_fill_dt(row.get("ts")) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return rows[:limit] if limit is not None else rows


def _fills_activity_snapshot(bot: str | None = None) -> dict:
    """Lightweight live telemetry so UI can distinguish idle vs stale."""
    now = datetime.now(UTC)
    h1 = now - timedelta(hours=1)
    h24 = now - timedelta(hours=24)
    last_fill_ts: str | None = None
    last_fill_dt: datetime | None = None
    fills_1h = 0
    fills_24h = 0
    source_counts: dict[str, int] = defaultdict(int)
    for row in _recent_live_fill_rows(bot=bot):
        ts_raw = row.get("ts")
        ts_dt = _parse_fill_dt(ts_raw)
        if ts_dt is None:
            continue
        if last_fill_dt is None or ts_dt > last_fill_dt:
            last_fill_dt = ts_dt
            last_fill_ts = str(ts_raw or "")
        if ts_dt >= h24:
            fills_24h += 1
            source_counts[str(row.get("source") or "unknown")] += 1
        if ts_dt >= h1:
            fills_1h += 1
    return {
        "last_fill_ts": last_fill_ts,
        "fills_1h": fills_1h,
        "fills_24h": fills_24h,
        "source_counts_24h": dict(sorted(source_counts.items())),
    }


# ---------------------------------------------------------------------------
# Live broker reality surface (added 2026-05-06)
# Exposes broker-side truth (Alpaca + IBKR) alongside the supervisor-journal
# counts so the dashboard can show "actual fills today" / "live unrealized
# PnL" rather than only signal-emit events. Never replaces existing fields.
# Each leg fails-soft to ``error_<broker>`` so a degraded broker does not
# take down the dashboard panel.
# ---------------------------------------------------------------------------


def _alpaca_live_state_snapshot(*, today_start_iso: str) -> dict:
    """Pull /v2/orders (filled today) + /v2/positions from Alpaca paper.

    Synchronous so it can run inside a regular FastAPI handler without
    spinning an asyncio loop. Uses ``httpx`` directly with the same
    secret-file resolution AlpacaVenue uses (``AlpacaConfig.from_env``).
    """
    snapshot: dict = {
        "ready": False,
        "today_filled_orders": 0,
        "today_realized_pnl": 0.0,
        "open_positions": [],
        "open_position_count": 0,
        "unrealized_pnl": 0.0,
        "equity": None,
        "buying_power": None,
        "account_number": None,
        "checked_utc": datetime.now(UTC).isoformat(),
    }
    try:
        from eta_engine.venues.alpaca import AlpacaConfig
    except Exception as exc:  # noqa: BLE001
        snapshot["error"] = f"alpaca_adapter_unavailable: {exc}"
        return snapshot
    try:
        cfg = AlpacaConfig.from_env()
    except Exception as exc:  # noqa: BLE001
        snapshot["error"] = f"alpaca_config_error: {exc}"
        return snapshot
    missing = cfg.missing_requirements()
    if missing:
        snapshot["error"] = "alpaca_missing_config"
        snapshot["missing"] = missing
        return snapshot
    try:
        import httpx  # noqa: PLC0415
    except ImportError:
        snapshot["error"] = "httpx_unavailable"
        return snapshot
    headers = {
        "APCA-API-KEY-ID": cfg.api_key_id,
        "APCA-API-SECRET-KEY": cfg.api_secret_key,
        "Accept": "application/json",
    }
    try:
        with httpx.Client(base_url=cfg.base_url, headers=headers, timeout=8.0) as client:
            acct_resp = client.get("/v2/account")
            if acct_resp.status_code == 200:
                acct = acct_resp.json() if isinstance(acct_resp.json(), dict) else {}
                snapshot["account_number"] = acct.get("account_number")
                snapshot["equity"] = _float_value(acct.get("equity"))
                snapshot["buying_power"] = _float_value(acct.get("buying_power"))
                snapshot["last_equity"] = _float_value(acct.get("last_equity"))
                last_eq = snapshot.get("last_equity")
                eq = snapshot.get("equity")
                if last_eq is not None and eq is not None:
                    snapshot["today_realized_pnl"] = round(eq - last_eq, 2)
            pos_resp = client.get("/v2/positions")
            if pos_resp.status_code == 200:
                positions = pos_resp.json() if isinstance(pos_resp.json(), list) else []
                slim_positions: list[dict] = []
                unreal = 0.0
                for p in positions:
                    if not isinstance(p, dict):
                        continue
                    upl = _float_value(p.get("unrealized_pl")) or 0.0
                    unreal += upl
                    slim_positions.append(
                        {
                            "symbol": p.get("symbol"),
                            "qty": _float_value(p.get("qty")),
                            "avg_entry_price": _float_value(p.get("avg_entry_price")),
                            "current_price": _float_value(p.get("current_price")),
                            "market_value": _float_value(p.get("market_value")),
                            "unrealized_pl": upl,
                            "unrealized_plpc": _float_value(p.get("unrealized_plpc")),
                            "side": p.get("side"),
                        }
                    )
                snapshot["open_positions"] = slim_positions
                snapshot["open_position_count"] = len(slim_positions)
                snapshot["unrealized_pnl"] = round(unreal, 2)
            ord_resp = client.get(
                "/v2/orders",
                params={"status": "closed", "after": today_start_iso, "limit": 500},
            )
            if ord_resp.status_code == 200:
                orders = ord_resp.json() if isinstance(ord_resp.json(), list) else []
                filled = [o for o in orders if isinstance(o, dict) and str(o.get("status") or "").lower() == "filled"]
                snapshot["today_filled_orders"] = len(filled)
                outcomes = _closed_outcomes_from_filled_orders(filled)
                snapshot["today_closed_outcome_count"] = outcomes["closed_outcome_count"]
                snapshot["today_evaluated_outcome_count"] = outcomes["evaluated_outcome_count"]
                snapshot["today_winning_outcomes"] = outcomes["winning_outcomes"]
                snapshot["today_losing_outcomes"] = outcomes["losing_outcomes"]
                snapshot["today_win_rate"] = outcomes["win_rate"]
                snapshot["recent_closed_outcomes"] = outcomes["recent_outcomes"]
                # Surface the most recent N for the panel tape.
                trimmed: list[dict] = []
                for o in filled[:30]:
                    trimmed.append(
                        {
                            "symbol": o.get("symbol"),
                            "side": o.get("side"),
                            "filled_qty": _float_value(o.get("filled_qty")),
                            "filled_avg_price": _float_value(o.get("filled_avg_price")),
                            "filled_at": o.get("filled_at"),
                            "client_order_id": o.get("client_order_id"),
                        }
                    )
                snapshot["recent_filled_orders"] = trimmed
        snapshot["ready"] = True
    except Exception as exc:  # noqa: BLE001 — broker degrade must not crash dashboard
        snapshot["error"] = f"alpaca_probe_failed: {exc}"
    return snapshot


# Cache for the IBKR probe — IB Gateway accumulates "orphan eServer"
# slots when a connect handshake times out, and cleanup is slow (~8s).
# Probing on every dashboard refresh can create orphans faster than IBG
# cleans them, dragging probes into a timeout loop. 60s freshness is
# plenty for an operator dashboard.
_IBKR_PROBE_CACHE: dict = {"snapshot": None, "ts": 0.0}
_IBKR_PROBE_CACHE_TTL_S = float(os.environ.get("ETA_DASHBOARD_IBKR_CACHE_TTL_S", "60"))
_IBKR_PROBE_DISK_CACHE_MAX_AGE_S = float(os.environ.get("ETA_DASHBOARD_IBKR_DISK_CACHE_MAX_AGE_S", "900"))
_IBKR_PROBE_LOCK = threading.Lock()


def _ibkr_probe_cache_state_path() -> Path:
    """Canonical last-good IBKR probe cache for dashboard restarts."""
    return _state_dir() / "broker_cache" / "ibkr_probe_cache.json"


def _persist_ibkr_probe_cache(snapshot: dict, *, ts: float) -> None:
    if snapshot.get("ready") is not True:
        return
    with contextlib.suppress(Exception):
        _write_json_atomic(
            _ibkr_probe_cache_state_path(),
            {
                "schema_version": 1,
                "ts": ts,
                "written_at_utc": datetime.now(UTC).isoformat(),
                "snapshot": snapshot,
            },
        )


def _load_persisted_ibkr_probe_cache(*, now_ts: float | None = None, include_stale: bool = False) -> dict:
    path = _ibkr_probe_cache_state_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    snapshot = payload.get("snapshot") if isinstance(payload, dict) else None
    if not isinstance(snapshot, dict):
        return {}
    cached_ts = _float_value(payload.get("ts"))
    if cached_ts is None or cached_ts <= 0:
        return {}
    now = time.time() if now_ts is None else now_ts
    age_s = max(0.0, now - cached_ts)
    stale = age_s > _IBKR_PROBE_DISK_CACHE_MAX_AGE_S
    if stale and not include_stale:
        return {}
    ibkr = dict(snapshot)
    ibkr.setdefault("cache_ts", cached_ts)
    ibkr.setdefault("cache_age_s", round(age_s, 1))
    ibkr.setdefault("last_known", True)
    ibkr["persisted_cache"] = True
    if stale:
        ibkr["stale_cache"] = True
    return {
        "ibkr": ibkr,
        "ibkr_cache_state": "stale_persisted" if stale else "persisted",
        "ibkr_cache_ts": cached_ts,
        "ibkr_cache_age_s": round(age_s, 1),
    }


def _dashboard_ibkr_client_id_candidates() -> list[int]:
    """Return deterministic IBKR client-id candidates for the dashboard probe.

    Older builds picked a random 8xx client id on each probe. That made stale
    dashboard workers spray the gateway with many different client sessions,
    which is exactly how we ended up with repeated "client id already in use"
    churn on the VPS. Prefer a small deterministic lane, with an env override
    when the operator needs a specific id.
    """
    explicit = str(os.environ.get("ETA_DASHBOARD_IBKR_CLIENT_ID", "") or "").strip()
    if explicit:
        with contextlib.suppress(ValueError):
            return [int(explicit)]

    try:
        base = int(os.environ.get("ETA_DASHBOARD_IBKR_CLIENT_ID_BASE", "1842"))
    except ValueError:
        base = 1842
    try:
        span = int(os.environ.get("ETA_DASHBOARD_IBKR_CLIENT_ID_SPAN", "1"))
    except ValueError:
        span = 1
    span = max(1, min(span, 8))
    offset = os.getpid() % span
    return [base + ((offset + idx) % span) for idx in range(span)]


def _dashboard_ibkr_connect_timeout_s() -> float:
    """Bound dashboard IBKR probes so first paint never waits on a wedged gateway."""
    try:
        timeout_s = float(os.environ.get("ETA_DASHBOARD_IBKR_TIMEOUT_S", "4"))
    except ValueError:
        timeout_s = 4.0
    return max(1.0, min(timeout_s, 12.0))


def _ibkr_client_id_retryable_error(exc: BaseException) -> bool:
    """Return True when another client-id candidate should be tried."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return (
        "client id already in use" in text
        or ("clientid" in text and "already in use" in text)
        or "error 326" in text
        or ("peer closed connection" in text and "already in use" in text)
        or type(exc).__name__ == "TimeoutError"
    )


# Cache for per-bot Alpaca PnL probe. Same TTL pattern as IBKR.
# Keyed dict: {"snapshot": {bot_id: {...}}, "ts": float}.
_ALPACA_PER_BOT_CACHE: dict = {"snapshot": None, "ts": 0.0}
_ALPACA_PER_BOT_CACHE_TTL_S = float(os.environ.get("ETA_DASHBOARD_ALPACA_PER_BOT_CACHE_TTL_S", "60"))
_ALPACA_PER_BOT_LOCK = threading.Lock()

# Drift alarm threshold (percentage points). live_wr lower than
# backtest_wr_target by more than this triggers drift_alarm=true.
_DRIFT_ALARM_PP_THRESHOLD = float(os.environ.get("ETA_DASHBOARD_DRIFT_ALARM_PP", "30"))
# Minimum number of fills today before drift_alarm can fire — under this
# we suppress the alarm because a tiny sample is meaningless.
_DRIFT_ALARM_MIN_FILLS = int(os.environ.get("ETA_DASHBOARD_DRIFT_ALARM_MIN_FILLS", "5"))


def _extract_bot_id_from_client_order_id(coid: str | None) -> str | None:
    """Extract bot_id prefix from an Alpaca client_order_id.

    The supervisor stamps Alpaca client_order_ids in the form
    ``<bot_id>_<8charhex>``, e.g. ``vwap_mr_btc_cef83eb7``. Bot ids
    themselves can contain underscores, so we strip the trailing
    ``_<hex>`` token and return the prefix as the bot_id.

    Returns ``None`` for empty/None input or values that don't match
    the expected shape.
    """
    if not coid:
        return None
    s = str(coid).strip()
    if not s:
        return None
    # Match a trailing underscore followed by an 8+ char hex token.
    m = re.match(r"^(?P<bot_id>.+?)_(?P<suffix>[0-9a-fA-F]{6,})$", s)
    if m and _BOT_ID_RE.match(m.group("bot_id") or ""):
        return m.group("bot_id")
    # Fall back: if the whole COID looks like a valid bot_id, use it.
    if _BOT_ID_RE.match(s):
        return s
    return None


def _parse_backtest_wr_from_text(text: str | None) -> float | None:
    """Best-effort parse of "85.7% WR, +$1947 on 3000 bars" → 0.857.

    Looks for the first `<number>% WR` token in the string. Returns the
    win-rate as a float in [0, 1] (e.g. 85.7 → 0.857), or None when no
    match is found. Tolerant of whitespace and comma-separated dollars.
    """
    if not text:
        return None
    try:
        m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*WR", str(text), flags=re.IGNORECASE)
        if not m:
            return None
        v = float(m.group(1))
        if v < 0 or v > 100:
            return None
        return round(v / 100.0, 4)
    except (TypeError, ValueError):
        return None


def _registry_backtest_wr_targets() -> dict[str, float]:
    """Return ``{bot_id: backtest_wr}`` parsed from the registry.

    Reads ``paper_soak_result`` first (most direct), falls back to the
    rationale string. Failures degrade to an empty dict so the
    drift-tracking layer simply omits the target field.
    """
    out: dict[str, float] = {}
    try:
        from eta_engine.strategies.per_bot_registry import ASSIGNMENTS  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return out
    for a in ASSIGNMENTS:
        bot_id = getattr(a, "bot_id", None)
        if not bot_id:
            continue
        extras = getattr(a, "extras", None) or {}
        soak = extras.get("paper_soak_result") if isinstance(extras, dict) else None
        wr = _parse_backtest_wr_from_text(soak)
        if wr is None:
            wr = _parse_backtest_wr_from_text(getattr(a, "rationale", None))
        if wr is not None:
            out[str(bot_id)] = wr
    return out


def _alpaca_per_bot_pnl_snapshot(*, today_start_iso: str) -> dict:
    """Return per-bot PnL aggregated from today's filled Alpaca orders.

    Fetches /v2/orders?status=closed&after=<today_start_iso> and groups
    by bot_id (extracted from the client_order_id prefix). For each bot:

    * ``fills_today``      — count of filled orders today
    * ``buy_qty``          — total quantity bought
    * ``sell_qty``         — total quantity sold
    * ``buy_notional``     — sum(filled_price * filled_qty) for buys
    * ``sell_notional``    — sum(filled_price * filled_qty) for sells
    * ``net_notional``     — sell_notional - buy_notional
                             (≈ realized + open delta; positive = net cash in)
    * ``wins``, ``losses`` — bot pairs of buy→sell (one round-trip per
                             matched fill). For asymmetric flow we count
                             completed pairs only.
    * ``live_wr_today``    — wins / (wins+losses) when n>=1, else None
    * ``backtest_wr_target`` — parsed from registry (None if unparseable)
    * ``drift_alarm``      — True when live_wr < backtest_wr_target by
                             more than ``_DRIFT_ALARM_PP_THRESHOLD`` and
                             fills_today >= ``_DRIFT_ALARM_MIN_FILLS``.

    Failures degrade to ``{"error": "...", "per_bot": {}}``. The
    dashboard request must NOT crash on Alpaca downtime.
    """
    snapshot: dict = {
        "ready": False,
        "checked_utc": datetime.now(UTC).isoformat(),
        "per_bot": {},
        "drift_alarm_threshold_pp": _DRIFT_ALARM_PP_THRESHOLD,
        "drift_alarm_min_fills": _DRIFT_ALARM_MIN_FILLS,
    }
    try:
        from eta_engine.venues.alpaca import AlpacaConfig  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        snapshot["error"] = f"alpaca_adapter_unavailable: {exc}"
        return snapshot
    try:
        cfg = AlpacaConfig.from_env()
    except Exception as exc:  # noqa: BLE001
        snapshot["error"] = f"alpaca_config_error: {exc}"
        return snapshot
    missing = cfg.missing_requirements()
    if missing:
        snapshot["error"] = "alpaca_missing_config"
        snapshot["missing"] = missing
        return snapshot
    try:
        import httpx  # noqa: PLC0415
    except ImportError:
        snapshot["error"] = "httpx_unavailable"
        return snapshot
    headers = {
        "APCA-API-KEY-ID": cfg.api_key_id,
        "APCA-API-SECRET-KEY": cfg.api_secret_key,
        "Accept": "application/json",
    }
    try:
        with httpx.Client(base_url=cfg.base_url, headers=headers, timeout=8.0) as client:
            ord_resp = client.get(
                "/v2/orders",
                params={"status": "closed", "after": today_start_iso, "limit": 500},
            )
            if ord_resp.status_code != 200:
                snapshot["error"] = f"alpaca_orders_http_{ord_resp.status_code}"
                return snapshot
            orders = ord_resp.json() if isinstance(ord_resp.json(), list) else []
    except Exception as exc:  # noqa: BLE001 — broker degrade must not crash dashboard
        snapshot["error"] = f"alpaca_per_bot_probe_failed: {exc}"
        return snapshot

    targets = _registry_backtest_wr_targets()
    by_bot: dict[str, dict] = {}
    for o in orders:
        if not isinstance(o, dict):
            continue
        if str(o.get("status") or "").lower() != "filled":
            continue
        bot_id = _extract_bot_id_from_client_order_id(o.get("client_order_id"))
        if not bot_id:
            bot_id = "_unknown"
        side = str(o.get("side") or "").lower()
        qty = _float_value(o.get("filled_qty")) or 0.0
        price = _float_value(o.get("filled_avg_price")) or 0.0
        notional = qty * price
        bucket = by_bot.setdefault(
            bot_id,
            {
                "fills_today": 0,
                "buy_qty": 0.0,
                "sell_qty": 0.0,
                "buy_notional": 0.0,
                "sell_notional": 0.0,
                "symbols": set(),
                "_realized_per_pair": [],
                "_buy_stack": [],  # list of (qty, price) for FIFO matching
            },
        )
        bucket["fills_today"] += 1
        sym = o.get("symbol")
        if sym:
            bucket["symbols"].add(str(sym))
        if side == "buy":
            bucket["buy_qty"] += qty
            bucket["buy_notional"] += notional
            bucket["_buy_stack"].append((qty, price))
        elif side == "sell":
            bucket["sell_qty"] += qty
            bucket["sell_notional"] += notional
            # FIFO match against the bot's outstanding buys to compute
            # realized per-pair PnL — gives us live_wr_today.
            remaining = qty
            while remaining > 1e-12 and bucket["_buy_stack"]:
                buy_qty, buy_price = bucket["_buy_stack"][0]
                matched = min(buy_qty, remaining)
                pair_pnl = (price - buy_price) * matched
                bucket["_realized_per_pair"].append(pair_pnl)
                remaining -= matched
                if matched >= buy_qty - 1e-12:
                    bucket["_buy_stack"].pop(0)
                else:
                    bucket["_buy_stack"][0] = (buy_qty - matched, buy_price)

    per_bot_out: dict[str, dict] = {}
    for bot_id, bucket in by_bot.items():
        wins = sum(1 for r in bucket["_realized_per_pair"] if r > 0)
        losses = sum(1 for r in bucket["_realized_per_pair"] if r < 0)
        live_wr: float | None = None
        if (wins + losses) > 0:
            live_wr = round(wins / (wins + losses), 4)
        bt_wr = targets.get(bot_id)
        drift_alarm = False
        drift_gap_pp: float | None = None
        if bt_wr is not None and live_wr is not None and bucket["fills_today"] >= _DRIFT_ALARM_MIN_FILLS:
            drift_gap_pp = round((bt_wr - live_wr) * 100.0, 2)
            if drift_gap_pp > _DRIFT_ALARM_PP_THRESHOLD:
                drift_alarm = True
        net_notional = round(bucket["sell_notional"] - bucket["buy_notional"], 2)
        per_bot_out[bot_id] = {
            "fills_today": bucket["fills_today"],
            "buy_qty": round(bucket["buy_qty"], 8),
            "sell_qty": round(bucket["sell_qty"], 8),
            "buy_notional": round(bucket["buy_notional"], 2),
            "sell_notional": round(bucket["sell_notional"], 2),
            "net_notional": net_notional,
            "symbols": sorted(bucket["symbols"]),
            "wins": wins,
            "losses": losses,
            "live_wr_today": live_wr,
            "backtest_wr_target": bt_wr,
            "drift_gap_pp": drift_gap_pp,
            "drift_alarm": drift_alarm,
        }

    snapshot["per_bot"] = per_bot_out
    snapshot["bot_count"] = len(per_bot_out)
    snapshot["drift_alarm_count"] = sum(1 for v in per_bot_out.values() if v.get("drift_alarm"))
    snapshot["ready"] = True
    return snapshot


def _alpaca_per_bot_pnl_cached(*, today_start_iso: str) -> dict:
    """Cached wrapper around :func:`_alpaca_per_bot_pnl_snapshot`.

    Same lock+TTL pattern as ``_ibkr_live_state_snapshot`` so back-to-back
    dashboard refreshes don't hammer Alpaca's REST. Cache is keyed on the
    UTC day to avoid serving yesterday's snapshot after midnight rollover.
    """
    now_ts = time.time()
    with _ALPACA_PER_BOT_LOCK:
        cached = _ALPACA_PER_BOT_CACHE.get("snapshot")
        cached_ts = float(_ALPACA_PER_BOT_CACHE.get("ts") or 0.0)
        cached_day = _ALPACA_PER_BOT_CACHE.get("today_start_iso")
        if cached is not None and cached_day == today_start_iso and (now_ts - cached_ts) < _ALPACA_PER_BOT_CACHE_TTL_S:
            cached_copy = dict(cached)
            cached_copy["served_from_cache"] = True
            cached_copy["cache_age_s"] = round(now_ts - cached_ts, 2)
            return cached_copy
    try:
        snap = _alpaca_per_bot_pnl_snapshot(today_start_iso=today_start_iso)
    except Exception as exc:  # noqa: BLE001 — fail-soft for the dashboard
        snap = {
            "ready": False,
            "error": f"alpaca_per_bot_unhandled: {exc}",
            "per_bot": {},
            "checked_utc": datetime.now(UTC).isoformat(),
        }
    with _ALPACA_PER_BOT_LOCK:
        _ALPACA_PER_BOT_CACHE["snapshot"] = dict(snap)
        _ALPACA_PER_BOT_CACHE["ts"] = time.time()
        _ALPACA_PER_BOT_CACHE["today_start_iso"] = today_start_iso
    return snap


def _ibkr_open_order_snapshot(trade: object) -> dict:
    """Return a sanitized read-only IBKR open-order row for OCO evidence."""
    if isinstance(trade, dict):
        return {
            "symbol": trade.get("symbol") or trade.get("local_symbol") or trade.get("localSymbol"),
            "local_symbol": trade.get("local_symbol") or trade.get("localSymbol"),
            "sec_type": trade.get("sec_type") or trade.get("secType"),
            "exchange": trade.get("exchange"),
            "action": trade.get("action") or trade.get("side"),
            "order_type": trade.get("order_type") or trade.get("orderType"),
            "qty": _float_value(trade.get("qty") or trade.get("totalQuantity")),
            "remaining": _float_value(trade.get("remaining")),
            "status": trade.get("status") or trade.get("order_status"),
            "parent_id": trade.get("parent_id") or trade.get("parentId"),
            "oca_group": trade.get("oca_group") or trade.get("ocaGroup"),
            "order_id": trade.get("order_id") or trade.get("orderId"),
            "perm_id": trade.get("perm_id") or trade.get("permId"),
        }
    order = getattr(trade, "order", None)
    contract = getattr(trade, "contract", None)
    status = getattr(trade, "orderStatus", None)
    return {
        "symbol": getattr(contract, "localSymbol", None) or getattr(contract, "symbol", None),
        "local_symbol": getattr(contract, "localSymbol", None),
        "sec_type": getattr(contract, "secType", None),
        "exchange": getattr(contract, "exchange", None),
        "action": getattr(order, "action", None),
        "order_type": getattr(order, "orderType", None),
        "qty": _float_value(getattr(order, "totalQuantity", None)),
        "remaining": _float_value(getattr(status, "remaining", None)),
        "status": getattr(status, "status", None),
        "parent_id": getattr(order, "parentId", None),
        "oca_group": getattr(order, "ocaGroup", None),
        "order_id": getattr(order, "orderId", None),
        "perm_id": getattr(order, "permId", None) or getattr(status, "permId", None),
    }


def _ibkr_live_state_snapshot(*, today_start_utc: datetime) -> dict:
    """Pull live IBKR positions and today's executions via ib_insync.

    Uses a one-shot connect on a high client_id (8xx range) to avoid
    colliding with the supervisor's clientId=187. Disconnects immediately
    so the supervisor's connection is undisturbed. Fails soft when ib_insync
    is missing or the gateway is down.

    Cached for ``ETA_DASHBOARD_IBKR_CACHE_TTL_S`` (default 60s). The lock
    serializes concurrent dashboard requests so we never spawn two
    overlapping probes — that's what creates the orphan-eServer pileup.
    """
    now_ts = time.time()
    with _IBKR_PROBE_LOCK:
        cached = _IBKR_PROBE_CACHE.get("snapshot")
        cached_ts = float(_IBKR_PROBE_CACHE.get("ts") or 0.0)
        if cached is not None and (now_ts - cached_ts) < _IBKR_PROBE_CACHE_TTL_S:
            # Refresh the dynamic 'today' field but keep the rest cached
            # so consumers get a stable shape even when serving from cache.
            cached_copy = dict(cached)
            cached_copy["served_from_cache"] = True
            cached_copy["cache_age_s"] = round(now_ts - cached_ts, 2)
            return cached_copy
    snapshot: dict = {
        "ready": False,
        "today_executions": 0,
        "today_realized_pnl": 0.0,
        "open_positions": [],
        "open_position_count": 0,
        "unrealized_pnl": 0.0,
        "futures_pnl": None,
        "account_net_liquidation": None,
        "account_summary_realized_pnl": None,
        "account_summary_unrealized_pnl": None,
        "account_mtd_pnl": None,
        "account_mtd_return_pct": None,
        "account_mtd_source": "",
        "account_mtd_error": "",
        "account_mtd_baseline_net_liquidation": None,
        "account_mtd_baseline_set_at": "",
        "account_mtd_baseline_origin": "",
        "account_mtd_baseline_note": "",
        "account_mtd_baseline_initialized": False,
        "account_summary_tags": {},
        "checked_utc": datetime.now(UTC).isoformat(),
    }
    if _truthy_env("ETA_DASHBOARD_DISABLE_BROKER_PROBES"):
        snapshot["disabled"] = True
        snapshot["error"] = "ibkr_probe_disabled"
        return snapshot
    # ib_insync's package init calls asyncio.get_event_loop() at import
    # time. FastAPI/AnyIO worker threads have no loop, so the import
    # itself raises. Install a fresh loop in this thread BEFORE the
    # import so the probe works inside sync handlers under uvicorn.
    import asyncio  # noqa: PLC0415

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    except Exception as exc:  # noqa: BLE001
        snapshot["error"] = f"event_loop_setup_failed: {exc}"
        return snapshot
    try:
        from ib_insync import IB  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        snapshot["error"] = f"ib_insync_unavailable: {exc}"
        with contextlib.suppress(Exception):
            loop.close()
        return snapshot
    host = os.environ.get("ETA_IBKR_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("ETA_IBKR_PORT", "4002"))
    except ValueError:
        port = 4002
    # Use a small deterministic client-id lane so stale workers don't spray
    # IB Gateway with random dashboard sessions on every probe.
    client_id_candidates = _dashboard_ibkr_client_id_candidates()
    client_id = client_id_candidates[0]
    # 5s wasn't enough under load — IBG is slow to handshake when the
    # supervisor is also pumping market data. Bump to 12s; the dashboard
    # endpoint only takes that hit when the gateway is actually slow.
    connect_timeout = _dashboard_ibkr_connect_timeout_s()
    ib = None
    try:
        last_connect_exc: Exception | None = None
        for idx, candidate_client_id in enumerate(client_id_candidates):
            client_id = candidate_client_id
            ib = IB()
            try:
                loop.run_until_complete(ib.connectAsync(host, port, clientId=client_id, timeout=connect_timeout))
                break
            except Exception as exc:  # noqa: BLE001
                last_connect_exc = exc
                with contextlib.suppress(Exception):
                    if ib.isConnected():
                        ib.disconnect()
                if idx < (len(client_id_candidates) - 1) and _ibkr_client_id_retryable_error(exc):
                    continue
                raise
        else:
            if last_connect_exc is not None:
                raise last_connect_exc

        try:
            try:
                portfolio = list(ib.portfolio()) if ib.isConnected() else []
            except Exception:  # noqa: BLE001
                portfolio = []
            managed_accounts = list(ib.managedAccounts()) if ib.isConnected() else []
            snapshot["managed_accounts"] = managed_accounts
            try:
                account_summary = list(ib.accountSummary()) if ib.isConnected() else []
            except Exception:  # noqa: BLE001
                account_summary = []
            preferred_account = next(
                (acct for acct in managed_accounts if acct not in (None, "", "All")),
                None,
            )
            snapshot["preferred_account"] = preferred_account
            summary_tags: dict[str, float] = {}
            for row in account_summary:
                tag = getattr(row, "tag", None)
                if tag not in {"FuturesPNL", "RealizedPnL", "UnrealizedPnL", "NetLiquidation"}:
                    continue
                value = _float_value(getattr(row, "value", None))
                if value is None:
                    continue
                account = getattr(row, "account", None)
                if tag not in summary_tags or account == preferred_account:
                    summary_tags[tag] = value
            snapshot["account_summary_tags"] = {tag: round(value, 2) for tag, value in summary_tags.items()}
            if "FuturesPNL" in summary_tags:
                snapshot["futures_pnl"] = round(summary_tags["FuturesPNL"], 2)
            if "NetLiquidation" in summary_tags:
                snapshot["account_net_liquidation"] = round(summary_tags["NetLiquidation"], 2)
            if "RealizedPnL" in summary_tags:
                snapshot["account_summary_realized_pnl"] = round(summary_tags["RealizedPnL"], 2)
            if "UnrealizedPnL" in summary_tags:
                snapshot["account_summary_unrealized_pnl"] = round(summary_tags["UnrealizedPnL"], 2)
            mtd_snapshot = _ibkr_client_portal_mtd_snapshot(
                managed_accounts=managed_accounts,
                preferred_account=preferred_account,
            )
            if _float_value(mtd_snapshot.get("mtd_pnl")) is None:
                tracked_mtd = _ibkr_net_liquidation_mtd_snapshot(
                    account_id=preferred_account or next(iter(managed_accounts), None),
                    net_liquidation=snapshot.get("account_net_liquidation"),
                    checked_at=snapshot["checked_utc"],
                )
                if tracked_mtd.get("ready"):
                    client_portal_error = str(mtd_snapshot.get("error") or "").strip()
                    if client_portal_error:
                        tracked_mtd["client_portal_error"] = client_portal_error
                    mtd_snapshot = tracked_mtd
            snapshot["account_mtd_source"] = str(mtd_snapshot.get("source") or "")
            snapshot["account_mtd_error"] = str(mtd_snapshot.get("error") or "")
            account_mtd_pnl = _float_value(mtd_snapshot.get("mtd_pnl"))
            if account_mtd_pnl is not None:
                snapshot["account_mtd_pnl"] = round(account_mtd_pnl, 2)
            account_mtd_return_pct = _float_value(mtd_snapshot.get("mtd_return_pct"))
            if account_mtd_return_pct is not None:
                snapshot["account_mtd_return_pct"] = round(account_mtd_return_pct, 2)
            baseline_net_liq = _float_value(mtd_snapshot.get("start_nav"))
            if baseline_net_liq is not None:
                snapshot["account_mtd_baseline_net_liquidation"] = round(baseline_net_liq, 2)
            snapshot["account_mtd_baseline_set_at"] = str(mtd_snapshot.get("baseline_set_at") or "")
            snapshot["account_mtd_baseline_origin"] = str(mtd_snapshot.get("baseline_origin") or "")
            snapshot["account_mtd_baseline_note"] = str(mtd_snapshot.get("baseline_note") or "")
            snapshot["account_mtd_baseline_initialized"] = bool(mtd_snapshot.get("baseline_initialized"))
            try:
                open_trades = list(ib.reqAllOpenOrders() or []) if ib.isConnected() else []
            except Exception:  # noqa: BLE001
                try:
                    open_trades = list(ib.openTrades() or []) if ib.isConnected() else []
                except Exception:  # noqa: BLE001
                    open_trades = []
            snapshot["open_orders"] = [_ibkr_open_order_snapshot(trade) for trade in open_trades]
            snapshot["open_order_count"] = len(snapshot["open_orders"])
            unreal = 0.0
            slim_positions: list[dict] = []
            for item in portfolio:
                try:
                    upl = float(getattr(item, "unrealizedPNL", 0.0) or 0.0)
                except (TypeError, ValueError):
                    upl = 0.0
                unreal += upl
                contract = getattr(item, "contract", None)
                slim_positions.append(
                    {
                        "symbol": getattr(contract, "localSymbol", None) or getattr(contract, "symbol", None),
                        "secType": getattr(contract, "secType", None),
                        "exchange": getattr(contract, "exchange", None),
                        "position": float(getattr(item, "position", 0.0) or 0.0),
                        "avg_cost": float(getattr(item, "averageCost", 0.0) or 0.0),
                        "market_price": float(getattr(item, "marketPrice", 0.0) or 0.0),
                        "market_value": float(getattr(item, "marketValue", 0.0) or 0.0),
                        "unrealized_pnl": upl,
                    }
                )
            snapshot["open_positions"] = slim_positions
            snapshot["open_position_count"] = len(slim_positions)
            snapshot["unrealized_pnl"] = round(unreal, 2)
            try:
                fills = list(ib.fills()) if ib.isConnected() else []
            except Exception:  # noqa: BLE001
                fills = []
            today_count = 0
            for fill in fills:
                exec_obj = getattr(fill, "execution", None)
                fill_time = getattr(exec_obj, "time", None) if exec_obj is not None else None
                if fill_time is None:
                    continue
                if isinstance(fill_time, datetime):
                    ft = fill_time if fill_time.tzinfo else fill_time.replace(tzinfo=UTC)
                    if ft >= today_start_utc:
                        today_count += 1
            snapshot["today_executions"] = today_count
            snapshot["today_realized_pnl"] = _derive_ibkr_today_realized_pnl(snapshot)
            snapshot["client_id"] = client_id
            snapshot["ready"] = True
        finally:
            with contextlib.suppress(Exception):
                if ib is not None and ib.isConnected():
                    ib.disconnect()
            with contextlib.suppress(Exception):
                loop.close()
    except Exception as exc:  # noqa: BLE001 — broker degrade must not crash dashboard
        # Include the exception type because asyncio.TimeoutError /
        # CancelledError both have empty str() reprs — without the type
        # the operator just sees "ibkr_probe_failed: " with no detail.
        exc_type = type(exc).__name__
        exc_msg = str(exc) or repr(exc)
        snapshot["error"] = f"ibkr_probe_failed:{exc_type}: {exc_msg}"
        snapshot["client_id"] = client_id
    # Cache outcome (success or failure) so back-to-back dashboard hits
    # don't pile orphan eServers in IBG. Failure is cached too — there is
    # no point in probing a wedged gateway every few seconds.
    cached_ts = time.time()
    with _IBKR_PROBE_LOCK:
        _IBKR_PROBE_CACHE["snapshot"] = dict(snapshot)
        _IBKR_PROBE_CACHE["ts"] = cached_ts
    _persist_ibkr_probe_cache(dict(snapshot), ts=cached_ts)
    return snapshot


def _live_broker_state_payload() -> dict:
    """Aggregate focus broker live state plus Alpaca/spot backburner evidence.

    Surfaces ``today_actual_fills``, ``today_realized_pnl``,
    ``total_unrealized_pnl`` derived from the brokers' own books — NOT
    from the supervisor decision journal. The supervisor counts continue
    to be served by ``/api/dashboard`` and ``/api/equity`` unchanged.
    """
    now_utc = datetime.now(UTC)
    today_start_utc = _dashboard_local_day_start_utc(now_utc)
    today_start_iso = today_start_utc.isoformat().replace("+00:00", "Z")
    tradovate = _tradovate_dashboard_status_payload()
    alpaca = _alpaca_live_state_snapshot(today_start_iso=today_start_iso)
    alpaca["policy_status"] = "paused_backburner"
    alpaca["policy_reason"] = "Alpaca and spot are parked on the backburner by operator focus policy."
    ibkr = _ibkr_live_state_snapshot(today_start_utc=today_start_utc)
    # Per-bot Alpaca breakdown (added 2026-05-06). Wrapped to fail-soft.
    try:
        per_bot_alpaca = _alpaca_per_bot_pnl_cached(today_start_iso=today_start_iso)
    except Exception as exc:  # noqa: BLE001
        per_bot_alpaca = {
            "ready": False,
            "error": f"per_bot_payload_failed: {exc}",
            "per_bot": {},
        }
    cellar_today_actual_fills = int(alpaca.get("today_filled_orders") or 0)
    cellar_today_realized_pnl = round(float(alpaca.get("today_realized_pnl") or 0.0), 2)
    cellar_total_unrealized_pnl = round(float(alpaca.get("unrealized_pnl") or 0.0), 2)
    cellar_open_position_count = int(alpaca.get("open_position_count") or 0)
    today_actual_fills = int(ibkr.get("today_executions") or 0)
    today_realized_pnl = round(float(ibkr.get("today_realized_pnl") or 0.0), 2)
    broker_mtd_pnl = _float_value(ibkr.get("account_mtd_pnl"))
    broker_mtd_return_pct = _float_value(ibkr.get("account_mtd_return_pct"))
    if broker_mtd_pnl is None:
        cached_mtd = _ibkr_cached_mtd_tracker_snapshot(now_utc)
        if cached_mtd:
            broker_mtd_pnl = _float_value(cached_mtd.get("account_mtd_pnl"))
            broker_mtd_return_pct = _float_value(cached_mtd.get("account_mtd_return_pct"))
            for key in (
                "account_mtd_pnl",
                "account_mtd_return_pct",
                "account_mtd_source",
                "account_mtd_baseline_set_at",
                "account_mtd_checked_at",
                "account_mtd_error",
            ):
                if ibkr.get(key) in (None, "") and cached_mtd.get(key) not in (None, ""):
                    ibkr[key] = cached_mtd[key]
    total_unrealized_pnl = round(float(ibkr.get("unrealized_pnl") or 0.0), 2)
    open_position_count = int(ibkr.get("open_position_count") or 0)
    all_venue_today_actual_fills = today_actual_fills + cellar_today_actual_fills
    all_venue_today_realized_pnl = round(today_realized_pnl + cellar_today_realized_pnl, 2)
    all_venue_total_unrealized_pnl = round(total_unrealized_pnl + cellar_total_unrealized_pnl, 2)
    all_venue_open_position_count = open_position_count + cellar_open_position_count
    win_rate_today = None
    closed_outcome_count_today = 0
    evaluated_outcome_count_today = 0
    all_trade_closes = _recent_trade_closes(limit=5000)
    recent_trade_closes = [row for row in all_trade_closes if not _trade_close_is_cellar(row)]
    cellar_trade_closes = [row for row in all_trade_closes if _trade_close_is_cellar(row)]
    close_history = _limit_close_history_recent_rows(_close_history_windows(recent_trade_closes, now=now_utc))
    all_venue_close_history = _limit_close_history_recent_rows(_close_history_windows(all_trade_closes, now=now_utc))
    close_outcomes_today = _closed_outcomes_from_trade_closes(
        recent_trade_closes,
        since=today_start_utc,
    )
    close_outcomes_30d = _closed_outcomes_from_trade_closes(
        recent_trade_closes,
        since=now_utc - timedelta(days=30),
    )
    if win_rate_today is None and close_outcomes_today["win_rate"] is not None:
        win_rate_today = _float_value(close_outcomes_today.get("win_rate"))
        closed_outcome_count_today = int(close_outcomes_today.get("closed_outcome_count") or 0)
        evaluated_outcome_count_today = int(close_outcomes_today.get("evaluated_outcome_count") or 0)
        win_rate_source = "trade_close_ledger_today"
    else:
        win_rate_source = ""
    # 30d win-rate from blotter fills (best-effort; uses local ledger
    # because broker REST is too narrow for 30-day history without paging).
    win_rate_30d: float | None = None
    win_rate_30d_source = ""
    try:
        wins = 0
        losses = 0
        cutoff = now_utc - timedelta(days=30)
        for row in _recent_live_fill_rows():
            if _trade_close_is_cellar(row):
                continue
            ts_dt = _parse_fill_dt(row.get("ts"))
            if ts_dt is None or ts_dt < cutoff:
                continue
            r = _float_value(row.get("realized_pnl") or row.get("pnl"))
            if r is None:
                continue
            if r > 0:
                wins += 1
            elif r < 0:
                losses += 1
        if (wins + losses) > 0:
            win_rate_30d = round(wins / (wins + losses), 4)
            win_rate_30d_source = "live_fill_ledger_30d"
    except Exception:  # noqa: BLE001
        win_rate_30d = None
    if win_rate_30d is None and close_outcomes_30d["win_rate"] is not None:
        win_rate_30d = _float_value(close_outcomes_30d.get("win_rate"))
        win_rate_30d_source = "trade_close_ledger_30d"
    broker_ready = bool(ibkr.get("ready")) and not ibkr.get("error")
    payload = {
        "server_ts": time.time(),
        "ready": broker_ready,
        "focus_policy": _dashboard_focus_policy_payload(),
        "reporting_timezone": DASHBOARD_LOCAL_TIME_ZONE_NAME,
        "today_start_utc": today_start_iso,
        "today_day_boundary": "local_midnight",
        "today_actual_fills": today_actual_fills,
        "today_realized_pnl": today_realized_pnl,
        "broker_mtd_pnl": broker_mtd_pnl,
        "broker_mtd_return_pct": broker_mtd_return_pct,
        "total_unrealized_pnl": total_unrealized_pnl,
        "open_position_count": open_position_count,
        "all_venue_today_actual_fills": all_venue_today_actual_fills,
        "all_venue_today_realized_pnl": all_venue_today_realized_pnl,
        "all_venue_total_unrealized_pnl": all_venue_total_unrealized_pnl,
        "all_venue_open_position_count": all_venue_open_position_count,
        "cellar_today_actual_fills": cellar_today_actual_fills,
        "cellar_today_realized_pnl": cellar_today_realized_pnl,
        "cellar_total_unrealized_pnl": cellar_total_unrealized_pnl,
        "cellar_open_position_count": cellar_open_position_count,
        "win_rate_30d": win_rate_30d,
        "win_rate_30d_source": win_rate_30d_source,
        "win_rate_today": win_rate_today,
        "closed_outcome_count_today": closed_outcome_count_today,
        "evaluated_outcome_count_today": evaluated_outcome_count_today,
        "win_rate_source": win_rate_source,
        "recent_close_count_30d": int(close_outcomes_30d.get("closed_outcome_count") or 0),
        "recent_close_evaluated_count_30d": int(close_outcomes_30d.get("evaluated_outcome_count") or 0),
        "recent_close_realized_pnl_30d": _float_value(close_outcomes_30d.get("realized_pnl")),
        "cellar_recent_close_count": len(cellar_trade_closes),
        "close_history": close_history,
        "all_venue_close_history": all_venue_close_history,
        "tradovate": tradovate,
        "alpaca": alpaca,
        "ibkr": ibkr,
        "per_bot_alpaca": per_bot_alpaca,
        "broker_snapshot_source": "live_broker_rest",
        "broker_snapshot_age_s": 0.0,
        "broker_snapshot_state": "fresh",
        "sources": {
            "session_pnl": "ibkr_live_broker_state",
            "broker_mtd_pnl": str(ibkr.get("account_mtd_source") or "unavailable"),
            "focus_mtd_closed_pnl": "trade_close_ledger",
            "tradovate_paper_portfolio": str(tradovate.get("status") or "dormant"),
        },
        "source": "live_broker_rest",
    }
    payload["position_exposure"] = _position_exposure_payload(payload, close_history=close_history)
    return payload


def _ibkr_refresh_probe_error(payload: dict) -> str:
    """Return the IBKR probe error that makes a manual refresh unsafe to trust."""
    if not isinstance(payload, dict):
        return ""
    ibkr = payload.get("ibkr") if isinstance(payload.get("ibkr"), dict) else {}
    error = str(ibkr.get("error") or payload.get("error") or "").strip()
    if not error:
        return ""
    if ibkr.get("ready") is False and error.startswith("ibkr_probe_failed:"):
        return error
    return ""


def _last_good_broker_state_after_failed_refresh(live_payload: dict) -> dict:
    """Prefer last-good broker truth when an explicit IBKR refresh times out."""
    error = _ibkr_refresh_probe_error(live_payload)
    if not error:
        return live_payload
    try:
        cached = _cached_live_broker_state_for_diagnostics()
    except Exception:  # noqa: BLE001 - direct broker endpoint must fail soft.
        cached = {}
    if not isinstance(cached, dict) or cached.get("error") or cached.get("ready") is not True:
        out = dict(live_payload)
        out["refresh_probe_failed"] = True
        out["refresh_probe_error"] = error
        return out
    out = dict(cached)
    out["refresh_requested"] = True
    out["refresh_probe_failed"] = True
    out["refresh_probe_error"] = error
    out["refresh_probe_source"] = live_payload.get("broker_snapshot_source") or live_payload.get("source")
    return out


@app.get("/api/live/broker_state")
def live_broker_state(response: Response, refresh: bool = False) -> dict:
    """Broker truth for dashboard reality-check panels.

    Added 2026-05-06. Sits alongside ``/api/dashboard`` (supervisor-journal
    truth) and ``/api/equity`` (per-bot heartbeat-derived equity). The
    dashboard payload also embeds this rollup under ``live_broker_state``
    so the front end can render reality-vs-journal side by side without
    a second round trip. Defaults to cached broker truth for fast operator
    first paint; pass ``refresh=1`` when explicitly requesting a fresh probe.
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    if not refresh:
        return _cached_live_broker_state_for_diagnostics()
    return _last_good_broker_state_after_failed_refresh(_live_broker_state_payload())


@app.get("/api/live/broker_summary")
def live_broker_summary(response: Response, refresh: bool = False) -> dict:
    """Compact broker truth for monitoring without large trade-history rows."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return _live_broker_summary_payload(refresh=refresh)


@app.get("/api/live/position_exposure")
def live_position_exposure(response: Response) -> dict:
    """Read-only broker exposure plus supervisor paper-watch close evidence."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    try:
        roster = bot_fleet_roster(Response(), since_days=1, live_broker_probe=True)
        live_state = roster.get("live_broker_state") if isinstance(roster, dict) else {}
        embedded = live_state.get("position_exposure") if isinstance(live_state, dict) else None
        if isinstance(embedded, dict):
            return embedded
    except Exception:
        pass
    live_state = _live_broker_state_payload()
    embedded = live_state.get("position_exposure") if isinstance(live_state, dict) else None
    if isinstance(embedded, dict):
        return embedded
    return _position_exposure_payload(live_state)


@app.get("/api/jarvis/broker_bracket_audit")
@app.get("/api/live/broker_bracket_audit")
def broker_bracket_audit_status(response: Response) -> dict:
    """Direct read-only broker-native OCO/bracket coverage audit."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return _broker_bracket_audit_endpoint_payload()


@app.get("/api/live/per_bot_alpaca")
def live_per_bot_alpaca(response: Response) -> dict:
    """Per-bot Alpaca PnL + drift-vs-backtest flag (added 2026-05-06).

    Sibling endpoint to ``/api/live/broker_state`` — surfaces the same
    per-bot rollup that ``live_broker_state`` embeds under the
    ``per_bot_alpaca`` key so consumers that only need the breakdown
    don't have to round-trip the aggregate snapshot.

    Cache TTL is 60s by default (``ETA_DASHBOARD_ALPACA_PER_BOT_CACHE_TTL_S``).
    Failure modes never crash — the helper returns ``ready=false`` plus an
    ``error`` field instead.
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    today_start_utc = _dashboard_local_day_start_utc()
    today_start_iso = today_start_utc.isoformat().replace("+00:00", "Z")
    try:
        return _alpaca_per_bot_pnl_cached(today_start_iso=today_start_iso)
    except Exception as exc:  # noqa: BLE001 — fail-soft
        return {
            "ready": False,
            "error": f"per_bot_endpoint_failed: {exc}",
            "per_bot": {},
            "checked_utc": datetime.now(UTC).isoformat(),
        }


@app.get("/api/preflight")
def preflight_throttle_map() -> dict:
    """Live correlation throttle map (which symbol pairs are throttled)."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    data = read_json_safe(_state_dir() / "safety" / "preflight_correlation_latest.json")
    if "_warning" in data:
        return {"throttles": []}
    return data


@app.get("/api/jarvis/sage_modulation_stats")
def sage_modulation_stats() -> dict:
    """Per-bot count of v22 agree-loosen / disagree-tighten / defer in last 24h."""
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    data = read_json_safe(_state_dir() / "sage" / "modulation_stats_24h.json")
    if "_warning" in data:
        return {"per_bot": {}, "_warning": "no_data"}
    return data


@app.get("/api/jarvis/sage_modulation_toggle")
def get_sage_modulation_toggle() -> dict:
    """Current state of the V22_SAGE_MODULATION feature flag."""
    enabled = os.environ.get("ETA_FF_V22_SAGE_MODULATION", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
        "y",
    )
    return {"enabled": enabled, "flag_name": "ETA_FF_V22_SAGE_MODULATION"}


@app.post("/api/jarvis/sage_modulation_toggle")
def post_sage_modulation_toggle(
    req: SageModulationToggleRequest,
    _: dict = Depends(require_step_up),  # noqa: B008 -- FastAPI dependency-injection idiom
) -> dict:
    """Flip ETA_FF_V22_SAGE_MODULATION (process env + persistent state file)."""
    val = "true" if req.enabled else "false"
    os.environ["ETA_FF_V22_SAGE_MODULATION"] = val
    flag_path = _state_dir() / "feature_flags.json"
    flag_path.parent.mkdir(parents=True, exist_ok=True)

    # Lock + read + modify + atomic-write
    lock_path = flag_path.with_suffix(".lock")
    with portalocker.Lock(str(lock_path), mode="a", timeout=5, flags=portalocker.LOCK_EX):
        existing: dict = {}
        if flag_path.exists():
            try:
                existing = json.loads(flag_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}
        existing["ETA_FF_V22_SAGE_MODULATION"] = val
        # Atomic write: write to temp file, then rename
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(flag_path.parent),
            prefix=".feature_flags_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(existing, indent=2))
            os.replace(tmp_name, str(flag_path))
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    _append_dashboard_event(
        "sage_modulation_toggle",
        {"enabled": req.enabled, "by": _["user"]},
    )
    return {"enabled": req.enabled}


# ─── Bot lifecycle endpoints (Wave-7 Task 8, 2026-04-27) ───────────
def _write_control_signal(bot_id: str, action: str, by_user: str) -> Path:
    """Write a control signal file the bot daemon polls."""
    from datetime import UTC, datetime

    sig_dir = _state_dir() / "bots" / bot_id / "control_signals"
    sig_dir.mkdir(parents=True, exist_ok=True)
    sig_path = sig_dir / f"{action}.json"
    sig_path.write_text(
        json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "action": action,
                "by": by_user,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return sig_path


def _validate_bot_id(bot_id: str) -> None:
    """Raise 400 if bot_id is not a safe identifier."""
    if not _BOT_ID_RE.match(bot_id):
        raise HTTPException(status_code=400, detail={"error_code": "invalid_bot_id"})


@app.post("/api/bot/{bot_id}/pause")
def bot_pause(bot_id: str, session: dict = Depends(require_session)) -> dict:  # noqa: B008
    """Signal the bot to pause new entries (existing positions kept)."""
    _validate_bot_id(bot_id)
    _write_control_signal(bot_id, "pause", session["user"])
    _append_dashboard_event("bot_pause", {"bot_id": bot_id, "by": session["user"]})
    return {"ok": True, "action": "pause", "bot_id": bot_id}


@app.post("/api/bot/{bot_id}/resume")
def bot_resume(bot_id: str, session: dict = Depends(require_session)) -> dict:  # noqa: B008
    """Signal the bot to resume taking new entries."""
    _validate_bot_id(bot_id)
    _write_control_signal(bot_id, "resume", session["user"])
    _append_dashboard_event("bot_resume", {"bot_id": bot_id, "by": session["user"]})
    return {"ok": True, "action": "resume", "bot_id": bot_id}


@app.post("/api/bot/{bot_id}/flatten")
def bot_flatten(bot_id: str, session: dict = Depends(require_step_up)) -> dict:  # noqa: B008
    """Step-up gated: signal bot to flatten ALL positions (reduce_only)."""
    _validate_bot_id(bot_id)
    _write_control_signal(bot_id, "flatten", session["user"])
    _append_dashboard_event("bot_flatten", {"bot_id": bot_id, "by": session["user"]})
    return {"ok": True, "action": "flatten", "bot_id": bot_id}


@app.post("/api/bot/{bot_id}/kill")
def bot_kill(bot_id: str, session: dict = Depends(require_step_up)) -> dict:  # noqa: B008
    """Step-up gated: trip the kill-switch latch for this bot."""
    from datetime import UTC, datetime

    _validate_bot_id(bot_id)
    latch_path = _state_dir() / "safety" / "kill_switch_latch.json"
    latch_path.parent.mkdir(parents=True, exist_ok=True)

    # Lock + read + modify + atomic-write
    lock_path = latch_path.with_suffix(".lock")
    with portalocker.Lock(str(lock_path), mode="a", timeout=5, flags=portalocker.LOCK_EX):
        latches: dict = {}
        if latch_path.exists():
            try:
                latches = json.loads(latch_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                latches = {}
        latches[bot_id] = {
            "latch_state": "tripped",
            "reason": "operator_kill",
            "tripped_at": datetime.now(UTC).isoformat(),
            "tripped_by": session["user"],
        }
        # Atomic write: write to temp file, then rename
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(latch_path.parent),
            prefix=".kill_switch_latch_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(latches, indent=2))
            os.replace(tmp_name, str(latch_path))
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    _write_control_signal(bot_id, "kill", session["user"])
    _append_dashboard_event("bot_kill", {"bot_id": bot_id, "by": session["user"]})
    return {"ok": True, "action": "kill", "bot_id": bot_id, "latch_state": "tripped"}


# ─── Live SSE stream (Wave-7, 2026-04-27) ─────────────────────────
@app.get("/api/live/stream")
async def live_stream(_: dict = Depends(require_session)) -> StreamingResponse:  # noqa: B008
    """SSE stream: 'verdict' events from today's audit JSONL,
    'fill' events from blotter fills JSONL.

    Re-resolves today's audit path on each iteration so midnight
    rotation is transparent.
    """
    from datetime import UTC
    from datetime import datetime as _dt
    from typing import TYPE_CHECKING

    from eta_engine.deploy.scripts.dashboard_sse import stream_audit_and_fills

    if TYPE_CHECKING:
        from collections.abc import AsyncIterator

    async def gen() -> AsyncIterator[str]:
        # Re-resolve today's audit path inside the generator so a
        # rollover at midnight gets picked up.
        today = _dt.now(UTC).strftime("%Y-%m-%d")
        audit_path = _state_dir() / "jarvis_audit" / f"{today}.jsonl"
        fills_path = _state_dir() / "blotter" / "fills.jsonl"
        async for event in stream_audit_and_fills(audit_path, fills_path):
            yield event

    return StreamingResponse(gen(), media_type="text/event-stream")


# ─── Cross-policy verdict diff (Tier-4 #17, 2026-04-27) ────────────
@app.get("/api/jarvis/policy_diff")
def jarvis_policy_diff(window_days: int = 30) -> dict:
    """For each registered candidate (v18/v19/v20/v21), what would it
    have done differently from the champion (v17) in the last
    ``window_days`` of audit records?

    Returns a per-candidate dict with metrics from
    ``score_policy_candidate.candidate_metrics``. Operator uses this to
    inspect whether a candidate's behavior differs meaningfully from
    the champion before flipping ETA_BANDIT_ENABLED on.
    """
    try:
        from datetime import UTC, datetime, timedelta

        from eta_engine.brain.jarvis_v3 import policies  # noqa: F401  (auto-register)
        from eta_engine.brain.jarvis_v3.candidate_policy import list_candidates
        from eta_engine.scripts.score_policy_candidate import (
            candidate_metrics,
            champion_metrics,
            load_audit_records,
        )

        # Canonical: <workspace>/var/eta_engine/state/jarvis_audit
        # Read fallback: legacy in-repo path ONLY (no LOCALAPPDATA — that  # HISTORICAL-PATH-OK
        # path violated the workspace write-root rule and never lands
        # writes after this migration).
        audit_dir = _state_dir() / "jarvis_audit"
        if not audit_dir.exists():
            legacy_audit = _LEGACY_STATE / "jarvis_audit"
            if legacy_audit.exists():
                audit_dir = legacy_audit
        cutoff = datetime.now(UTC) - timedelta(days=window_days)
        records = load_audit_records(list(audit_dir.glob("*.jsonl")), since=cutoff)
        champion = champion_metrics(records)
        diffs: dict[str, dict] = {}
        for c in list_candidates():
            if c["name"] == "v17":
                continue
            try:
                diffs[c["name"]] = candidate_metrics(records, candidate_module=c["name"])
            except Exception as exc:  # noqa: BLE001
                diffs[c["name"]] = {"error": str(exc)}
        return {
            "window_days": window_days,
            "n_records": len(records),
            "champion": champion,
            "candidates": diffs,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error_code": "diff_failed", "error_detail": str(exc)}


@app.get("/api/kaizen")
def kaizen_summary() -> dict:
    """Kaizen ledger -- retrospectives + tickets."""
    data = _read_json("kaizen_ledger.json")
    return {
        "retrospectives": len(data.get("retrospectives", [])),
        "tickets_total": len(data.get("tickets", [])),
        "tickets_open": sum(1 for t in data.get("tickets", []) if t.get("status") == "OPEN"),
        "tickets_shipped": sum(1 for t in data.get("tickets", []) if t.get("status") == "SHIPPED"),
        "latest_tickets": data.get("tickets", [])[-5:],
    }


@app.get("/api/firm-scorecard")
def firm_scorecard() -> dict:
    """Firm benchmark scorecard -- composite + 9 category scores."""
    path = _state_dir() / "firm_scorecard_latest.json"
    if not path.exists():
        path2 = _state_dir().parent / "var" / "eta_engine" / "state" / "firm_scorecard_latest.json"
        if path2.exists():
            return json.loads(path2.read_text(encoding="utf-8"))
        return {
            "status": "not_ready",
            "composite_score": 0.0,
            "grade": "N/A",
            "categories": {},
            "summary": {"message": "Scorecard not yet computed. Run FIRM_SCORECARD background task."},
        }
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/state/{filename}")
def get_state_file(filename: str) -> dict:
    """Fetch a raw JSON state file. Filename is safelisted."""
    safe = {
        "avengers_heartbeat.json",
        "dashboard_payload.json",
        "last_task.json",
        "kaizen_ledger.json",
        "shadow_ledger.json",
        "usage_tracker.json",
        "distiller.json",
        "precedent_graph.json",
        "strategy_candidates.json",
        "twin_verdict.json",
        "causal_review.json",
        "drift_summary.json",
        "cache_warmup.json",
        "audit_daily_summary.json",
    }
    if filename not in safe:
        raise HTTPException(status_code=403, detail="filename not on safelist")
    return _read_json(filename)


@app.get("/api/personas")
def list_personas() -> dict:
    """Return each persona's peak manual + skill catalog + MCP capabilities.

    Consumed by the status page to render a training/identity panel. Also
    useful for operator introspection via `curl /api/personas | jq`.
    """
    try:
        from eta_engine.brain.jarvis_v3.training.collaboration import (
            PROTOCOLS,
        )
        from eta_engine.brain.jarvis_v3.training.mcp_awareness import (
            PERSONA_MCPS,
        )
        from eta_engine.brain.jarvis_v3.training.peak_manuals import (
            PEAK_MANUALS,
        )
        from eta_engine.brain.jarvis_v3.training.skills_catalog import (
            PERSONA_SKILLS,
        )
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=f"training module missing: {exc}") from exc

    return {
        "personas": [
            {
                "name": name,
                "manual": manual.model_dump(),
                "skills": [s.model_dump() for s in PERSONA_SKILLS.get(name, [])],
                "mcps": [p.model_dump() for p in PERSONA_MCPS.get(name, [])],
            }
            for name, manual in PEAK_MANUALS.items()
        ],
        "protocols": [p.model_dump() for p in PROTOCOLS],
    }


@app.get("/api/personas/{persona}/eval")
def persona_eval(persona: str) -> dict:
    """Return the most recent eval report for a persona, if one has been run."""
    safe = persona.upper()
    path = STATE_DIR / "persona_evals" / f"{safe}-latest.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no eval report for {persona}")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/jarvis/decisions")
def jarvis_decisions(n: int = 20, subsystem: str | None = None) -> dict:
    """Tail the last N JARVIS audit-log decisions.

    Source is the JSONL file JarvisAdmin writes on every
    ``request_approval`` / ``select_llm_tier`` call. Consumed by the
    status page 'JARVIS Decision Log' card for real-time visibility
    into what JARVIS is approving/denying across the fleet.

    Query params:
      * n: how many of the most recent records to return (default 20)
      * subsystem: optional filter like "bot.mnq" or "bot.btc_hybrid"
    """
    audit_path = _state_dir() / "jarvis_audit.jsonl"
    if not audit_path.exists():
        return {
            "decisions": [],
            "total": 0,
            "source": str(audit_path),
            "note": "no jarvis audit log yet -- no bots have asked for approval",
        }
    lines: list[str] = []
    try:
        with audit_path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot read audit log: {exc}") from exc

    # Parse bottom-up so the most recent are first. Keep only valid JSON.
    decisions: list[dict] = []
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if subsystem is not None:
            sub = rec.get("request", {}).get("subsystem")
            if sub != subsystem:
                continue
        decisions.append(
            {
                "ts": rec.get("ts"),
                "subsystem": rec.get("request", {}).get("subsystem"),
                "action": rec.get("request", {}).get("action"),
                "verdict": rec.get("response", {}).get("verdict"),
                "reason_code": rec.get("response", {}).get("reason_code"),
                "reason": rec.get("response", {}).get("reason"),
                "size_cap_mult": rec.get("response", {}).get("size_cap_mult"),
                "stress_composite": rec.get("stress_composite"),
                "session_phase": rec.get("session_phase"),
                "jarvis_action": rec.get("jarvis_action"),
            }
        )
        if len(decisions) >= max(1, min(n, 500)):
            break

    return {
        "decisions": decisions,
        "total": len(lines),
        "returned": len(decisions),
        "source": str(audit_path),
        "filter": {"subsystem": subsystem} if subsystem else None,
    }


@app.get("/api/jarvis/summary")
def jarvis_summary(window: int = 500) -> dict:
    """Rolling-window summary of JARVIS decisions: counts by subsystem + verdict.

    Gives the operator panel a one-shot health snapshot: "how many
    orders did JARVIS gate in the last 500 decisions, split across
    each bot and each verdict?"
    """
    audit_path = _state_dir() / "jarvis_audit.jsonl"
    if not audit_path.exists():
        return {"window": window, "total": 0, "by_subsystem": {}, "by_verdict": {}}
    try:
        with audit_path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot read audit log: {exc}") from exc

    tail = lines[-window:] if window > 0 else lines
    by_subsystem: dict[str, int] = {}
    by_verdict: dict[str, int] = {}
    by_sub_verdict: dict[str, dict[str, int]] = {}
    processed = 0
    for raw in tail:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        sub = rec.get("request", {}).get("subsystem", "unknown")
        verdict = rec.get("response", {}).get("verdict", "unknown")
        by_subsystem[sub] = by_subsystem.get(sub, 0) + 1
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
        sub_v = by_sub_verdict.setdefault(sub, {})
        sub_v[verdict] = sub_v.get(verdict, 0) + 1
        processed += 1
    return {
        "window": window,
        "total": processed,
        "by_subsystem": by_subsystem,
        "by_verdict": by_verdict,
        "by_sub_verdict": by_sub_verdict,
    }


@app.get("/api/tasks")
def list_tasks() -> dict:
    """Return the 12 BackgroundTask names + owners + cadences."""
    from eta_engine.brain.avengers import TASK_CADENCE, TASK_OWNERS

    return {
        "tasks": [{"name": k.value, "owner": TASK_OWNERS[k], "cadence": TASK_CADENCE[k]} for k in TASK_CADENCE],
    }


@app.get("/api/brokers")
def broker_readiness() -> dict:
    """Return the paper-broker readiness snapshot for focus venues plus cellar.

    Consumed by the 'Broker Paper' dashboard card to answer:
    "which venues can actually place orders right now?"

    Alpaca remains adapter-visible, but the operator paused Alpaca/spot
    strategies; active readiness now means IBKR/Tastytrade futures focus.
    """
    try:
        from eta_engine.venues.alpaca import alpaca_paper_readiness
        from eta_engine.venues.ibkr import ibkr_paper_readiness
        from eta_engine.venues.tastytrade import tastytrade_paper_readiness
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"broker adapters not importable: {exc}",
        ) from exc
    try:
        ibkr = ibkr_paper_readiness()
    except Exception as exc:  # noqa: BLE001 -- surface the failure, don't crash the dashboard
        ibkr = {"adapter_available": False, "ready": False, "error": str(exc)}
    try:
        tasty = tastytrade_paper_readiness()
    except Exception as exc:  # noqa: BLE001
        tasty = {"adapter_available": False, "ready": False, "error": str(exc)}
    try:
        alpaca = alpaca_paper_readiness()
    except Exception as exc:  # noqa: BLE001
        alpaca = {"adapter_available": False, "ready": False, "error": str(exc)}
    alpaca["policy_status"] = "paused_cellar"
    alpaca["policy_reason"] = "Alpaca/spot strategies paused by operator focus policy."
    brokers = {"ibkr": ibkr, "tastytrade": tasty, "alpaca": alpaca}
    return {
        "brokers": brokers,
        "focus_policy": _dashboard_focus_policy_payload(),
        "paused_brokers": list(_PAUSED_CELLAR_BROKERS),
        "pending_brokers": list(_PENDING_FOCUS_BROKERS),
        "dormant_brokers": list(_DORMANT_FOCUS_BROKERS),
        "active_brokers": sorted(
            name for name, report in brokers.items() if name in _ACTIVE_FOCUS_BROKERS and report.get("ready")
        ),
    }


def _resolve_fleet_dir() -> Path | None:
    """Find the BTC broker-paper fleet directory across possible deploy layouts.

    Resolution order (first match wins):

    1. ``ETA_BTC_FLEET_DIR`` env override -- operator-scoped hard pin.
       Tests use this to point at a tmp_path; production can pin a
       custom writable location off the default.
    2. ``STATE_DIR/broker_fleet`` -- the natural operator-scoped path.
    3. ``eta_engine`` subtrees under either ``STATE_DIR`` or
       ``Path.home()`` -- legacy layouts.
    4. ``btc_broker_fleet.DEFAULT_OUT_DIR`` -- the package-relative
       default (``docs/btc_live/broker_fleet`` under the installed
       package). Last because it would otherwise shadow the operator
       override when the dev tree happens to contain real fleet data.
    """
    env_pin = os.environ.get("ETA_BTC_FLEET_DIR")
    if env_pin:
        pinned = Path(env_pin)
        return pinned if pinned.exists() else None
    candidates: list[Path] = [
        STATE_DIR / "broker_fleet",
        STATE_DIR.parent / "eta_engine" / "docs" / "btc_live" / "broker_fleet",
        Path.home() / "eta_engine" / "docs" / "btc_live" / "broker_fleet",
    ]
    try:
        from eta_engine.scripts.btc_broker_fleet import DEFAULT_OUT_DIR

        candidates.append(DEFAULT_OUT_DIR)
    except Exception:  # noqa: BLE001 -- import errors should not crash the endpoint
        pass
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


@app.get("/api/btc/lanes")
def btc_lanes() -> dict:
    """Return the current state of the four BTC broker-paper lanes.

    Reads the fleet manifest (written by btc_broker_fleet) and the
    per-worker lane state files (written by PaperLaneRunner). Answers
    'what is each lane doing right now?' without exposing any secrets.
    """
    chosen = _resolve_fleet_dir()
    if chosen is None:
        # Surface the best-known default so operators see where we looked.
        try:
            from eta_engine.scripts.btc_broker_fleet import DEFAULT_OUT_DIR

            default = str(DEFAULT_OUT_DIR)
        except Exception:  # noqa: BLE001
            default = str(STATE_DIR / "broker_fleet")
        return {
            "fleet_dir": default,
            "manifest": None,
            "lanes": [],
            "note": "fleet dir not found; start the fleet via python -m eta_engine.scripts.btc_broker_fleet --start",
        }
    manifest_path = chosen / "btc_broker_fleet_latest.json"
    manifest: dict | None = None
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = None
    workers: list[dict] = []
    lane_files = sorted(chosen.glob("*.lane.json"))
    for lane_file in lane_files:
        try:
            state = json.loads(lane_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        worker_id = state.get("worker_id") or lane_file.stem.replace(".lane", "")
        # Also pull the heartbeat status file for this worker if present.
        hb_path = chosen / f"{worker_id}.json"
        heartbeat: dict | None = None
        if hb_path.exists():
            try:
                heartbeat = json.loads(hb_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                heartbeat = None
        workers.append(
            {
                "worker_id": worker_id,
                "broker": state.get("broker"),
                "lane": state.get("lane"),
                "symbol": state.get("symbol"),
                "active_order_id": state.get("active_order_id"),
                "active_order_status": state.get("active_order_status"),
                "active_order_filled_qty": state.get("active_order_filled_qty"),
                "active_order_avg_price": state.get("active_order_avg_price"),
                "submitted_orders": state.get("submitted_orders"),
                "reconciled_orders": state.get("reconciled_orders"),
                "terminal_orders": state.get("terminal_orders"),
                "last_event": state.get("last_event"),
                "last_event_utc": state.get("last_event_utc"),
                "last_reconcile_utc": state.get("last_reconcile_utc"),
                "heartbeat_status": heartbeat.get("status") if heartbeat else None,
                "pid": heartbeat.get("pid") if heartbeat else None,
                "execution_state": (heartbeat.get("execution_state") if heartbeat else None),
            }
        )
    return {
        "fleet_dir": str(chosen),
        "manifest": manifest,
        "lanes": workers,
        "lane_count": len(workers),
    }


@app.get("/api/btc/trades")
def btc_trades(n: int = 30) -> dict:
    """Tail the paper-trade ledger written by the BTC fleet.

    Surfaces the last N status transitions across all four lanes for
    the 'BTC Paper Trades' dashboard card.
    """
    chosen = _resolve_fleet_dir()
    if chosen is None:
        return {
            "trades": [],
            "total": 0,
            "note": "fleet dir not found",
        }
    ledger = chosen / "btc_paper_trades.jsonl"
    if not ledger.exists():
        return {
            "trades": [],
            "total": 0,
            "source": str(ledger),
            "note": "no trades yet -- either the fleet hasn't run or BTC_PAPER_LANE_AUTO_SUBMIT is not set",
        }
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"cannot read trades ledger: {exc}",
        ) from exc
    trades: list[dict] = []
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            trades.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
        if len(trades) >= max(1, min(n, 500)):
            break
    return {
        "trades": trades,
        "total": len(lines),
        "returned": len(trades),
        "source": str(ledger),
    }


def _resolve_mnq_supervisor_dir() -> Path | None:
    """Find the MNQ live-supervisor output directory.

    Mirrors :func:`_resolve_fleet_dir` but for the MNQ side. Honors
    ``ETA_MNQ_SUPERVISOR_DIR`` as an operator pin + test isolation.
    """
    env_pin = os.environ.get("ETA_MNQ_SUPERVISOR_DIR")
    if env_pin:
        pinned = Path(env_pin)
        return pinned if pinned.exists() else None
    candidates: list[Path] = [
        STATE_DIR / "mnq_live",
        STATE_DIR.parent / "eta_engine" / "docs" / "mnq_live",
        Path.home() / "eta_engine" / "docs" / "mnq_live",
    ]
    try:
        from eta_engine.scripts.mnq_live_supervisor import DEFAULT_OUT_DIR

        candidates.append(DEFAULT_OUT_DIR)
    except Exception:  # noqa: BLE001
        pass
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


@app.get("/api/mnq/supervisor")
def mnq_supervisor() -> dict:
    """Return the MNQ live-supervisor state + recent journal tail.

    Surfaces ``mnq_live_state.json`` (heartbeat + routing counters
    written by :class:`MnqLiveSupervisor` every tick) and the last
    handful of journal events so the dashboard card can show:
    how many bars consumed, orders routed vs blocked, paused state,
    and what JARVIS actually did on recent ticks.
    """
    chosen = _resolve_mnq_supervisor_dir()
    if chosen is None:
        try:
            from eta_engine.scripts.mnq_live_supervisor import DEFAULT_OUT_DIR

            default = str(DEFAULT_OUT_DIR)
        except Exception:  # noqa: BLE001
            default = str(STATE_DIR / "mnq_live")
        return {
            "supervisor_dir": default,
            "state": None,
            "recent_events": [],
            "note": "mnq_live dir not found; start the supervisor via "
            "python -m eta_engine.scripts.mnq_live_supervisor "
            "--bars <file.jsonl>",
        }
    state_path = chosen / "mnq_live_state.json"
    journal_path = chosen / "mnq_live_decisions.jsonl"

    state: dict | None = None
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = None

    recent: list[dict] = []
    if journal_path.exists():
        try:
            lines = journal_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for raw in reversed(lines[-50:]):
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            recent.append(
                {
                    "ts": ev.get("ts"),
                    "actor": ev.get("actor"),
                    "intent": ev.get("intent"),
                    "outcome": ev.get("outcome"),
                    "rationale": ev.get("rationale", "")[:200],
                }
            )
            if len(recent) >= 20:
                break

    return {
        "supervisor_dir": str(chosen),
        "state": state,
        "recent_events": recent,
        "state_path": str(state_path),
        "journal_path": str(journal_path),
    }


@app.get("/api/systems")
def all_systems_status() -> dict:
    """Return a top-level rollup across every major subsystem.

    Each entry is ``{status: GREEN|YELLOW|RED, detail: str}``. GREEN =
    running + healthy, YELLOW = partial / degraded, RED = down or
    blocked. Powers the status-page top banner.
    """
    out: dict[str, dict] = {}

    # Dashboard itself (always GREEN if we're answering)
    out["dashboard"] = {
        "status": "GREEN",
        "detail": f"state_dir_exists={_state_dir().exists()}",
    }

    # Brokers: try readiness checks, tolerate import errors
    ibkr_ready = False
    tasty_ready = False
    alpaca_ready = False
    try:
        from eta_engine.venues.ibkr import ibkr_paper_readiness

        ibkr_ready = bool(ibkr_paper_readiness().get("ready"))
    except Exception:  # noqa: BLE001
        pass
    try:
        from eta_engine.venues.tastytrade import tastytrade_paper_readiness

        tasty_ready = bool(tastytrade_paper_readiness().get("ready"))
    except Exception:  # noqa: BLE001
        pass
    try:
        from eta_engine.venues.alpaca import alpaca_paper_readiness

        alpaca_ready = bool(alpaca_paper_readiness().get("ready"))
    except Exception:  # noqa: BLE001
        pass
    ready_set = {
        "ibkr": ibkr_ready,
        "tastytrade": tasty_ready,
        "alpaca": alpaca_ready,
    }
    ready_names = sorted(name for name, ok in ready_set.items() if ok)
    # GREEN requires the active futures venue pair. Alpaca/spot is paused
    # in the cellar and must not make the systems card look healthier.
    if ibkr_ready and tasty_ready:
        out["brokers"] = {
            "status": "GREEN",
            "detail": f"futures-focus ready: {','.join(name for name in ready_names if name != 'alpaca')}",
        }
    elif ibkr_ready or tasty_ready:
        out["brokers"] = {
            "status": "YELLOW",
            "detail": (
                "partial futures-focus readiness: "
                f"{','.join(name for name in ready_names if name != 'alpaca') or 'none'}; alpaca paused"
            ),
        }
    else:
        out["brokers"] = {
            "status": "RED",
            "detail": "no active futures-focus brokers ready; alpaca paused",
        }

    # BTC fleet: count active lanes
    fleet_dir = _resolve_fleet_dir()
    if fleet_dir is None:
        out["btc_fleet"] = {"status": "RED", "detail": "fleet dir not found"}
    else:
        active = 0
        total = 0
        for lane_file in fleet_dir.glob("*.lane.json"):
            total += 1
            try:
                state = json.loads(lane_file.read_text(encoding="utf-8"))
                if state.get("active_order_id"):
                    active += 1
            except (OSError, json.JSONDecodeError):
                continue
        if total == 0:
            out["btc_fleet"] = {
                "status": "YELLOW",
                "detail": "0 lanes (fleet not started)",
            }
        elif active == total:
            out["btc_fleet"] = {
                "status": "GREEN",
                "detail": f"{active}/{total} lanes ACTIVE",
            }
        elif active > 0:
            out["btc_fleet"] = {
                "status": "YELLOW",
                "detail": f"{active}/{total} lanes ACTIVE",
            }
        else:
            out["btc_fleet"] = {
                "status": "YELLOW",
                "detail": f"{total} lanes idle (no active orders)",
            }

    # MNQ supervisor: state file existence + paused flag
    mnq_dir = _resolve_mnq_supervisor_dir()
    if mnq_dir is None:
        out["mnq_supervisor"] = {
            "status": "YELLOW",
            "detail": "not running",
        }
    else:
        state_path = mnq_dir / "mnq_live_state.json"
        if not state_path.exists():
            out["mnq_supervisor"] = {
                "status": "YELLOW",
                "detail": "no state file yet",
            }
        else:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                state = {}
            if state.get("paused"):
                out["mnq_supervisor"] = {
                    "status": "RED",
                    "detail": "paused (kill or refused STRATEGY_DEPLOY)",
                }
            else:
                bars = state.get("bars_consumed", 0)
                out["mnq_supervisor"] = {
                    "status": "GREEN",
                    "detail": f"armed, {bars} bars consumed",
                }

    # JARVIS audit log: any recent activity?
    audit_path = _state_dir() / "jarvis_audit.jsonl"
    if not audit_path.exists():
        out["jarvis"] = {
            "status": "YELLOW",
            "detail": "no audit log yet",
        }
    else:
        try:
            line_count = sum(1 for _ in audit_path.open("r", encoding="utf-8"))
        except OSError:
            line_count = 0
        out["jarvis"] = {
            "status": "GREEN" if line_count > 0 else "YELLOW",
            "detail": f"{line_count} decisions logged",
        }

    # Compute overall rollup
    statuses = [v["status"] for v in out.values()]
    if "RED" in statuses:
        overall = "RED"
    elif "YELLOW" in statuses:
        overall = "YELLOW"
    else:
        overall = "GREEN"

    return {
        "overall": overall,
        "systems": out,
    }


@app.post("/api/tasks/{task}/fire")
def fire_task(task: str, session: dict = Depends(require_step_up)) -> dict:  # noqa: B008
    """Manually fire a BackgroundTask. Useful for ad-hoc retrospectives."""
    from eta_engine.brain.avengers import BackgroundTask

    try:
        BackgroundTask(task.upper())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"unknown task: {task}") from exc
    try:
        result = run_background_task(task.upper(), _state_dir(), _log_dir(), timeout_s=120)
    except subprocess.TimeoutExpired as exc:
        _append_dashboard_event("task_fire_timeout", {"task": task.upper(), "by": session["user"]})
        raise HTTPException(
            status_code=504,
            detail={"error_code": "task_timeout", "task": task.upper(), "detail": str(exc)},
        ) from exc

    payload = {
        "task": task.upper(),
        "returncode": result.returncode,
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-1000:],
    }
    _append_dashboard_event(
        "task_fire",
        {"task": task.upper(), "by": session["user"], "returncode": result.returncode},
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail={"error_code": "task_failed", **payload})
    return payload


@app.get("/api/ops/audit_timeline")
def ops_audit_timeline(limit: int = 50) -> dict:
    """Recent dashboard control/auth events."""
    capped = max(1, min(limit, 200))
    event_path = _state_dir() / "dashboard_events.jsonl"
    if not event_path.exists():
        return {"events": [], "source": str(event_path)}
    try:
        lines = read_jsonl_tail(event_path, capped)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot read audit timeline: {exc}") from exc
    events: list[dict] = []
    for raw in lines:
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return {"events": events, "source": str(event_path)}


@app.get("/api/telemetry")
def dashboard_telemetry() -> dict:
    """Lightweight in-process telemetry for operator visibility."""
    rows = []
    for path, count in sorted(_REQ_COUNTS.items()):
        lat = _REQ_LAT_MS.get(path, deque())
        sorted_lat = sorted(lat)
        rows.append(
            {
                "path": path,
                "count": count,
                "errors": _REQ_ERRORS.get(path, 0),
                "avg_ms": round(sum(lat) / len(lat), 2) if lat else None,
                "p95_ms": round(sorted_lat[max(0, int(len(sorted_lat) * 0.95) - 1)], 2) if lat else None,
            },
        )
    return {"uptime_s": round(time.time() - _START_TS, 2), "routes": rows}


# ── Legacy Command Center compatibility ──
@app.get("/api/public/dashboard")
async def public_dashboard() -> JSONResponse:
    """Bridge for old Command Center React app."""
    from fastapi.responses import JSONResponse

    try:
        response = Response()
        payload = bot_fleet_roster(response=response)
        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse({"status": "offline", "bots": [], "error": str(exc)})


def _local_master_status_payload() -> dict[str, object]:
    """Return the local Command Center status without proxying back into this app."""
    paper = _paper_live_transition_payload(refresh=False)
    paper_ready = bool(paper.get("critical_ready"))
    operator_queue = _operator_queue_payload()
    launch_blocked_raw = paper.get("operator_queue_launch_blocked_count")
    if launch_blocked_raw is None:
        launch_blocked_raw = operator_queue.get("launch_blocked_count")
    try:
        launch_blocked = int(launch_blocked_raw or 0)
    except (TypeError, ValueError):
        launch_blocked = 0
    blocked = int(operator_queue.get("summary", {}).get("BLOCKED") or 0)
    runtime_mode = "paper_live" if paper_ready else "paper_sim"
    generated_at = datetime.now(UTC).isoformat()
    cached_live_broker_state = _cached_live_broker_state_for_gateway_reconcile()
    broker_gateway = _reconcile_broker_gateway_with_live_state(
        _broker_gateway_snapshot(),
        cached_live_broker_state,
    )
    gateway_ibkr = broker_gateway.get("ibkr") if isinstance(broker_gateway.get("ibkr"), dict) else {}
    gateway_status = str(gateway_ibkr.get("status") or broker_gateway.get("status") or "unknown").lower()
    gateway_detail = str(gateway_ibkr.get("detail") or broker_gateway.get("detail") or "")
    broker_router = _broker_router_snapshot()
    router_status = str(broker_router.get("status") or "unknown").lower()
    target_exit_summary = _target_exit_summary_for_master_status()
    target_exit_status = str(target_exit_summary.get("status") or "unknown").lower()
    target_exit_card_status = _target_exit_card_status(target_exit_summary)
    broker_bracket_audit = _broker_bracket_audit_payload(
        target_exit_summary=target_exit_summary,
        live_broker_state=cached_live_broker_state,
    )
    broker_bracket_audit_status = str(
        broker_bracket_audit.get("summary") or "AUDIT_UNAVAILABLE",
    )
    broker_bracket_audit_card_status = _broker_bracket_audit_card_status(broker_bracket_audit)
    broker_bracket_position_summary = (
        broker_bracket_audit.get("position_summary")
        if isinstance(broker_bracket_audit.get("position_summary"), dict)
        else {}
    )
    broker_bracket_unprotected_symbols = (
        broker_bracket_position_summary.get("unprotected_symbols")
        if isinstance(broker_bracket_position_summary.get("unprotected_symbols"), list)
        else []
    )
    broker_bracket_primary = (
        broker_bracket_audit.get("primary_unprotected_position")
        if isinstance(broker_bracket_audit.get("primary_unprotected_position"), dict)
        else {}
    )
    broker_bracket_actions = (
        broker_bracket_audit.get("operator_actions")
        if isinstance(broker_bracket_audit.get("operator_actions"), list)
        else []
    )
    broker_bracket_action_labels = [
        str(action.get("label") or "")
        for action in broker_bracket_actions
        if isinstance(action, dict) and action.get("label")
    ]
    broker_bracket_order_actions = [
        action for action in broker_bracket_actions if isinstance(action, dict) and action.get("order_action") is True
    ]
    broker_bracket_primary_action = (
        broker_bracket_actions[0] if broker_bracket_actions and isinstance(broker_bracket_actions[0], dict) else {}
    )
    broker_bracket_order_action = broker_bracket_order_actions[0] if broker_bracket_order_actions else {}
    broker_bracket_prop_dry_run_blocked = bool(broker_bracket_audit.get("operator_action_required")) and not bool(
        broker_bracket_audit.get("ready_for_prop_dry_run")
    )
    vps_root_reconciliation = _vps_root_reconciliation_payload()

    def _gateway_card_status(status: str) -> str:
        if status == "connected":
            return "GREEN"
        if status == "down":
            return "RED"
        return "YELLOW"

    def _router_card_status(status: str) -> str:
        if status in {"ok", "idle", "processing"}:
            return "GREEN"
        if status in {"held", "degraded"}:
            return "YELLOW"
        if status in {"blocked", "failed", "error", "unknown"}:
            return "RED"
        return "YELLOW"

    def _vps_root_card_status(payload: dict[str, object]) -> str:
        status = str(payload.get("status") or "unknown").lower()
        risk = str(payload.get("risk_level") or "unknown").lower()
        if status == "missing":
            return "YELLOW"
        if status in {"stale", "stale_review_required"} or payload.get("artifact_stale") is True:
            return "YELLOW"
        if status == "review_required" or risk in {"high", "medium"}:
            return "YELLOW"
        return "GREEN"

    def _vps_root_card_detail(payload: dict[str, object]) -> str:
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        age = payload.get("plan_age_s") or payload.get("inventory_age_s")
        age_text = "unknown"
        if isinstance(age, (int, float)):
            age_text = f"{int(age // 60)}m"
        return (
            f"risk={payload.get('risk_level')}; cleanup_allowed={payload.get('cleanup_allowed')}; "
            f"artifact_stale={payload.get('artifact_stale')}; snapshot_age={age_text}; "
            f"source_deleted={summary.get('source_or_governance_deleted', 0)}; "
            f"submodule_drift={summary.get('submodule_drift', 0)}; "
            f"dirty_companions={summary.get('dirty_companion_repos', 0)}; "
            f"generated_untracked={summary.get('generated_untracked', 0)}; "
            f"status_rows={counts.get('status', 0)}; "
            "dormant_submodules="
            f"{summary.get('submodule_uninitialized', counts.get('submodule_uninitialized', 0))}"
        )

    paper_live = dict(paper)
    paper_live.update(
        {
            "mode": runtime_mode,
            "status": paper.get("status") or "unknown",
            "critical_ready": paper_ready,
            "paper_ready_bots": int(paper.get("paper_ready_bots") or 0),
            "operator_queue_blocked_count": blocked,
            "operator_queue_launch_blocked_count": launch_blocked,
        }
    )
    paper_held_by_bracket_audit = broker_bracket_prop_dry_run_blocked and paper_ready and launch_blocked == 0
    paper_live_effective_status = (
        "held_by_bracket_audit" if paper_held_by_bracket_audit else str(paper.get("status") or "unknown")
    )
    paper_live_effective_detail = ""
    if paper_held_by_bracket_audit:
        paper_live_effective_detail = (
            f"held by Bracket Audit: {' or '.join(broker_bracket_action_labels)}"
            if broker_bracket_action_labels
            else "held by Bracket Audit"
        )
    paper_live.update(
        {
            "raw_status": str(paper.get("status") or "unknown"),
            "effective_status": paper_live_effective_status,
            "effective_detail": paper_live_effective_detail,
            "held_by_bracket_audit": paper_held_by_bracket_audit,
        }
    )
    paper_card_status = (
        "RED" if launch_blocked else "YELLOW" if paper_held_by_bracket_audit else "GREEN" if paper_ready else "YELLOW"
    )
    router_card_status = _router_card_status(router_status)
    broker_card_status = _worst_card_status(router_card_status, target_exit_card_status)
    broker_detail = router_status
    if target_exit_card_status != "GREEN":
        broker_detail = _append_detail_once(
            broker_detail,
            (
                f"exit_watch={target_exit_status}; "
                f"{int(target_exit_summary.get('missing_bracket_count') or 0)} missing bracket(s)"
            ),
        )
    return {
        "status": "live",
        "mode": "autonomous",
        "uptime": "connected",
        "cc_proxy": "local",
        "generated_at": generated_at,
        "runtime": {
            "mode": runtime_mode,
            "paper_live_ready": paper_ready,
            "operator_queue_blocked_count": blocked,
            "operator_queue_launch_blocked_count": launch_blocked,
        },
        "paper": {
            "mode": runtime_mode,
            "status": paper.get("status") or "unknown",
            "critical_ready": paper_ready,
            "paper_ready_bots": int(paper.get("paper_ready_bots") or 0),
        },
        "paper_live": paper_live,
        "target_exit_summary": target_exit_summary,
        # Compatibility alias for probes and consumers that expect the card name
        # rather than the older summary key.
        "target_exit": target_exit_summary,
        "broker_bracket_audit": broker_bracket_audit,
        "vps_root_reconciliation": vps_root_reconciliation,
        "systems": {
            "dashboard": {
                "status": "GREEN",
                "detail": "dashboard API answering local master status",
                "source": "local",
                "checked_at": generated_at,
            },
            "ibkr": {
                "status": _gateway_card_status(gateway_status),
                "detail": gateway_detail or gateway_status,
                "source": "broker_gateway",
                "raw_status": gateway_status,
                "checked_at": gateway_ibkr.get("checked_at") or broker_gateway.get("checked_at"),
            },
            "broker": {
                "status": broker_card_status,
                "detail": broker_detail,
                "source": "broker_router",
                "raw_status": router_status,
                "target_exit_status": target_exit_status,
                "target_exit_card_status": target_exit_card_status,
                "active_blocker_count": int(broker_router.get("active_blocker_count") or 0),
            },
            "target_exit": {
                "status": target_exit_card_status,
                "detail": str(target_exit_summary.get("summary_line") or target_exit_status),
                "source": target_exit_summary.get("source") or "target_exit_summary",
                "raw_status": target_exit_status,
                "missing_bracket_count": int(target_exit_summary.get("missing_bracket_count") or 0),
                "force_flatten_due_count": int(
                    (
                        target_exit_summary.get("position_staleness")
                        if isinstance(target_exit_summary.get("position_staleness"), dict)
                        else {}
                    ).get("force_flatten_due_count")
                    or 0
                ),
            },
            "broker_bracket_audit": {
                "status": broker_bracket_audit_card_status,
                "detail": str(broker_bracket_audit.get("next_action") or broker_bracket_audit_status),
                "source": "broker_bracket_audit",
                "raw_status": broker_bracket_audit_status,
                "operator_action_required": bool(
                    broker_bracket_audit.get("operator_action_required"),
                ),
                "prop_dry_run_blocked": broker_bracket_prop_dry_run_blocked,
                "ready_for_prop_dry_run": bool(
                    broker_bracket_audit.get("ready_for_prop_dry_run"),
                ),
                "missing_bracket_count": int(broker_bracket_position_summary.get("missing_bracket_count") or 0),
                "unprotected_symbols": broker_bracket_unprotected_symbols,
                "broker_bracket_required_position_count": int(
                    broker_bracket_position_summary.get("broker_bracket_required_position_count") or 0
                ),
                "broker_open_position_count": int(
                    broker_bracket_position_summary.get("broker_open_position_count") or 0
                ),
                "operator_action_count": len(broker_bracket_action_labels),
                "operator_action_labels": broker_bracket_action_labels,
                "order_action_count": len(broker_bracket_order_actions),
                "primary_action_label": str(broker_bracket_primary_action.get("label") or ""),
                "primary_action_detail": str(broker_bracket_primary_action.get("detail") or ""),
                "order_action_label": str(broker_bracket_order_action.get("label") or ""),
                "order_action_detail": str(broker_bracket_order_action.get("detail") or ""),
                "primary_symbol": str(broker_bracket_primary.get("symbol") or ""),
                "primary_venue": str(broker_bracket_primary.get("venue") or ""),
                "primary_sec_type": str(broker_bracket_primary.get("sec_type") or ""),
            },
            "paper_live": {
                "status": paper_card_status,
                "detail": paper_live_effective_status,
                "source": "paper_live_transition",
                "critical_ready": paper_ready,
                "effective_status": paper_live_effective_status,
                "effective_detail": paper_live_effective_detail,
                "held_by_bracket_audit": paper_held_by_bracket_audit,
                "operator_queue_blocked_count": blocked,
                "operator_queue_launch_blocked_count": launch_blocked,
            },
            "vps_root": {
                "status": _vps_root_card_status(vps_root_reconciliation),
                "detail": _vps_root_card_detail(vps_root_reconciliation),
                "source": "vps_root_reconciliation",
                "checked_at": generated_at,
            },
        },
        "daily": {},
    }


@app.get("/api/vps/root-reconciliation", response_model=None)
def vps_root_reconciliation() -> dict[str, object]:
    """Read-only VPS root dirty-tree reconciliation plan for ops review."""
    return _vps_root_reconciliation_payload()


@app.get("/api/vps/root/reconciliation", response_model=None)
def vps_root_reconciliation_alias() -> dict[str, object]:
    """Slash-separated alias for root reconciliation probes and bookmarks."""
    return _vps_root_reconciliation_payload()


@app.get("/api/master/status", response_model=None)
def master_status() -> dict[str, object]:
    """Compatibility status endpoint for public ops and beta-app launch tabs."""
    return _local_master_status_payload()


@app.get("/api/runtime-status", response_model=None)
def runtime_status() -> dict[str, object]:
    """Compatibility runtime detail bridge (paper_live/paper_sim)."""
    data = _local_master_status_payload()
    paper = data.get("paper", {})
    paper_live = data.get("paper_live", {})
    runtime = data.get("runtime", {})
    runtime_payload = dict(runtime) if isinstance(runtime, dict) else {}
    if isinstance(paper_live, dict):
        runtime_payload["paper_live_effective_status"] = paper_live.get("effective_status")
        runtime_payload["paper_live_held_by_bracket_audit"] = bool(paper_live.get("held_by_bracket_audit"))
    return {
        "paper": paper,
        "paper_live": paper_live,
        "runtime": runtime_payload,
        "mode": paper.get("mode", "unknown") if isinstance(paper, dict) else "unknown",
        "effective_status": (paper_live.get("effective_status") if isinstance(paper_live, dict) else None),
        "held_by_bracket_audit": bool(
            paper_live.get("held_by_bracket_audit") if isinstance(paper_live, dict) else False
        ),
    }


@app.get("/api/bridge-status", response_model=None)
def bridge_status() -> dict[str, object]:
    """Compatibility bridge for daily PnL and paper status."""
    data = _local_master_status_payload()
    return {
        "daily": data.get("daily", {}),
        "paper": data.get("paper", {}),
        "paper_live": data.get("paper_live", {}),
    }


def _fm_health_snapshot() -> dict[str, object]:
    """Read the cached Force Multiplier health artifact without probing CLIs."""
    candidates = [
        _state_dir() / "fm_health.json",
        _LEGACY_STATE / "fm_health.json",  # HISTORICAL-PATH-OK read fallback only
    ]
    for path in candidates:
        if path.exists():
            payload = _read_json_file(path)
            if payload:
                return {
                    "path": str(path),
                    "payload": payload,
                }
    return {
        "path": str(candidates[0]),
        "payload": {},
    }


@app.get("/api/fm/status", response_model=None)
def force_multiplier_public_status() -> JSONResponse:
    """Public-ops compatibility route for Wave-19 Force Multiplier status."""
    try:
        from eta_engine.brain.multi_model import force_multiplier_status

        payload = dict(force_multiplier_status(probe=False))
        payload["status"] = "ok"
    except Exception as exc:  # noqa: BLE001 - public ops should fail soft
        payload = {
            "status": "degraded",
            "mode": "force_multiplier",
            "error": str(exc)[:200],
        }
    payload["generated_at"] = datetime.now(tz=UTC).isoformat()
    payload["health_snapshot"] = _fm_health_snapshot()
    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "no-store, max-age=0"},
    )
