# VPS Ops Hardening Runbook

This lane is read-only. It proves the VPS/control plane is alive, refreshes
operator-facing safety artifacts, and keeps promotion blocked until broker and
prop gates are clean. It never submits, cancels, flattens, or promotes orders.

## One-shot refresh

Run from `C:\EvolutionaryTradingAlgo`:

```powershell
python -m eta_engine.scripts.broker_bracket_audit --json
python -m eta_engine.scripts.prop_strategy_promotion_audit --json
python -m eta_engine.scripts.vps_ops_hardening_audit --json-out --json
```

The canonical summary is written to:

```text
C:\EvolutionaryTradingAlgo\var\eta_engine\state\vps_ops_hardening_latest.json
```

## Scheduled refresh

Register the every-5-minute VPS audit task:

```powershell
powershell -ExecutionPolicy Bypass -File eta_engine\deploy\scripts\register_vps_ops_hardening_audit_task.ps1 -Start
```

The VPS bootstrap now registers the same task:

```powershell
powershell -ExecutionPolicy Bypass -File eta_engine\deploy\vps_bootstrap.ps1
```

## Status meanings

- `GREEN_READY_FOR_SOAK`: runtime is healthy, service config matches, and safety gates are ready for operator review.
- `YELLOW_SAFETY_BLOCKED`: runtime is healthy, but trading/promotion remains blocked by bracket, paper-soak, or prop readiness evidence.
- `YELLOW_RESTART_REQUIRED`: runtime is healthy, but tracked WinSW XML and installed service XML drifted and need an elevated restart/install.
- `RED_RUNTIME_DEGRADED`: a critical service, port, or endpoint is down.

## Current blocker rule

If the audit reports `promotion_allowed: false`, do not promote the strategy,
do not route live prop orders, and do not override holds. Clear the listed
`next_actions` first, then rerun the one-shot refresh.

As of the latest hardening refresh on 2026-05-11, runtime was healthy but the
trading lane stayed `YELLOW_SAFETY_BLOCKED` because broker-native bracket/OCO
proof was missing for `MCLM6`, `MNQM6`, `MYMM6`, `NGM26`, and `NQM6`, and the
primary strategy remained in paper soak.
