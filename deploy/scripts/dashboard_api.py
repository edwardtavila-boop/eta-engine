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
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import portalocker
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from eta_engine.deploy.scripts.dashboard_services import ensure_dir_writable, read_jsonl_tail, run_background_task

if TYPE_CHECKING:
    from collections.abc import Callable

_BOT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
CANONICAL_BOT_FLEET_TITLE = "Evolutionary Trading Algo // Bot Fleet Roster"
DASHBOARD_VERSION = "v1"
DASHBOARD_RELEASE_STAGE = "pre_beta"
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
        "id": "cc-strategy-supercharge-results",
        "title": "Strategy Supercharge Results",
        "source": "endpoint",
        "endpoint": "/api/jarvis/strategy_supercharge_results",
        "required": True,
        "stale_after_s": 60,
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
        "endpoint": "/api/bot-fleet?since_days=1",
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
        "endpoint": "/api/bot-fleet",
        "required": True,
        "stale_after_s": 20,
    },
    {
        "id": "fl-health-badges",
        "title": "Bot Health Badges",
        "source": "endpoint",
        "endpoint": "/api/bot-fleet",
        "required": True,
        "stale_after_s": 20,
    },
)

# State/log dirs: canonical workspace paths per CLAUDE.md hard rule #1
# ("everything writes under C:\EvolutionaryTradingAlgo"). Legacy in-repo
# locations remain as read fallbacks (handled in ``_state_dir`` /
# ``_log_dir``) so the API can still surface state files persisted
# before the migration; new writes always land at the canonical paths.  # HISTORICAL-PATH-OK
_REPO_ROOT     = Path(__file__).resolve().parents[2]   # .../eta_engine/
_WORKSPACE_ROOT = _REPO_ROOT.parent                     # .../EvolutionaryTradingAlgo/
_DEFAULT_STATE = _WORKSPACE_ROOT / "var" / "eta_engine" / "state"
_DEFAULT_LOG   = _WORKSPACE_ROOT / "logs" / "eta_engine"
# Legacy in-repo locations kept ONLY for read fallback during the
# migration window. Never used as a write target. Once a fresh canonical
# session has rolled over, a follow-up PR can delete the fallbacks.
_LEGACY_STATE  = _REPO_ROOT / "state"  # HISTORICAL-PATH-OK
_LEGACY_LOG    = _REPO_ROOT / "logs"
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
LOG_DIR   = Path(os.environ.get("ETA_LOG_DIR", os.environ.get("ETA_LOG_DIR", str(_DEFAULT_LOG))))
_START_TS = time.time()


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
    elif age_s > 180:
        status = "stale"
    elif action == "restart_failed" or restart_ok is False:
        status = "failed"
    elif probe_healthy is False:
        status = "degraded"
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


def _dashboard_diagnostics_payload() -> dict:
    """Single source-of-truth rollup for Command Center self-diagnostics."""
    server_ts = time.time()
    generated_at = datetime.fromtimestamp(server_ts, UTC).isoformat()
    cards = _dashboard_card_health_payload()
    dashboard_proxy_watchdog = _dashboard_proxy_watchdog_payload(server_ts=server_ts)

    try:
        roster = bot_fleet_roster(Response(), since_days=1)
    except Exception as exc:  # noqa: BLE001 -- diagnostics should fail soft.
        roster = {"bots": [], "confirmed_bots": 0, "summary": {}, "_error": str(exc)}

    try:
        equity = equity_curve(range="1d", normalize=True, since_days=1, response=Response())
    except Exception as exc:  # noqa: BLE001 -- diagnostics should fail soft.
        equity = {"series": [], "summary": {}, "source": "error", "_error": str(exc)}

    operator_queue = _operator_queue_payload()
    paper_live_transition = _paper_live_transition_payload(refresh=False)
    readiness = _bot_strategy_readiness_payload()
    roster_bots = roster.get("bots") if isinstance(roster.get("bots"), list) else []
    roster_summary = roster.get("summary") if isinstance(roster.get("summary"), dict) else {}
    equity_series = equity.get("series") if isinstance(equity.get("series"), list) else []
    equity_summary = equity.get("summary") if isinstance(equity.get("summary"), dict) else {}
    card_summary = cards.get("summary") if isinstance(cards.get("summary"), dict) else {}
    operator_summary = (
        operator_queue.get("summary") if isinstance(operator_queue.get("summary"), dict) else {}
    )
    top_operator_blockers = (
        operator_queue.get("top_blockers") if isinstance(operator_queue.get("top_blockers"), list) else []
    )
    top_launch_blockers = (
        operator_queue.get("top_launch_blockers")
        if isinstance(operator_queue.get("top_launch_blockers"), list)
        else []
    )
    first_operator_blocker = (
        top_operator_blockers[0] if top_operator_blockers and isinstance(top_operator_blockers[0], dict) else {}
    )
    first_launch_blocker = (
        top_launch_blockers[0] if top_launch_blockers and isinstance(top_launch_blockers[0], dict) else {}
    )
    first_failed_gate = _first_failed_gate(
        paper_live_transition if isinstance(paper_live_transition, dict) else {}
    )
    readiness_summary = readiness.get("summary") if isinstance(readiness.get("summary"), dict) else {}
    readiness_lanes = readiness_summary.get("launch_lanes") if isinstance(readiness_summary, dict) else {}
    readiness_lane_counts = readiness_lanes if isinstance(readiness_lanes, dict) else {}
    readiness_blocked_data = int(
        readiness_summary.get("blocked_data")
        or readiness_lane_counts.get("blocked_data")
        or 0
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

    return {
        **_dashboard_contract(),
        "source_of_truth": "dashboard_diagnostics",
        "generated_at": generated_at,
        "server_ts": server_ts,
        "api_build": {
            "name": "eta-command-center-v1",
            "dashboard_version": DASHBOARD_VERSION,
            "release_stage": DASHBOARD_RELEASE_STAGE,
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
            "truth_status": str(roster.get("truth_status") or roster_summary.get("truth_status") or "unknown"),
            "truth_summary_line": str(
                roster.get("truth_summary_line")
                or roster_summary.get("truth_summary_line")
                or "",
            ),
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
        "operator_queue": {
            "blocked": int(operator_summary.get("BLOCKED") or 0),
            "observed": int(operator_summary.get("OBSERVED") or 0),
            "unknown": int(operator_summary.get("UNKNOWN") or 0),
            "launch_blocked": int(operator_queue.get("launch_blocked_count") or 0),
            "top_blocker_op_id": str(first_operator_blocker.get("op_id") or ""),
            "top_blocker_title": str(first_operator_blocker.get("title") or ""),
            "top_launch_blocker_op_id": str(first_launch_blocker.get("op_id") or ""),
            "top_launch_blocker_detail": str(
                first_launch_blocker.get("detail")
                or first_launch_blocker.get("title")
                or ""
            ),
            "error": operator_queue.get("error"),
        },
        "paper_live_transition": {
            "status": str(paper_live_transition.get("status") or "unknown"),
            "critical_ready": bool(paper_live_transition.get("critical_ready")),
            "paper_ready_bots": int(paper_live_transition.get("paper_ready_bots") or 0),
            "first_launch_blocker_op_id": transition_first_launch_blocker,
            "first_launch_next_action": transition_first_launch_next_action,
            "first_failed_gate": {
                "name": str(first_failed_gate.get("name") or ""),
                "detail": str(first_failed_gate.get("detail") or ""),
                "next_action": str(first_failed_gate.get("next_action") or ""),
            },
            "source_age_s": paper_live_transition.get("source_age_s"),
            "error": paper_live_transition.get("error"),
        },
        "dashboard_proxy_watchdog": dashboard_proxy_watchdog,
        "checks": {
            "api_contract": True,
            "card_contract": int(card_summary.get("dead") or 0) == 0 and int(card_summary.get("stale") or 0) == 0,
            "bot_fleet_contract": isinstance(roster.get("bots"), list),
            "equity_contract": "series" in equity,
            "bot_strategy_readiness_contract": readiness.get("status") == "ready" and not readiness.get("error"),
            "operator_queue_contract": isinstance(operator_queue, dict) and "summary" in operator_queue,
            "paper_live_transition_contract": isinstance(paper_live_transition, dict)
            and "status" in paper_live_transition,
            "dashboard_proxy_watchdog_contract": dashboard_proxy_watchdog.get("status")
            in {"ok", "missing", "stale", "failed", "degraded", "unknown"},
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
    diagnostics_summary = (
        diagnostics_cards.get("summary") if isinstance(diagnostics_cards.get("summary"), dict) else {}
    )
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
        findings.append(
            f"dead_cards length mismatch: card-health={len(card_dead)} diagnostics={len(diagnostics_dead)}"
        )
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
    direct_truth = str(bot_fleet.get("truth_status") or fleet_summary.get("truth_status") or "")
    diag_total = int(diag_fleet.get("bot_total") or 0)
    diag_confirmed = int(diag_fleet.get("confirmed_bots") or 0)
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
        findings.append(
            f"bot_fleet confirmed mismatch: endpoint={direct_confirmed} diagnostics={diag_confirmed}"
        )
    if direct_truth and diag_truth and direct_truth != diag_truth:
        findings.append(f"bot_fleet truth_status mismatch: endpoint={direct_truth!r} diagnostics={diag_truth!r}")
    if direct_points != diag_points:
        findings.append(f"equity point_count mismatch: endpoint={direct_points} diagnostics={diag_points}")
    if direct_equity_truth and diag_equity_truth and direct_equity_truth != diag_equity_truth:
        findings.append(
            "equity session_truth_status mismatch: "
            f"endpoint={direct_equity_truth!r} diagnostics={diag_equity_truth!r}"
        )
    if direct_equity_source and diag_equity_source and direct_equity_source != diag_equity_source:
        findings.append(
            f"equity source mismatch: endpoint={direct_equity_source!r} diagnostics={diag_equity_source!r}"
        )

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
            strategy_readiness.get("next_action")
            or strategy_readiness.get("next_promotion_step")
            or "",
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
        gateway_config = (
            data.get("gateway_config")
            if isinstance(data.get("gateway_config"), dict)
            else {}
        )
        if not gateway_config:
            continue
        single_source = (
            data.get("single_source")
            if isinstance(data.get("single_source"), dict)
            else {}
        )
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
        account_snapshot = (
            details.get("account_snapshot")
            if isinstance(details.get("account_snapshot"), dict)
            else {}
        )
        account_summary = (
            account_snapshot.get("summary")
            if isinstance(account_snapshot.get("summary"), dict)
            else None
        )
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

    plan_path = _vps_root_reconciliation_plan_path()
    inventory_path = _vps_root_dirty_inventory_path()
    plan = _read_json_file(plan_path)
    inventory = _read_json_file(inventory_path)
    if not plan and not inventory:
        return {
            "status": "missing",
            "source": "missing",
            "plan_path": str(plan_path),
            "inventory_path": str(inventory_path),
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
    dirty_companion_repos = _as_int(
        summary.get("dirty_companion_repos") or counts.get("dirty_companion_repos")
    )
    manual_review_required = (
        risk_level in {"high", "medium"}
        or not cleanup_allowed
        or source_deleted > 0
        or unknown_deleted > 0
        or source_untracked > 0
        or submodule_drift > 0
        or dirty_companion_repos > 0
    )
    recommended_action = "review VPS root reconciliation plan before any root cleanup"
    if steps and isinstance(steps[0], dict):
        recommended_action = str(steps[0].get("action") or recommended_action)

    return {
        "status": "review_required" if manual_review_required else "ready_for_review",
        "source": "vps_root_reconciliation_plan" if plan else "vps_root_dirty_inventory",
        "plan_status": plan.get("status") or "missing",
        "plan_path": str(plan_path),
        "inventory_path": str(inventory_path),
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
        result.get("error_message")
        or result.get("reason")
        or raw.get("error")
        or raw.get("note")
        or raw.get("detail")
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
        heartbeat.get("pending_dir")
        or os.environ.get("ETA_BROKER_ROUTER_PENDING_DIR")
        or str(state_root / "pending")
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

    if hold_active:
        status = "held"
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
        max(0, int((datetime.now(UTC) - heartbeat_dt).total_seconds()))
        if heartbeat_dt is not None
        else None
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
    synchronized_seconds = {
        second: names for second, names in signal_buckets.items() if len(names) > 1
    }
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
        row for row in rows
        if row.get("heartbeat_age_s") is not None and float(row.get("heartbeat_age_s") or 0) <= 300
    ]
    if runtime.get("_warning") and fresh_rows:
        runtime = {
            "mode": "running",
            "detail": "fresh_supervisor_heartbeats",
            "updated_at": str(
                supervisor_liveness.get("main_heartbeat_ts")
                or supervisor_liveness.get("keepalive_ts")
                or ""
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

_API_PUBLIC_PATHS = frozenset({
    "/api/auth/session",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/step-up",
})


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


def _operator_queue_payload() -> dict:
    """Return JARVIS/operator blockers without letting status probes break the dashboard."""
    try:
        from eta_engine.scripts.jarvis_status import build_operator_queue_summary

        payload = build_operator_queue_summary()
    except Exception as exc:  # noqa: BLE001 -- dashboard should render degraded state
        return {
            "source": "jarvis_status",
            "error": str(exc),
            "summary": {},
            "top_blockers": [],
        }
    return payload if isinstance(payload, dict) else {
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
    return payload if isinstance(payload, dict) else {
        "source": "jarvis_status",
        "error": "bot strategy readiness summary returned a non-object payload",
        "status": "unreadable",
        "summary": {},
        "row_count": 0,
        "rows": [],
        "rows_by_bot": {},
        "top_actions": [],
    }


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
    return payload if isinstance(payload, dict) else {
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
    return payload if isinstance(payload, dict) else {
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
    return payload if isinstance(payload, dict) else {
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
    return Path(os.environ.get(
        "ETA_DASHBOARD_USERS_PATH",
        str(STATE_DIR / "auth" / "users.json"),
    ))


def _sessions_path() -> Path:
    """Resolve sessions.json path at call time so env-var monkeypatching works."""
    return Path(os.environ.get(
        "ETA_DASHBOARD_SESSIONS_PATH",
        str(STATE_DIR / "auth" / "sessions.json"),
    ))


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
            log_line = json.dumps({
                "ts": time.time(),
                "level": "WARNING",
                "event": "login_rate_limit_tripped",
                "username": username,
                "client_ip": client_ip,
                "failures_in_window": len(fails),
                "window_seconds": _LOGIN_WINDOW_SECONDS,
            })
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
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
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
    payload["strategy_supercharge_manifest"] = _strategy_supercharge_manifest_payload()
    payload["strategy_supercharge_results"] = _strategy_supercharge_results_payload()
    # Additive: live broker reality (Alpaca + IBKR) so the panel can show
    # broker-side fills/PnL alongside supervisor-journal counts. Wraps in
    # try/except — a degraded broker must never tank the whole payload
    # since /api/dashboard is the front page bootstrap.
    try:
        payload["live_broker_state"] = _live_broker_state_payload()
    except Exception as exc:  # noqa: BLE001
        payload["live_broker_state"] = {
            "ready": False,
            "error": f"live_broker_state_failed: {exc}",
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
        issues.append({
            "school": "sage_health",
            "neutral_rate": 0.0,
            "n_consultations": 0,
            "severity": "warn",
            "detail": f"sage health monitor unavailable: {exc}",
        })

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
        return {"rows": [], "drifted_count": 0, "ts": datetime.now(UTC).isoformat(),
                "error": "registry import failed"}

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

        cls = _INSTRUMENT_CLASS_TO_BROAD.get(
            str((a.extras or {}).get("instrument_class", "")).strip().lower(), ""
        )
        rows.append({
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
        })

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
                "symbol": symbol, "side": side,
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
        all_window = bars[-(hours * approx_bars_per_hour):]
        step = max(1, len(all_window) // target_pts)

        timeline = []
        for end_idx in range(50, len(all_window), step):
            window = all_window[max(0, end_idx - 50): end_idx]
            if len(window) < 30:
                continue
            ctx = MarketContext(bars=window, side=side, symbol=symbol)
            r = consult_sage(ctx, parallel=False, use_cache=False, apply_edge_weights=False)
            ts = window[-1].get("ts") or window[-1].get("timestamp") or ""
            timeline.append({
                "ts": str(ts),
                "composite_bias": r.composite_bias.value,
                "conviction": round(r.conviction, 4),
                "alignment_score": round(r.alignment_score, 4),
            })
        return {
            "symbol": symbol, "side": side, "hours": hours,
            "n_points": len(timeline), "timeline": timeline,
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
        schools.append({
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
        })
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
                {"name": c.name, "interpretation": c.interpretation,
                 "modifier": c.verdict_modifier, "cap_mult": c.cap_mult}
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
        n = (e.get("n_aligned_wins", 0) + e.get("n_aligned_losses", 0))
        avg_r = (e.get("sum_r", 0.0) / n) if n > 0 else 0.0
        rows.append({
            "school": name,
            "n_obs": e.get("n_obs", 0),
            "n_aligned": n,
            "avg_r": round(avg_r, 4),
            "sum_r": e.get("sum_r", 0.0),
        })
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
            title = (
                f"Kaizen loop {started_at}".strip()
                if started_at
                else "Kaizen loop latest"
            )
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


def _target_exit_summary(rows: list[dict], *, broker_open_position_count: int | None = None) -> dict:
    """Summarize open-position target/stop supervision for operator cards."""
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
            state.get("target_exit_visibility")
            if isinstance(state.get("target_exit_visibility"), dict)
            else {}
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
            visibility.get("stop_distance_pct")
            if "stop_distance_pct" in visibility
            else state.get("stop_distance_pct")
        )
        if stop_distance is not None:
            candidate = _candidate(row, stop_distance, stop_distance_pct)
            if nearest_stop is None or abs(stop_distance) < abs(float(nearest_stop["distance_points"])):
                nearest_stop = candidate

    touched_count = target_touched_count + stop_touched_count
    supervisor_local_count = max(0, open_count - broker_bracket_count)
    effective_broker_open_count = (
        int(broker_open_position_count)
        if broker_open_position_count is not None
        else 0
    )
    if open_count == 0:
        status = "flat"
    elif touched_count > 0:
        status = "alert"
    elif missing_bracket_count > 0:
        status = "missing_brackets"
    elif effective_broker_open_count == 0 and supervisor_local_count > 0:
        status = "paper_watching"
    elif watching_count > 0 or supervisor_watch_count > 0 or broker_bracket_count > 0:
        status = "watching"
    else:
        status = "unknown"

    if open_count == 0:
        summary_line = "flat; no open positions need target/stop supervision"
    else:
        nearest_text = (
            f"; nearest target {nearest_target['bot']} "
            f"{float(nearest_target['distance_points']):.2f} pts"
            if nearest_target
            else "; nearest target n/a"
        )
        if broker_open_position_count is not None:
            summary_line = (
                f"{effective_broker_open_count} broker open; "
                f"{supervisor_local_count} supervisor paper-local open; "
                f"{supervisor_watch_count} supervisor watcher(s); "
                f"{broker_bracket_count} broker bracket(s); {missing_bracket_count} missing bracket(s)"
                f"{nearest_text}"
            )
        elif supervisor_local_count > 0:
            summary_line = (
                f"0 broker open; "
                f"{supervisor_local_count} supervisor paper-local open; "
                f"{supervisor_watch_count} supervisor watcher(s); "
                f"{broker_bracket_count} broker bracket(s); {missing_bracket_count} missing bracket(s)"
                f"{nearest_text}"
            )
        else:
            summary_line = (
                f"{open_count} open; {supervisor_watch_count} supervisor watcher(s); "
                f"{broker_bracket_count} broker bracket(s); {missing_bracket_count} missing bracket(s)"
                f"{nearest_text}"
            )

    return {
        "status": status,
        "summary_line": summary_line,
        "open_position_count": open_count,
        "broker_open_position_count": effective_broker_open_count,
        "broker_open_position_count_observed": broker_open_position_count is not None,
        "supervisor_local_position_count": supervisor_local_count,
        "watching_count": watching_count,
        "supervisor_watch_count": supervisor_watch_count,
        "broker_bracket_count": broker_bracket_count,
        "missing_bracket_count": missing_bracket_count,
        "target_touched_count": target_touched_count,
        "stop_touched_count": stop_touched_count,
        "nearest_target": nearest_target,
        "nearest_stop": nearest_stop,
        "nearest_target_bot": nearest_target.get("bot") if nearest_target else None,
        "nearest_target_distance_points": (
            nearest_target.get("distance_points") if nearest_target else None
        ),
        "nearest_target_distance_pct": (
            nearest_target.get("distance_pct") if nearest_target else None
        ),
        "nearest_stop_bot": nearest_stop.get("bot") if nearest_stop else None,
        "nearest_stop_distance_points": (
            nearest_stop.get("distance_points") if nearest_stop else None
        ),
        "nearest_stop_distance_pct": (
            nearest_stop.get("distance_pct") if nearest_stop else None
        ),
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
        open_pos.get("entry_ts")
        or open_pos.get("opened_at")
        or open_pos.get("ts")
        or "",
    )
    if not signal_at and position_opened_at:
        signal_at = position_opened_at
    last_signal_age_s = _age_seconds(signal_at)
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
    last_bar_ts = (
        open_pos.get("last_bar_ts")
        or open_pos.get("mark_ts")
        or sup.get("last_bar_ts")
    )
    target_exit_visibility = _supervisor_exit_visibility(
        side=side,
        entry_price=entry_price,
        mark_price=mark_price,
        bracket_stop=bracket_stop,
        bracket_target=bracket_target,
        last_bar_high=last_bar_high,
        last_bar_low=last_bar_low,
        broker_bracket=broker_bracket,
    ) if open_pos else {"status": "flat", "owner": "none"}
    position_state = {"state": "flat", "open": False}
    if open_pos:
        position_state = {
            "state": "open",
            "open": True,
            "side": side,
            "qty": qty,
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
        "id":                  str(sup.get("id") or ""),
        "name":                str(sup.get("name") or ""),
        "symbol":              str(sup.get("symbol") or ""),
        "tier":                str(sup.get("strategy") or ""),
        "venue":               str(sup.get("broker") or "paper-sim"),
        "status":              str(sup.get("status") or "unknown"),
        "todays_pnl":          float(today.get("pnl") or 0.0),
        "todays_pnl_source":   "supervisor_heartbeat",
        "last_trade_ts":       None,
        "last_trade_age_s":    None,
        "last_trade_side":     None,
        "last_trade_r":        None,
        "last_trade_qty":      None,
        "last_signal_ts":      signal_at or None,
        "last_signal_age_s":   last_signal_age_s,
        "last_signal_side":    last_side if signal_at else None,
        "last_activity_ts":    activity_ts or None,
        "last_activity_age_s": activity_age_s,
        "last_activity_side":  activity_side,
        "last_activity_type":  activity_type,
        "last_bar_ts":         bar_at or None,
        "data_ts":             now_ts,
        "data_age_s":          0.0,
        "heartbeat_ts":        heartbeat_at or None,
        "heartbeat_age_s":     heartbeat_age_s,
        "source":              "jarvis_strategy_supervisor",
        "confirmed":           True,
        "mode":                str(sup.get("mode") or ""),
        "last_jarvis_verdict": str(sup.get("last_jarvis_verdict") or ""),
        "strategy_readiness":  strategy_readiness,
        "open_position":       open_pos,
        "open_positions":      open_positions,
        "position_state":      position_state,
        "bracket_stop":        bracket_stop,
        "bracket_target":      bracket_target,
        "broker_bracket":      broker_bracket,
        "bracket_src":         bracket_src,
        "launch_lane":         str(sup.get("launch_lane") or strategy_readiness.get("launch_lane") or ""),
        "can_paper_trade":     bool(sup.get("can_paper_trade") or strategy_readiness.get("can_paper_trade")),
        "can_live_trade":      bool(sup.get("can_live_trade") or strategy_readiness.get("can_live_trade")),
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
        rows = [
            row for row in rows
            if str(row.get("name") or row.get("id") or "") == bot
        ]
    return rows


@app.get("/api/bot-fleet")
def bot_fleet_roster(
    response: Response,
    bot: str | None = None,
    since_days: int = 1,
    include_disabled: bool = False,
) -> dict:
    """Roster: scan state/bots/<name>/status.json for each bot."""
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
    for bot_dir in (sorted(bots_dir.iterdir()) if bots_dir.exists() else []):
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
        if isinstance(live_last_fill, dict) and (
            local_ts is None or (live_ts is not None and live_ts >= local_ts)
        ):
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
                last_fill.get("hold_seconds")
                or last_fill.get("duration_s")
                or last_fill.get("time_in_trade_s")
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
            max(0, int((datetime.now(UTC) - last_trade_dt).total_seconds()))
            if last_trade_dt is not None
            else None
        )
        status["last_activity_age_s"] = (
            status["last_trade_age_s"]
            if status.get("last_activity_type") == "trade"
            else None
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
        hb_age = (
            max(0, int((datetime.now(UTC) - hb_dt).total_seconds()))
            if hb_dt is not None
            else None
        )
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
        rows = [
            r for r in rows
            if str(r.get("name") or r.get("id") or "") not in sup_ids
        ]
        rows.extend(sup_rows)
    existing_ids = {
        str(value)
        for row in rows
        for value in (row.get("bot_id"), row.get("id"), row.get("name"))
        if value
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
        1 for r in rows
        if r.get("source") == "jarvis_strategy_supervisor" or r.get("confirmed") is True
    )
    mnq_rows = [r for r in rows if str(r.get("symbol") or "").upper().startswith("MNQ")]
    def _is_readiness_only_runtime_inventory(row: dict) -> bool:
        return (
            str(row.get("status") or "").lower() == "readiness_only"
            or str(row.get("mode") or "").lower() == "readiness_snapshot"
        )
    mnq_readiness_only = [
        r for r in mnq_rows
        if _is_readiness_only_runtime_inventory(r)
    ]
    mnq_runtime_rows = [r for r in mnq_rows if not _is_readiness_only_runtime_inventory(r)]
    truth = _truth_snapshot(rows, server_ts=now_ts)
    signal_cadence = _signal_cadence_summary(rows, server_ts=now_ts)
    try:
        live_broker_state = _live_broker_state_payload()
    except Exception as exc:  # noqa: BLE001
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
    broker_open_position_count = _float_value(live_broker_state.get("open_position_count"))
    target_exit_summary = _target_exit_summary(
        rows,
        broker_open_position_count=(
            int(broker_open_position_count)
            if broker_open_position_count is not None
            else None
        ),
    )
    if isinstance(live_broker_state, dict):
        live_broker_state["position_exposure"] = _position_exposure_payload(
            live_broker_state,
            target_exit_summary=target_exit_summary,
        )
    broker_summary = _broker_summary_fields(live_broker_state)
    portfolio_summary = _portfolio_summary_payload(
        rows,
        live_broker_state,
        hidden_disabled_count=hidden_disabled_count,
    )
    broker_gateway = _broker_gateway_snapshot()
    ibkr_gateway = (
        broker_gateway.get("ibkr")
        if isinstance(broker_gateway.get("ibkr"), dict)
        else {}
    )
    return {
        "bots":              rows,
        "confirmed_bots":    confirmed_bots,
        "summary": {
            "bot_total": len(rows),
            "confirmed_bots": confirmed_bots,
            "mnq_total": len(mnq_runtime_rows),
            "mnq_runtime_total": len(mnq_runtime_rows),
            "mnq_inventory_total": len(mnq_rows),
            "mnq_readiness_only": len(mnq_readiness_only),
            "mnq_running": sum(
                1 for r in mnq_runtime_rows
                if str(r.get("status") or "").lower() == "running"
            ),
            "truth_status": truth["truth_status"],
            "truth_summary_line": truth["truth_summary_line"],
            "latest_signal_ts": signal_cadence["latest_signal_ts"],
            "signal_cadence_status": signal_cadence["status"],
            "signal_update_count": signal_cadence["signal_update_count"],
            "unique_signal_seconds": signal_cadence["unique_signal_seconds"],
            "max_same_second": signal_cadence["max_same_second"],
            "target_exit_status": target_exit_summary["status"],
            "open_position_count_visible": target_exit_summary["open_position_count"],
            "supervisor_exit_watch_count": target_exit_summary["supervisor_watch_count"],
            "portfolio_hidden_disabled_count": portfolio_summary["hidden_disabled_count"],
            "ibkr_gateway_status": ibkr_gateway.get("status") or broker_gateway.get("status"),
            "ibkr_gateway_detail": ibkr_gateway.get("detail") or broker_gateway.get("detail"),
            **broker_summary,
        },
        "portfolio_summary": portfolio_summary,
        "latest_signal_ts":  signal_cadence["latest_signal_ts"],
        "signal_cadence":    signal_cadence,
        "target_exit_summary": target_exit_summary,
        "server_ts":         now_ts,
        "live":              fills_stats,
        "live_broker_state": live_broker_state,
        "broker_gateway":    broker_gateway,
        "broker_router":     _broker_router_snapshot(),
        "window_since_days": since_days,
        **truth,
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
                    supervisor_status.get("can_paper_trade")
                    or strategy_readiness.get("can_paper_trade")
                ),
                "can_live_trade": bool(
                    supervisor_status.get("can_live_trade")
                    or strategy_readiness.get("can_live_trade")
                ),
                "readiness_next_action": str(
                    supervisor_status.get("readiness_next_action")
                    or strategy_readiness.get("next_action")
                    or "",
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
            row.get("last_trade_ts")
            or row.get("last_activity_ts")
            or row.get("last_signal_ts")
            or "",
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
    }
    truth = _truth_snapshot(rows, server_ts=now_ts)
    return {
        "bot_id": bot,
        "range": range_label,
        "series": series,
        "curve": series,
        "summary": summary,
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
            not series
            or source_mtime is None
            or (agg_mtime is not None and agg_mtime > source_mtime)
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
            bot_count = (
                sum(1 for p in bots_dir.iterdir() if p.is_dir())
                if bots_dir.exists()
                else 7
            )
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
            bot_count = (
                sum(1 for p in bots_dir.iterdir() if p.is_dir())
                if bots_dir.exists()
                else 7
            )
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
    source_age_s = (
        max(0, int(server_ts - source_mtime))
        if source_mtime is not None
        else None
    )
    source_updated_at = (
        datetime.fromtimestamp(source_mtime, UTC).isoformat()
        if source_mtime is not None
        else None
    )
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
                    totals[bot] += float(row.get("realized_r", 0.0))
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
            eq += float(row.get("realized_r", 0.0))
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


def _recent_trade_closes(limit: int = 25) -> list[dict]:
    """Return the newest Jarvis trade-close ledger rows without heavy reads."""
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
            out.append(row)
        if len(out) >= limit:
            break
    return out


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
    return {
        "venue": venue,
        "symbol": str(symbol),
        "side": raw_side,
        "qty": qty,
        "avg_entry_price": _float_value(
            row.get("avg_entry_price") if venue == "alpaca" else row.get("avg_cost")
        ),
        "current_price": _float_value(
            row.get("current_price") if venue == "alpaca" else row.get("market_price")
        ),
        "market_value": _float_value(row.get("market_value")),
        "unrealized_pnl": _float_value(
            row.get("unrealized_pl") if venue == "alpaca" else row.get("unrealized_pnl")
        ),
        "unrealized_pct": _float_value(row.get("unrealized_plpc")),
        "sec_type": row.get("secType") or row.get("sec_type"),
        "exchange": row.get("exchange"),
    }


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
    return {
        "ts": row.get("ts") or extra.get("close_ts"),
        "close_ts": extra.get("close_ts") or row.get("ts"),
        "bot_id": row.get("bot_id"),
        "symbol": extra.get("symbol") or row.get("symbol"),
        "side": extra.get("side") or row.get("direction"),
        "qty": _float_value(qty_value),
        "fill_price": _float_value(fill_value),
        "realized_pnl": _float_value(pnl_value),
        "realized_r": _float_value(row.get("realized_r")),
        "action_taken": row.get("action_taken"),
        "layers_updated": layers_updated if isinstance(layers_updated, list) else [],
        "layer_errors": layer_errors if isinstance(layer_errors, list) else [],
    }


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
            pnl = (
                (price - entry_price) * close_qty
                if lot["side"] == "buy"
                else (entry_price - price) * close_qty
            )
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
    fills = _float_value(live_broker_state.get("today_actual_fills"))
    open_positions = _float_value(live_broker_state.get("open_position_count"))
    win_rate = _float_value(live_broker_state.get("win_rate_30d"))
    win_rate_today = _float_value(live_broker_state.get("win_rate_today"))
    closed_outcomes_today = _float_value(live_broker_state.get("closed_outcome_count_today"))
    out: dict[str, object] = {
        "pnl_summary_source": "live_broker_state",
    }
    if realized is not None:
        out["broker_today_realized_pnl"] = round(realized, 2)
    if unrealized is not None:
        out["broker_total_unrealized_pnl"] = round(unrealized, 2)
    if realized is not None or unrealized is not None:
        out["broker_net_pnl"] = round((realized or 0.0) + (unrealized or 0.0), 2)
    if fills is not None:
        out["broker_today_actual_fills"] = int(fills)
    if open_positions is not None:
        out["broker_open_position_count"] = int(open_positions)
    if win_rate is not None:
        out["broker_win_rate_30d"] = win_rate
    if win_rate_today is not None:
        out["broker_win_rate_today"] = win_rate_today
        out["broker_win_rate_source"] = str(live_broker_state.get("win_rate_source") or "")
    if closed_outcomes_today is not None:
        out["broker_closed_outcomes_today"] = int(closed_outcomes_today)
    return out


def _portfolio_sleeve_for_symbol(symbol: object) -> str:
    """Group symbols into dashboard-ready portfolio sleeves."""
    raw = str(symbol or "").upper().replace("/", "").replace("-", "").strip()
    root = re.sub(r"(USD|USDT)$", "", raw)
    root = re.sub(r"\d+$", "", root)
    if root in {"BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "DOGE", "DOT", "LINK"}:
        return "crypto"
    if root in {"MBT", "MET"}:
        return "crypto_futures"
    if root in {"MNQ", "NQ", "ES", "MES", "M2K", "RTY", "MYM", "YM"}:
        return "equity_index_futures"
    if root in {"CL", "MCL", "NG", "GC", "MGC"}:
        return "commodities"
    if root in {"6E", "M6E", "ZN", "ZB", "ZF", "ZT"}:
        return "rates_fx"
    return "other"


def _portfolio_summary_payload(
    rows: list[dict],
    live_broker_state: dict,
    *,
    hidden_disabled_count: int = 0,
) -> dict:
    """API-level allocation and PnL truth for premium dashboard graphs."""
    rows = [row for row in rows if isinstance(row, dict)]
    live_broker_state = live_broker_state if isinstance(live_broker_state, dict) else {}
    broker_summary = _broker_summary_fields(live_broker_state)
    exposure = (
        live_broker_state.get("position_exposure")
        if isinstance(live_broker_state.get("position_exposure"), dict)
        else {}
    )

    sleeve_map: dict[str, dict[str, Any]] = {}
    for row in rows:
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
        allocation_sleeves.append({
            **bucket,
            "symbols": sorted(bucket["symbols"]),
            "today_bot_pnl": round(float(bucket["today_bot_pnl"]), 2),
        })
    allocation_sleeves.sort(
        key=lambda row: (-int(row["open_position_count"]), -int(row["bot_count"]), row["sleeve"]),
    )

    contributors: list[dict[str, Any]] = []
    for pos in exposure.get("open_positions") or []:
        if not isinstance(pos, dict):
            continue
        unrealized = _float_value(pos.get("unrealized_pnl"))
        contributors.append({
            "type": "open_position_unrealized",
            "venue": str(pos.get("venue") or ""),
            "symbol": str(pos.get("symbol") or ""),
            "sleeve": _portfolio_sleeve_for_symbol(pos.get("symbol")),
            "side": pos.get("side"),
            "qty": _float_value(pos.get("qty")),
            "market_value": _float_value(pos.get("market_value")),
            "unrealized_pnl": round(unrealized, 2) if unrealized is not None else None,
            "source": "live_broker_state.position_exposure",
        })
    for close in exposure.get("recent_closes") or []:
        if not isinstance(close, dict):
            continue
        realized = _float_value(close.get("realized_pnl"))
        if realized in (None, 0.0):
            continue
        contributors.append({
            "type": "recent_close_realized",
            "venue": "",
            "bot_id": close.get("bot_id"),
            "symbol": str(close.get("symbol") or ""),
            "sleeve": _portfolio_sleeve_for_symbol(close.get("symbol")),
            "realized_pnl": round(realized, 2),
            "source": "live_broker_state.position_exposure",
        })
    contributors.sort(
        key=lambda row: abs(
            float(row.get("unrealized_pnl") or row.get("realized_pnl") or 0.0),
        ),
        reverse=True,
    )

    return {
        "schema_version": 1,
        "source": "live_broker_state" if broker_summary else "bot_rows",
        "allocation_sleeves": allocation_sleeves,
        "pnl_contributors": contributors[:12],
        "hidden_disabled_count": int(hidden_disabled_count),
        "broker_net_pnl": broker_summary.get("broker_net_pnl"),
        "broker_today_realized_pnl": broker_summary.get("broker_today_realized_pnl"),
        "broker_total_unrealized_pnl": broker_summary.get("broker_total_unrealized_pnl"),
        "open_position_count": int(exposure.get("open_position_count") or 0),
        "bot_count": len(rows),
    }


def _position_exposure_payload(
    live_broker_state: dict,
    *,
    recent_closes: list[dict] | None = None,
    target_exit_summary: dict | None = None,
) -> dict:
    """Read-only open-position and close-evidence rollup for the dashboard."""
    live_broker_state = live_broker_state if isinstance(live_broker_state, dict) else {}
    target_exit_summary = target_exit_summary if isinstance(target_exit_summary, dict) else {}
    alpaca = live_broker_state.get("alpaca") if isinstance(live_broker_state.get("alpaca"), dict) else {}
    ibkr = live_broker_state.get("ibkr") if isinstance(live_broker_state.get("ibkr"), dict) else {}

    open_positions: list[dict] = []
    for row in alpaca.get("open_positions") or []:
        normalized = _normalize_live_position(row, venue="alpaca")
        if normalized:
            open_positions.append(normalized)
    for row in ibkr.get("open_positions") or []:
        normalized = _normalize_live_position(row, venue="ibkr")
        if normalized:
            open_positions.append(normalized)

    if recent_closes is None:
        recent_closes = _recent_trade_closes(limit=25)
    normalized_closes: list[dict] = []
    for row in recent_closes:
        normalized = _normalize_trade_close(row)
        if normalized:
            normalized_closes.append(normalized)

    open_position_count = len(open_positions)
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
        "server_ts": time.time(),
        "open_position_count": open_position_count,
        "broker_open_position_count": open_position_count,
        "supervisor_local_position_count": supervisor_local_count,
        "supervisor_watch_count": supervisor_watch_count,
        "symbols_open": sorted({p["symbol"] for p in open_positions if p.get("symbol")}),
        "open_positions": open_positions,
        "recent_closes": normalized_closes,
        "recent_close_count": len(normalized_closes),
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


def _normalize_live_fill_row(row: dict, *, source: str, source_path: str | None = None) -> dict | None:
    if not isinstance(row, dict):
        return None
    status = str(
        _first_present(row, ("status", "order_status", "orderStatus", "event", "result_status"))
        or ""
    ).replace("_", "").replace(" ", "").upper()
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
                    slim_positions.append({
                        "symbol": p.get("symbol"),
                        "qty": _float_value(p.get("qty")),
                        "avg_entry_price": _float_value(p.get("avg_entry_price")),
                        "current_price": _float_value(p.get("current_price")),
                        "market_value": _float_value(p.get("market_value")),
                        "unrealized_pl": upl,
                        "unrealized_plpc": _float_value(p.get("unrealized_plpc")),
                        "side": p.get("side"),
                    })
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
                    trimmed.append({
                        "symbol": o.get("symbol"),
                        "side": o.get("side"),
                        "filled_qty": _float_value(o.get("filled_qty")),
                        "filled_avg_price": _float_value(o.get("filled_avg_price")),
                        "filled_at": o.get("filled_at"),
                        "client_order_id": o.get("client_order_id"),
                    })
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
_IBKR_PROBE_LOCK = threading.Lock()

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
        bucket = by_bot.setdefault(bot_id, {
            "fills_today": 0,
            "buy_qty": 0.0,
            "sell_qty": 0.0,
            "buy_notional": 0.0,
            "sell_notional": 0.0,
            "symbols": set(),
            "_realized_per_pair": [],
            "_buy_stack": [],  # list of (qty, price) for FIFO matching
        })
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
        if (
            bt_wr is not None
            and live_wr is not None
            and bucket["fills_today"] >= _DRIFT_ALARM_MIN_FILLS
        ):
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
    snapshot["drift_alarm_count"] = sum(
        1 for v in per_bot_out.values() if v.get("drift_alarm")
    )
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
        if (
            cached is not None
            and cached_day == today_start_iso
            and (now_ts - cached_ts) < _ALPACA_PER_BOT_CACHE_TTL_S
        ):
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
        "account_summary_realized_pnl": None,
        "account_summary_unrealized_pnl": None,
        "account_summary_tags": {},
        "checked_utc": datetime.now(UTC).isoformat(),
    }
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
    # Pick a transient client_id well outside the supervisor's range.
    client_id = int(os.environ.get("ETA_DASHBOARD_IBKR_CLIENT_ID", str(secrets.randbelow(100) + 800)))
    # 5s wasn't enough under load — IBG is slow to handshake when the
    # supervisor is also pumping market data. Bump to 12s; the dashboard
    # endpoint only takes that hit when the gateway is actually slow.
    connect_timeout = float(os.environ.get("ETA_DASHBOARD_IBKR_TIMEOUT_S", "12"))
    ib = IB()
    try:
        try:
            loop.run_until_complete(
                ib.connectAsync(host, port, clientId=client_id, timeout=connect_timeout)
            )
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
            summary_tags: dict[str, float] = {}
            for row in account_summary:
                tag = getattr(row, "tag", None)
                if tag not in {"FuturesPNL", "RealizedPnL", "UnrealizedPnL"}:
                    continue
                value = _float_value(getattr(row, "value", None))
                if value is None:
                    continue
                account = getattr(row, "account", None)
                if tag not in summary_tags or account == preferred_account:
                    summary_tags[tag] = value
            snapshot["account_summary_tags"] = {
                tag: round(value, 2) for tag, value in summary_tags.items()
            }
            if "FuturesPNL" in summary_tags:
                snapshot["futures_pnl"] = round(summary_tags["FuturesPNL"], 2)
            if "RealizedPnL" in summary_tags:
                snapshot["account_summary_realized_pnl"] = round(summary_tags["RealizedPnL"], 2)
            if "UnrealizedPnL" in summary_tags:
                snapshot["account_summary_unrealized_pnl"] = round(summary_tags["UnrealizedPnL"], 2)
            unreal = 0.0
            slim_positions: list[dict] = []
            for item in portfolio:
                try:
                    upl = float(getattr(item, "unrealizedPNL", 0.0) or 0.0)
                except (TypeError, ValueError):
                    upl = 0.0
                unreal += upl
                contract = getattr(item, "contract", None)
                slim_positions.append({
                    "symbol": getattr(contract, "localSymbol", None) or getattr(contract, "symbol", None),
                    "secType": getattr(contract, "secType", None),
                    "exchange": getattr(contract, "exchange", None),
                    "position": float(getattr(item, "position", 0.0) or 0.0),
                    "avg_cost": float(getattr(item, "averageCost", 0.0) or 0.0),
                    "market_price": float(getattr(item, "marketPrice", 0.0) or 0.0),
                    "market_value": float(getattr(item, "marketValue", 0.0) or 0.0),
                    "unrealized_pnl": upl,
                })
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
                if ib.isConnected():
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
    with _IBKR_PROBE_LOCK:
        _IBKR_PROBE_CACHE["snapshot"] = dict(snapshot)
        _IBKR_PROBE_CACHE["ts"] = time.time()
    return snapshot


def _live_broker_state_payload() -> dict:
    """Aggregate Alpaca + IBKR live state for the dashboard.

    Surfaces ``today_actual_fills``, ``today_realized_pnl``,
    ``total_unrealized_pnl`` derived from the brokers' own books — NOT
    from the supervisor decision journal. The supervisor counts continue
    to be served by ``/api/dashboard`` and ``/api/equity`` unchanged.
    """
    today_start_utc = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_iso = today_start_utc.isoformat().replace("+00:00", "Z")
    alpaca = _alpaca_live_state_snapshot(today_start_iso=today_start_iso)
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
    today_actual_fills = (
        int(alpaca.get("today_filled_orders") or 0)
        + int(ibkr.get("today_executions") or 0)
    )
    today_realized_pnl = round(
        float(alpaca.get("today_realized_pnl") or 0.0)
        + float(ibkr.get("today_realized_pnl") or 0.0),
        2,
    )
    total_unrealized_pnl = round(
        float(alpaca.get("unrealized_pnl") or 0.0)
        + float(ibkr.get("unrealized_pnl") or 0.0),
        2,
    )
    open_position_count = (
        int(alpaca.get("open_position_count") or 0)
        + int(ibkr.get("open_position_count") or 0)
    )
    win_rate_today = _float_value(alpaca.get("today_win_rate"))
    closed_outcome_count_today = int(alpaca.get("today_closed_outcome_count") or 0)
    evaluated_outcome_count_today = int(alpaca.get("today_evaluated_outcome_count") or 0)
    # 30d win-rate from blotter fills (best-effort; uses local ledger
    # because broker REST is too narrow for 30-day history without paging).
    win_rate_30d: float | None = None
    try:
        wins = 0
        losses = 0
        cutoff = datetime.now(UTC) - timedelta(days=30)
        for row in _recent_live_fill_rows():
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
    except Exception:  # noqa: BLE001
        win_rate_30d = None
    payload = {
        "server_ts": time.time(),
        "today_actual_fills": today_actual_fills,
        "today_realized_pnl": today_realized_pnl,
        "total_unrealized_pnl": total_unrealized_pnl,
        "open_position_count": open_position_count,
        "win_rate_30d": win_rate_30d,
        "win_rate_today": win_rate_today,
        "closed_outcome_count_today": closed_outcome_count_today,
        "evaluated_outcome_count_today": evaluated_outcome_count_today,
        "win_rate_source": "alpaca_filled_order_pairs" if win_rate_today is not None else "",
        "alpaca": alpaca,
        "ibkr": ibkr,
        "per_bot_alpaca": per_bot_alpaca,
        "source": "live_broker_rest",
    }
    payload["position_exposure"] = _position_exposure_payload(payload)
    return payload


@app.get("/api/live/broker_state")
def live_broker_state(response: Response) -> dict:
    """Live broker truth (Alpaca + IBKR) for dashboard reality-check panels.

    Added 2026-05-06. Sits alongside ``/api/dashboard`` (supervisor-journal
    truth) and ``/api/equity`` (per-bot heartbeat-derived equity). The
    dashboard payload also embeds this rollup under ``live_broker_state``
    so the front end can render reality-vs-journal side by side without
    a second round trip.
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return _live_broker_state_payload()


@app.get("/api/live/position_exposure")
def live_position_exposure(response: Response) -> dict:
    """Read-only broker exposure plus supervisor paper-watch close evidence."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    try:
        roster = bot_fleet_roster(Response(), since_days=1)
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
    today_start_utc = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
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
        "1", "true", "yes", "on", "y",
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
    with portalocker.Lock(str(lock_path), mode="a", timeout=5,
                          flags=portalocker.LOCK_EX):
        existing: dict = {}
        if flag_path.exists():
            try:
                existing = json.loads(flag_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}
        existing["ETA_FF_V22_SAGE_MODULATION"] = val
        # Atomic write: write to temp file, then rename
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(flag_path.parent), prefix=".feature_flags_", suffix=".tmp",
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
    sig_path.write_text(json.dumps({
        "ts": datetime.now(UTC).isoformat(),
        "action": action,
        "by": by_user,
    }, indent=2), encoding="utf-8")
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
    with portalocker.Lock(str(lock_path), mode="a", timeout=5,
                          flags=portalocker.LOCK_EX):
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
            dir=str(latch_path.parent), prefix=".kill_switch_latch_", suffix=".tmp",
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
    """Return the paper-broker readiness snapshot for IBKR + Tastytrade + Alpaca.

    Consumed by the 'Broker Paper' dashboard card to answer:
    "which venues can actually place orders right now?"

    Alpaca was added 2026-05-05 as the active crypto-paper venue while
    Tastytrade cert sandbox crypto enablement is pending operator action
    (api.support@tastytrade.com).
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
    brokers = {"ibkr": ibkr, "tastytrade": tasty, "alpaca": alpaca}
    return {
        "brokers": brokers,
        "active_brokers": sorted(
            name for name, report in brokers.items() if report.get("ready")
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
    # GREEN requires the two coverage-critical lanes: a futures venue
    # (IBKR) AND a crypto venue (Alpaca, since Tastytrade cert crypto is
    # pending support enablement). A single venue is YELLOW.
    if ibkr_ready and alpaca_ready:
        out["brokers"] = {
            "status": "GREEN",
            "detail": f"ready: {','.join(ready_names)}",
        }
    elif ready_names:
        out["brokers"] = {
            "status": "YELLOW",
            "detail": f"only {','.join(ready_names)} ready",
        }
    else:
        out["brokers"] = {
            "status": "RED",
            "detail": "no brokers ready",
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
    launch_blocked = int(operator_queue.get("launch_blocked_count") or 0)
    blocked = int(operator_queue.get("summary", {}).get("BLOCKED") or 0)
    runtime_mode = "paper_live" if paper_ready else "paper_sim"
    generated_at = datetime.now(UTC).isoformat()
    broker_gateway = _broker_gateway_snapshot()
    gateway_ibkr = broker_gateway.get("ibkr") if isinstance(broker_gateway.get("ibkr"), dict) else {}
    gateway_status = str(gateway_ibkr.get("status") or broker_gateway.get("status") or "unknown").lower()
    gateway_detail = str(gateway_ibkr.get("detail") or broker_gateway.get("detail") or "")
    broker_router = _broker_router_snapshot()
    router_status = str(broker_router.get("status") or "unknown").lower()
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
        if status == "review_required" or risk in {"high", "medium"}:
            return "YELLOW"
        return "GREEN"

    def _vps_root_card_detail(payload: dict[str, object]) -> str:
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        return (
            f"risk={payload.get('risk_level')}; cleanup_allowed={payload.get('cleanup_allowed')}; "
            f"source_deleted={summary.get('source_or_governance_deleted', 0)}; "
            f"submodule_drift={summary.get('submodule_drift', 0)}; "
            f"dirty_companions={summary.get('dirty_companion_repos', 0)}"
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
    paper_card_status = (
        "GREEN"
        if paper_ready and launch_blocked == 0
        else "RED"
        if launch_blocked
        else "YELLOW"
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
                "status": _router_card_status(router_status),
                "detail": router_status,
                "source": "broker_router",
                "raw_status": router_status,
                "active_blocker_count": int(broker_router.get("active_blocker_count") or 0),
            },
            "paper_live": {
                "status": paper_card_status,
                "detail": str(paper.get("status") or "unknown"),
                "source": "paper_live_transition",
                "critical_ready": paper_ready,
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


@app.get("/api/master/status", response_model=None)
def master_status() -> dict[str, object]:
    """Compatibility status endpoint for public ops and beta-app launch tabs."""
    return _local_master_status_payload()

@app.get("/api/runtime-status", response_model=None)
def runtime_status() -> dict[str, object]:
    """Compatibility runtime detail bridge (paper_live/paper_sim)."""
    data = _local_master_status_payload()
    paper = data.get("paper", {})
    runtime = data.get("runtime", {})
    return {
        "paper": paper,
        "runtime": runtime,
        "mode": paper.get("mode", "unknown") if isinstance(paper, dict) else "unknown",
    }

@app.get("/api/bridge-status", response_model=None)
def bridge_status() -> dict[str, object]:
    """Compatibility bridge for daily PnL and paper status."""
    data = _local_master_status_payload()
    return {
        "daily": data.get("daily", {}),
        "paper": data.get("paper", {}),
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

        payload = dict(force_multiplier_status())
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
