# Sage Live Activation Runbook

> **What this is:** the single document the operator follows to flip the
> 23-school sage modulator on for live trading. Conservative by design —
> ladder of safety with rollback at every step.

**Owner:** Edward Avila · **Created:** 2026-04-27 · **Last drill:** _none yet_

---

## Pre-flight checklist (must all be GREEN before flipping anything)

| # | Check | Command | Pass criteria |
|---|---|---|---|
| 1 | Tests pass on the active branch | `python -m pytest eta_engine/tests/ -k "sage or jarvis" -q` | exit 0, no FAIL |
| 2 | Lint clean | `ruff check eta_engine/brain/jarvis_admin.py eta_engine/brain/jarvis_v3 eta_engine/bots/base_bot.py` | "All checks passed!" |
| 3 | Sage upkeep tasks installed on VPS | `Get-ScheduledTask Eta-Sage-OnChain-Warm,Eta-Sage-Health-Daily` | both `Ready` |
| 4 | On-chain warmer has run at least once | `Get-Content state\sage\last_health_report.json` (or any non-empty `state\sage\*.json`) | file exists, valid JSON |
| 5 | Edge tracker file exists (or absent — both OK) | `Test-Path state\sage\edge_tracker.json` | True OR False (no error) |
| 6 | Champion v17 is the live policy | `python -c "from eta_engine.brain.feature_flags import is_enabled; print(is_enabled('V22_SAGE_MODULATION'))"` | `False` |

**If any check fails, STOP. Fix it before proceeding.**

---

## Stage 1 — Paper-only burn-in (24h minimum)

**Goal:** verify v22 produces sensible verdicts on every order without affecting live capital.

```powershell
# 1. Set the flag for the CURRENT shell only (not persisted)
$env:ETA_FF_V22_SAGE_MODULATION = "true"

# 2. Run the paper fleet only (NOT live brokers)
python -m eta_engine.scripts.btc_broker_fleet --paper-only --start

# 3. Tail the JARVIS audit log -- look for v22 verdicts
Get-Content state\jarvis_audit\$(Get-Date -Format 'yyyy-MM-dd').jsonl -Wait |
    Select-String -Pattern '"reason":"[^"]*\[v22 sage'
```

**Pass criteria after 24h:**
- At least 50 ORDER_PLACE requests evaluated through v22 (visible in audit log)
- ZERO uncaught exceptions in `state/logs/eta.jsonl` or stderr
- At least one `[v22 sage agrees ...]` verdict (proves agree path works)
- At least one `[v22 sage disagrees ...]` verdict (proves disagree path works)
- Sage health snapshot (`state/sage/last_health_report.json`) shows zero `critical` issues

**If pass:** proceed to Stage 2.
**If fail:** revert (`Remove-Item Env:\ETA_FF_V22_SAGE_MODULATION`), capture the failing audit line, file a kaizen ticket.

---

## Stage 2 — Single-bot live trial (MNQ only, 48h)

**Goal:** prove sage modulation under a live broker for ONE bot before extending to the fleet.

```powershell
# 1. Persist the flag for the user (survives reboot)
[System.Environment]::SetEnvironmentVariable(
    'ETA_FF_V22_SAGE_MODULATION', 'true', 'User'
)

# 2. Stop ALL fleet daemons
Stop-ScheduledTask -TaskName 'Apex-BTC-Fleet'
Stop-ScheduledTask -TaskName 'Apex-MNQ-Supervisor' -ErrorAction SilentlyContinue

# 3. Start ONLY the MNQ supervisor (live IBKR paper)
Start-ScheduledTask -TaskName 'Apex-MNQ-Supervisor'

# 4. Watch order flow for 48h
Get-Content state\jarvis_audit\$(Get-Date -Format 'yyyy-MM-dd').jsonl -Wait |
    Select-String 'BOT_MNQ.*ORDER_PLACE'
```

**Pass criteria after 48h:**
- MNQ bot placed at least one order
- v22 agreed-loosen and/or disagreed-tighten verdicts both observed
- No DENIED verdicts caused by v22 errors (look for `reason_code=v22_*_error`)
- Realized R distribution UNCHANGED at p=0.05 vs prior 30-day baseline (no sage-induced selection effect)
- Sage health snapshot still shows zero `critical` issues

**If pass:** proceed to Stage 3.
**If fail:** flip the flag off (`[System.Environment]::SetEnvironmentVariable('ETA_FF_V22_SAGE_MODULATION', 'false', 'User')`), restart MNQ supervisor, capture the audit lines that caused the failure.

---

## Stage 3 — Full fleet activation

```powershell
# 1. Confirm flag is True
[System.Environment]::GetEnvironmentVariable('ETA_FF_V22_SAGE_MODULATION', 'User')

# 2. Restart every fleet daemon to pick up the env var
Stop-ScheduledTask -TaskName 'Apex-BTC-Fleet'
Start-ScheduledTask -TaskName 'Apex-BTC-Fleet'
Start-ScheduledTask -TaskName 'Apex-MNQ-Supervisor'

# 3. Verify all 7 bots are on the new path
python -c "from eta_engine.brain.feature_flags import ETA_FLAGS; import json; print(json.dumps(ETA_FLAGS.snapshot(), indent=2))"
```

**Steady-state monitoring (every 24h for first week):**

| Cadence | Check | Trigger to investigate |
|---|---|---|
| Hourly | Audit log v22 firings count | <10/h during US RTH |
| Daily | `state/sage/last_health_report.json` issues count | any `critical` |
| Daily | Edge tracker per-school n_obs growth | any school stuck at n_obs=0 |
| Weekly | v22 agree-loosen + disagree-tighten ratio | drift > 2x WoW |

---

## Emergency rollback (always available, <30s)

```powershell
# 1. Flip the flag off (no commit, no redeploy)
[System.Environment]::SetEnvironmentVariable('ETA_FF_V22_SAGE_MODULATION', 'false', 'User')

# 2. Restart the affected daemon(s)
Stop-ScheduledTask -TaskName 'Apex-BTC-Fleet'; Start-ScheduledTask -TaskName 'Apex-BTC-Fleet'
Stop-ScheduledTask -TaskName 'Apex-MNQ-Supervisor'; Start-ScheduledTask -TaskName 'Apex-MNQ-Supervisor'

# 3. Verify v17 is back in the live path
python -c "from eta_engine.brain.feature_flags import is_enabled; assert not is_enabled('V22_SAGE_MODULATION'), 'STILL ON'"
```

The flag is read fresh on every `request_approval` call so the next ORDER_PLACE after the daemon restart uses v17 again. No code is reverted; no commit is made; the change is purely an env-var flip.

---

## What to escalate

- **Crash loop in `JarvisAdmin.request_approval`:** route is wrapped in try/except that falls back to v17, but if you see repeated `v22_sage_confluence raised XXX -- falling back to v17` warnings, file a high-priority kaizen ticket and turn the flag off.
- **Sage health report shows >1 critical school for >24h:** the `Eta-Sage-Health-Daily` task already alerts (when alerter wired); if not wired yet, manually check `state/sage/last_health_report.json` once a day.
- **Edge tracker file growing >10 MB:** rotation isn't built yet; archive manually with `Move-Item state\sage\edge_tracker.json state\sage\_archive_$(Get-Date -Format 'yyyy-MM-dd')\edge_tracker.json` then restart any running fleet daemon.

---

## Reference: what v22 actually does

When the flag is on and `payload['sage_bars']` has >= 30 bars:

1. Calls v17 (champion) first.
2. If v17 returned `APPROVED` or `CONDITIONAL`, runs the 23-school sage on the bars.
3. If sage **conviction >= 0.65 and aligned >= 0.70 and v17 said CONDITIONAL** → loosens the size cap by 1.2x (up to 1.0).
4. If sage **conviction >= 0.65 and aligned <= 0.30** → tightens to 0.30 if v17 said APPROVED, or DEFERS if v17 said CONDITIONAL.
5. Otherwise → returns v17 verdict unchanged.

Sage falls back to v17 silently if:
- `sage_bars` missing / non-list / fewer than 30 bars
- Sage consultation raises (logged at WARNING)
- v17 already said DENIED or DEFERRED (sage doesn't second-guess kills)
