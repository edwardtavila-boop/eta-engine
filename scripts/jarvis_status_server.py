"""
Hermes + JARVIS direct contact-point server.

Runs alongside Hermes Agent on the VPS as ``ETA-Jarvis-Status-Server``.
Listens on 127.0.0.1:8643 (next to Hermes on :8642). Provides a public
operator-facing surface that shows:

  * Hermes is alive AND its address (the API + how to authenticate)
  * JARVIS is alive AND the 33 MCP tools available
  * Live snapshot (zeus) summary in HTML for browser viewing
  * JSON endpoints for automation

Operator hits ``http://127.0.0.1:8643`` in any browser (via the existing
SSH tunnel - the tunnel watcher forwards both 8642 + 8643) and sees the
"are they running" page.

Why a sidecar instead of adding routes to Hermes:

  * Hermes-agent is an upstream package we don't want to fork.
  * A sidecar is independently restartable - if Hermes hangs, the
    operator still has a status page that says "Hermes is hung."
  * Sidecar can read the same shared state files (audit log, memory db,
    sentiment cache, etc.) without going through MCP - fast page loads.

Endpoints:
  GET /         - HTML operator status page (cached 5s)
  GET /health   - simple JSON {"status": "ok"}
  GET /status   - full JSON status (zeus snapshot summary + addresses)
  GET /contact  - JSON contact card (addresses, tools, auth instructions)
  GET /tools    - JSON list of all 33 MCP tools by category

Stdlib only (no aiohttp dependency - uses http.server). Designed to run
under the same venv that already has eta_engine on PYTHONPATH.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

logger = logging.getLogger("eta_engine.scripts.jarvis_status_server")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8643
HERMES_API_BASE = "http://127.0.0.1:8642"

# Snapshot cache so a chatty browser polling every second doesn't
# rebuild the zeus snapshot every hit.
_SNAPSHOT_CACHE: dict[str, Any] = {"asof": 0.0, "data": None}
_SNAPSHOT_TTL_S = 5
_SNAPSHOT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Snapshot composition (best-effort; never raises)
# ---------------------------------------------------------------------------


def _try_zeus_snapshot() -> dict[str, Any] | None:
    try:
        from eta_engine.brain.jarvis_v3 import zeus
        return zeus.snapshot().to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.warning("status_server: zeus snapshot unavailable: %s", exc)
        return None


def _try_tool_list() -> list[str]:
    try:
        from eta_engine.mcp_servers import jarvis_mcp_server
        return [t["name"] for t in jarvis_mcp_server.list_tools()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("status_server: tool list unavailable: %s", exc)
        return []


def _cached_snapshot() -> dict[str, Any]:
    now = time.monotonic()
    with _SNAPSHOT_LOCK:
        if (_SNAPSHOT_CACHE["data"] is not None
            and (now - _SNAPSHOT_CACHE["asof"]) < _SNAPSHOT_TTL_S):
            return _SNAPSHOT_CACHE["data"]
        data = _try_zeus_snapshot()
        _SNAPSHOT_CACHE["asof"] = now
        _SNAPSHOT_CACHE["data"] = data or {}
        return _SNAPSHOT_CACHE["data"]


def _contact_card() -> dict[str, Any]:
    return {
        "platform": "Hermes-JARVIS Brain-OS",
        "version": "1.0.0",
        "asof": datetime.now(UTC).isoformat(),
        "addresses": {
            "hermes_api": HERMES_API_BASE,
            "status_server": f"http://{DEFAULT_HOST}:{DEFAULT_PORT}",
            "tunnel_required": True,
            "tunnel_command": (
                "ssh -L 8642:127.0.0.1:8642 -L 8643:127.0.0.1:8643 forex-vps"
            ),
        },
        "authentication": {
            "method": "Bearer token",
            "env_var": "API_SERVER_KEY",
            "where_set": (
                "VPS hermes_secrets.bat - sourced by hermes_run.bat at "
                "ETA-Hermes-Agent task start"
            ),
        },
        "how_to_chat": {
            "endpoint": f"{HERMES_API_BASE}/v1/chat/completions",
            "method": "POST",
            "headers": {
                "Authorization": "Bearer <API_SERVER_KEY>",
                "Content-Type": "application/json",
            },
            "body_example": {
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "status"}],
                "max_tokens": 256,
                "stream": False,
            },
        },
        "available_tools_count": len(_try_tool_list()),
        "available_skills": [
            "jarvis-trading", "jarvis-zeus", "jarvis-daily-review",
            "jarvis-drawdown-response", "jarvis-anomaly-investigator",
            "jarvis-pre-event-prep", "jarvis-trade-narrator",
            "jarvis-adversarial-inspector", "jarvis-council",
            "jarvis-sentiment-overlay", "jarvis-topology", "jarvis-bus",
        ],
        "operator_quickstart": [
            "1. ssh -L 8642:127.0.0.1:8642 -L 8643:127.0.0.1:8643 forex-vps",
            "2. open http://127.0.0.1:8643 in browser to confirm live",
            "3. open Hermes-desktop, type 'zeus' for unified snapshot",
        ],
        "see_also": {
            "operator_runbook": "eta_engine/docs/LIVE_CUTOVER_OPERATOR_RUNBOOK.md",
            "complete_reference": "eta_engine/docs/HERMES_BRAIN_OS_COMPLETE.md",
            "future_tracks_menu": "eta_engine/docs/HERMES_BRAIN_FUTURE_TRACKS.md",
        },
    }


def _categorize_tools(tools: list[str]) -> dict[str, list[str]]:
    cats: dict[str, list[str]] = {
        "read": [], "write": [], "destructive": [],
        "analytics": [], "coordination": [], "telemetry": [], "unified": [],
    }
    destructive = {
        "jarvis_deploy_strategy", "jarvis_retire_strategy",
        "jarvis_kill_switch",
    }
    write_back = {
        "jarvis_set_size_modifier", "jarvis_pin_school_weight",
        "jarvis_clear_override", "jarvis_apply_regime_pack",
    }
    analytics = {
        "jarvis_explain_consult_causal", "jarvis_replay_consult",
        "jarvis_counterfactual", "jarvis_attribution_cube",
        "jarvis_current_regime", "jarvis_list_regime_packs",
        "jarvis_kelly_recommend", "jarvis_topology",
    }
    coordination = {
        "jarvis_register_agent", "jarvis_list_agents",
        "jarvis_acquire_lock", "jarvis_release_lock",
    }
    telemetry = {
        "jarvis_cost_summary", "jarvis_cost_today", "jarvis_cost_anomaly",
    }
    unified = {"jarvis_zeus"}
    for t in tools:
        if t in destructive:
            cats["destructive"].append(t)
        elif t in write_back:
            cats["write"].append(t)
        elif t in analytics:
            cats["analytics"].append(t)
        elif t in coordination:
            cats["coordination"].append(t)
        elif t in telemetry:
            cats["telemetry"].append(t)
        elif t in unified:
            cats["unified"].append(t)
        else:
            cats["read"].append(t)
    return cats


# ---------------------------------------------------------------------------
# HTML render
# ---------------------------------------------------------------------------


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes + JARVIS - Brain-OS Status</title>
<style>
:root {{
  --bg: #0a0e1a;
  --panel: #131826;
  --border: #2a3142;
  --green: #22c55e;
  --cyan: #06b6d4;
  --yellow: #eab308;
  --red: #ef4444;
  --text: #e4e7ed;
  --muted: #94a3b8;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; font-family: -apple-system, BlinkMacSystemFont, "SF Mono",
    Consolas, monospace;
  background: var(--bg); color: var(--text); padding: 24px; line-height: 1.5;
}}
h1 {{ margin: 0 0 4px; font-size: 28px; letter-spacing: -0.5px; }}
h2 {{ margin: 32px 0 12px; font-size: 16px; color: var(--cyan);
     text-transform: uppercase; letter-spacing: 1px; }}
.subtitle {{ color: var(--muted); margin-bottom: 32px; font-size: 14px; }}
.status-dot {{
  display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  margin-right: 8px; vertical-align: middle;
}}
.dot-green {{ background: var(--green); box-shadow: 0 0 8px var(--green); }}
.dot-yellow {{ background: var(--yellow); box-shadow: 0 0 8px var(--yellow); }}
.dot-red {{ background: var(--red); box-shadow: 0 0 8px var(--red); }}
.grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }}
.panel {{
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px;
}}
.panel-label {{ color: var(--muted); font-size: 11px; text-transform: uppercase;
                letter-spacing: 1px; margin-bottom: 8px; }}
.panel-value {{ font-size: 24px; font-weight: 600; }}
.panel-sub {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
code, pre {{ background: #0a0e1a; padding: 2px 6px; border-radius: 3px;
             font-family: "SF Mono", Consolas, monospace; font-size: 13px; }}
pre {{ padding: 12px; overflow-x: auto; border: 1px solid var(--border); }}
.tag {{ display: inline-block; padding: 2px 8px; background: #1e293b;
        border-radius: 4px; font-size: 12px; margin: 2px; }}
.footer {{ margin-top: 48px; color: var(--muted); font-size: 12px;
           padding-top: 16px; border-top: 1px solid var(--border); }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border); }}
th {{ color: var(--muted); font-weight: 500; font-size: 11px;
      text-transform: uppercase; letter-spacing: 1px; }}
.refresh-note {{ position: fixed; top: 16px; right: 16px; color: var(--muted);
                  font-size: 11px; }}
</style>
</head>
<body>

<div class="refresh-note">auto-refresh 30s - cache {cache_age_s}s</div>

<h1>
  <span class="status-dot {hermes_dot}"></span>Hermes
  <span style="color:var(--muted)">+</span>
  <span class="status-dot {jarvis_dot}"></span>JARVIS
</h1>
<div class="subtitle">Brain-OS for the trading framework - running on VPS - {asof}</div>

<h2>Live snapshot</h2>
<div class="grid">
  <div class="panel">
    <div class="panel-label">Fleet</div>
    <div class="panel-value">{n_bots} bots</div>
    <div class="panel-sub">{tier_summary}</div>
  </div>
  <div class="panel">
    <div class="panel-label">Regime</div>
    <div class="panel-value">{regime}</div>
    <div class="panel-sub">confidence {confidence}</div>
  </div>
  <div class="panel">
    <div class="panel-label">Active overrides</div>
    <div class="panel-value">{n_overrides_total}</div>
    <div class="panel-sub">{n_size_pins} size - {n_school_pins} school</div>
  </div>
  <div class="panel">
    <div class="panel-label">MCP tools</div>
    <div class="panel-value">{n_tools}</div>
    <div class="panel-sub">read - write - analytics - zeus</div>
  </div>
</div>

<h2>Contact</h2>
<div class="panel">
  <table>
    <tr><th style="width:160px">Hermes API</th><td><code>{hermes_api}</code></td></tr>
    <tr><th>Status server</th><td><code>http://{status_host}:{status_port}</code> (you are here)</td></tr>
    <tr><th>Auth header</th><td><code>Authorization: Bearer &lt;API_SERVER_KEY&gt;</code></td></tr>
    <tr><th>Tunnel</th><td><pre>ssh -L 8642:127.0.0.1:8642 -L 8643:127.0.0.1:8643 forex-vps</pre></td></tr>
  </table>
</div>

<h2>Skills loaded</h2>
<div class="panel">
{skills_tags}
</div>

<h2>JSON endpoints</h2>
<div class="panel">
  <table>
    <tr><th>/health</th><td>simple alive check</td></tr>
    <tr><th>/contact</th><td>full contact card + auth instructions</td></tr>
    <tr><th>/status</th><td>full zeus snapshot JSON</td></tr>
    <tr><th>/tools</th><td>33 MCP tools by category</td></tr>
  </table>
</div>

<div class="footer">
  served by jarvis_status_server - sidecar on Hermes VPS - stdlib http.server<br>
  page is read-only; all writes go through Hermes-desktop or MCP tools<br>
  see <code>HERMES_BRAIN_OS_COMPLETE.md</code> for the full reference
</div>

<script>setTimeout(function() {{ location.reload(); }}, 30000);</script>
</body>
</html>"""


def _render_html(snap: dict[str, Any], tools: list[str], cache_age_s: float) -> str:
    fleet = snap.get("fleet_status") or {}
    regime = snap.get("regime") or {}
    overrides = snap.get("overrides") or {}
    sizes = overrides.get("size_modifiers") or {}
    schools = overrides.get("school_weights") or {}
    n_size = len(sizes) if isinstance(sizes, dict) else 0
    n_school = sum(
        len(v) for v in schools.values() if isinstance(v, dict)
    ) if isinstance(schools, dict) else 0
    tier_counts = fleet.get("tier_counts") or {}
    tier_summary = " - ".join(f"{k}:{v}" for k, v in tier_counts.items()) or "no fleet data"
    asof = snap.get("asof") or datetime.now(UTC).isoformat()

    hermes_dot = "dot-green" if snap else "dot-yellow"
    jarvis_dot = "dot-green" if tools else "dot-yellow"

    skills_list = [
        "jarvis-trading", "jarvis-zeus", "jarvis-daily-review",
        "jarvis-drawdown-response", "jarvis-anomaly-investigator",
        "jarvis-pre-event-prep", "jarvis-trade-narrator",
        "jarvis-adversarial-inspector", "jarvis-council",
        "jarvis-sentiment-overlay", "jarvis-topology", "jarvis-bus",
    ]
    skills_tags = " ".join(f'<span class="tag">{s}</span>' for s in skills_list)

    return _HTML_TEMPLATE.format(
        asof=asof,
        hermes_dot=hermes_dot,
        jarvis_dot=jarvis_dot,
        n_bots=fleet.get("n_bots", 0),
        tier_summary=tier_summary,
        regime=regime.get("regime", "UNKNOWN"),
        confidence=regime.get("confidence", 0.0),
        n_overrides_total=n_size + n_school,
        n_size_pins=n_size,
        n_school_pins=n_school,
        n_tools=len(tools),
        hermes_api=HERMES_API_BASE,
        status_host=DEFAULT_HOST,
        status_port=DEFAULT_PORT,
        skills_tags=skills_tags,
        cache_age_s=int(cache_age_s),
    )


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Silence default per-request stdout chatter; use the logger.
        logger.debug("%s - %s", self.address_string(), format % args)

    def _send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, default=str, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            if path == "/" or path == "/index.html":
                snap = _cached_snapshot()
                tools = _try_tool_list()
                age = time.monotonic() - _SNAPSHOT_CACHE["asof"]
                self._send_html(_render_html(snap, tools, age))
                return
            if path == "/health":
                self._send_json({
                    "status": "ok",
                    "service": "jarvis_status_server",
                    "hermes_api": HERMES_API_BASE,
                    "asof": datetime.now(UTC).isoformat(),
                })
                return
            if path == "/contact":
                self._send_json(_contact_card())
                return
            if path == "/status":
                self._send_json({
                    "asof": datetime.now(UTC).isoformat(),
                    "zeus": _cached_snapshot(),
                    "tool_count": len(_try_tool_list()),
                })
                return
            if path == "/tools":
                tools = _try_tool_list()
                self._send_json({
                    "asof": datetime.now(UTC).isoformat(),
                    "total": len(tools),
                    "by_category": _categorize_tools(tools),
                    "all": tools,
                })
                return
            self._send_json({"error": "not_found", "path": path}, status=404)
        except Exception as exc:  # noqa: BLE001
            logger.exception("status_server handler failed")
            with contextlib.suppress(OSError):
                self._send_json({"error": "internal", "detail": str(exc)[:200]}, status=500)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Block forever serving the status endpoints."""
    server = HTTPServer((host, port), _Handler)
    logger.info("jarvis_status_server listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


def _setup_logging() -> None:
    """Log to stderr only.

    The runner bat (jarvis_status_run.bat) redirects stderr to ``.err``
    and stdout to ``.log`` files. Adding a FileHandler here would race
    the bat's redirect for write access and trigger PermissionError on
    Windows. stderr is the canonical channel for service log output.
    """
    root = logging.getLogger()
    if root.handlers:
        # Re-runs: don't pile up duplicate handlers.
        return
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
    ))
    root.addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes+JARVIS direct contact-point server.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    _setup_logging()
    # Ensure eta_engine is importable when launched directly
    sys.path.insert(0, os.environ.get("PYTHONPATH", r"C:\EvolutionaryTradingAlgo"))
    try:
        serve(host=args.host, port=args.port)
        return 0
    except OSError as exc:
        logger.error("status_server failed to start: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
