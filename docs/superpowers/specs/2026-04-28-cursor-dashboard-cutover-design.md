# Cursor Dashboard Full Cutover — Design Spec

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `dashboard_api.py` (the Cursor-designed dashboard) the permanent, fully-functional
operator surface at `app.evolutionarytradingalgo.com`, with all 16 JARVIS supervisor bots visible
and the legacy `command_center/server/app.py` retired.

**Architecture:** `dashboard_api.py` runs on port 8420 (already wired to the cloudflared tunnel).
Two code fixes land first: (1) the state-dir default is repointed from `LOCALAPPDATA` to the
repo-relative `<eta_engine>/state/` so every file read lands on the right directory; (2) the
JARVIS supervisor heartbeat is merged into `/api/bot-fleet` so the 16 supervisor bots appear in
the roster. A new VPS cutover script then stops any legacy process, re-registers the Eta-Dashboard
scheduled task, starts the new server, and health-checks it. `command_center/` is gitignored to
stop polluting `git status`.

**Tech stack:** FastAPI, uvicorn, Windows Scheduled Tasks, PowerShell, portalocker, pytest.

---

## Section 1 — Code changes to `dashboard_api.py`

### 1a. Fix state-dir default

**Problem:** The current default resolves to `%LOCALAPPDATA%\eta_engine\state`.
All supervisor/bot state is written to `<repo>/state/`. This mismatch means every
`_state_dir()` call reads an empty directory on the VPS.

**Fix:** Replace the `LOCALAPPDATA`-based default with a repo-relative path derived from `__file__`:

```python
_REPO_ROOT    = Path(__file__).resolve().parents[2]   # eta_engine/
_DEFAULT_STATE = _REPO_ROOT / "state"
_DEFAULT_LOG   = _REPO_ROOT / "logs"
```

`APEX_STATE_DIR` / `APEX_LOG_DIR` env-var overrides are preserved so tests stay clean.

### 1b. Supervisor merge in `/api/bot-fleet`

**Problem:** The endpoint only scans `state/bots/<name>/status.json`. The 16 JARVIS supervisor
bots write to `state/jarvis_intel/supervisor/heartbeat.json` and never touch `state/bots/`.

**Fix:** After building `rows` from `state/bots/`, import and call
`jarvis_supervisor_bot_accounts()` from `eta_engine.scripts.jarvis_supervisor_bridge`
(tracked module, repo-relative default path). Normalize each supervisor-bot dict into the
roster shape the frontend (`bot_fleet.js`) consumes:

| Supervisor field       | Roster field          | Notes                            |
|------------------------|-----------------------|----------------------------------|
| `id` / `name`          | `name`, `id`          |                                  |
| `symbol`               | `symbol`              |                                  |
| `strategy_kind`        | `tier`                | e.g. "orb", "hybrid"             |
| `broker` ("paper-sim") | `venue`               |                                  |
| `status`               | `status`              | "running" / "idle"               |
| `today.pnl`            | `todays_pnl`          |                                  |
| `updated_at`           | `last_trade_ts`       |                                  |
| computed age           | `last_trade_age_s`    | seconds since `updated_at`       |
| `open_position.side`   | `last_trade_side`     | or `direction` if no open pos    |
| `None`                 | `last_trade_r`        | not tracked by supervisor        |

Deduplication rule: if a bot name appears in both `state/bots/` AND the supervisor heartbeat,
the supervisor row wins (it is strictly newer live data).

Add `source: "jarvis_strategy_supervisor"` to each merged row for future UI badging.

### 1c. Add `confirmed_bots` to the `/api/bot-fleet` response

The response gains one top-level key:
```json
{ "bots": [...], "confirmed_bots": 16, "server_ts": ..., "live": {...} }
```
`confirmed_bots` = count of rows where `source == "jarvis_strategy_supervisor"` or
`confirmed == True`.

### 1d. New unit tests

File: `tests/test_dashboard_api.py`

- `test_bot_fleet_includes_supervisor_bots` — write a mock heartbeat under `tmp_path`,
  set `APEX_STATE_DIR=tmp_path`, call the endpoint, assert supervisor bots appear in
  `response["bots"]` with correct `todays_pnl` and `status`.
- `test_bot_fleet_default_state_dir_is_repo_relative` — import `_DEFAULT_STATE` from
  `dashboard_api`, assert it is a child of the `eta_engine/` directory and does NOT
  contain `AppData` or `Local`.

---

## Section 2 — VPS cutover script

**File:** `deploy/scripts/cutover_dashboard_b.ps1`

Steps the script executes in order:

1. **Git pull** — `git -C $EtaEngineDir pull --ff-only`
2. **Kill legacy port-8420 process** — find PID via `Get-NetTCPConnection -LocalPort 8420`,
   stop process; no-op if nothing is listening.
3. **Re-register `Eta-Dashboard` task** — call `register_operator_tasks.ps1` scoped to the
   `Eta-Dashboard` entry (or inline the registration so the cutover script is self-contained).
   Task command: `python -m uvicorn eta_engine.deploy.scripts.dashboard_api:app --host 127.0.0.1 --port 8420`
   Working dir: `$EtaEngineDir`. Trigger: AtStartup + immediate.
4. **Start task** — `Start-ScheduledTask -TaskName "Eta-Dashboard"`
5. **Health check loop** — poll `http://127.0.0.1:8420/health` every 2 s for up to 30 s;
   exit 1 if still down.
6. **Bot-fleet check** — `GET /api/bot-fleet`, print `confirmed_bots` count and first 5 bot
   names.
7. **Write receipt** — `state/ops/cutover_dashboard_b.json` with timestamp, confirmed_bots,
   git SHA.

---

## Section 3 — Decommission `command_center/`

- Add `command_center/` to `.gitignore` so the untracked directory no longer pollutes
  `git status`.
- The directory is left on disk for 48 h as a rollback safety net, then can be deleted
  manually.
- No code depends on `command_center/` in the tracked tree (the bridge was already
  extracted to `scripts/jarvis_supervisor_bridge.py`).

---

## Error handling

- If `jarvis_supervisor_bot_accounts()` throws (corrupt heartbeat, missing file), the
  existing silent-failure contract in the bridge applies: returns `[]`, roster still works
  with only `state/bots/` data.
- If `state/bots/` is empty AND supervisor returns `[]`, the response is
  `{"bots": [], "confirmed_bots": 0, ...}` — dashboard shows "no bots reporting" instead
  of a 500.
- Cutover script writes `state/ops/cutover_dashboard_b.json` with `"status": "failed"` and
  the error message if the health check loop times out, then exits 1.

---

## Testing strategy

All new tests are unit tests (no live server, no Playwright):
- `test_bot_fleet_includes_supervisor_bots` — mocks state dir + heartbeat, calls endpoint
  directly via `TestClient`.
- `test_bot_fleet_default_state_dir_is_repo_relative` — import-level assertion, runs in
  < 1 ms.
- Existing `test_jarvis_supervisor_bridge.py` (5 tests) already covers the bridge contract.
- Pre-commit hook runs all tests before the commit lands.

---

## Open questions (none — resolved in brainstorm)

- State dir default: repo-relative wins over LOCALAPPDATA. `APEX_STATE_DIR` override kept.
- Port: stays 8420. Cloudflared tunnel is already wired to 8420. No DNS change needed.
- Decommission timing: gitignore immediately; delete manually after 48 h.
