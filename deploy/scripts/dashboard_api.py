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
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import portalocker
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from eta_engine.deploy.scripts.dashboard_services import ensure_dir_writable, read_jsonl_tail, run_background_task

if TYPE_CHECKING:
    from collections.abc import Callable

_BOT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# State/log dirs: repo-relative so every deployment reads the right directory.
# APEX_STATE_DIR / APEX_LOG_DIR env vars still override (used by tests).
_REPO_ROOT     = Path(__file__).resolve().parents[2]   # .../eta_engine/
_DEFAULT_STATE = _REPO_ROOT / "state"
_DEFAULT_LOG   = _REPO_ROOT / "logs"

STATE_DIR = Path(os.environ.get("APEX_STATE_DIR", str(_DEFAULT_STATE)))
LOG_DIR   = Path(os.environ.get("APEX_LOG_DIR",   str(_DEFAULT_LOG)))
_START_TS = time.time()


def _state_dir() -> Path:
    """Lazy state-dir resolver so tests can monkeypatch APEX_STATE_DIR."""
    return Path(os.environ.get("APEX_STATE_DIR", str(_DEFAULT_STATE)))


def _log_dir() -> Path:
    """Lazy log-dir resolver so tests can monkeypatch APEX_LOG_DIR."""
    return Path(os.environ.get("APEX_LOG_DIR", str(_DEFAULT_LOG)))


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
    state_dir = _state_dir()
    log_dir = _log_dir()
    state_writable = ensure_dir_writable(state_dir)
    return {
        "status": "ok",
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
    """Latest kaizen ticket from state/kaizen/tickets/."""
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
    }


def _sup_bot_to_roster_row(sup: dict, now_ts: float) -> dict:
    """Convert a jarvis_supervisor_bot_accounts() row into /api/bot-fleet roster shape."""
    from datetime import UTC, datetime
    today = sup.get("today") or {}
    updated_at = str(sup.get("updated_at") or "")
    last_trade_age_s: int | None = None
    if updated_at:
        try:
            dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            last_trade_age_s = max(0, int(now_ts - dt.timestamp()))
        except (ValueError, OSError):
            pass
    open_pos = sup.get("open_position") or {}
    last_side: str | None = None
    if isinstance(open_pos, dict) and open_pos.get("side"):
        last_side = str(open_pos["side"])
    elif sup.get("direction"):
        last_side = str(sup["direction"]).upper()
    return {
        "id":                  str(sup.get("id") or ""),
        "name":                str(sup.get("name") or ""),
        "symbol":              str(sup.get("symbol") or ""),
        "tier":                str(sup.get("strategy") or ""),
        "venue":               str(sup.get("broker") or "paper-sim"),
        "status":              str(sup.get("status") or "unknown"),
        "todays_pnl":          float(today.get("pnl") or 0.0),
        "todays_pnl_source":   "supervisor_heartbeat",
        "last_trade_ts":       updated_at or None,
        "last_trade_age_s":    last_trade_age_s,
        "last_trade_side":     last_side,
        "last_trade_r":        None,
        "last_trade_qty":      None,
        "data_ts":             now_ts,
        "data_age_s":          0.0,
        "heartbeat_age_s":     last_trade_age_s,
        "source":              "jarvis_strategy_supervisor",
        "confirmed":           True,
        "mode":                str(sup.get("mode") or ""),
        "last_jarvis_verdict": str(sup.get("last_jarvis_verdict") or ""),
    }


@app.get("/api/bot-fleet")
def bot_fleet_roster(
    response: Response,
    bot: str | None = None,
    since_days: int = 1,
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
        last_trade_dt = _parse_fill_dt(status.get("last_trade_ts"))
        status["last_trade_age_s"] = (
            max(0, int((datetime.now(UTC) - last_trade_dt).total_seconds()))
            if last_trade_dt is not None
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
        rows.append(status)
    # --- Supervisor merge ---------------------------------------------------
    # The JARVIS strategy supervisor writes its 16-bot roster to the heartbeat.
    # Those bots never appear in state/bots/, so we merge them in here.
    # Supervisor rows win on name collision (they carry live session data).
    try:
        from eta_engine.scripts.jarvis_supervisor_bridge import (
            jarvis_supervisor_bot_accounts,
        )
        sup_hb = _state_dir() / "jarvis_intel" / "supervisor" / "heartbeat.json"
        sup_accounts = jarvis_supervisor_bot_accounts(heartbeat_path=sup_hb)
        if sup_accounts:
            sup_ids = {str(s.get("id") or "") for s in sup_accounts}
            rows = [
                r for r in rows
                if str(r.get("name") or r.get("id") or "") not in sup_ids
            ]
            rows.extend(_sup_bot_to_roster_row(s, now_ts) for s in sup_accounts)
    except Exception:
        pass  # Never crash the roster because the supervisor is unavailable

    confirmed_bots = sum(
        1 for r in rows
        if r.get("source") == "jarvis_strategy_supervisor" or r.get("confirmed") is True
    )
    return {
        "bots":              rows,
        "confirmed_bots":    confirmed_bots,
        "server_ts":         now_ts,
        "live":              fills_stats,
        "window_since_days": since_days,
    }


@app.get("/api/bot-fleet/{bot_id}")
def bot_fleet_drilldown(bot_id: str) -> dict:
    """Per-bot drill: status + recent fills + recent verdicts + sage effects."""
    if not _BOT_ID_RE.match(bot_id):
        raise HTTPException(status_code=400, detail={"error_code": "invalid_bot_id"})
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe
    bot_dir = _state_dir() / "bots" / bot_id
    if not bot_dir.exists():
        return {
            "_warning": "no_data",
            "status": {"_warning": "no_data"},
            "recent_fills": [],
            "recent_verdicts": [],
            "sage_effects": {"_warning": "no_data"},
        }
    status = read_json_safe(bot_dir / "status.json")
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
    fills_path = _state_dir() / "blotter" / "fills.jsonl"
    if fills_path.exists():
        try:
            for raw in reversed(fills_path.read_text(encoding="utf-8").splitlines()):
                line = raw.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("bot") or "") != bot_id:
                    continue
                key = (row.get("ts"), row.get("side"), row.get("price"), row.get("qty"))
                if key in dedup_keys:
                    continue
                dedup_keys.add(key)
                merged_fills.append(row)
                if len(merged_fills) >= 80:
                    break
        except OSError:
            pass
    merged_fills.sort(
        key=lambda x: str(x.get("ts") or ""),
        reverse=True,
    )
    return {
        "status": status,
        "recent_fills": merged_fills[:50],
        "recent_verdicts": read_json_safe(bot_dir / "recent_verdicts.json"),
        "sage_effects": read_json_safe(bot_dir / "sage_effects.json"),
    }


@app.get("/api/live/fills")
def live_fills(limit: int = 30, response: Response = None) -> dict:
    """Latest fills for tape bootstrap/fallback rendering."""
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    limit = max(1, min(limit, 100))
    fills_path = _state_dir() / "blotter" / "fills.jsonl"
    if not fills_path.exists():
        return {"fills": [], "server_ts": time.time()}
    rows: list[dict] = []
    try:
        for raw in reversed(fills_path.read_text(encoding="utf-8").splitlines()):
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
    except OSError:
        return {"fills": [], "server_ts": time.time()}
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
        return {
            "bot_id": bot,
            "range": range,
            "series": [],
            "summary": {
                "current_equity": None,
                "today_pnl": None,
                "week_pnl": None,
                "month_pnl": None,
                "total_pnl": None,
            },
            "_warning": "no_data",
            "server_ts": time.time(),
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

    # Preserve legacy keys (today/thirty_day) for backwards-compat with old test
    out = {
        "bot_id": bot,
        "range": range,
        "series": series,
        "summary": summary,
        "baseline_equity": baseline,
        "server_ts": time.time(),
        "data_ts": source_mtime,
        "source": series_source,
        "since_days": since_days,
        "live": _fills_activity_snapshot(bot=bot),
    }
    # Carry through legacy keys so existing consumers (and the
    # `test_equity_returns_curve` test) continue to work.
    for legacy_key in ("today", "thirty_day", "week", "month", "all_time"):
        if legacy_key in data:
            out[legacy_key] = data[legacy_key]
    return out


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


def _fills_activity_snapshot(bot: str | None = None) -> dict:
    """Lightweight live telemetry so UI can distinguish idle vs stale."""
    fills_path = _state_dir() / "blotter" / "fills.jsonl"
    if not fills_path.exists():
        return {"last_fill_ts": None, "fills_1h": 0, "fills_24h": 0}
    now = datetime.now(UTC)
    h1 = now - timedelta(hours=1)
    h24 = now - timedelta(hours=24)
    last_fill_ts: str | None = None
    fills_1h = 0
    fills_24h = 0
    try:
        for raw in fills_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if bot and str(row.get("bot") or "") != bot:
                continue
            ts_raw = row.get("ts")
            ts_dt = _parse_fill_dt(ts_raw)
            if ts_dt is None:
                continue
            ts_txt = str(ts_raw or "")
            if last_fill_ts is None or ts_txt > last_fill_ts:
                last_fill_ts = ts_txt
            if ts_dt >= h24:
                fills_24h += 1
            if ts_dt >= h1:
                fills_1h += 1
    except OSError:
        pass
    return {"last_fill_ts": last_fill_ts, "fills_1h": fills_1h, "fills_24h": fills_24h}


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
        "detail": f"state_dir_exists={_state_dir().exists()}",
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
