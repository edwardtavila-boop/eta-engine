"""APEX PREDATOR  //  scripts.jarvis_dashboard  --  MASTER COMMAND CENTER
=========================================================================
The canonical operator console for the Apex Predator framework. Surfaces
every JARVIS supervisor signal the operator needs to glance at without
spelunking journals: drift verdict, breaker state, deadman heartbeat,
forecast quality, daemon health, promotion queue, calibration drift,
decision-journal tail, alert tail.

The module is dual-mode:

* **Library** (default on import): exposes :func:`collect_state` and
  :data:`INDEX_HTML` for tests and the FastAPI dashboard backend.
* **Server** (via ``python -m apex_predator.scripts.jarvis_dashboard``
  or :func:`serve`): runs a stdlib :class:`http.server.ThreadingHTTPServer`
  binding ``127.0.0.1`` by default. Pair with Cloudflare Tunnel for
  remote access; see ``deploy/HOST_RUNBOOK.md``.

Routes:

    GET /                       --  HTML shell (INDEX_HTML)
    GET /api/state              --  collect_state() as JSON
    GET /healthz                --  liveness ("ok\\n")
    GET /manifest.webmanifest   --  PWA manifest (installable on phones)
    GET /sw.js                  --  service worker (offline shell cache)
    GET /icon.svg               --  app icon (192x192-friendly SVG)

The server is intentionally stdlib-only -- import-time side-effect free,
no FastAPI, no uvicorn. The :class:`_Handler` overrides
:meth:`http.server.BaseHTTPRequestHandler.log_message` so noisy access
logs don't spam systemd journals.

Drift card schema (``_render_drift`` output):

    {
        "state":         <verdict>         # "OK" | "WARN" | "AUTO_DEMOTE" | "NO_DATA"
        "journal":       <str>             # path the panel reads
        "strategy_id":   <str | None>      # last entry's strategy
        "kl":            <float | None>    # kl_divergence of last entry
        "sharpe_delta":  <float | None>    # sharpe_delta_sigma of last entry
        "mean_delta":    <float | None>    # mean_return_delta of last entry
        "n_live":        <int | None>      # live_sample_size of last entry
        "n_backtest":    <int | None>      # bt_sample_size of last entry
        "entries":       <int>             # count of valid journal lines
        "counts":        {<verdict>: int}  # per-verdict count
        "reason":        <str>             # "; ".join(reasons) of last entry
    }
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import time
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Module-level paths (monkeypatched by tests)
# ---------------------------------------------------------------------------

DRIFT_JOURNAL: Path = Path("~/.jarvis/drift.jsonl").expanduser()
DECISION_JOURNAL: Path = Path("~/.jarvis/decision_journal.jsonl").expanduser()
ALERTS_LOG: Path = Path("docs/alerts_log.jsonl")  # obs.alert_dispatcher writes here
AUDIT_LOG: Path = Path("~/.local/state/apex_predator/mcc_audit.jsonl").expanduser()
PUSH_SUBSCRIPTIONS: Path = Path("~/.local/state/apex_predator/mcc_push_subscriptions.jsonl").expanduser()
KILL_REQUEST: Path = Path("~/.local/state/apex_predator/mcc_kill_request.json").expanduser()
PAUSE_REQUESTS: Path = Path("~/.local/state/apex_predator/mcc_pause_requests.jsonl").expanduser()
ALERT_ACKS: Path = Path("~/.local/state/apex_predator/mcc_alert_acks.jsonl").expanduser()

# Live tail size (cards show last N journal/alert entries).
TAIL_LINES: int = 20

# Hard rule: bots boot paused, never auto-unpause. The operator must type
# this exact string in the body of any /api/cmd/unpause-bot request.
UNPAUSE_CONFIRM_TOKEN: str = "I_UNDERSTAND_LIVE_RISK"


# ---------------------------------------------------------------------------
# Drift card
# ---------------------------------------------------------------------------


def read_drift_journal(path: Path) -> list[dict[str, Any]]:
    """Return every well-formed JSON-line entry. Malformed lines skipped."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _render_drift() -> dict[str, Any]:
    """Build the drift card from the journal pointed at by ``DRIFT_JOURNAL``."""
    entries = read_drift_journal(DRIFT_JOURNAL)
    if not entries:
        return {
            "state": "NO_DATA",
            "journal": str(DRIFT_JOURNAL),
            "strategy_id": None,
            "kl": None,
            "sharpe_delta": None,
            "mean_delta": None,
            "n_live": None,
            "n_backtest": None,
            "entries": 0,
            "counts": {},
            "reason": "",
        }

    counts: dict[str, int] = {}
    for e in entries:
        v = e.get("verdict")
        if isinstance(v, str):
            counts[v] = counts.get(v, 0) + 1

    last = entries[-1]
    reasons = last.get("reasons") or []
    reason_text = "; ".join(str(r) for r in reasons) if isinstance(reasons, list) else ""

    return {
        "state": last.get("verdict") or "NO_DATA",
        "journal": str(DRIFT_JOURNAL),
        "strategy_id": last.get("strategy_id"),
        "kl": last.get("kl_divergence"),
        "sharpe_delta": last.get("sharpe_delta_sigma"),
        "mean_delta": last.get("mean_return_delta"),
        "n_live": last.get("live_sample_size"),
        "n_backtest": last.get("bt_sample_size"),
        "entries": len(entries),
        "counts": counts,
        "reason": reason_text,
    }


# ---------------------------------------------------------------------------
# Per-panel placeholders
# ---------------------------------------------------------------------------
# Each panel below returns its own card dict. Panels backed by real
# subsystems (breaker, journal, alerts) read those subsystems' state.
# Panels for subsystems still under construction return a structured
# placeholder so the HTML layer always sees the key.
def _render_breaker() -> dict[str, Any]:
    return {"state": "UNKNOWN", "tripped_at": None}


def _render_deadman() -> dict[str, Any]:
    return {"last_heartbeat": None, "stale_seconds": None}


def _render_forecast() -> dict[str, Any]:
    return {"horizon_minutes": None, "confidence": None}


def _render_daemons() -> dict[str, Any]:
    return {"healthy": [], "down": []}


def _render_promotion() -> dict[str, Any]:
    return {"in_flight": []}


def _render_calibration() -> dict[str, Any]:
    return {"last_run": None, "ks_pvalue": None}


def _tail_jsonl(path: Path, n: int = TAIL_LINES) -> list[dict[str, Any]]:
    """Return the last ``n`` well-formed JSON-line entries from ``path``."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines()[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _render_journal() -> dict[str, Any]:
    return {"path": str(DECISION_JOURNAL), "tail": _tail_jsonl(DECISION_JOURNAL)}


def _render_alerts() -> dict[str, Any]:
    return {"path": str(ALERTS_LOG), "tail": _tail_jsonl(ALERTS_LOG)}


# ---------------------------------------------------------------------------
# Audit log + operator identity (Cloudflare Access JWT)
# ---------------------------------------------------------------------------


def _audit(action: str, *, operator: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Append one row to AUDIT_LOG. Returns the record (echoed in response)."""
    rec: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "action": action,
        "operator": operator,
        "payload": payload,
    }
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def _operator_from_jwt(headers: Any) -> str:
    """Extract operator email from the ``Cf-Access-Jwt-Assertion`` header.

    Signature is NOT verified -- the cloudflared tunnel only forwards
    requests that already cleared Cloudflare Access at the edge, so the
    JWT is trusted-by-channel. Returns "anonymous" when the header is
    missing or unparseable (covers local dev without the tunnel).
    """
    raw = headers.get("Cf-Access-Jwt-Assertion") or headers.get("cf-access-jwt-assertion")
    if not raw:
        return "anonymous"
    try:
        parts = raw.split(".")
        if len(parts) != 3:
            return "anonymous"
        # JWT segments are base64url with no padding -- pad before decoding.
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        if not isinstance(payload, dict):
            return "anonymous"
        return str(payload.get("email") or payload.get("identity") or "anonymous")
    except (ValueError, KeyError, json.JSONDecodeError, binascii.Error, UnicodeDecodeError):
        return "anonymous"


def collect_state() -> dict[str, Any]:
    """Aggregate every panel into one snapshot for the HTML poller."""
    return {
        "drift": _render_drift(),
        "breaker": _render_breaker(),
        "deadman": _render_deadman(),
        "forecast": _render_forecast(),
        "daemons": _render_daemons(),
        "promotion": _render_promotion(),
        "calibration": _render_calibration(),
        "journal": _render_journal(),
        "alerts": _render_alerts(),
    }


# ---------------------------------------------------------------------------
# Static HTML template -- consumed by the dashboard server (deploy/scripts/
# dashboard_api.py) and asserted-against by test_jarvis_hardening.
# Element ids must match the JS poller; do not rename without updating both.
# ---------------------------------------------------------------------------
INDEX_HTML: str = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <meta name="theme-color" content="#0b0d10" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <meta name="apple-mobile-web-app-title" content="JARVIS" />
  <link rel="manifest" href="/manifest.webmanifest" />
  <link rel="icon" type="image/svg+xml" href="/icon.svg" />
  <link rel="apple-touch-icon" href="/icon.svg" />
  <title>JARVIS // Master Command Center</title>
  <style>
    :root {
      --bg: #07090d; --panel: #11161d; --panel2: #161b22;
      --border: #1f2631; --border2: #30363d;
      --text: #e7ecf2; --dim: #8b949e; --mute: #4c5564;
      --ok: #56d364; --warn: #d29922; --bad: #f85149;
      --accent: #00ffa3; --accent2: #00d1ff;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text); }
    body {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      padding: max(env(safe-area-inset-top), 12px) 12px max(env(safe-area-inset-bottom), 12px);
      -webkit-tap-highlight-color: transparent;
      overscroll-behavior-y: contain;
    }
    header {
      display: flex; align-items: center; justify-content: space-between;
      gap: 12px; margin: 0 0 12px;
    }
    header h1 {
      margin: 0; font-size: 14px; letter-spacing: 0.18em; text-transform: uppercase;
      color: var(--accent);
    }
    header h1 .sep { color: var(--mute); margin: 0 6px; }
    header h1 .sub { color: var(--dim); }
    .pulse {
      display: inline-block; width: 8px; height: 8px; border-radius: 50%;
      background: var(--ok); box-shadow: 0 0 8px var(--ok);
      animation: pulse 2s ease-in-out infinite;
    }
    .pulse.stale { background: var(--bad); box-shadow: 0 0 8px var(--bad); }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 10px;
    }
    .card {
      background: var(--panel2); border: 1px solid var(--border2);
      border-radius: 8px; padding: 12px 14px; min-height: 88px;
    }
    .card h2 {
      margin: 0 0 8px; font-size: 11px; letter-spacing: 0.16em;
      text-transform: uppercase; color: var(--dim);
      display: flex; align-items: center; justify-content: space-between;
    }
    .card h2 .badge { font-size: 10px; color: var(--mute); }
    .row {
      display: flex; justify-content: space-between; gap: 12px;
      padding: 3px 0; font-size: 13px; line-height: 1.4;
    }
    .row > span:first-child { color: var(--dim); }
    .row > span:last-child  { color: var(--text); text-align: right; word-break: break-word; }
    .ok   { color: var(--ok)   !important; }
    .warn { color: var(--warn) !important; }
    .bad  { color: var(--bad)  !important; }
    .controls {
      margin: 0 0 12px; display: flex; flex-wrap: wrap; gap: 8px;
    }
    .btn {
      flex: 0 1 auto; min-height: 44px; padding: 8px 14px;
      background: var(--panel2); color: var(--text);
      border: 1px solid var(--border2); border-radius: 6px;
      font-family: inherit; font-size: 12px; letter-spacing: 0.08em;
      text-transform: uppercase; cursor: pointer;
      transition: background 0.15s, border-color 0.15s;
    }
    .btn:hover  { background: var(--panel); border-color: var(--accent); }
    .btn:active { transform: translateY(1px); }
    .btn.danger        { color: var(--bad);  border-color: var(--bad); }
    .btn.danger:hover  { background: rgba(248, 81, 73, 0.12); }
    .btn.success       { color: var(--ok);   border-color: var(--ok); }
    .btn.success:hover { background: rgba(86, 211, 100, 0.12); }
    .btn.voice         { color: var(--accent2); border-color: var(--accent2); }
    .btn.voice.listening {
      background: rgba(0, 209, 255, 0.18);
      animation: pulse 1.2s ease-in-out infinite;
    }
    .toast {
      position: fixed; left: 50%; bottom: 16px; transform: translateX(-50%);
      max-width: 90vw; padding: 10px 16px; border-radius: 6px;
      background: var(--panel2); border: 1px solid var(--border2);
      color: var(--text); font-size: 12px; z-index: 100;
      box-shadow: 0 4px 18px rgba(0,0,0,0.5);
    }
    .toast.ok  { border-color: var(--ok);  color: var(--ok); }
    .toast.bad { border-color: var(--bad); color: var(--bad); }
    .tail {
      margin: 8px 0 0 0; padding: 0; max-height: 120px; overflow-y: auto;
      font-size: 11px; color: var(--dim); list-style: none;
    }
    .tail li { padding: 2px 0; border-bottom: 1px dashed var(--border); }
    .tail li:last-child { border-bottom: none; }
    footer {
      margin-top: 14px; color: var(--mute); font-size: 11px;
      text-align: center; letter-spacing: 0.08em;
    }
    @media (max-width: 480px) {
      body { padding: 10px 8px; }
      .grid { grid-template-columns: 1fr; gap: 8px; }
      .card { padding: 11px 12px; }
      header h1 { font-size: 12px; }
      .btn { font-size: 11px; padding: 8px 10px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>
      <span class="pulse" id="hb-pulse" title="last poll"></span>
      &nbsp;JARVIS<span class="sep">//</span><span class="sub">Master Command Center</span>
    </h1>
    <span id="hb-ts" style="font-size:11px;color:var(--mute);">--</span>
  </header>
  <div class="controls">
    <button class="btn danger"  id="btn-kill-trip"   type="button">Kill Switch &#8226; TRIP</button>
    <button class="btn success" id="btn-kill-reset"  type="button">Kill &#8226; Reset</button>
    <button class="btn"         id="btn-pause"       type="button">Pause Bot</button>
    <button class="btn"         id="btn-unpause"     type="button">Unpause Bot</button>
    <button class="btn"         id="btn-ack"         type="button">Ack Alert</button>
    <button class="btn voice"   id="btn-voice"       type="button">&#127908; Voice</button>
    <button class="btn"         id="btn-push"        type="button">&#128276; Notify Me</button>
  </div>
  <div class="grid">
    <div class="card" id="card-drift">
      <h2>drift <span class="badge" id="drift-counts">--</span></h2>
      <div class="row"><span>state</span><span id="drift-state">--</span></div>
      <div class="row"><span>strategy</span><span id="drift-strategy">--</span></div>
      <div class="row"><span>kl</span><span id="drift-kl">--</span></div>
      <div class="row"><span>&Delta;sharpe</span><span id="drift-dsharpe">--</span></div>
      <div class="row"><span>&Delta;mean</span><span id="drift-dmean">--</span></div>
      <div class="row"><span>n</span><span id="drift-n">--</span></div>
      <div class="row"><span>reason</span><span id="drift-reason">--</span></div>
    </div>
    <div class="card" id="card-breaker"><h2>breaker</h2><div class="row"><span>state</span><span id="breaker-state">--</span></div></div>
    <div class="card" id="card-deadman"><h2>deadman</h2><div class="row"><span>last</span><span id="deadman-last">--</span></div></div>
    <div class="card" id="card-forecast"><h2>forecast</h2><div class="row"><span>horizon</span><span id="forecast-horizon">--</span></div></div>
    <div class="card" id="card-daemons"><h2>daemons</h2><div class="row"><span>down</span><span id="daemons-down">--</span></div></div>
    <div class="card" id="card-promotion"><h2>promotion</h2><div class="row"><span>in-flight</span><span id="promotion-inflight">--</span></div></div>
    <div class="card" id="card-calibration"><h2>calibration</h2><div class="row"><span>p-value</span><span id="calibration-p">--</span></div></div>
    <div class="card" id="card-journal">
      <h2>journal</h2>
      <div class="row"><span>tail</span><span id="journal-tail">--</span></div>
      <ul class="tail" id="journal-tail-list"></ul>
    </div>
    <div class="card" id="card-alerts">
      <h2>alerts</h2>
      <div class="row"><span>tail</span><span id="alerts-tail">--</span></div>
      <ul class="tail" id="alerts-tail-list"></ul>
    </div>
  </div>
  <footer>apex predator // jarvis master command center</footer>
  <div id="toast-host"></div>
  <script>
    const $ = (id) => document.getElementById(id);
    const colorFor = (s) => s === 'OK' ? 'ok' : s === 'WARN' ? 'warn'
      : (s === 'AUTO_DEMOTE' || s === 'BAD' || s === 'TRIPPED') ? 'bad' : '';
    function setText(id, v, klass) {
      const el = $(id); if (!el) return;
      el.textContent = (v == null || v === '') ? '--' : String(v);
      el.classList.remove('ok','warn','bad');
      if (klass) el.classList.add(klass);
    }
    function toast(msg, klass) {
      const host = $('toast-host'); if (!host) return;
      const t = document.createElement('div');
      t.className = 'toast ' + (klass || '');
      t.textContent = msg;
      host.appendChild(t);
      setTimeout(() => t.remove(), 4000);
    }
    function render(s) {
      const d = s.drift || {};
      setText('drift-state',    d.state, colorFor(d.state));
      setText('drift-strategy', d.strategy_id);
      setText('drift-kl',       d.kl != null ? d.kl.toFixed(3) : null);
      setText('drift-dsharpe',  d.sharpe_delta != null ? d.sharpe_delta.toFixed(2) : null);
      setText('drift-dmean',    d.mean_delta != null ? d.mean_delta.toFixed(4) : null);
      setText('drift-n',        (d.n_live != null && d.n_backtest != null) ? `${d.n_live}/${d.n_backtest}` : null);
      setText('drift-reason',   d.reason);
      const c = d.counts || {}; const ck = Object.keys(c);
      setText('drift-counts', ck.length ? ck.map(k => `${k.charAt(0)}:${c[k]}`).join(' ') : null);
      const b = s.breaker || {};   setText('breaker-state', b.state, colorFor(b.state));
      const dm = s.deadman || {};  setText('deadman-last', dm.last_heartbeat);
      const fc = s.forecast || {}; setText('forecast-horizon', fc.horizon_minutes != null ? `${fc.horizon_minutes}m` : null);
      const dn = s.daemons || {};  setText('daemons-down', (dn.down || []).length, (dn.down || []).length ? 'bad' : 'ok');
      const pr = s.promotion || {};setText('promotion-inflight', (pr.in_flight || []).length);
      const cb = s.calibration || {};setText('calibration-p', cb.ks_pvalue != null ? cb.ks_pvalue.toFixed(3) : null);
      const jr = s.journal || {};  setText('journal-tail', (jr.tail || []).length);
      const al = s.alerts  || {};  setText('alerts-tail',  (al.tail  || []).length);
      renderTail('journal-tail-list', jr.tail || []);
      renderTail('alerts-tail-list',  al.tail || []);
      $('hb-pulse').classList.remove('stale');
      $('hb-ts').textContent = new Date().toLocaleTimeString();
    }
    function renderTail(id, rows) {
      const ul = $(id); if (!ul) return;
      ul.innerHTML = '';
      rows.slice(-6).reverse().forEach(r => {
        const li = document.createElement('li');
        const ts = r.ts || r.generated_at || r.timestamp || '';
        const summary = r.summary || r.reason || r.message || r.verdict || JSON.stringify(r).slice(0, 80);
        li.textContent = (ts ? ts.slice(11, 19) + '  ' : '') + summary;
        ul.appendChild(li);
      });
    }
    // ---- live state via SSE (auto-reconnects) -------------------------
    let sse;
    function connect() {
      try { if (sse) sse.close(); } catch (e) {}
      sse = new EventSource('/api/state/stream');
      sse.onmessage = (e) => { try { render(JSON.parse(e.data)); } catch (err) {} };
      sse.onerror = () => {
        $('hb-pulse').classList.add('stale');
        setTimeout(connect, 3000);
      };
    }
    connect();
    // initial paint from non-stream endpoint so the UI populates fast
    fetch('/api/state', { cache: 'no-store' }).then(r => r.ok && r.json()).then(s => s && render(s)).catch(() => {});

    // ---- action helpers -----------------------------------------------
    async function postCmd(path, body) {
      try {
        const r = await fetch(path, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body || {}),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) { toast(data.error || ('HTTP ' + r.status), 'bad'); return null; }
        toast('OK: ' + path.split('/').pop(), 'ok');
        return data;
      } catch (err) { toast(String(err), 'bad'); return null; }
    }
    function ask(prompt) { return window.prompt(prompt); }

    $('btn-kill-trip').onclick = () => {
      if (!confirm('Trip the kill switch? All bots will be flattened.')) return;
      const reason = ask('Reason (audit log):') || 'manual operator trip';
      postCmd('/api/cmd/kill-switch-trip', { reason });
    };
    $('btn-kill-reset').onclick = () => {
      if (!confirm('Reset (clear) the kill-switch request?')) return;
      postCmd('/api/cmd/kill-switch-reset', {});
    };
    $('btn-pause').onclick = () => {
      const bot_id = ask('Bot id to pause (e.g. mnq, eth_perp):'); if (!bot_id) return;
      const reason = ask('Reason (optional):') || '';
      postCmd('/api/cmd/pause-bot', { bot_id, reason });
    };
    $('btn-unpause').onclick = () => {
      const bot_id = ask('Bot id to UNPAUSE:'); if (!bot_id) return;
      const confirm_token = ask('Type confirm token to unpause (see UNPAUSE_CONFIRM_TOKEN in MCC):');
      if (!confirm_token) return;
      postCmd('/api/cmd/unpause-bot', { bot_id, confirm: confirm_token });
    };
    $('btn-ack').onclick = () => {
      const alert_id = ask('Alert id to ack:'); if (!alert_id) return;
      const note = ask('Note (optional):') || '';
      postCmd('/api/cmd/ack-alert', { alert_id, note });
    };

    // ---- voice control (Web Speech API) -------------------------------
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const voiceBtn = $('btn-voice');
    if (!SR) { voiceBtn.disabled = true; voiceBtn.title = 'Web Speech API not available'; }
    voiceBtn.onclick = () => {
      if (!SR) return;
      const recog = new SR();
      recog.lang = 'en-US'; recog.interimResults = false; recog.maxAlternatives = 1;
      voiceBtn.classList.add('listening');
      recog.onresult = (e) => {
        const cmd = (e.results[0][0].transcript || '').toLowerCase().trim();
        toast('heard: ' + cmd);
        if (cmd.includes('kill switch') || cmd.includes('kill the switch')) {
          if (confirm('VOICE: trip kill switch?')) postCmd('/api/cmd/kill-switch-trip', { reason: 'voice: ' + cmd });
        } else if (cmd.startsWith('pause ')) {
          const bot_id = cmd.replace(/^pause\\s+/, '').trim();
          if (bot_id && confirm('VOICE: pause bot ' + bot_id + '?')) postCmd('/api/cmd/pause-bot', { bot_id, reason: 'voice' });
        } else if (cmd.startsWith('ack ')) {
          const alert_id = cmd.replace(/^ack\\s+/, '').trim();
          if (alert_id) postCmd('/api/cmd/ack-alert', { alert_id, note: 'voice' });
        } else {
          toast('voice command not recognized', 'bad');
        }
      };
      recog.onend   = () => voiceBtn.classList.remove('listening');
      recog.onerror = (e) => { voiceBtn.classList.remove('listening'); toast('voice error: ' + e.error, 'bad'); };
      recog.start();
    };

    // ---- web push subscription ----------------------------------------
    function urlBase64ToUint8Array(b64) {
      const pad = '='.repeat((4 - (b64.length % 4)) % 4);
      const s = (b64 + pad).replace(/-/g, '+').replace(/_/g, '/');
      const raw = atob(s); const out = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
      return out;
    }
    $('btn-push').onclick = async () => {
      try {
        if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
          toast('web push not supported in this browser', 'bad'); return;
        }
        const perm = await Notification.requestPermission();
        if (perm !== 'granted') { toast('notification permission denied', 'bad'); return; }
        const reg = await navigator.serviceWorker.ready;
        const r = await fetch('/api/push/vapid-public-key');
        if (!r.ok) { toast('VAPID key not configured on server', 'bad'); return; }
        const { key } = await r.json();
        const sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(key),
        });
        const j = sub.toJSON();
        const out = await postCmd('/api/push/subscribe', { endpoint: j.endpoint, keys: j.keys });
        if (out) toast('push subscription active', 'ok');
      } catch (err) { toast('push subscribe failed: ' + err, 'bad'); }
    };

    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch(() => {});
    }
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# PWA shell -- manifest, service worker, icon
# ---------------------------------------------------------------------------

MANIFEST_JSON: str = json.dumps(
    {
        "name": "JARVIS Master Command Center",
        "short_name": "JARVIS",
        "description": "Apex Predator operator command center.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#07090d",
        "theme_color": "#0b0d10",
        "icons": [
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"},
            {"src": "/icon.svg", "sizes": "192x192", "type": "image/svg+xml"},
            {"src": "/icon.svg", "sizes": "512x512", "type": "image/svg+xml"},
        ],
    }
)

SERVICE_WORKER_JS: str = """\
// JARVIS MCC service worker -- shell cache only.
// Live data (/api/state) is always network-first.
const SHELL = 'jarvis-mcc-shell-v1';
const SHELL_FILES = ['/', '/manifest.webmanifest', '/icon.svg'];
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)));
  self.skipWaiting();
});
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))
    ))
  );
  self.clients.claim();
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/')) return; // live: network-only
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request))
  );
});
"""

ICON_SVG: str = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%"  stop-color="#00ffa3"/>
      <stop offset="100%" stop-color="#00d1ff"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="96" fill="#07090d"/>
  <circle cx="256" cy="256" r="168" fill="none" stroke="url(#g)" stroke-width="10" opacity="0.55"/>
  <circle cx="256" cy="256" r="120" fill="none" stroke="url(#g)" stroke-width="6" opacity="0.85"/>
  <circle cx="256" cy="256" r="22" fill="url(#g)"/>
  <text x="256" y="430" text-anchor="middle"
        font-family="ui-monospace, monospace" font-weight="700"
        font-size="56" fill="url(#g)" letter-spacing="6">JARVIS</text>
</svg>
"""


# ---------------------------------------------------------------------------
# HTTP server (stdlib)
# ---------------------------------------------------------------------------

DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8765

# SSE poll cadence (seconds between state pushes on /api/state/stream).
SSE_INTERVAL_SEC: float = 1.0
# Max payload size accepted on POST (defends against accidental floods).
POST_MAX_BYTES: int = 64 * 1024


class _Handler(BaseHTTPRequestHandler):
    """Minimal stdlib handler -- one route table, no framework."""

    server_version = "JarvisMCC/1.0"

    # ---- response helpers --------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, obj: Any) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self._send(status, body, "application/json")

    def _read_json_body(self) -> dict[str, Any]:
        """Parse a JSON body. Returns {} on missing / malformed."""
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return {}
        if length <= 0 or length > POST_MAX_BYTES:
            return {}
        try:
            raw = self.rfile.read(length)
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return {}

    def _operator(self) -> str:
        return _operator_from_jwt(self.headers)

    # ---- GET routing -------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 -- stdlib contract
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            try:
                self._send_json(HTTPStatus.OK, collect_state())
            except Exception as exc:  # noqa: BLE001 -- never crash the dashboard
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path == "/api/state/stream":
            self._stream_state()
            return
        if path == "/api/push/vapid-public-key":
            key = os.environ.get("MCC_VAPID_PUBLIC_KEY", "").strip()
            if not key:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "MCC_VAPID_PUBLIC_KEY not set"})
                return
            self._send_json(HTTPStatus.OK, {"key": key})
            return
        if path == "/healthz":
            self._send(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
            return
        if path == "/manifest.webmanifest":
            self._send(
                HTTPStatus.OK, MANIFEST_JSON.encode("utf-8"), "application/manifest+json", cache="public, max-age=3600"
            )
            return
        if path == "/sw.js":
            self._send(HTTPStatus.OK, SERVICE_WORKER_JS.encode("utf-8"), "application/javascript; charset=utf-8")
            return
        if path == "/icon.svg":
            self._send(HTTPStatus.OK, ICON_SVG.encode("utf-8"), "image/svg+xml", cache="public, max-age=86400")
            return
        self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")

    # ---- POST routing ------------------------------------------------------

    _ACTION_ROUTES: dict[str, str] = {
        "/api/cmd/kill-switch-trip": "_action_kill_trip",
        "/api/cmd/kill-switch-reset": "_action_kill_reset",
        "/api/cmd/pause-bot": "_action_pause",
        "/api/cmd/unpause-bot": "_action_unpause",
        "/api/cmd/ack-alert": "_action_ack_alert",
        "/api/push/subscribe": "_action_push_subscribe",
    }

    def do_POST(self) -> None:  # noqa: N802 -- stdlib contract
        path = self.path.split("?", 1)[0]
        handler_name = self._ACTION_ROUTES.get(path)
        if not handler_name:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown action", "path": path})
            return
        body = self._read_json_body()
        operator = self._operator()
        try:
            handler = getattr(self, handler_name)
            status, payload = handler(body, operator)
        except Exception as exc:  # noqa: BLE001 -- never crash the dashboard
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        self._send_json(status, payload)

    # ---- action handlers ---------------------------------------------------

    def _action_kill_trip(self, body: dict[str, Any], operator: str) -> tuple[int, dict[str, Any]]:
        reason = str(body.get("reason") or "").strip() or "manual operator trip via MCC"
        rec = {
            "tripped_at": datetime.now(UTC).isoformat(),
            "operator": operator,
            "reason": reason,
            "scope": str(body.get("scope") or "ALL"),
        }
        KILL_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        KILL_REQUEST.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
        audit = _audit("kill-switch-trip", operator=operator, payload=rec)
        return HTTPStatus.OK, {"ok": True, "audit": audit, "request_file": str(KILL_REQUEST)}

    def _action_kill_reset(self, body: dict[str, Any], operator: str) -> tuple[int, dict[str, Any]]:
        if KILL_REQUEST.exists():
            KILL_REQUEST.unlink()
        audit = _audit("kill-switch-reset", operator=operator, payload={"reason": body.get("reason")})
        return HTTPStatus.OK, {"ok": True, "audit": audit}

    def _action_pause(self, body: dict[str, Any], operator: str) -> tuple[int, dict[str, Any]]:
        bot_id = str(body.get("bot_id") or "").strip()
        if not bot_id:
            return HTTPStatus.BAD_REQUEST, {"error": "bot_id required"}
        rec = {
            "ts": datetime.now(UTC).isoformat(),
            "intent": "pause",
            "bot_id": bot_id,
            "operator": operator,
            "reason": str(body.get("reason") or "").strip(),
        }
        PAUSE_REQUESTS.parent.mkdir(parents=True, exist_ok=True)
        with PAUSE_REQUESTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        audit = _audit("pause-bot", operator=operator, payload=rec)
        return HTTPStatus.OK, {"ok": True, "audit": audit}

    def _action_unpause(self, body: dict[str, Any], operator: str) -> tuple[int, dict[str, Any]]:
        bot_id = str(body.get("bot_id") or "").strip()
        confirm = str(body.get("confirm") or "")
        if not bot_id:
            return HTTPStatus.BAD_REQUEST, {"error": "bot_id required"}
        if confirm != UNPAUSE_CONFIRM_TOKEN:
            return HTTPStatus.FORBIDDEN, {
                "error": "unpause requires confirm token",
                "expected_confirm_token": UNPAUSE_CONFIRM_TOKEN,
            }
        rec = {
            "ts": datetime.now(UTC).isoformat(),
            "intent": "unpause",
            "bot_id": bot_id,
            "operator": operator,
            "reason": str(body.get("reason") or "").strip(),
        }
        PAUSE_REQUESTS.parent.mkdir(parents=True, exist_ok=True)
        with PAUSE_REQUESTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        audit = _audit("unpause-bot", operator=operator, payload=rec)
        return HTTPStatus.OK, {"ok": True, "audit": audit}

    def _action_ack_alert(self, body: dict[str, Any], operator: str) -> tuple[int, dict[str, Any]]:
        alert_id = str(body.get("alert_id") or "").strip()
        if not alert_id:
            return HTTPStatus.BAD_REQUEST, {"error": "alert_id required"}
        rec = {
            "ts": datetime.now(UTC).isoformat(),
            "alert_id": alert_id,
            "operator": operator,
            "note": str(body.get("note") or "").strip(),
        }
        ALERT_ACKS.parent.mkdir(parents=True, exist_ok=True)
        with ALERT_ACKS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        audit = _audit("ack-alert", operator=operator, payload=rec)
        return HTTPStatus.OK, {"ok": True, "audit": audit}

    def _action_push_subscribe(self, body: dict[str, Any], operator: str) -> tuple[int, dict[str, Any]]:
        endpoint = str(body.get("endpoint") or "").strip()
        keys = body.get("keys")
        if not endpoint or not isinstance(keys, dict):
            return HTTPStatus.BAD_REQUEST, {"error": "endpoint and keys required"}
        rec = {
            "ts": datetime.now(UTC).isoformat(),
            "operator": operator,
            "endpoint": endpoint,
            "keys": keys,
        }
        PUSH_SUBSCRIPTIONS.parent.mkdir(parents=True, exist_ok=True)
        with PUSH_SUBSCRIPTIONS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        audit = _audit("push-subscribe", operator=operator, payload={"endpoint": endpoint})
        return HTTPStatus.OK, {"ok": True, "audit": audit}

    # ---- SSE state stream --------------------------------------------------

    def _stream_state(self) -> None:
        """Push state snapshots forever as Server-Sent Events.

        Sends a snapshot every ``SSE_INTERVAL_SEC`` plus a comment-only ping
        every 15s to keep proxies/Cloudflare from idling out the connection.
        Exits cleanly on client disconnect (BrokenPipeError / ConnectionReset).
        """
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")  # disable nginx buffering
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        last_ping = time.monotonic()
        while True:
            try:
                state = collect_state()
                chunk = ("data: " + json.dumps(state, default=str) + "\n\n").encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
                now = time.monotonic()
                if now - last_ping >= 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = now
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            except Exception:  # noqa: BLE001 -- never crash the dashboard
                return
            time.sleep(SSE_INTERVAL_SEC)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 -- stdlib contract
        # Silence the default access log; systemd journals stay clean.
        return


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Run the master command center server in the foreground."""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="jarvis-mcc",
        description="JARVIS Master Command Center -- operator dashboard server.",
    )
    parser.add_argument("--host", default=os.environ.get("JARVIS_MCC_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("JARVIS_MCC_PORT", DEFAULT_PORT)))
    args = parser.parse_args(argv)
    serve(host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover -- entry point
    main()


__all__ = [
    "ALERTS_LOG",
    "ALERT_ACKS",
    "AUDIT_LOG",
    "DECISION_JOURNAL",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DRIFT_JOURNAL",
    "ICON_SVG",
    "INDEX_HTML",
    "KILL_REQUEST",
    "MANIFEST_JSON",
    "PAUSE_REQUESTS",
    "PUSH_SUBSCRIPTIONS",
    "SERVICE_WORKER_JS",
    "SSE_INTERVAL_SEC",
    "TAIL_LINES",
    "UNPAUSE_CONFIRM_TOKEN",
    "collect_state",
    "main",
    "read_drift_journal",
    "serve",
]
