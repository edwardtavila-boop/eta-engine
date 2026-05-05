# Codex Overnight Operator

Codex is installed as a conservative operator lane for unattended windows. It
does not place trades, mutate live routing, or run git. Its job is to keep the
AI coordination layer alive, reclaim stale AI tasks, run/read health evidence,
and leave a clear canonical report for the next attended batch.

## VPS Registration

Run from the VPS as Administrator:

```powershell
cd C:\EvolutionaryTradingAlgo\eta_engine\deploy
pwsh -ExecutionPolicy Bypass -File .\vps_bootstrap.ps1
```

The bootstrap calls `deploy\scripts\register_codex_operator_task.ps1`, which
registers:

- `ETA-Codex-Overnight-Operator` every 10 minutes.
- `ETA-ThreeAI-Sync` every 4 hours.

## Truth Surfaces

- Latest Codex report:
  `C:\EvolutionaryTradingAlgo\var\eta_engine\state\codex_operator\codex_operator_latest.json`
- Codex report history:
  `C:\EvolutionaryTradingAlgo\var\eta_engine\state\codex_operator\codex_operator_history.jsonl`
- Three-AI task queue:
  `C:\EvolutionaryTradingAlgo\var\eta_engine\state\agent_coordination`
- Three-AI sync history:
  `C:\EvolutionaryTradingAlgo\var\eta_engine\state\three_ai_coordination.jsonl`

## Manual One-Shot

```powershell
cd C:\EvolutionaryTradingAlgo\eta_engine
python scripts\codex_overnight_operator.py --json
```

Use `--no-health` for a fast coordination-only smoke.
