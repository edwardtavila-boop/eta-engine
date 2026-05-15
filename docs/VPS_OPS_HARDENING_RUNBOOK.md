# VPS Ops Hardening Runbook

This lane is read-only. It proves the VPS/control plane is alive, refreshes
operator-facing safety artifacts, and keeps promotion blocked until broker and
prop gates are clean. It never submits, cancels, flattens, or promotes orders.

## One-shot refresh

Run from `C:\EvolutionaryTradingAlgo`:

```powershell
python -m eta_engine.scripts.broker_bracket_audit --json
python -m eta_engine.scripts.prop_strategy_promotion_audit --json
python -m eta_engine.scripts.operator_queue_heartbeat --cached-readiness --changed-only
python -m eta_engine.scripts.vps_ops_hardening_audit --json-out --json
```

The canonical summaries are written to:

```text
C:\EvolutionaryTradingAlgo\var\eta_engine\state\vps_ops_hardening_latest.json
C:\EvolutionaryTradingAlgo\var\eta_engine\state\operator_queue_snapshot.json
```

## Scheduled refresh

Register the every-5-minute VPS audit task:

```powershell
powershell -ExecutionPolicy Bypass -File eta_engine\deploy\scripts\register_vps_ops_hardening_audit_task.ps1 -Start
powershell -ExecutionPolicy Bypass -File eta_engine\deploy\scripts\register_broker_state_refresh_task.ps1 -Start
powershell -ExecutionPolicy Bypass -File eta_engine\deploy\scripts\register_operator_queue_heartbeat_task.ps1 -Start
```

`ETA-VpsOpsHardeningAudit` refreshes the safety-gate audit.
`ETA-BrokerStateRefreshHeartbeat` warms the read-only IBKR broker-state cache
used for current PnL, MTD, fills, open positions, and EST reporting. It writes
`broker_state_refresh_heartbeat.json`. `ETA-OperatorQueueHeartbeat` refreshes
the read-only operator queue snapshot that dashboard diagnostics use for blocker
counts and stale-state detection. These tasks never submit, cancel, flatten, or
promote orders.

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

If the bracket audit reports `READY_NO_OPEN_EXPOSURE` but the dashboard still
shows stale operator blockers, refresh `ETA-OperatorQueueHeartbeat` before
debugging the UI. A stale queue snapshot is a truth-surface problem, not
permission to bypass broker, paper-soak, or prop drawdown gates.

If `supervisor_reconcile.status` reports
`BLOCKED_BROKER_SUPERVISOR_RECONCILE`, the VPS runtime can be healthy while new
entries stay intentionally halted. Treat the listed `broker_only_symbols`,
`supervisor_only_symbols`, and `divergent_symbols` as the first human action;
do not clear entry holds or promotion gates until IBKR/Tastytrade broker
positions and the supervisor book agree again.

If the dashboard shows stale broker PnL or `broker_snapshot_state=stale_persisted`,
start `ETA-BrokerStateRefreshHeartbeat` or run
`python -m eta_engine.scripts.broker_state_refresh_heartbeat --json`. This is a
read-only cache refresh against `/api/live/broker_state?refresh=1`; it must never
be used as an order-entry path.
