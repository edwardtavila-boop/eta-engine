# L2 Live Cutover Runbook

**Goal:** Walk an L2 strategy from subscription-active → first live trade in a controlled, reversible sequence.

**Time horizon:** ~30 days from subscription activation to first live trade for any strategy.

**Stop conditions:** Any step that fails → halt and triage. Do not skip steps under time pressure.

> **Historical snapshot note:** This runbook captures an older L2 cutover
> plan. Treat its live-cutover sequencing as historical context, and defer
> to the current ETA readiness and launch surfaces before acting.

---

## Stage 0 — IBKR subscriptions active (operator action)

**Prerequisites:**
- [ ] CME Real-Time (NP, L1) subscribed
- [ ] CME Real-Time Depth-of-Book (NP, L2) subscribed
- [ ] (Optional) NYMEX/COMEX/CBOT Real-Time L1 for non-CME symbols
- [ ] Paper account `DUQ319869` inherits subscriptions (toggle in Settings → Paper Trading)

**Verify:**
```bash
ssh forex-vps "powershell -Command \"cd C:\EvolutionaryTradingAlgo; & 'C:\Program Files\Python312\python.exe' -m eta_engine.scripts.verify_ibkr_subscriptions\""
```

**Expected:**
```
CME       MNQ     REALTIME       [OK] PASS   (last=..., bid=..., ask=...)
CME       MNQ     [OK] PASS      depth-of-book streaming
```

**If FAIL:** see `eta_engine/docs/PHASE1_CAPTURE_SETUP.md` Step 0b troubleshooting.

---

## Stage 1 — Capture daemon health (24-48h)

**Goal:** Verify Phase 1 daemons are writing depth + tick files at expected cadence.

```bash
ssh forex-vps "powershell -Command \"if (Test-Path C:\EvolutionaryTradingAlgo\mnq_data\depth) { Get-ChildItem C:\EvolutionaryTradingAlgo\mnq_data\depth | Sort-Object Length -Descending | Select-Object -First 5 Name, Length, LastWriteTime | Format-Table -AutoSize | Out-String | Write-Host }\""
```

**Pass criteria:**
- [ ] At least one depth file per symbol with size > 1 MB after first session
- [ ] Tick dir exists with files > 100 KB per symbol
- [ ] `python -m eta_engine.scripts.capture_health_monitor` returns GREEN

**Halt if:** any symbol shows 0-byte files after 1h of market hours.

---

## Stage 2 — Mark captures expected (every session start)

**Goal:** Flip overlay to fail-CLOSED on missing depth (protects against silent daemon failure).

Operator wires this once into the session-start hook. Manual call to verify:
```python
from eta_engine.strategies.l2_strategy_registry import session_start_hook
summary = session_start_hook()
print(summary)  # should show MNQ in symbols_marked_expected
```

---

## Stage 3 — Shadow soak (14 days)

**Goal:** Validate L2 strategies fire signals on real data without trading them.

Daily cron:
```bash
python -m eta_engine.scripts.l2_backtest_harness --strategy book_imbalance --symbol MNQ --days 1
python -m eta_engine.scripts.l2_backtest_harness --strategy microprice_drift --symbol MNQ --days 1
python -m eta_engine.scripts.l2_backtest_harness --strategy aggressor_flow --symbol MNQ --days 1
```

Each run appends a digest to `l2_backtest_runs.jsonl`. After 7+ days:

```bash
python -m eta_engine.scripts.l2_sweep_harness --symbol MNQ --days 14
```

**Pass criteria for shadow → paper:**
- [ ] `l2_promotion_evaluator` recommends `paper` for at least one strategy
- [ ] Best sweep config has `n_trades >= 30` AND `walk_forward_passes`
- [ ] No risk alerts in `alerts_log.jsonl` in last 14 days

---

## Stage 4 — Paper-soak (7+ days, minimum 30 trades)

**Goal:** Trade with `max_qty_contracts=1` on paper account. Order router must:
1. Call `trading_gate.check_pre_trade_gate(symbol)` before placing
2. Use `signal.signal_id` as the broker client-order-ID
3. Call `l2_observability.emit_signal()` on signal generation
4. Call `l2_observability.emit_fill()` on every broker execution event

Per-day audit:
```bash
python -m eta_engine.scripts.l2_fill_audit --days 1
python -m eta_engine.scripts.l2_confidence_calibration --strategy book_imbalance_v1
```

**Pass criteria for paper → live:**
- [ ] `l2_fill_audit` reports overall verdict PASS (slip within 1.5× predicted)
- [ ] `l2_promotion_evaluator` recommends `live`
- [ ] Walk-forward OOS sharpe ≥ 0.5
- [ ] Sweep best deflated sharpe ≥ 0.5
- [ ] Brier score on confidence ≤ 0.30 with n ≥ 100
- [ ] Operator signs the L2_STRATEGY_DECISION_MEMO

**Halt if:**
- Any falsification criterion fires in `l2_promotion_evaluator`
- `l2_fill_audit` reports FAIL (slip > 2× predicted)
- Daily loss limit hit on paper

---

## Stage 5 — Historical live-cutover scenario (single strategy, single symbol, max_qty=1)

**Historical pre-cutover checklist for an already-approved launch:**
- [ ] Operator written PM Decision Log (template at `docs/L2_STRATEGY_DECISION_MEMO.md`)
- [ ] Red Team dissent verbatim in the memo
- [ ] Daily loss limit configured in broker
- [ ] Kill-switch tested (force-stop scheduled task, verify daemons exit cleanly)
- [ ] Operator-facing dashboard shows the bot

**Historical cutover sequence for an already-approved launch:**
1. Set strategy `promotion_status` to `live` in `l2_strategy_registry.py`
2. Commit + deploy to VPS
3. Restart the live order router
4. Watch first 3 fills closely (compare predicted vs actual slip)
5. After 1 hour: run `l2_fill_audit` to confirm live slip matches paper

**Rollback:**
- Set `promotion_status` back to `paper`
- Operator action: close any open positions manually
- File a post-mortem within 24h (template: `docs/L2_POST_MORTEM_TEMPLATE.md`)

---

## Stage 6 — Live monitoring (continuous)

**Daily cron (cloud-side):**
```
06:00 ET: python -m eta_engine.scripts.l2_promotion_evaluator --json
06:00 ET: python -m eta_engine.scripts.l2_confidence_calibration
06:30 ET: python -m eta_engine.scripts.health_dashboard --alert-hours 24
```

**Weekly cron:**
```
Sun 06:00 ET: python -m eta_engine.scripts.l2_sweep_harness --symbol MNQ --days 14
Sun 06:30 ET: python -m eta_engine.scripts.l2_fill_audit --days 7
```

**Retirement triggers (auto-checked daily):**
- 60-day OOS sharpe < 0
- 14-day rolling sharpe < -0.5 in any window
- Brier > 0.30 after n ≥ 100
- Sharpe CI 95% upper bound < 0

Any trigger fires → strategy auto-flipped to `deactivated` status and operator paged.

---

## Appendix A — Tools by stage

| Stage | Tools |
|---|---|
| 0 — Subs | `verify_ibkr_subscriptions.py` |
| 1 — Capture | `capture_health_monitor.py`, `disk_space_monitor.py` |
| 2 — Hook | `l2_strategy_registry.session_start_hook()` |
| 3 — Shadow | `l2_backtest_harness.py`, `l2_sweep_harness.py`, `l2_promotion_evaluator.py` |
| 4 — Paper | `trading_gate.check_pre_trade_gate()`, `l2_observability.emit_signal()`, `l2_observability.emit_fill()`, `l2_fill_audit.py`, `l2_confidence_calibration.py` |
| 5 — Live | All Stage 4 tools + manual decision memo |
| 6 — Monitoring | All above + `health_dashboard.py` |

## Appendix B — Logs

| File | Written by | Read by |
|---|---|---|
| `mnq_data/depth/<sym>_<date>.jsonl` | `capture_depth_snapshots.py` | overlay, harness, sweep |
| `mnq_data/ticks/<sym>_<date>.jsonl` | `capture_tick_stream.py` | bar_builder, overlay |
| `mnq_data/history_l1/<sym>_<tf>_l1.csv` | `bar_builder_l1.py` | aggressor_flow harness |
| `logs/eta_engine/l2_backtest_runs.jsonl` | `l2_backtest_harness.py` | evaluator, sweep |
| `logs/eta_engine/l2_sweep_runs.jsonl` | `l2_sweep_harness.py` | evaluator |
| `logs/eta_engine/l2_signal_log.jsonl` | strategies via `l2_observability.emit_signal` | fill audit, calibration |
| `logs/eta_engine/broker_fills.jsonl` | order router via `l2_observability.emit_fill` | fill audit, calibration |
| `logs/eta_engine/l2_fill_audit.jsonl` | `l2_fill_audit.py` | evaluator |
| `logs/eta_engine/l2_calibration.jsonl` | `l2_confidence_calibration.py` | evaluator |
| `logs/eta_engine/l2_promotion_decisions.jsonl` | `l2_promotion_evaluator.py` | operator |
| `logs/eta_engine/trading_gate.jsonl` | `trading_gate.check_pre_trade_gate` | health dashboard |
| `logs/eta_engine/capture_health.jsonl` | `capture_health_monitor.py` | gate, evaluator |
| `logs/eta_engine/disk_space.jsonl` | `disk_space_monitor.py` | gate |
| `logs/eta_engine/alerts_log.jsonl` | various | dashboard, evaluator |
