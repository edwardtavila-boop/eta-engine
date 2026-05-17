# Supervisor Heartbeat Runbook

The live Jarvis strategy supervisor heartbeat is canonical at:

```text
C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\supervisor\heartbeat.json
```

Older dashboards or manual checks can accidentally inspect generic legacy paths like
`state\supervisor\heartbeat.json`. Treat those as diagnostic hints only; the Jarvis
supervisor truth path above is authoritative.

## Quick Check

From `C:\EvolutionaryTradingAlgo\eta_engine`:

```powershell
python scripts\supervisor_heartbeat_check.py --json --write-report
```

The command writes the latest diagnostic to:

```text
C:\EvolutionaryTradingAlgo\var\eta_engine\state\health\supervisor_heartbeat_check_latest.json
```

On the VPS, run the same check through SSH:

```powershell
ssh forex-vps "cd C:\EvolutionaryTradingAlgo\eta_engine; python scripts\supervisor_heartbeat_check.py --json --write-report"
```

## Reading The Result

- `status=fresh` with `diagnosis=canonical_fresh_legacy_path_mismatch` means the supervisor is healthy and the stale warning came from a wrong path.
- `status=main_loop_stuck` means the independent keepalive is fresh but `heartbeat.json` stopped advancing. The process is alive, but the trade tick loop is blocked and the ETA watchdog should restart the paper supervisor.
- `status=paper_main_loop_stuck` means the managed paper-sim service is alive under `supervisor_mock`, but its main tick loop is blocked. This is the paper-soak variant of `main_loop_stuck`.
- `status=stale` means the canonical heartbeat exists but is older than the threshold, so inspect or restart the `ETAJarvisSupervisor` WinSW service. Use the scheduled task `ETA-Jarvis-Strategy-Supervisor` only as the fallback lane when the service is not installed.
- `status=missing` or `status=invalid` means the canonical heartbeat is absent or corrupt and the supervisor lane needs repair before trusting downstream dashboards.

## Always-On Paper Self-Heal

Register the long-running watchdog during VPS bootstrap, or repair it directly:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\scripts\register_eta_watchdog_task.ps1 -Root C:\EvolutionaryTradingAlgo -Start -RestartExistingProcess
```

Run the registration from an elevated shell because the task is installed as `NT AUTHORITY\SYSTEM`, which is what gives the watchdog permission to restart the WinSW supervisor service.

The watchdog treats `heartbeat.json` as the progress signal. A fresh `heartbeat_keepalive.json` proves the process is alive, but it no longer masks a stuck main loop. Paper-mode supervisors are restarted automatically; live-money restarts remain fail-closed unless the operator explicitly sets `ETA_WATCHDOG_ALLOW_LIVE_RESTART=1`.
