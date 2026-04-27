# JARVIS Command Center + Bot Fleet Dashboard — Rebuild Design

**Date:** 2026-04-27
**Status:** Design (awaiting implementation plan)
**Owner:** Edward Avila

---

## Problem

The current dashboard at `http://127.0.0.1:8420/` glitches and is hard to debug. Backend is healthy (the `firm/eta_engine/command_center/server/app.py` FastAPI app returns rich JSON), but the frontend is a 945KB bundled HTML artifact that breaks in ways nobody can diagnose. Operator needs a "supercharged" JARVIS command center + bot fleet view to monitor the 7-bot fleet as it goes live.

## Goal

Replace the broken dashboard with one that:
- Shows JARVIS's full state at a glance (verdicts, sage health, kill-switch, V22 toggle, edge leaderboard, model-tier routing)
- Shows the bot fleet at a glance (roster, drill-down, equity curve, drawdown, sage modulation effects, position drift, lifecycle controls)
- Refreshes in real-time (SSE for verdict + fill streams; 5s polling for everything else)
- Is debuggable — no opaque bundles, every panel inspectable from browser DevTools
- Uses one backend (the cleaner `eta_engine/deploy/scripts/dashboard_api.py`) instead of two competing dashboards

## Approach (chosen during brainstorming)

**Approach A: Vanilla SPA + Tailwind via CDN.**

- Single `index.html` shell + ~5 small JS modules + 1 CSS file
- No build step, no npm, no node_modules
- All panels are inspectable / editable / debuggable from the browser
- Tailwind via CDN provides a consistent design system without a build chain
- SSE + fetch are first-class browser APIs — no framework needed

Approach B (Vite + React/Preact bundle) was rejected because that's exactly the pattern the current broken dashboard uses. Approach C (server-rendered + HTMX) was rejected because it introduces a new pattern at the same time as shipping a live-trading tool.

---

## Architecture

### Backend extensions (added to `eta_engine/deploy/scripts/dashboard_api.py`)

The existing 25 endpoints stay. New endpoints port the rich functionality from `firm/eta_engine/command_center/server/app.py`:

```
Auth:
    POST /api/auth/login
    POST /api/auth/logout
    POST /api/auth/step-up
    GET  /api/auth/session

JARVIS:
    GET  /api/jarvis/governor
    GET  /api/jarvis/governor/markdown
    GET  /api/jarvis/edge_leaderboard         (+ ?bot=<id> for per-bot)
    GET  /api/jarvis/model_tier
    GET  /api/jarvis/kaizen_latest
    GET  /api/jarvis/sage_modulation_toggle   (returns current flag state)
    POST /api/jarvis/sage_modulation_toggle   (step-up gated; flips ETA_FF_V22_SAGE_MODULATION)
    GET  /api/jarvis/sage_modulation_stats    (per-bot agree/disagree/defer counts in last 24h)

Fleet:
    GET  /api/bot-fleet                       (roster: 7 bots)
    GET  /api/bot-fleet/{bot_id}              (drill-down: recent fills + verdicts + sage effects)
    GET  /api/master/status
    GET  /api/preflight                       (correlation throttle map)
    GET  /api/risk_gates                      (per-bot kill-switch + DD + cap state)
    GET  /api/positions/reconciler

Trades:
    GET  /api/trades                          (recent fills across fleet)
    GET  /api/equity                          (today + 30d equity curve)

Bot lifecycle (all step-up gated for flatten/kill):
    POST /api/bot/{bot_id}/pause
    POST /api/bot/{bot_id}/resume
    POST /api/bot/{bot_id}/flatten            (step-up required)
    POST /api/bot/{bot_id}/kill               (step-up required)

Master actions (step-up required):
    POST /api/master/actions/evaluate
    POST /api/master/actions/request
    POST /api/master/approvals/{approval_id}

Live stream (Server-Sent Events):
    GET  /api/live/stream                     (yields verdict + fill events)
```

### Frontend file layout

```
deploy/status_page/
├── index.html              shell + layout grid + login modal + tab nav + bottom fill tape
├── theme.css               Tailwind CDN base + dark-mode tokens + panel-specific styles
└── js/
    ├── auth.js             session check, login flow, step-up modal
    ├── live.js             SSE manager + Poller (5s) + visibility-suspend
    ├── panels.js           Panel base class, formatters, error/loading/stale states
    ├── command_center.js   10 JARVIS panels
    └── bot_fleet.js        12 fleet panels
```

### Top-level layout

```
┌────────────────────────────────────────────────────────────────────┐
│ TOP BAR (fixed):                                                  │
│   • Kill-switch state lamp                                        │
│   • V22_SAGE_MODULATION toggle                                    │
│   • Stress score gauge                                            │
│   • Fleet aggregate equity + DD                                   │
│   • Alerts badge                                                  │
│   • SSE connection dot (green/yellow/red)                         │
│   • User chip + logout                                            │
├────────────────────────────────────────────────────────────────────┤
│ TAB NAV: [ JARVIS Command Center ] [ Bot Fleet ]                  │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  ACTIVE VIEW                                                      │
│  - JARVIS view: 3-column grid of 10 panels                        │
│  - Fleet view:  roster table (top half) + drill-down + equity     │
│                 curve (bottom half)                               │
│                                                                    │
├────────────────────────────────────────────────────────────────────┤
│ LIVE FILL TAPE (fixed bottom): scrolling fills, all bots, real-time│
└────────────────────────────────────────────────────────────────────┘
```

---

## Components

### Per-panel component map

Each panel is one `Panel` subclass with: container DOM id, refresh strategy (`sse` | `poll-5s` | `event`), endpoint, render function.

#### JARVIS Command Center (10 panels)

| ID                            | Container                       | Refresh    | Endpoint                                              |
|-------------------------------|---------------------------------|------------|-------------------------------------------------------|
| Live verdict stream           | `#cc-verdict-stream`            | sse        | `/api/live/stream` (verdict events)                   |
| Sage explain (current symbol) | `#cc-sage-explain`              | poll-5s    | `/api/jarvis/sage_explain?symbol={sel}`               |
| Sage health alerts            | `#cc-sage-health`               | poll-5s    | `/api/jarvis/health`                                  |
| 23-school disagreement heatmap| `#cc-disagreement-heatmap`      | poll-5s    | `/api/jarvis/sage_disagreement_heatmap?symbol={sel}`  |
| Stress + session + kill lamp  | `#cc-stress-mood`               | poll-5s    | `/api/jarvis/summary`                                 |
| Bandit / multi-policy diff    | `#cc-policy-diff`               | poll-5s    | `/api/jarvis/policy_diff`                             |
| V22_SAGE_MODULATION toggle    | `#cc-v22-toggle`                | poll-5s + POST | `GET /api/jarvis/sage_modulation_toggle` + `POST` (step-up) |
| Edge tracker leaderboard      | `#cc-edge-leaderboard`          | poll-5s    | `/api/jarvis/edge_leaderboard`                        |
| Model tier routing            | `#cc-model-tier`                | poll-5s    | `/api/jarvis/model_tier`                              |
| Latest kaizen ticket          | `#cc-kaizen-latest`             | poll-5s    | `/api/jarvis/kaizen_latest`                           |

#### Bot Fleet (12 panels)

| ID                       | Container                  | Refresh                          | Endpoint                                          |
|--------------------------|----------------------------|----------------------------------|---------------------------------------------------|
| Fleet roster table       | `#fl-roster`               | poll-5s                          | `/api/bot-fleet`                                  |
| Per-bot drill-down       | `#fl-drilldown`            | poll-5s on selection change      | `/api/bot-fleet/{sel}`                            |
| Aggregate equity curve   | `#fl-equity-curve`         | poll-5s                          | `/api/equity`                                     |
| Drawdown brake state     | `#fl-drawdown`             | poll-5s                          | `/api/risk_gates`                                 |
| Sage modulation effect   | `#fl-sage-effect`          | poll-5s                          | `/api/jarvis/sage_modulation_stats`               |
| Correlation throttle map | `#fl-correlation`          | poll-5s                          | `/api/preflight`                                  |
| Per-bot edge tracker     | `#fl-edge-per-bot`         | poll-5s on selection change      | `/api/jarvis/edge_leaderboard?bot={sel}`          |
| Position reconciler      | `#fl-position-reconciler`  | poll-5s                          | `/api/positions/reconciler`                       |
| Risk gate ladder         | `#fl-risk-ladder`          | poll-5s                          | `/api/risk_gates`                                 |
| Bot lifecycle controls   | `#fl-controls`             | event                            | `POST /api/bot/{id}/{action}` (step-up gated)    |
| Live fill tape           | `#fl-fill-tape`            | sse                              | `/api/live/stream` (fill events)                  |
| Health badges            | `#fl-health-badges`        | poll-5s                          | `/api/bot-fleet` (subset)                         |

### Frontend module responsibilities

| File                  | Responsibility                                                                                                       |
|-----------------------|----------------------------------------------------------------------------------------------------------------------|
| `index.html`          | DOM shell, layout grid, login modal, panel containers (empty divs with stable IDs), bottom fill tape strip           |
| `theme.css`           | Tailwind CDN + dark-mode design tokens + cards/badges/tables/scroll-list base styles                                 |
| `js/auth.js`          | `checkSession()`, `login()`, `logout()`, `requireStepUp()`, exports `session` global, renders login modal            |
| `js/live.js`          | `LiveStream` (EventSource wrapper, exponential backoff reconnect), `Poller` (5s scheduler with visibility suspend)   |
| `js/panels.js`        | `Panel` base class (`render`, `setLoading`, `setError`, `markStale`), formatters (`formatNumber`, `formatPct`, ...) |
| `js/command_center.js`| All 10 JARVIS panels as `Panel` subclasses                                                                            |
| `js/bot_fleet.js`     | All 12 fleet panels + lifecycle button handlers as `Panel` subclasses                                                |

---

## Data Flow

### Initial page load

1. Browser opens `/` → returns `index.html` (shell only, panels are empty divs).
2. `theme.css` + JS modules load via `<script type="module">`.
3. `auth.js` runs first via `DOMContentLoaded`:
   - `GET /api/auth/session`
   - If unauthenticated → show login modal, BLOCK rest
   - If authenticated → proceed
4. `live.js` opens `EventSource('/api/live/stream')`, registers `verdict` and `fill` handlers.
5. `Poller` starts; every 5s, calls each registered panel's `refresh()`.
6. `command_center.js` and `bot_fleet.js` instantiate their 22 panels; each registers with the `Poller`.
7. Initial paint: every panel calls `refresh()` once immediately, then every 5s.

### Login flow

1. User submits login modal → `POST /api/auth/login {username, password}`.
2. Server bcrypt-checks against `state/auth/users.json`.
3. On success: creates session in `state/auth/sessions.json`, returns `Set-Cookie: session=<token>; HttpOnly; SameSite=Strict`.
4. On failure: 401 with rate-limit header (after 5 failed attempts: 429 with `Retry-After`).
5. Browser stores session cookie automatically; `auth.js` dismisses modal and kicks off the rest of the load.

### SSE stream lifecycle

1. Server tail-follows two files:
   - `state/jarvis_audit/<today>.jsonl` → emits `event: verdict`
   - `state/blotter/fills.jsonl` → emits `event: fill`
2. `live.js` dispatches:
   - `verdict` → command-center verdict-stream panel prepends row
   - `fill` → fleet fill-tape panel prepends row
3. On disconnect: exponential backoff retry (1s, 2s, 4s, 8s, max 30s); stale badge on dependent panels until reconnected.
4. At midnight when audit JSONL rotates: server re-resolves the path on each iteration; tail handle reopens.

**Auth note:** SSE uses same-origin **cookie** auth. `EventSource` does not support custom headers, so we rely on the session cookie (set during login) being automatically attached by the browser on same-origin requests. The server validates the session on connection open; if invalid, returns 401 and the client shows the login modal. Do NOT try to pass an `Authorization` header.

### Polling scheduler

- Every 5s tick: for each registered panel, fetch endpoint, render or set error.
- When `document.hidden` → suspend (save CPU/network in background tab).
- When `document.visible` → immediate force-refresh, then resume cadence.
- Per-panel "stale" badge appears if last refresh > 30s ago.

### Operator action flow (e.g., "Kill MNQ")

1. User clicks "Kill MNQ" → confirm modal: "Type 'kill mnq' to confirm".
2. `requireStepUp()` check: if last step-up < 15 min → proceed; else `POST /api/auth/step-up {pin}` first.
3. `POST /api/bot/mnq/kill`:
   - Server validates session + step-up freshness
   - Calls `FleetCoordinator.kill('mnq')`, trips kill-switch latch
   - Returns `{ok: true, latch_state: 'tripped', reason: 'operator_kill'}`
4. Roster panel polls `/api/bot-fleet` on next 5s tick → reflects new state.
5. Verdict stream (SSE) emits the audit event of the kill action immediately.
6. Toast notification: "MNQ killed. Latch tripped at 14:32:01."

### Selection state

When user clicks a bot in `#fl-roster`:
- `bot_fleet.js` sets `selectedBotId = 'mnq'`, `selectedSymbol = 'MNQ'`
- Emits custom event `selection-changed`
- Panels listening: `#fl-drilldown`, `#fl-edge-per-bot`, `#cc-sage-explain`, `#cc-disagreement-heatmap` trigger immediate refresh with new selection
- Other panels keep normal 5s cadence

### Auth gate matrix

| Action                                      | Auth required | Step-up required |
|---------------------------------------------|:-------------:|:----------------:|
| `GET /` (HTML shell)                        | no            | no               |
| `GET /api/*`                                | yes           | no               |
| `POST /api/bot/{id}/pause`, `/resume`       | yes           | no               |
| `POST /api/bot/{id}/flatten`, `/kill`       | yes           | **yes**          |
| `POST /api/jarvis/sage_modulation_toggle`   | yes           | **yes**          |
| `POST /api/master/actions/*`                | yes           | **yes**          |

---

## Error Handling + Observability

### Backend failure modes

| Failure                                            | Behavior                                                                                  |
|----------------------------------------------------|-------------------------------------------------------------------------------------------|
| State file missing                                 | Return `{}` with `200` + `_warning: "no_data"` field                                      |
| State file corrupt JSON                            | Log + return `{"error_code": "state_corrupt", "error_detail": "<file>"}` with `200`       |
| Audit JSONL rotates at midnight                    | Server re-resolves `state/jarvis_audit/<today>.jsonl` on each iteration; reopens handle   |
| Bot lifecycle action fails (FleetCoordinator down) | Return `503 {"error_code": "fleet_coord_unreachable"}`                                    |
| Concurrent JSON write from another process         | `portalocker` for read; retry once on `LockError`                                         |
| Endpoint handler throws                            | FastAPI exception handler → `500 {"error_code":"internal","error_detail":"<exc-class>"}` + log full traceback to `state/logs/dashboard.jsonl` |
| Auth session expired mid-request                   | `401 {"error_code": "session_expired"}`                                                   |
| Step-up expired (>15 min)                          | `403 {"error_code": "step_up_required"}`                                                  |
| Disk full when writing session/feature_flags       | Log + return `503 {"error_code": "disk_full"}`                                            |
| Rate limit exceeded (login)                        | `429` with `Retry-After` header                                                           |

### Frontend failure modes

| Failure                                  | Behavior                                                                                 |
|------------------------------------------|------------------------------------------------------------------------------------------|
| SSE drops                                | `EventSource.onerror` → exponential backoff (1s/2s/4s/8s, max 30s); top-bar dot turns yellow → red after 30s; affected panels get stale badge |
| Panel endpoint 500                       | Panel turns red, shows `error_detail`, "Retry now" button + auto-retry on next 5s tick   |
| Panel endpoint returns `_warning: "no_data"` | Panel shows neutral "No data yet" state with the warning text                          |
| Panel renderer throws (JS bug)           | `try/catch` around every `render(data)`; on throw → panel turns red + full stack to `console.error` + retries on next tick |
| Network completely down                  | Top bar shows "OFFLINE" badge + last-online timestamp; polling pauses; SSE retries in background |
| JS module fails to load                  | `window.onerror` catches; shell shows red banner: "Module load failed: <name>. Refresh." |
| Tab hidden                               | Polling suspends; SSE stays connected (low cost)                                         |
| Tab returns to foreground                | Force-refresh all panels once; resume 5s cadence                                         |
| 401 from any endpoint mid-session        | Global fetch wrapper kicks login modal back open; queued requests retry after re-auth    |

### Observability

**Backend:** structured JSONL logs to `state/logs/dashboard.jsonl`:
```json
{"ts":"...","level":"info","event":"req","method":"GET","path":"/api/bot-fleet","status":200,"ms":12,"user":"edward"}
{"ts":"...","level":"warn","event":"state_corrupt","file":"state/sage/edge_tracker.json","detail":"..."}
{"ts":"...","level":"error","event":"handler_exc","path":"/api/bot/mnq/kill","exc":"...","traceback":"..."}
```

**Frontend in-page diagnostics (always visible):**
- Top-bar SSE status dot (green / yellow / red)
- Per-panel last-refreshed badge (`updated 3s ago` / `stale 47s ago`)
- Per-panel error state (red border + error message + retry button)
- Top-right error toast feed (last 5 events, auto-dismiss 10s)

**Dev mode:**
- Frontend `?debug=1` → verbose console.log + per-panel `<details>` of last JSON + visible polling cadence
- Backend `ETA_DASHBOARD_DEBUG=1` → debug-level logs + stack traces in 500 bodies + CORS open to `*` for local frontend dev

### Explicit YAGNI (NOT building)

- No service worker / offline cache (localhost dashboard)
- No WebGL charts (Chart.js + Canvas is plenty)
- No analytics/telemetry pipeline
- No Sentry / error reporting service
- No PWA install prompt
- No mobile responsive design beyond "doesn't look broken on a tablet"
- No multi-user collaboration
- No alerting (Resend + webhook already exist)
- No historical backfill UI for sage / edge tracker
- No strategy editor / config UI
- No theming / customization

---

## Testing

### Backend (pytest)

| Test category         | Coverage                                                                       | Approach                                                                  |
|-----------------------|--------------------------------------------------------------------------------|---------------------------------------------------------------------------|
| Endpoint smoke        | Every new endpoint returns valid JSON for happy path + cold-start              | FastAPI `TestClient`, one test per new endpoint                           |
| State-file resilience | Endpoints don't 500 when backing file missing / corrupt                        | Temp state dir, omit / poison file, assert 200 + `_warning`               |
| Auth flow             | Login → cookie → protected endpoint 200; expired session → 401                 | `httpx` client, manipulate session table                                  |
| Step-up flow          | Without: 403; fresh PIN: 200; >15 min: 403                                     | Time-mock with `freezegun`                                                |
| SSE stream            | Tail-follow correctness across midnight rotation; reconnect after restart      | Spin up app subprocess, append to JSONL, assert events received           |
| Lifecycle actions     | pause/resume idempotent; flatten/kill require step-up; concurrent kills don't double-trip | Mock `FleetCoordinator`; assert state file mutations                |
| V22 toggle            | POST flips env + writes `state/feature_flags.json`; GET reflects current value | Isolated env to avoid cross-test pollution                                |
| Login rate limit      | 5 failed attempts → 429 with `Retry-After`                                     | Hammer endpoint with bad creds                                            |

Target: every new endpoint has at least 1 happy-path test + 1 failure-mode test.

### Frontend (Playwright — already a dep)

| Test                          | How                                                                                          |
|-------------------------------|----------------------------------------------------------------------------------------------|
| Page loads cleanly with auth  | Headless Chromium → POST login → load `/` → assert no `console.error`                       |
| Every panel renders           | Wait for `[data-panel-id]` divs to lose `loading` class; assert none have `error` class      |
| SSE reconnect                 | Disconnect EventSource → wait 3s → reconnect → assert `data-stream-status="connected"`       |
| Lifecycle button gating       | Click "Kill" without step-up → assert PIN modal; submit wrong PIN → assert error             |
| Stale badge appears           | Mock `/api/bot-fleet` to hang → wait 31s → assert panel shows stale badge                    |

5 Playwright tests, runs in <30s.

---

## Rollout Plan

### Stage 0 — Build alongside (no traffic shift)

- New endpoints land on dev branch in `dashboard_api.py`
- New frontend in `deploy/status_page/` (replacing current `index.html`)
- Old `firm/eta_engine/command_center/server/app.py` stays untouched
- Run new dashboard on port 8421 for QA: `uvicorn ... --port 8421`
- Operator visits `http://127.0.0.1:8421/` to test

**Pass criteria:**
- Backend tests green
- Playwright suite green
- Manual QA: every panel renders, lifecycle buttons work, SSE shows verdicts within 1s of audit append

### Stage 1 — Cutover

- Stop firm command_center process on 8420
- Update scheduled task / launcher to point at `eta_engine.deploy.scripts.dashboard_api`
- Start on 8420
- Operator visits `http://127.0.0.1:8420/` — sees new dashboard

**Rollback (instant):** revert scheduled task to old command_center, restart. No code changes to undo.

### Stage 2 — Decommission

- After 7 days of stable operation, move `firm/eta_engine/command_center/` to `_archive/`
- Single source of truth restored

### Cross-cutting

| Concern                                       | How                                                                                              |
|-----------------------------------------------|--------------------------------------------------------------------------------------------------|
| Doesn't break the engine on import            | Frontend is browser-side; backend opt-in (only loaded when uvicorn imports it)                  |
| Doesn't break fleet daemons                   | Lifecycle actions go through `FleetCoordinator` — same path the firm command_center uses today   |
| Backward compat                               | The 25 existing `dashboard_api.py` endpoints retain their contracts; we only ADD endpoints       |
| Migration of existing state files             | None needed — read the same files the firm command_center already reads                          |

---

## Success Criteria

1. **It doesn't glitch.** Page loads cleanly every time; no opaque bundle failures.
2. **Every panel inspectable.** Right-click → View Source shows readable HTML/JS, not a bundled blob.
3. **Sub-second perceived latency** on verdict + fill panels (SSE).
4. **5s freshness** on the other 20 panels.
5. **Operator-only actions gated** behind session + step-up; no accidental kills.
6. **Single source of truth** — one dashboard at `http://127.0.0.1:8420/`, not two.
7. **Rollback works** — flipping back to the old command_center takes <30s and requires no code changes.
