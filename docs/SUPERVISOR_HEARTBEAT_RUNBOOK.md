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
- `status=stale` means the canonical heartbeat exists but is older than the threshold, so inspect or restart `ETA-Jarvis-Strategy-Supervisor`.
- `status=missing` or `status=invalid` means the canonical heartbeat is absent or corrupt and the supervisor lane needs repair before trusting downstream dashboards.
