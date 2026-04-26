# Runbook — JARVIS-supervised paper run for both bots

**Audience:** Edward Avila (operator).
**Goal:** Run both bots in paper mode under JARVIS supervision, gather data, no real money flows.
**Last updated:** 2026-04-26 (post v0.1.70 / mnq_bot bff9108 polish).

---

## TL;DR — three commands

```powershell
# 1. JARVIS supervisor (long-running daemon, watches both bots)
cd C:\Users\edwar\OneDrive\Desktop\Base\eta_engine
python scripts/jarvis_live.py

# 2. eta_engine BTC bot (paper mode by default)
python scripts/btc_live.py --bars 1440 --warmup-bars 0

# 3. mnq_bot (from its own repo)
cd C:\Users\edwar\projects\mnq_bot
python scripts/live_sim.py --variant r5_real_wide_target --n-days 5
```

All three exit 0 in paper mode with no orders flowing to any broker.

---

## What "JARVIS hands" means

JARVIS (`eta_engine/brain/jarvis_v3` + `obs/jarvis_supervisor`) is the
admin/supervisor layer over the fleet. Every subsystem calls
`request_approval()` against JARVIS; if JARVIS stops ticking, every
gate silently falls through to stale policy.

`scripts/jarvis_live.py` is the long-running daemon that keeps JARVIS
alive. It:

1. Builds a `JarvisContextBuilder` from `docs/premarket_inputs.json`
   (hot-reloadable -- operator can overwrite the file and the next tick
   picks it up).
2. Wraps the context engine in `JarvisSupervisor` so staleness /
   dominance / flatline / invalid are all caught.
3. Fans out health alerts via Telegram / Discord / Slack when env
   credentials are present (`TELEGRAM_BOT_TOKEN`, `DISCORD_WEBHOOK_URL`,
   `SLACK_WEBHOOK_URL`). Without env -> dry-run mode (no transport).
4. Emits per-tick health to:
   - `docs/jarvis_live_health.json` (latest only)
   - `docs/jarvis_live_log.jsonl` (append-only history)

When JARVIS health is GREEN, the bots' `request_approval()` calls
return immediately. When it goes YELLOW/RED (staleness, dominance,
flatline), the bots see denied approvals and stop opening new positions.

---

## Pre-flight checks (run once before each session)

### eta_engine side

```powershell
cd C:\Users\edwar\OneDrive\Desktop\Base\eta_engine

# 1. Verify all packages import + smoke runs
python scripts/verify_all.py
# Expected: Passed: 17/17  Failed: 0

# 2. Audit gates green (gate registry, R-item code coverage, deferral criteria)
python scripts/_audit_alert_events.py | tail -3
python scripts/_audit_roadmap_vs_code.py | tail -3
python scripts/_audit_deferral_criteria.py --strict
# Expected: zero missing events, 19/19 R-items OK, exit 0 strict

# 3. Full pytest sweep
python -m pytest -x -q
# Expected: ~4509 passed, 10 skipped
```

### mnq_bot side

```powershell
cd C:\Users\edwar\projects\mnq_bot

# 1. Doctor health check
python -c "from mnq.cli.doctor import run_all_checks; [print(r) for r in run_all_checks()]"
# Expected: 8 checks, all OK or warn (no fail)
# - broker_dormancy MUST be OK ("no live broker configured" or active broker)

# 2. Spec-vs-code audit (advisory; surfaces unbacked variants)
python scripts/_audit_spec_vs_code.py | tail -5
# Expected: list of unbacked variants (currently 30+; this is documented
# as H1 in docs/RED_TEAM_REVIEW_2026_04_25.md, not a blocker for paper)

# 3. Full pytest sweep
python -m pytest -q
# Expected: 1315 passed, 2 skipped
```

---

## Operator-facing safety guarantees (paper mode)

### eta_engine

| Layer | Guarantee |
|---|---|
| Broker dormancy | `venues/router.py::DORMANT_BROKERS = frozenset({"tradovate"})`. Tradovate orders refused at routing layer. |
| R1 broker-equity drift | Reconciler wired in `_amain`; `broker_equity_drift` event registered in `alerts.yaml`; pushover + email on transition. |
| Trailing DD | `TrailingDDTracker` wired in `_amain`; refuses to boot live mode without it. |
| 30%-rule (Apex) | `ConsistencyGuard` wired; emits `consistency_status` alert on WARNING/VIOLATION transition. |
| Kill switch latch | Disk-backed; refuses re-boot until cleared with explicit operator name. |
| Asymmetric tolerances | `tolerance_below=$20/0.05%` (eval-protective) vs `tolerance_above=$200/0.5%` (anti-spam). |
| Audits at every commit | `_audit_alert_events.py`, `_audit_roadmap_vs_code.py`, `_audit_deferral_criteria.py` run via pre-commit. |

### mnq_bot

| Layer | Guarantee |
|---|---|
| Live mode refused | `forward_to_broker` requires THREE concurrent flags: `APEX_DRY_RUN!=true` AND `APEX_LIVE_READY=1` AND broker not in `DORMANT_BROKERS`. Default config = paper-only. |
| Broker dormancy | `mnq.venues.dormancy.DORMANT_BROKERS = frozenset({"tradovate"})`. Tradovate refused at the doctor + webhook layer. |
| Production-mode warning | `OrderBook` logs a structured `ORDERBOOK_UNSAFE_PRODUCTION` warning when constructed without `gate_chain` AND `MNQ_ENV` is `production`/`live`/`prod`. |
| Adaptive-learner journal | `record_trade_outcome` stub appends to `data/learner_journal.jsonl` (was silently failing). |
| Journal-path coherence | `live_sim` writes to `gate_chain.JOURNAL_PATH.parent` so the gate chain reads what the writer writes. |

---

## Architectural BLOCKERs still open (operator decisions required)

These are documented with explicit "Lands when" exit criteria in
`mnq_bot/docs/RED_TEAM_REVIEW_2026_04_25.md`. Paper mode is safe; the
BLOCKERs only materialize when the operator wants to flip mnq_bot to
live trading.

| ID | Disposition |
|---|---|
| **mnq_bot B1** | No production live entrypoint. Pick: refactor `webhook.py` to delegate to `mnq.executor.venue_router.VenueRouter`, OR build new `scripts/run_eta_live.py`, OR mark mnq_bot paper-only forever. |
| **mnq_bot B3** | `OrderBook(journal)` defaults `gate_chain=None`. Operator decides whether to make `gate_chain` required positional (breaking change). v0.2.1 ships a production-mode warning as risk reduction. |
| **mnq_bot B4** | Firm six-stage review is dead code in live path; runs against synthetic bar. Pick: per-bar review on real tape, OR rename `firm_live_review` to drop the misleading "_live". |
| **mnq_bot H2** | `_shim_guard` self-heal has no source. Pick: ship known-good fixtures, OR port firm package out of OneDrive. |
| **eta_engine H4** | broker server-side timestamp parsing (deeper than v0.1.69's byte-identical heuristic). Lands when adapter protocol return type extends to `tuple[float, datetime] \| None`. |
| **eta_engine M1, M2** | multi-broker drift fan-out + KillVerdict synthesis on drift. Data-blocked on 30+ days of live-paper empirics. |

---

## Daily run pattern (no operator interruption)

```powershell
# Start of session (one terminal, leave running):
cd C:\Users\edwar\OneDrive\Desktop\Base\eta_engine
python scripts/jarvis_live.py
# Long-running. Tail docs/jarvis_live_health.json for state.

# Open a SECOND terminal:
cd C:\Users\edwar\OneDrive\Desktop\Base\eta_engine
python scripts/btc_live.py --bars 1440 --warmup-bars 0
# Single 1440-bar (1 day) BTC paper run. Check audit_ok: GREEN at the end.

# Open a THIRD terminal:
cd C:\Users\edwar\projects\mnq_bot
python scripts/live_sim.py --variant r5_real_wide_target --n-days 5
# 5-day paper sim of the v2.2 variant. Writes journal to
# data/live_sim/journal.sqlite (the canonical path the gate chain reads).
```

End of session: `Ctrl+C` JARVIS, copy any artifacts you care about
out of `docs/btc_live/` and `data/live_sim/`. State files (kill
switch latch, consistency guard, trailing DD tracker) persist across
sessions and are read by the next boot.

---

## Common alerts and what they mean

See also `docs/runbooks/broker_equity_drift_response.md` for the R1
drift detector.

| Alert | When it fires | What to do |
|---|---|---|
| `broker_equity_drift` | broker net-liq cushion < tier-A logical equity by more than tolerance | See drift_response.md decision tree. |
| `kill_switch_latched` | Catastrophic verdict (FLATTEN_ALL, FLATTEN_TIER_A_PREEMPTIVE) latched to disk | Investigate before clearing. `python -m eta_engine.scripts.clear_kill_switch --confirm --operator <name>` only after triage. |
| `boot_refused` | Runtime refused to boot due to latched kill switch | Same as above; clear after triage. |
| `consistency_status` | 30%-rule WARNING or VIOLATION transition | Apex eval concentration risk. Pause new entries until cleared. |
| `apex_preempt` | Trailing DD cushion crossed preempt threshold ($500 by default) | Tier-A bots auto-flattened. Investigate drift source. |
| `apex_sla_breach` | A monitored probe has been outside SLO for the cooldown window | Check probe-specific runbook. |
| `apex_sla_recovered` | A previously-breaching probe is back inside SLO | Confirmatory. |
| `tier_a_invariant_violation` | `sum(bot.state.equity for tier_a) != broker_net_liq` | Likely config bug (bot allocations not slicing the shared account). |

---

## Files this runbook references

- `eta_engine/scripts/jarvis_live.py` — JARVIS supervisor daemon
- `eta_engine/scripts/btc_live.py` — BTC paper/live launcher
- `eta_engine/scripts/run_eta_live.py` — eta_engine runtime supervisor
- `eta_engine/configs/alerts.yaml` — event routing (12 events, validated by audit)
- `mnq_bot/scripts/live_sim.py` — mnq_bot paper sim
- `mnq_bot/eta_v3_framework/python/webhook.py` — webhook receiver (paper-gated)
- `mnq_bot/docs/RED_TEAM_REVIEW_2026_04_25.md` — full Red Team verdict
- `eta_engine/docs/red_team_d2_d3_review.md` — eta_engine Red Team residuals
