"""
Deploy // dashboard_api
=======================
Minimal FastAPI backend for the Apex Predator dashboard.

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
    _DEFAULT_STATE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "apex_predator" / "state"
    _DEFAULT_LOG   = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "apex_predator" / "logs"
else:
    _DEFAULT_STATE = Path.home() / ".local" / "state" / "apex_predator"
    _DEFAULT_LOG   = Path.home() / ".local" / "log" / "apex_predator"

STATE_DIR = Path(os.environ.get("APEX_STATE_DIR", _DEFAULT_STATE))
LOG_DIR   = Path(os.environ.get("APEX_LOG_DIR",   _DEFAULT_LOG))


app = FastAPI(
    title="Apex Predator Dashboard",
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
        "<h1>Apex Predator</h1><p>Status page not bundled. "
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
        from apex_predator.brain.jarvis_v3.training.collaboration import (
            PROTOCOLS,
        )
        from apex_predator.brain.jarvis_v3.training.mcp_awareness import (
            PERSONA_MCPS,
        )
        from apex_predator.brain.jarvis_v3.training.peak_manuals import (
            PEAK_MANUALS,
        )
        from apex_predator.brain.jarvis_v3.training.skills_catalog import (
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
        raise HTTPException(status_code=500,
                            detail=f"cannot read audit log: {exc}") from exc

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
        decisions.append({
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
        })
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
        raise HTTPException(status_code=500,
                            detail=f"cannot read audit log: {exc}") from exc

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
    from apex_predator.brain.avengers import TASK_CADENCE, TASK_OWNERS
    return {
        "tasks": [
            {"name": k.value, "owner": TASK_OWNERS[k], "cadence": TASK_CADENCE[k]}
            for k in TASK_CADENCE
        ],
    }


@app.get("/api/brokers")
def broker_readiness() -> dict:
    """Return the paper-broker readiness snapshot for IBKR + Tastytrade.

    Consumed by the 'Broker Paper' dashboard card to answer:
    "are the four BTC lanes actually able to place orders right now?"
    """
    try:
        from apex_predator.venues.ibkr import ibkr_paper_readiness
        from apex_predator.venues.tastytrade import tastytrade_paper_readiness
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
            name for name, report in {"ibkr": ibkr, "tastytrade": tasty}.items()
            if report.get("ready")
        ),
    }


@app.get("/api/btc/lanes")
def btc_lanes() -> dict:
    """Return the current state of the four BTC broker-paper lanes.

    Reads the fleet manifest (written by btc_broker_fleet) and the
    per-worker lane state files (written by PaperLaneRunner). Answers
    'what is each lane doing right now?' without exposing any secrets.
    """
    fleet_dir = STATE_DIR.parent / "apex_predator" / "docs" / "btc_live" / "broker_fleet"
    # Fallback: respect the package layout if running from the source tree.
    fallbacks = [
        fleet_dir,
        Path.home() / "apex_predator" / "docs" / "btc_live" / "broker_fleet",
        STATE_DIR / "broker_fleet",
    ]
    chosen: Path | None = None
    for candidate in fallbacks:
        if candidate.exists():
            chosen = candidate
            break
    if chosen is None:
        return {
            "fleet_dir": str(fallbacks[0]),
            "manifest": None,
            "lanes": [],
            "note": "fleet dir not found; start the fleet via "
                    "python -m apex_predator.scripts.btc_broker_fleet --start",
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
        workers.append({
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
            "execution_state": (
                heartbeat.get("execution_state") if heartbeat else None
            ),
        })
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
    fleet_dir = STATE_DIR.parent / "apex_predator" / "docs" / "btc_live" / "broker_fleet"
    fallbacks = [
        fleet_dir,
        Path.home() / "apex_predator" / "docs" / "btc_live" / "broker_fleet",
        STATE_DIR / "broker_fleet",
    ]
    chosen: Path | None = None
    for candidate in fallbacks:
        if candidate.exists():
            chosen = candidate
            break
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
            "note": "no trades yet -- either the fleet hasn't run or "
                    "BTC_PAPER_LANE_AUTO_SUBMIT is not set",
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


@app.post("/api/tasks/{task}/fire")
def fire_task(task: str) -> dict:
    """Manually fire a BackgroundTask. Useful for ad-hoc retrospectives."""
    from apex_predator.brain.avengers import BackgroundTask
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
