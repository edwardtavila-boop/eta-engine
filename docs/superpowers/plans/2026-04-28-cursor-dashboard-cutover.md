# Cursor Dashboard Full Cutover — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `dashboard_api.py` fully functional as the permanent operator surface at
`app.evolutionarytradingalgo.com` — correct state dir, all 16 JARVIS supervisor bots visible,
VPS cutover script to flip the switch, and the legacy `command_center/` gitignored.

**Architecture:** Three code changes land in `dashboard_api.py` (state-dir fix, supervisor merge,
`confirmed_bots` field). A self-contained PowerShell cutover script handles the VPS flip.
`command_center/` is added to `.gitignore`. All code changes are TDD with unit tests first.

**Tech Stack:** Python 3.12, FastAPI, pytest + TestClient, PowerShell 5.1, Windows Scheduled Tasks.

---

## File map

| Action | Path |
|--------|------|
| Modify | `deploy/scripts/dashboard_api.py` (lines 49–58, ~730–860) |
| Modify | `tests/test_dashboard_api.py` (add 2 tests to `TestDashboardAPI`) |
| Create | `deploy/scripts/cutover_dashboard_b.ps1` |
| Modify | `.gitignore` (add `command_center/`) |

---

## Task 1 — Fix state-dir default (TDD)

**Files:**
- Modify: `deploy/scripts/dashboard_api.py` (lines 49–58)
- Modify: `tests/test_dashboard_api.py` (add test to `TestDashboardAPI`)

---

- [ ] **Step 1 — Write the failing test**

Add this method inside `class TestDashboardAPI` in
`tests/test_dashboard_api.py`, after the last existing test:

```python
def test_default_state_dir_is_repo_relative(self):
    """_DEFAULT_STATE must be under the eta_engine repo, not LOCALAPPDATA."""
    from eta_engine.deploy.scripts.dashboard_api import _DEFAULT_STATE
    s = str(_DEFAULT_STATE).replace("\\", "/")
    assert "AppData" not in s, f"state dir leaked into AppData: {s}"
    assert "eta_engine" in s.lower(), f"state dir not under eta_engine: {s}"
```

- [ ] **Step 2 — Run to verify it fails**

```
cd C:\EvolutionaryTradingAlgo\eta_engine
python -m pytest tests/test_dashboard_api.py::TestDashboardAPI::test_default_state_dir_is_repo_relative -v
```

Expected: **FAILED** — `AssertionError: state dir leaked into AppData: …/AppData/Local/eta_engine/state`

- [ ] **Step 3 — Fix the state-dir default**

In `deploy/scripts/dashboard_api.py`, replace lines 49–58:

```python
# State/log dirs: Windows defaults; overridable via env
if os.name == "nt":
    _DEFAULT_STATE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "eta_engine" / "state"
    _DEFAULT_LOG = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "eta_engine" / "logs"
else:
    _DEFAULT_STATE = Path.home() / ".local" / "state" / "eta_engine"
    _DEFAULT_LOG = Path.home() / ".local" / "log" / "eta_engine"

STATE_DIR = Path(os.environ.get("APEX_STATE_DIR", _DEFAULT_STATE))
LOG_DIR = Path(os.environ.get("APEX_LOG_DIR", _DEFAULT_LOG))
```

with:

```python
# State/log dirs: repo-relative so every deployment reads the right directory.
# APEX_STATE_DIR / APEX_LOG_DIR env vars still override (used by tests).
_REPO_ROOT     = Path(__file__).resolve().parents[2]   # .../eta_engine/
_DEFAULT_STATE = _REPO_ROOT / "state"
_DEFAULT_LOG   = _REPO_ROOT / "logs"

STATE_DIR = Path(os.environ.get("APEX_STATE_DIR", str(_DEFAULT_STATE)))
LOG_DIR   = Path(os.environ.get("APEX_LOG_DIR",   str(_DEFAULT_LOG)))
```

- [ ] **Step 4 — Run the test to verify it passes**

```
python -m pytest tests/test_dashboard_api.py::TestDashboardAPI::test_default_state_dir_is_repo_relative -v
```

Expected: **PASSED**

- [ ] **Step 5 — Run full suite to check for regressions**

```
python -m pytest tests/test_dashboard_api.py -x -q
```

Expected: all existing tests still pass (they set `APEX_STATE_DIR` via the `app_client` fixture so the new default doesn't interfere).

- [ ] **Step 6 — Commit**

```
git add deploy/scripts/dashboard_api.py tests/test_dashboard_api.py
git commit -m "fix(dashboard): state-dir default → repo-relative path"
```

---

## Task 2 — Supervisor merge in `/api/bot-fleet` (TDD)

**Files:**
- Modify: `deploy/scripts/dashboard_api.py` (~line 730 and ~line 742–860)
- Modify: `tests/test_dashboard_api.py`

---

- [ ] **Step 1 — Write the failing test**

Add this method inside `class TestDashboardAPI` in `tests/test_dashboard_api.py`:

```python
def test_bot_fleet_includes_supervisor_bots(self, app_client, tmp_path):
    """Supervisor heartbeat bots appear in /api/bot-fleet even when state/bots/ is empty."""
    import os, json
    from pathlib import Path

    state = Path(os.environ["APEX_STATE_DIR"])
    # Ensure state/bots/ exists but is empty (no legacy bots)
    (state / "bots").mkdir(parents=True, exist_ok=True)

    # Write supervisor heartbeat
    sup_dir = state / "jarvis_intel" / "supervisor"
    sup_dir.mkdir(parents=True, exist_ok=True)
    hb = {
        "ts": "2026-04-28T12:00:00+00:00",
        "mode": "paper_sim",
        "bots": [
            {
                "bot_id": "mnq_futures",
                "symbol": "MNQ1",
                "strategy_kind": "orb",
                "direction": "long",
                "n_entries": 5,
                "n_exits": 5,
                "realized_pnl": 2.0,
                "open_position": None,
                "last_jarvis_verdict": "APPROVED",
                "last_bar_ts": "2026-04-28T12:00:00+00:00",
            },
            {
                "bot_id": "btc_hybrid",
                "symbol": "BTC",
                "strategy_kind": "hybrid",
                "direction": "long",
                "n_entries": 2,
                "n_exits": 1,
                "realized_pnl": -0.5,
                "open_position": {"side": "BUY", "entry_price": 67000.0},
                "last_jarvis_verdict": "CONDITIONAL",
                "last_bar_ts": "2026-04-28T12:00:00+00:00",
            },
        ],
    }
    (sup_dir / "heartbeat.json").write_text(json.dumps(hb))

    r = app_client.get("/api/bot-fleet")
    assert r.status_code == 200
    data = r.json()
    names = [b["name"] for b in data["bots"]]
    assert "mnq_futures" in names, f"mnq_futures missing from roster: {names}"
    assert "btc_hybrid" in names, f"btc_hybrid missing from roster: {names}"

    mnq = next(b for b in data["bots"] if b["name"] == "mnq_futures")
    assert mnq["todays_pnl"] == 2.0
    assert mnq["status"] == "running"
    assert mnq["source"] == "jarvis_strategy_supervisor"
    assert mnq["venue"] == "paper-sim"
    assert mnq["tier"] == "orb"

    assert data["confirmed_bots"] == 2
```

- [ ] **Step 2 — Run to verify it fails**

```
python -m pytest tests/test_dashboard_api.py::TestDashboardAPI::test_bot_fleet_includes_supervisor_bots -v
```

Expected: **FAILED** — supervisor bots not in response / `confirmed_bots` key missing.

- [ ] **Step 3 — Add `_sup_bot_to_roster_row` helper**

In `deploy/scripts/dashboard_api.py`, add this function just **before** the line
`@app.get("/api/bot-fleet")` (around line 733):

```python
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
        "id":                   str(sup.get("id") or ""),
        "name":                 str(sup.get("name") or ""),
        "symbol":               str(sup.get("symbol") or ""),
        "tier":                 str(sup.get("strategy") or ""),
        "venue":                str(sup.get("broker") or "paper-sim"),
        "status":               str(sup.get("status") or "unknown"),
        "todays_pnl":           float(today.get("pnl") or 0.0),
        "todays_pnl_source":    "supervisor_heartbeat",
        "last_trade_ts":        updated_at or None,
        "last_trade_age_s":     last_trade_age_s,
        "last_trade_side":      last_side,
        "last_trade_r":         None,
        "last_trade_qty":       None,
        "data_ts":              now_ts,
        "data_age_s":           0.0,
        "heartbeat_age_s":      last_trade_age_s,
        "source":               "jarvis_strategy_supervisor",
        "confirmed":            True,
        "mode":                 str(sup.get("mode") or ""),
        "last_jarvis_verdict":  str(sup.get("last_jarvis_verdict") or ""),
    }
```

- [ ] **Step 4 — Refactor early-return in `bot_fleet_roster` and add supervisor merge**

In `deploy/scripts/dashboard_api.py`, inside `bot_fleet_roster`, replace the section:

```python
    bots_dir = _state_dir() / "bots"
    if not bots_dir.exists():
        return {"bots": []}
```

with:

```python
    bots_dir = _state_dir() / "bots"
```

Then, at the end of `bot_fleet_roster`, replace the `return` statement:

```python
    return {
        "bots": rows,
        "server_ts": now_ts,
        "live": fills_stats,
        "window_since_days": since_days,
    }
```

with:

```python
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
        "bots":             rows,
        "confirmed_bots":   confirmed_bots,
        "server_ts":        now_ts,
        "live":             fills_stats,
        "window_since_days": since_days,
    }
```

Also update the early-return guard (the `if not bots_dir.exists(): return {"bots": []}` that you removed) so the function still handles missing `bots/` without crashing. The for-loop already checks `if not bot_dir.is_dir(): continue`, and `bots_dir.iterdir()` raises `FileNotFoundError` if the dir doesn't exist. Wrap the loop:

Find the line `for bot_dir in sorted(bots_dir.iterdir()):` and change it to:

```python
    for bot_dir in (sorted(bots_dir.iterdir()) if bots_dir.exists() else []):
```

- [ ] **Step 5 — Run the new test**

```
python -m pytest tests/test_dashboard_api.py::TestDashboardAPI::test_bot_fleet_includes_supervisor_bots -v
```

Expected: **PASSED**

- [ ] **Step 6 — Run full suite**

```
python -m pytest tests/test_dashboard_api.py -x -q
```

Expected: all existing tests still pass.

- [ ] **Step 7 — Commit**

```
git add deploy/scripts/dashboard_api.py tests/test_dashboard_api.py
git commit -m "feat(dashboard): merge JARVIS supervisor bots into /api/bot-fleet roster"
```

---

## Task 3 — Write the VPS cutover script

**Files:**
- Create: `deploy/scripts/cutover_dashboard_b.ps1`

---

- [ ] **Step 1 — Create the script**

Create `deploy/scripts/cutover_dashboard_b.ps1` with the following content:

```powershell
# deploy/scripts/cutover_dashboard_b.ps1
# ============================================================
# Stage 2 cutover: make dashboard_api.py the live operator surface.
#
# Run on the VPS from the eta_engine repo root:
#   powershell -ExecutionPolicy Bypass -File deploy\scripts\cutover_dashboard_b.ps1
# ============================================================
$ErrorActionPreference = "Stop"
$EtaEngineDir = $PSScriptRoot | Split-Path -Parent | Split-Path -Parent
$Python = Join-Path $EtaEngineDir ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

function Write-Log  { param($m) Write-Host "[cutover] $m" -ForegroundColor Cyan }
function Write-OK   { param($m) Write-Host "[ OK  ] $m" -ForegroundColor Green }
function Write-Fail { param($m) Write-Host "[FAIL ] $m" -ForegroundColor Red }
function Die        { param($m) Write-Fail $m; exit 1 }

# ------------------------------------------------------------------
# 1. Git pull
# ------------------------------------------------------------------
Write-Log "Step 1: git pull..."
$pull = & git -C $EtaEngineDir pull --ff-only 2>&1 | Out-String
Write-Log $pull.Trim()
$sha = & git -C $EtaEngineDir rev-parse --short HEAD
Write-OK "HEAD is now $sha"

# ------------------------------------------------------------------
# 2. Kill whatever is on port 8420
# ------------------------------------------------------------------
Write-Log "Step 2: clearing port 8420..."
$conns = Get-NetTCPConnection -LocalPort 8420 -State Listen -ErrorAction SilentlyContinue
if ($conns) {
    foreach ($c in $conns) {
        $pid_ = $c.OwningProcess
        Write-Log "  killing PID $pid_ on port 8420"
        Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 3
    $still = Get-NetTCPConnection -LocalPort 8420 -State Listen -ErrorAction SilentlyContinue
    if ($still) { Die "port 8420 still occupied after kill" }
    Write-OK "port 8420 cleared"
} else {
    Write-Log "  port 8420 was already free"
}

# ------------------------------------------------------------------
# 3. Re-register Eta-Dashboard scheduled task
# ------------------------------------------------------------------
Write-Log "Step 3: registering Eta-Dashboard task..."
$taskName = "Eta-Dashboard"
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "-m uvicorn eta_engine.deploy.scripts.dashboard_api:app --host 127.0.0.1 --port 8420" `
    -WorkingDirectory $EtaEngineDir

$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -User $env:USERNAME `
    -RunLevel Limited | Out-Null
Write-OK "task $taskName registered"

# ------------------------------------------------------------------
# 4. Start the task immediately
# ------------------------------------------------------------------
Write-Log "Step 4: starting $taskName..."
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 4
Write-OK "task started"

# ------------------------------------------------------------------
# 5. Health-check loop (30 s)
# ------------------------------------------------------------------
Write-Log "Step 5: health check..."
$up = $false
for ($i = 0; $i -lt 15; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8420/health" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $up = $true; break }
    } catch {}
    Start-Sleep -Seconds 2
}
if (-not $up) { Die "/health never returned 200 after 30 s — check logs" }
Write-OK "/health OK"

# ------------------------------------------------------------------
# 6. Bot-fleet check
# ------------------------------------------------------------------
Write-Log "Step 6: bot-fleet check..."
try {
    $r2 = Invoke-WebRequest -Uri "http://127.0.0.1:8420/api/bot-fleet" -UseBasicParsing -TimeoutSec 5
    $j  = $r2.Content | ConvertFrom-Json
    $n  = $j.confirmed_bots
    $names = ($j.bots | Select-Object -ExpandProperty name) -join ", "
    Write-OK "confirmed_bots=$n  bots=[$names]"
} catch {
    Write-Fail "/api/bot-fleet error: $_"
    Write-Log  "Dashboard is live but supervisor data may be missing — check heartbeat path."
}

# ------------------------------------------------------------------
# 7. Write cutover receipt
# ------------------------------------------------------------------
Write-Log "Step 7: writing receipt..."
$stateDir = Join-Path $EtaEngineDir "state\ops"
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$receipt = @{
    event       = "cutover_dashboard_b"
    ts          = (Get-Date).ToString("o")
    git_sha     = $sha
    port        = 8420
    task        = $taskName
    confirmed_bots = if ($n) { $n } else { "unknown" }
    status      = "success"
} | ConvertTo-Json
Set-Content -Path (Join-Path $stateDir "cutover_dashboard_b.json") -Value $receipt -Encoding UTF8
Write-OK "receipt written to state/ops/cutover_dashboard_b.json"

Write-Host ""
Write-Host "======================================================" -ForegroundColor Green
Write-Host "  Cutover complete.  app.evolutionarytradingalgo.com  " -ForegroundColor Green
Write-Host "  is now served by dashboard_api.py on port 8420.     " -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Green
```

- [ ] **Step 2 — Commit**

```
git add deploy/scripts/cutover_dashboard_b.ps1
git commit -m "feat(deploy): VPS cutover script for dashboard_b"
```

---

## Task 4 — Gitignore `command_center/`

**Files:**
- Modify: `.gitignore`

---

- [ ] **Step 1 — Add the entry**

Open `.gitignore` in the repo root and append:

```
# Stage-2 decommission: legacy command_center (VPS-only, untracked)
command_center/
```

- [ ] **Step 2 — Verify `git status` is cleaner**

```
git status --short
```

Expected: `command_center/` no longer appears as `??`.

- [ ] **Step 3 — Commit**

```
git add .gitignore
git commit -m "chore: gitignore legacy command_center/ directory"
```

---

## Task 5 — Push and run the full test suite

**Files:** none changed here — this is verification.

---

- [ ] **Step 1 — Run the complete pre-commit suite locally**

```
cd C:\EvolutionaryTradingAlgo\eta_engine
python -m pytest -x -q --no-header
```

Expected: all tests pass (≥ 4502 passed, ≤ 35 skipped).

- [ ] **Step 2 — Push the branch**

```
git push origin claude/review-progress-ykCsb
```

---

## Task 6 — VPS deployment

**Prerequisites:** VPS reachable via RDP. Python venv active. Repo cloned at
`C:\EvolutionaryTradingAlgo\eta_engine`.

---

- [ ] **Step 1 — RDP into the VPS**

Open Remote Desktop Connection to the VPS IP. Log in as the usual user.

- [ ] **Step 2 — Open PowerShell in the repo root**

```powershell
cd C:\EvolutionaryTradingAlgo\eta_engine
```

- [ ] **Step 3 — Run the cutover script**

```powershell
powershell -ExecutionPolicy Bypass -File deploy\scripts\cutover_dashboard_b.ps1
```

Expected output (abridged):
```
[ OK  ] HEAD is now <sha>
[ OK  ] port 8420 cleared
[ OK  ] task Eta-Dashboard registered
[ OK  ] task started
[ OK  ] /health OK
[ OK  ] confirmed_bots=16  bots=[mnq_futures, btc_hybrid, ...]
[ OK  ] receipt written to state/ops/cutover_dashboard_b.json
======================================================
  Cutover complete.  app.evolutionarytradingalgo.com
  is now served by dashboard_api.py on port 8420.
======================================================
```

- [ ] **Step 4 — Smoke-test in a browser**

Visit `https://app.evolutionarytradingalgo.com`.

Verify:
- Login page loads
- After login, Bot Fleet Roster panel shows ≥ 16 bots
- Bot names include `mnq_futures` (or equivalent supervisor bot IDs)
- Day PnL and status fields are populated (not blank / "—")

- [ ] **Step 5 — Check confirmed_bots via curl (optional)**

From the VPS terminal:
```powershell
(Invoke-WebRequest "http://127.0.0.1:8420/api/bot-fleet").Content |
  ConvertFrom-Json | Select-Object confirmed_bots, @{n='count';e={$_.bots.Count}}
```

Expected: `confirmed_bots` ≥ 16, `count` ≥ 16.

---

## Rollback plan

If something breaks after the cutover script:

1. Find and kill the new process on 8420:
   ```powershell
   Get-NetTCPConnection -LocalPort 8420 | % { Stop-Process -Id $_.OwningProcess -Force }
   ```
2. Start the old command_center manually:
   ```powershell
   cd C:\EvolutionaryTradingAlgo\eta_engine
   python -m uvicorn command_center.server.app:app --host 127.0.0.1 --port 8420
   ```
3. Debug dashboard_api logs:
   ```powershell
   Get-Content $env:LOCALAPPDATA\eta_engine\logs\dashboard_api.log -Tail 50
   ```
