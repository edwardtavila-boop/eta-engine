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

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

# State/log dirs: Windows defaults; overridable via env
if os.name == "nt":
    _DEFAULT_STATE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "eta_engine" / "state"
    _DEFAULT_LOG   = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "eta_engine" / "logs"
else:
    _DEFAULT_STATE = Path.home() / ".local" / "state" / "eta_engine"
    _DEFAULT_LOG   = Path.home() / ".local" / "log" / "eta_engine"

STATE_DIR = Path(os.environ.get("APEX_STATE_DIR", _DEFAULT_STATE))
LOG_DIR   = Path(os.environ.get("APEX_LOG_DIR",   _DEFAULT_LOG))


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
# Endpoints
# ---------------------------------------------------------------------------

_STATUS_PAGE = Path(__file__).resolve().parent.parent / "status_page" / "index.html"


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    """Serve the status page at the root URL."""
    if _STATUS_PAGE.exists():
        return HTMLResponse(_STATUS_PAGE.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<h1>Evolutionary Trading Algo</h1><p>Status page not bundled. "
        "See /health or /api/dashboard.</p>",
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
            "# no metrics file yet -- PROMETHEUS_EXPORT task has not run\n"
            "apex_up 0\n",
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


@app.get("/api/kaizen")
def kaizen_summary() -> dict:
    """Kaizen ledger -- retrospectives + tickets."""
    data = _read_json("kaizen_ledger.json")
    return {
        "retrospectives": len(data.get("retrospectives", [])),
        "tickets_total":  len(data.get("tickets", [])),
        "tickets_open":   sum(1 for t in data.get("tickets", [])
                              if t.get("status") == "OPEN"),
        "tickets_shipped": sum(1 for t in data.get("tickets", [])
                               if t.get("status") == "SHIPPED"),
        "latest_tickets": data.get("tickets", [])[-5:],
    }


@app.get("/api/state/{filename}")
def get_state_file(filename: str) -> dict:
    """Fetch a raw JSON state file. Filename is safelisted."""
    safe = {
        "avengers_heartbeat.json", "dashboard_payload.json", "last_task.json",
        "kaizen_ledger.json", "shadow_ledger.json", "usage_tracker.json",
        "distiller.json", "precedent_graph.json", "strategy_candidates.json",
        "twin_verdict.json", "causal_review.json", "drift_summary.json",
        "cache_warmup.json", "audit_daily_summary.json",
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
        raise HTTPException(status_code=503,
                            detail=f"training module missing: {exc}") from exc

    return {
        "personas": [
            {
                "name": name,
                "manual": manual.model_dump(),
                "skills": [s.model_dump() for s in PERSONA_SKILLS.get(name, [])],
                "mcps":   [p.model_dump() for p in PERSONA_MCPS.get(name, [])],
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
        raise HTTPException(status_code=404,
                            detail=f"no eval report for {persona}")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/tasks")
def list_tasks() -> dict:
    """Return the 12 BackgroundTask names + owners + cadences."""
    from eta_engine.brain.avengers import TASK_CADENCE, TASK_OWNERS
    return {
        "tasks": [
            {"name": k.value, "owner": TASK_OWNERS[k], "cadence": TASK_CADENCE[k]}
            for k in TASK_CADENCE
        ],
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
        [sys.executable, "-m", "deploy.scripts.run_task", task.upper(),
         "--state-dir", str(STATE_DIR), "--log-dir", str(LOG_DIR)],
        capture_output=True, text=True, timeout=120,
    )
    return {
        "task": task.upper(),
        "returncode": result.returncode,
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-1000:],
    }
