# JARVIS Full Activation Guide

The wave-7-16 supercharge stack is **deployed but opt-in**. This guide covers the
final flip from JarvisAdmin-only mode to the full intelligence layer.

## What's Already Done (As Of 2026-04-27)

- All 61 modules deployed at `C:\EvolutionaryTradingAlgo\eta_engine\` on the VPS
- Python 3.12.10 venv with: pytest, numpy, pandas, pydantic, scikit-learn,
  polars, pyarrow, qiskit 2.4 + qiskit-aer + qiskit-algorithms,
  dwave-ocean-sdk 9.3, tzdata
- All 227 wave-7-16 + integration tests pass on VPS
- `state/jarvis_intel/` directories created
- `.env` synced from old install
- Genesis calibrator artifact written (will be replaced by real fit
  once trade history accumulates)
- `state/quantum/` directory created
- Scheduled task `ETA-Quantum-Daily-Rebalance` registered (fires 21:00 daily)
- Both cloud quantum backends verified REAL on the VPS:
  - D-Wave (`dwave.samplers.SimulatedAnnealingSampler`)
  - Qiskit (QAOA via `StatevectorSampler`)

## Finish-Line Hardening Added 2026-04-29

- JARVIS remains the live policy authority. The health endpoint now reports
  `policy_authority: JARVIS` plus the active feature-flag state for online
  learning, Sage modulation, and bandit live routing.
- `OnlineUpdater` is persistence-backed and fail-safe by default. With
  `ETA_FF_ONLINE_LEARNING=true`, bot pre-flight can shrink cold setup buckets
  after enough realized-R samples; it does not expand size unless a caller
  opts in explicitly.
- Sage ML no longer silently returns a neutral placeholder when no model file
  is present. It falls back to a deterministic conservative classifier and
  labels the verdict source as `deterministic_fallback`.
- Sage replay now accepts JSON/JSONL closed-trade journals and file-backed
  bar sources so school weights can be audited against real historical trades.
- Optional telemetry paths are wired through `MarketContext`: on-chain,
  funding/basis, options, and peer-return payloads survive memoization and
  regime rebuilds before reaching their schools.
- Quantum remains budget/credential-gated. `/api/jarvis/health` tails
  `state/quantum/jobs.jsonl` and reports recent jobs, fallbacks, and estimated
  cost so classical fallback is visible instead of hidden.
- Qiskit QAOA no longer fabricates an all-zero answer when the SDK returns no
  usable best measurement. Small QUBOs recover through exact enumeration;
  larger ones recover through the existing simulated-annealing verifier.
- `brain.rl_agent` no longer uses stochastic random exploration. The default
  baseline is a deterministic guardrail policy with transparent action-score
  feedback, so JARVIS/risk layers do not see random direction flips.
- `scripts/score_policy_candidate.py` now reports the registered-candidate
  replay lane honestly. `--candidate v18` is surfaced as active replay, while
  missing candidates are called out instead of mislabeled as scaffold status.
- `BarReplay.from_parquet(...)` now streams cached parquet through the shared
  loader with exact symbol filtering, so backtests can use local cache truth
  without touching dormant Databento/network refresh paths.
- Runtime helper defaults now write ETA state/logs under the canonical
  workspace (`var/eta_engine/state`, `logs/eta_engine`, or `var/cloudflare`)
  instead of per-user app-data folders, preserving the single-root contract.
- Deploy smoke/readiness now probes canonical workspace state/log directories
  and documents IBKR primary plus Tastytrade secondary as active broker setup;
  Tradovate credentials stay dormant-only.
- Decision-journal defaults now write operational GRADER/watchdog events to
  `var/eta_engine/state/decision_journal.jsonl`; live journal JSONL is runtime
  state and is ignored if a legacy docs copy is recreated locally.
- Runtime and alert-log defaults now write to `logs/eta_engine/runtime_log.jsonl`
  and `logs/eta_engine/alerts_log.jsonl`; cross-regime verification tests use
  explicit temp output so full gates do not churn tracked docs snapshots.
- Alert-log and runtime-log readers now prefer `logs/eta_engine/*.jsonl` with
  legacy `docs/*.jsonl` fallbacks, so diagnostics inspect live runtime truth
  without losing access to older snapshots.
- DR and repo-health diagnostics now resolve state/log paths through the same
  canonical helpers, including workspace-level runtime logs during failover
  backup/restore checks.
- The VPS failover drill now separates environment-limited bash availability
  from true `deploy/install_vps.sh` syntax failures, so Windows WSL launcher
  gaps do not masquerade as broken deploy code.
- The VPS failover idempotent-resume check now follows the live deterministic
  order-id router and `idempotent_order_id` preflight evidence instead of
  looking for broker-order semantics in the pure JARVIS VPS admin vocabulary.
- Drift-watchdog defaults now append to
  `var/eta_engine/state/drift_watchdog.jsonl`, and DR checks require canonical
  runtime/drift evidence instead of treating stale tracked-doc snapshots as
  live failover history.
- `scripts/runtime_log_smoke.py` can append a safe `runtime_smoke` row to
  `logs/eta_engine/runtime_log.jsonl` without starting bots or contacting
  brokers, giving DR/readiness checks a canonical runtime heartbeat.
- `scripts/drift_watchdog_smoke.py` can append a safe `drift_watchdog_smoke`
  row to `var/eta_engine/state/drift_watchdog.jsonl` without strategy replay
  or broker access, giving DR/readiness checks canonical drift-state evidence.
- The VPS failover drill now attaches `.env.example`, active/dormant broker
  key groups, and exact VPS `bash -n deploy/install_vps.sh` validation commands
  to the remaining operator/environment amber results.
- `scripts/vps_failover_summary.py` gives automation a read-only red/amber
  blocker summary with extracted next commands, without printing the full DR
  checklist or touching live broker/runtime state.
- `scripts/operator_action_queue.py --json` now includes `OP-18`, a dynamic
  VPS failover readiness item backed by the same summary payload, so heartbeat
  automation and dashboards can see DR blockers from the existing operator
  queue instead of scraping checklist text.
- `scripts/jarvis_status.py --json` now embeds a compact `operator_queue`
  snapshot with blocker counts and top actions, giving dashboards one JARVIS
  status call for both policy health and current operator blockers.
- The dashboard API now exposes `/api/jarvis/operator_queue` and embeds the
  same `operator_queue` block in `/api/dashboard`, so UI clients can render
  prioritized DR/operator blockers without shelling out or scraping logs.
- The bundled live dashboard now renders an `Operator Blockers` JARVIS panel
  plus a top-bar ops counter sourced from `/api/jarvis/operator_queue`, keeping
  DR blockers visible in the operator UI instead of buried in JSON.
- Operator-queue summaries now flatten blocker `next_actions`, and the default
  `jarvis_status` text output prints the blocker count plus top OP id, so both
  humans and dashboards see the same next-step lane.
- `scripts/operator_queue_snapshot.py` writes a canonical automation snapshot
  to `var/eta_engine/state/operator_queue_snapshot.json` with blocker counts,
  top OP id, and first next action, giving 10-minute wakeups a diffable status
  artifact without starting the dashboard server.
- The same snapshot writer preserves
  `var/eta_engine/state/operator_queue_snapshot.previous.json` and embeds a
  `drift` block comparing blocker count, top OP id, status, and first action,
  so heartbeat reports can distinguish unchanged blockers from new drift.
- `scripts/operator_queue_heartbeat.py --changed-only` wraps the canonical
  snapshot writer with a quiet-by-default notification payload, letting
  10-minute automation emit operator-queue alerts only when drift actually
  changes.
- `scripts/jarvis_dashboard.py` now exposes the JARVIS drift-watch panel from
  the promotion drift journal, including last verdict, KL, Sharpe delta, mean
  delta, sample counts, rolling verdict counts, and joined investigation
  reasons.
- JARVIS NL audit rollups now anchor relative windows to the latest audit-log
  timestamp when no explicit clock is supplied, so archived logs answer
  consistently instead of drifting with wall-clock time.
- The portfolio rebalancer now emits an auditable advisory plan that preserves
  total baseline budget by default, dampens highly correlated winners, and only
  mutates live bot sizing when `apply_rebalance_plan(..., dry_run=False)` is
  called against `BaseBot.set_equity_ceiling`.

## Activation: Wave-12 Intelligence Layer

The full intelligence stack (memory_rag, causal, world_model,
firm_board_debate, premortem, ood, operator_coach, risk_budget,
narrative) sits behind a single environment variable.

### Enable for the entire fleet

```powershell
# On the VPS, set the env var (persists across logins):
[System.Environment]::SetEnvironmentVariable(
    'ETA_USE_JARVIS_FULL', '1', 'User',
)
```

Or for the current session only:

```powershell
$env:ETA_USE_JARVIS_FULL = "1"
```

Or in `.env`:

```ini
ETA_USE_JARVIS_FULL=1
```

### What changes when enabled

Every bot's `_ask_jarvis()` call now routes through
`JarvisFull.consult()` which invokes:

1. operator_override check (HARD/SOFT/KILL pause)
2. JarvisAdmin.request_approval (existing v17/v22 sage logic preserved)
3. memory_rag analog episode lookup → cautions/boosts
4. causal_layer scoring (Granger + intervention)
5. world_model_full action ranking
6. firm_board_debate (3-round iterative debate)
7. premortem failure-mode enumeration → kill_prob
8. ood_detector novelty score → confidence attenuation
9. operator_coach override-pattern advice
10. risk_budget_allocator drawdown-aware envelope
11. narrative_generator prose summary (logged)
12. Persistent audit to `state/jarvis_intel/verdicts.jsonl`

The verdict is the same shape `(allowed, size_mult, reason_code)`
the bots already expect — no bot code changes required.

### Disable / rollback

```powershell
[System.Environment]::SetEnvironmentVariable(
    'ETA_USE_JARVIS_FULL', '', 'User',
)
```

Bots immediately fall back to JarvisAdmin-only mode.

## Activation: Real Cloud Quantum Hardware

Local simulators are active by default — no token needed. To use real
hardware:

### D-Wave Leap (recommended, free tier available)

```powershell
[System.Environment]::SetEnvironmentVariable(
    'DWAVE_API_TOKEN', '<your_leap_token>', 'User',
)
```

Then run the daily rebalance with `--enable-cloud`:

```powershell
cd C:\EvolutionaryTradingAlgo\eta_engine
.venv\Scripts\python.exe scripts\quantum_daily_rebalance.py --enable-cloud
```

To make the scheduled task always use real cloud, update its arguments:

```powershell
$task = Get-ScheduledTask -TaskName "ETA-Quantum-Daily-Rebalance"
$action = $task.Actions[0]
$action.Arguments = "$($action.Arguments) --enable-cloud"
Set-ScheduledTask -TaskName "ETA-Quantum-Daily-Rebalance" -Action $action
```

### IBM Quantum Cloud (paid)

```powershell
[System.Environment]::SetEnvironmentVariable(
    'QISKIT_IBM_TOKEN', '<your_ibm_token>', 'User',
)
```

The `cloud_adapter` will automatically dispatch to `EstimatorV2(mode=backend)`
on `least_busy(simulator=False)` when this token is set and `--enable-cloud`
is passed.

## Built-In Safeguards (Active Already)

The cloud_adapter has these on by default — no operator action needed:

| Guardrail | Default | Purpose |
|---|---|---|
| `max_cost_per_job_usd` | 0.50 | Per-job spend cap |
| `max_cost_per_day_usd` | 5.00 | Daily quantum budget |
| `classical_validate_cloud` | True | Cross-checks every cloud result against classical SA — uses classical if cloud's noisy result is worse |
| Result cache TTL | 24h | Don't pay twice for same problem |
| Audit log | always | `state/quantum/jobs.jsonl` records backend, runtime, cost estimate, fallback attribution |

## Rollout Recipe (Recommended)

**Stage 1 — Shadow (1 week)**
```powershell
# Don't set ETA_USE_JARVIS_FULL yet. Just observe:
ssh forex-vps "cd C:\EvolutionaryTradingAlgo\eta_engine; .venv\Scripts\python.exe -c 'from eta_engine.brain.jarvis_v3.health_check import jarvis_health; print(jarvis_health().summary)'"
```
Verify health stays OK and the running daemons are happy.

**Stage 2 — Annotated mode (1 week)**
```powershell
# Flip the env var; bots route through JarvisFull but it's conservative
# and won't downgrade JarvisAdmin verdicts unless causal_veto_can_downgrade
[System.Environment]::SetEnvironmentVariable('ETA_USE_JARVIS_FULL', '1', 'User')
```
Monitor `state/jarvis_intel/verdicts.jsonl` — every consultation gets logged.
Run daily-brief: `python -c "from eta_engine.brain.jarvis_v3.daily_brief import generate_daily_brief; print(generate_daily_brief().to_markdown())"`

**Stage 3 — Active modulation**
Once you trust the layer, enable causal-veto downgrades by editing
`brain/jarvis_v3/intelligence.py:IntelligenceConfig`:
```python
causal_veto_can_downgrade: bool = True
```
Now if causal score < threshold, the layer downgrades APPROVED → DEFERRED.

**Stage 4 — Live cloud quantum**
Set `DWAVE_API_TOKEN`, add `--enable-cloud` to the scheduled task argument.
Watch `state/quantum/jobs.jsonl` for the per-job cost ledger.

## Daily Operator Tools

```powershell
# Health check
.venv\Scripts\python.exe -c "from eta_engine.brain.jarvis_v3.health_check import jarvis_health; print(jarvis_health().summary)"

# Daily brief
.venv\Scripts\python.exe -c "from eta_engine.brain.jarvis_v3.daily_brief import generate_daily_brief; print(generate_daily_brief().to_markdown())"

# Self-drift monitor
.venv\Scripts\python.exe -c "from eta_engine.brain.jarvis_v3.self_drift_monitor import detect_self_drift; print(detect_self_drift().summary)"

# Recent verdicts breakdown
.venv\Scripts\python.exe -c "from eta_engine.brain.jarvis_v3.admin_query import recent_verdicts; r = recent_verdicts(n_hours=24); print(f'{r.n_total} verdicts, avg conf {r.avg_confidence}, by-verdict={r.by_final_verdict}')"

# Force a quantum rebalance now
Start-ScheduledTask -TaskName "ETA-Quantum-Daily-Rebalance"

# View latest allocation recommendation
type C:\EvolutionaryTradingAlgo\state\quantum\current_allocation.json
```

## Where Each Module Logs

| Module | Output |
|---|---|
| JarvisFull verdicts | `state/jarvis_intel/verdicts.jsonl` |
| Trade closes (feedback loop) | `state/jarvis_intel/trade_closes.jsonl` |
| Postmortems (auto-generated for losses ≤ -1.5R) | `state/jarvis_intel/postmortems/<signal_id>.md` |
| Daily briefs | `state/jarvis_intel/daily_briefs/<date>.md` |
| Operator override panel state | `state/operator_override.json` |
| Memory journal | `state/memory/episodes.jsonl` |
| Filter bandit posterior | `state/filter_bandit/posterior.json` |
| Quantum job audit | `state/quantum/jobs.jsonl` |
| Quantum daily allocation | `state/quantum/current_allocation.json` |
| Pre-live promotion decisions | `state/jarvis_intel/promotion_decisions.jsonl` |
| Open theses (thesis tracker) | `state/jarvis_intel/open_theses.json` |
| Thesis breaches | `state/jarvis_intel/thesis_breaches.jsonl` |
| A/B experiments | `state/jarvis_intel/ab_experiments.json` |
| Regression test cases | `state/jarvis_intel/regression_cases.json` |
