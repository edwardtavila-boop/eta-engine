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

import json
import os
import subprocess
import sys
from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

# State/log dirs: Windows defaults; overridable via env
if os.name == "nt":
    _DEFAULT_STATE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "eta_engine" / "state"
    _DEFAULT_LOG = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "eta_engine" / "logs"
else:
    _DEFAULT_STATE = Path.home() / ".local" / "state" / "eta_engine"
    _DEFAULT_LOG = Path.home() / ".local" / "log" / "eta_engine"

STATE_DIR = Path(os.environ.get("APEX_STATE_DIR", _DEFAULT_STATE))
LOG_DIR = Path(os.environ.get("APEX_LOG_DIR", _DEFAULT_LOG))


app = FastAPI(
    title="Evolutionary Trading Algo Dashboard",
    description="Read-only state surface for the JARVIS + Avengers stack",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _read_json(name: str) -> dict:
    path = STATE_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{name} not found in {STATE_DIR}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"parse error: {e}") from e


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
def auth_login(req: LoginRequest, response: Response) -> dict:
    from eta_engine.deploy.scripts.dashboard_auth import (
        create_session,
        verify_password,
    )
    if not verify_password(_users_path(), req.username, req.password):
        raise HTTPException(status_code=401, detail={"error_code": "bad_credentials"})
    token = create_session(_sessions_path(), user=req.username)
    secure = os.environ.get("ETA_DASHBOARD_COOKIE_SECURE", "false").strip().lower() in ("1", "true", "yes", "on", "y")
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        samesite="strict",
        secure=secure,
        max_age=24 * 3600,
    )
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
        httponly=True,
        samesite="strict",
        secure=secure,
    )
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
    if not pin or req.pin != pin:
        raise HTTPException(status_code=403, detail={"error_code": "bad_pin"})
    mark_step_up(_sessions_path(), session)
    return {"stepped_up": True}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

_STATUS_PAGE = Path(__file__).resolve().parent.parent / "status_page" / "index.html"


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    """Serve the status page at the root URL."""
    if _STATUS_PAGE.exists():
        return HTMLResponse(_STATUS_PAGE.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<h1>Evolutionary Trading Algo</h1><p>Status page not bundled. See /health or /api/dashboard.</p>",
    )


@app.get("/status", response_class=HTMLResponse)
def status_page() -> HTMLResponse:
    """Alias for /."""
    return root()


@app.get("/favicon.ico", response_model=None)
def favicon() -> FileResponse | HTMLResponse:
    fav = _STATUS_PAGE.parent / "favicon.ico"
    if fav.exists():
        return FileResponse(str(fav))
    return HTMLResponse(status_code=204)


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics() -> PlainTextResponse:
    """Prometheus OpenMetrics endpoint. Reads the textfile written by
    PROMETHEUS_EXPORT task. Scrape with Prometheus / Grafana / UptimeKuma."""
    prom_file = STATE_DIR / "prometheus" / "avengers.prom"
    if not prom_file.exists():
        return PlainTextResponse(
            "# no metrics file yet -- PROMETHEUS_EXPORT task has not run\napex_up 0\n",
            media_type="text/plain; version=0.0.4",
        )
    return PlainTextResponse(
        prom_file.read_text(encoding="utf-8"),
        media_type="text/plain; version=0.0.4",
    )


@app.get("/health")
def health() -> dict:
    """Liveness probe."""
    return {
        "status": "ok",
        "state_dir": str(STATE_DIR),
        "log_dir": str(LOG_DIR),
        "state_dir_exists": STATE_DIR.exists(),
    }


@app.get("/api/heartbeat")
def heartbeat() -> dict:
    """Latest Avengers daemon heartbeat."""
    return _read_json("avengers_heartbeat.json")


@app.get("/api/dashboard")
def dashboard_payload() -> dict:
    """Dashboard payload assembled by ROBIN every minute."""
    return _read_json("dashboard_payload.json")


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
    """Liveness probe for the JARVIS-routes layer."""
    return {"status": "ok", "router": "jarvis"}


# ─── Sage explain (Wave-6 #3, 2026-04-27) ──────────────────────────
@app.get("/api/jarvis/sage_explain")
def sage_explain_endpoint(symbol: str = "MNQ", side: str = "long") -> dict:
    """1-paragraph LLM (or template-fallback) narrative of the current
    sage report for ``symbol``.

    Bars are fetched from state/raw_state/<symbol>_bars.json when present;
    otherwise returns a stub indicating no recent bars available.
    """
    try:
        from pathlib import Path
        bars_file = Path(STATE_DIR) / "raw_state" / f"{symbol}_bars.json" if "STATE_DIR" in globals() else None
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
@app.get("/api/jarvis/sage_disagreement_heatmap")
def sage_disagreement_heatmap_endpoint(symbol: str = "MNQ") -> dict:
    """Per-school disagreement counts vs the current composite bias
    over the last cached sage consultation. Used to render the
    disagreement heatmap on the operator + investor dashboards.
    """
    try:
        from pathlib import Path
        bars_file = Path(STATE_DIR) / "raw_state" / f"{symbol}_bars.json" if "STATE_DIR" in globals() else None
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
        # Per-school: bias vs composite
        per_school_disagree = {
            name: {
                "bias": v.bias.value,
                "aligned_with_composite": v.bias == report.composite_bias,
                "conviction": round(v.conviction, 4),
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
        from pathlib import Path

        from eta_engine.brain.jarvis_v3 import policies  # noqa: F401  (auto-register)
        from eta_engine.brain.jarvis_v3.candidate_policy import list_candidates
        from eta_engine.scripts.score_policy_candidate import (
            candidate_metrics,
            champion_metrics,
            load_audit_records,
        )

        audit_dir = (
            Path(STATE_DIR) / "jarvis_audit"
            if "STATE_DIR" in globals()
            else Path.home() / "AppData/Local/eta_engine/state/jarvis_audit"
        )
        if not audit_dir.exists():
            audit_dir = Path(__file__).resolve().parents[2] / "state" / "jarvis_audit"
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
    audit_path = STATE_DIR / "jarvis_audit.jsonl"
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
    audit_path = STATE_DIR / "jarvis_audit.jsonl"
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
    """Return the paper-broker readiness snapshot for IBKR + Tastytrade.

    Consumed by the 'Broker Paper' dashboard card to answer:
    "are the four BTC lanes actually able to place orders right now?"
    """
    try:
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
    return {
        "brokers": {
            "ibkr": ibkr,
            "tastytrade": tasty,
        },
        "active_brokers": sorted(
            name for name, report in {"ibkr": ibkr, "tastytrade": tasty}.items() if report.get("ready")
        ),
    }


def _resolve_fleet_dir() -> Path | None:
    """Find the BTC broker-paper fleet directory across possible deploy layouts.

    Resolution order (first match wins):

    1. ``APEX_BTC_FLEET_DIR`` env override -- operator-scoped hard pin.
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
    env_pin = os.environ.get("APEX_BTC_FLEET_DIR")
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
    ``APEX_MNQ_SUPERVISOR_DIR`` as an operator pin + test isolation.
    """
    env_pin = os.environ.get("APEX_MNQ_SUPERVISOR_DIR")
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
        "detail": f"state_dir_exists={STATE_DIR.exists()}",
    }

    # Brokers: try readiness checks, tolerate import errors
    ibkr_ready = False
    tasty_ready = False
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
    if ibkr_ready and tasty_ready:
        out["brokers"] = {
            "status": "GREEN",
            "detail": "ibkr+tastytrade ready",
        }
    elif ibkr_ready or tasty_ready:
        ready_one = "ibkr" if ibkr_ready else "tastytrade"
        out["brokers"] = {
            "status": "YELLOW",
            "detail": f"only {ready_one} ready",
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
    audit_path = STATE_DIR / "jarvis_audit.jsonl"
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
def fire_task(task: str) -> dict:
    """Manually fire a BackgroundTask. Useful for ad-hoc retrospectives."""
    from eta_engine.brain.avengers import BackgroundTask

    try:
        BackgroundTask(task.upper())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"unknown task: {task}") from exc
    # Fire async via subprocess so we don't block the response
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "deploy.scripts.run_task",
            task.upper(),
            "--state-dir",
            str(STATE_DIR),
            "--log-dir",
            str(LOG_DIR),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return {
        "task": task.upper(),
        "returncode": result.returncode,
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-1000:],
    }
