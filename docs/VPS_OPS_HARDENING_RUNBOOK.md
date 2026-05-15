# VPS Ops Hardening Runbook

This lane is read-only. It proves the VPS/control plane is alive, refreshes
operator-facing safety artifacts, and keeps promotion blocked until broker and
prop gates are clean. It never submits, cancels, flattens, or promotes orders.

## One-shot refresh

Run from `C:\EvolutionaryTradingAlgo`:

```powershell
python -m eta_engine.scripts.broker_bracket_audit --json
python -m eta_engine.scripts.supervisor_broker_reconcile_heartbeat --json
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
powershell -ExecutionPolicy Bypass -File eta_engine\deploy\scripts\register_crypto_dashboard_refresh_task.ps1 -Start
powershell -ExecutionPolicy Bypass -File eta_engine\deploy\scripts\register_index_futures_bar_refresh_task.ps1 -Start
powershell -ExecutionPolicy Bypass -File eta_engine\deploy\scripts\register_broker_state_refresh_task.ps1 -Start
powershell -ExecutionPolicy Bypass -File eta_engine\deploy\scripts\register_supervisor_broker_reconcile_task.ps1 -Start
powershell -ExecutionPolicy Bypass -File eta_engine\deploy\scripts\register_operator_queue_heartbeat_task.ps1 -Start
```

`ETA-VpsOpsHardeningAudit` refreshes the safety-gate audit.
`ETA-Crypto-Dashboard-Refresh` keeps the dashboard-watched
`data\BTC_5m.csv`, `data\ETH_5m.csv`, and `data\SOL_5m.csv` files fresh every
5 minutes via public Coinbase candles and writes
`crypto_dashboard_refresh_latest.json`.
`ETA-IndexFutures-Bar-Refresh` keeps the shadow-replay canonical
`mnq_data\history\NQ1_5m.csv` and `mnq_data\history\MNQ1_5m.csv` files fresh
every 10 minutes via the public yfinance fallback and writes
`index_futures_bar_refresh_latest.json`. This is market-data plumbing only; it
does not prove broker PnL or strategy edge.
`ETA-BrokerStateRefreshHeartbeat` warms the read-only IBKR broker-state cache
used for current PnL, MTD, fills, open positions, and EST reporting. It writes
`broker_state_refresh_heartbeat.json`. `ETA-SupervisorBrokerReconcile` refreshes
the read-only broker-vs-supervisor position artifact
`jarvis_intel\supervisor\reconcile_last.json` from current IBKR positions plus
the current supervisor heartbeat; it also writes
`supervisor_broker_reconcile_heartbeat.json`. `ETA-OperatorQueueHeartbeat`
refreshes the read-only operator queue snapshot that dashboard diagnostics use
for blocker counts and stale-state detection. These tasks never submit, cancel,
flatten, acknowledge, or promote orders.

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
positions and the supervisor book agree again. If the symbols look stale, run
`python -m eta_engine.scripts.supervisor_broker_reconcile_heartbeat --json`
first so the gate reflects current broker and supervisor truth before deciding
whether to flatten broker-only exposure or clear stale supervisor files.

If the dashboard shows stale broker PnL or `broker_snapshot_state=stale_persisted`,
start `ETA-BrokerStateRefreshHeartbeat` or run
`python -m eta_engine.scripts.broker_state_refresh_heartbeat --json`. This is a
read-only cache refresh against `/api/live/broker_state?refresh=1`; it must never
be used as an order-entry path.

If the dashboard shows `BTC`, `ETH`, or `SOL` bar-feed staleness while the main
IBKR/PAXOS accumulator is otherwise healthy, start
`ETA-Crypto-Dashboard-Refresh` or run
`python scripts/refresh_crypto_dashboard_bars.py --json`. That path only refreshes
the dashboard-facing 5-minute CSVs under `C:\EvolutionaryTradingAlgo\data`; it is
not an execution or routing path.

If shadow replay or the dashboard reports stale `NQ1_5m.csv` or `MNQ1_5m.csv`,
start `ETA-IndexFutures-Bar-Refresh` or run
`python eta_engine\scripts\refresh_index_futures_bars.py --json`. This refreshes
only canonical replay bars under `C:\EvolutionaryTradingAlgo\mnq_data\history`
and writes `index_futures_bar_refresh_latest.json`; it is not broker-backed PnL
proof and must not be used to clear promotion/order-entry gates by itself.
