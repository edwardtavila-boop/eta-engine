# APEX PREDATOR // Chaos Drill Coverage Matrix

**Coverage:** 16 / 16  (100.0%)

| Surface | Module | Drill | Status | Notes |
|---|---|---|---|---|
| circuit_breaker | `apex_predator.brain.avengers.circuit_breaker` | breaker | [PASS] | Existing breaker drill in chaos_drill.py |
| deadman_switch | `apex_predator.brain.avengers.deadman` | deadman | [PASS] | Existing deadman drill in chaos_drill.py |
| push_bus | `apex_predator.brain.avengers.push` | push | [PASS] | Existing push-bus drill in chaos_drill.py |
| drift_detector | `apex_predator.brain.avengers.drift_detector` | drift | [PASS] | Existing drift drill in chaos_drill.py |
| kill_switch_runtime | `apex_predator.core.kill_switch_runtime` | kill_switch_runtime | [PASS] | v0.1.56 CLOSURE: breaches 3% daily loss cap + verifies FLATTEN_ALL |
| risk_engine | `apex_predator.core.risk_engine` | risk_engine | [PASS] | v0.1.56 CLOSURE: trips risk-pct / leverage / daily-loss / DD guards |
| cftc_nfa_compliance | `apex_predator.core.cftc_nfa_compliance` | cftc_nfa_compliance | [PASS] | v0.1.56 CLOSURE: hits OWNS_ACCOUNT / external capital / pool / blackout |
| two_factor | `apex_predator.core.two_factor` | two_factor | [PASS] | v0.1.56 CLOSURE: missing claim + stale claim + fresh claim paths |
| smart_router | `apex_predator.core.smart_router` | smart_router | [PASS] | v0.1.56 CLOSURE: post-only reject / fallback / iceberg reveal |
| firm_gate | `apex_predator.brain.sweep_firm_gate` | firm_gate | [PASS] | v0.1.56 CLOSURE: GO / KILL / raising-runner / None-runner branches |
| oos_qualifier | `apex_predator.strategies.oos_qualifier` | oos_qualifier | [PASS] | v0.1.56 CLOSURE: failing qualification + empty-bars fallback |
| shadow_paper_tracker | `apex_predator.strategies.shadow_paper_tracker` | shadow_paper_tracker | [PASS] | v0.1.56 CLOSURE: 3-window streak rule + losing-window gate + reset |
| live_shadow_guard | `apex_predator.core.live_shadow` | live_shadow_guard | [PASS] | v0.1.56 CLOSURE: full-fill slippage + exhausted book + invalid order |
| runtime_allowlist | `apex_predator.strategies.runtime_allowlist` | runtime_allowlist | [PASS] | v0.1.56 CLOSURE: TTL freshness + invalidate + base-ordering guarantees |
| pnl_drift | `apex_predator.brain.pnl_drift` | pnl_drift | [PASS] | v0.1.56 CLOSURE: stationary phase silent + regime break down-alarm |
| order_state_reconcile | `apex_predator.core.order_state_reconcile` | order_state_reconcile | [PASS] | v0.1.56 CLOSURE: fill / cancel / ghost / orphan divergences + idempotency |
