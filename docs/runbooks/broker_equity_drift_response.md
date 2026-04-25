# Runbook — Broker-equity drift alert response

**Trigger:** You received a Pushover / email alert with title
`broker_equity_drift` (level: warn). The R1 reconciler observed that
the broker's reported net liquidation diverges from the bot runtime's
logical equity by more than the configured tolerance.

**Audience:** Edward Avila (operator). Solo founder, no escalation
chain. You are also the responder.

**Last updated:** 2026-04-24 (v0.1.65 — process gap #5 closure from
the v0.1.64 R1 Red Team review).

---

## TL;DR

1. **Open the alert payload.** The `reason` field is the classification.
2. **Open `docs/runtime_log.jsonl` and tail the last 60 entries.** Look at
   the `broker_equity` block in each tick.
3. **Decide:**
   - `broker_below_logical` (cushion over-stated) — this is the dangerous
     direction. Apex eval bust risk. **Stop adding new entries; consider
     flatten.**
   - `broker_above_logical` (cushion under-stated) — usually MTM lag,
     funding accrual, or an unbooked credit. Less urgent; verify and
     keep running.
4. **Reconcile the discrepancy** before placing new orders. The tools
   below help.
5. **Resolve.** When drift returns to `within_tolerance` for ≥5
   consecutive ticks, the latch clears and re-entry of the band will
   re-fire the alert.

---

## What the reasons mean

| `reason`               | Sign of `drift_usd` | Severity | What it implies |
|------------------------|---------------------|----------|-----------------|
| `within_tolerance`     | abs ≤ tol           | INFO     | Healthy. No alert. |
| `broker_below_logical` | positive            | **WARN** | Bot books say you have more cushion than the broker confirms. Risk: silent commission/funding bleed, unbooked partial fill, broker-side risk hold. **Most dangerous direction for Apex eval.** |
| `broker_above_logical` | negative            | INFO     | Broker shows more than the bot books expect. Usually MTM lag, dividend/funding credit, or rebate. **Verify; not immediately dangerous.** |
| `no_broker_data`       | n/a                 | INFO     | Broker source returned `None` — adapter dormant, network failure, or null adapter is wired. **Drift detection is OFF this tick** — check why. |

---

## Step-by-step response

### 1. Read the alert payload

The Pushover / email body contains a `payload` JSON dict like:

```json
{
  "logical_equity_usd": 50100.00,
  "broker_equity_usd": 49800.00,
  "drift_usd": 300.00,
  "drift_pct_of_logical": 0.00599,
  "reason": "broker_below_logical",
  "ts": "2026-04-24T18:42:11+00:00"
}
```

Make a note of:
- `drift_usd` — magnitude in USD
- `reason` — direction
- `ts` — exactly when it tripped

### 2. Tail the runtime log

```powershell
# Last 60 tick entries
Get-Content docs/runtime_log.jsonl -Tail 60 |
  ConvertFrom-Json |
  Select-Object ts, kind, @{Name='be'; Expression={$_.meta.broker_equity}} |
  Format-Table -AutoSize
```

Or with grep (Git Bash / WSL):

```bash
tail -60 docs/runtime_log.jsonl | \
  jq -c '{ts, kind, be: .meta.broker_equity}' | \
  grep -i "broker_below\|broker_above"
```

You're looking for:
- **First tick that flipped to out-of-tolerance** (the alert fires
  on transition; the tick log captures the leading edge)
- **Whether drift is growing, stable, or oscillating**
- **Whether other classifications (no_broker_data, broker_above) are
  interleaved** — that suggests broker-side latency, not real drift

### 3. Decide direction-of-travel response

#### `broker_below_logical` (DANGEROUS)

Treat this like a developing fire. Most likely causes, in order of
operational frequency:

1. **Silent commission or slippage bleed** — IBKR debited per-trade
   commissions that haven't propagated into the bot's fill journal
   yet. Check `docs/btc_live/btc_paper_journal.jsonl` (or the
   equivalent for the active bot) for unexplained commission rows.
2. **Unbooked partial fill** — broker filled part of an order, the
   ack came back with quantity=0, the bot's fill journal records
   the order as still pending, but the broker has already cleared
   the trade and updated net-liq.
3. **Broker-side risk hold** — IBKR / Tastytrade put a margin hold
   on a position they consider risky. Logical equity doesn't see
   this; broker net-liq does.
4. **Funding/carry accrual that underflows the threshold** — for
   futures this is rare but non-zero overnight.

**Action:**
- **Pause new entries immediately.** Set `apex_go_state.tier_a_mnq_live`
  (and any other tier-A flag) to `false` in `roadmap_state.json` via
  `python -m apex_predator.scripts.go_trigger --pause-tier-a`. This
  blocks NEW entries; existing positions keep running.
- **Inspect open positions** via the broker UI directly:
  - IBKR: Client Portal → Account → Portfolio
  - Tastytrade: web app → Positions tab
- **Pull the latest balance** to confirm the alert wasn't stale:
  ```bash
  python -m apex_predator.scripts.connect_brokers --probe
  ```
- **If drift > $500 sustained for ≥5 minutes:** flatten tier-A
  preemptively rather than waiting for the kill switch:
  ```bash
  python -m apex_predator.scripts.run_apex_live --flatten-tier-a \
      --reason "broker drift sustained 5min, manual flatten"
  ```
  (This emits a `kill_switch` event — operator gets a sms confirmation.)

#### `broker_above_logical` (USUALLY BENIGN)

Most likely causes:

1. **MTM lag** — broker's snapshot is from N seconds ago and the
   market moved against your open positions; the bot's tick-mark
   logic already captured the new price.
2. **Dividend / funding credit** — broker credited an interest /
   dividend payment that the bot doesn't track.
3. **Rebate** — IBKR Smart Routing rebates can show up unannounced.

**Action:**
- **Verify it's transient.** Tail the runtime log for the next 60
  ticks; the classification should drift back toward `within_tolerance`
  within minutes. If it sticks at `broker_above_logical` for >30 min,
  the bot's logical equity calculation is undercount-ing something
  systematic — open a kaizen ticket.
- **No urgent flatten.** This direction does not threaten the eval.

#### `no_broker_data`

Drift detection is OFF for the tick. Causes:

1. **Adapter dormant** — `NullBrokerEquityAdapter` is wired (e.g.
   live mode but IBKR + Tastytrade creds are both missing). Check
   the boot banner: `broker_equity : <name> ...`. If the name
   contains `null` or `paper-null`, the adapter cannot fetch.
2. **Network / API failure** — IBKR Client Portal session expired,
   Tastytrade auth token rotated. The poller log line will show
   the underlying exception.
3. **Stale cache** — the poller's TTL elapsed without a successful
   fetch.

**Action:**
- **If single-tick:** ignore. The next successful poll re-engages
  the detector.
- **If sustained (>1 min):** the adapter is broken. Restart the
  broker session:
  ```bash
  # IBKR Client Portal session refresh
  python -m apex_predator.scripts.connect_brokers --reconnect ibkr
  # OR Tastytrade
  python -m apex_predator.scripts.connect_brokers --reconnect tastytrade
  ```
  If reconnection fails, you are **flying blind on drift** — pause
  new entries until the adapter recovers.

---

## Tools cheat sheet

```bash
# Tail the live runtime log filtered to broker_equity blocks
tail -f docs/runtime_log.jsonl | jq -c '.meta.broker_equity'

# Re-audit alert event registry (ensures every dispatcher.send is routed)
python scripts/_audit_alert_events.py

# Audit alerts log for the last N broker_equity_drift dispatches
grep '"event": "broker_equity_drift"' docs/alerts_log.jsonl | tail -20

# Check current adapter wiring (boot banner replay -- dry-run)
python -m apex_predator.scripts.run_apex_live \
    --max-bars 1 --tick-interval 0 --dry-run
```

---

## When to escalate to a kaizen ticket

Open a ticket in `state/kaizen_ledger.json` (via `python -m
apex_predator.scripts.jarvis_cli kaizen open ...`) when:

- Drift sustains >$500 / >5 min and the cause is not in the
  list above (i.e. genuinely novel).
- The same ticker / bot / venue surfaces drift alerts >3 times
  in 7 days — pattern, not incident.
- The alert fires while `no_broker_data` is also being logged in
  the same window — race condition between poller and reconciler.
- Drift was caught only by the reconciler and NOT by Apex's
  own books at the same time — gap in our detection coverage.

---

## What this runbook does NOT cover

- **KillVerdict synthesis on sustained drift.** The reconciler is
  observation-only as of v0.1.65. The decision to flip a
  `KillVerdict(PAUSE_NEW_ENTRIES)` or `FLATTEN_TIER_A` is the
  operator's per-incident call. Automation is v0.2.x scope and
  data-blocked on tolerance calibration empirics.
- **Per-bot drift isolation.** Aggregate-only detection means you
  cannot tell which tier-A bot is the source. Triage by inspecting
  per-bot fill journals.
- **Cross-account drift.** Single-broker comparison only. Multi-account
  reconciliation is v0.2.x scope (M1 in the residual ledger).

---

## References

- R1 design rationale: `docs/red_team_d2_d3_review.md`
- Reconciler implementation: `core/broker_equity_reconciler.py`
- Adapter contract: `core/broker_equity_adapter.py`
- Wire-up in `_amain`: `scripts/run_apex_live.py::_build_broker_equity_adapter`
- Event registry: `configs/alerts.yaml` (key: `broker_equity_drift`)
- Audit script: `scripts/_audit_alert_events.py`
- Wire-up smoke test: `tests/test_amain_wire_up.py`
- Registry CI gate: `tests/test_alert_event_registry.py`
