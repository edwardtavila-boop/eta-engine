# Red Team Review — D-series Apex Eval Hardening

**Date:** 2026-04-24
**Scope:** D2 (`TrailingDDTracker`) and D3 (`ConsistencyGuard`) modules and
their wiring into `scripts/run_apex_live.py`.
**Reviewer:** `risk-advocate` agent (Opus 4.7, adversarial posture).
**Outcome:** 3 BLOCKERs identified, all closed in v0.1.58.

This document captures the adversarial teardown of the D-series work, the
fixes shipped in response, and the residual risks that remain (documented
so they cannot be forgotten).

---

## Executive summary

Before this review the D-series modules had unit-level coverage but the
**wiring** into `run_apex_live.py` had three gaps that could let an Apex
eval fail silently. The review classified them as BLOCKERs because each
could cause the runtime to *appear* correct (all alerts green, all tests
green) while the eval was either already busted or about to bust.

| ID | Finding | Severity | Status |
|---:|---|---|---|
| B1 | UTC-midnight day-bucketing splits overnight equity-futures sessions across two Apex day keys | CRITICAL | Closed |
| B2 | Legacy `build_apex_eval_snapshot` fallback lacks freeze rule; silently under-protects in live mode when tracker is not wired | CRITICAL | Closed |
| B3 | 30%-rule VIOLATION was advisory-only: alert + log, no enforcement action on `is_paused` | HIGH | Closed |

Plus several HIGH findings that were accepted as known residual risk (see
"Residual risks" below).

---

## B1 — Session-day bucketing bug

**Finding.** `run_apex_live.py` was keying today's consistency-guard entry
by `utc_today_iso()`, which buckets PnL by **UTC calendar midnight**. The
Apex trading day is defined as the 24-hour window ending at **17:00
US/Central** (CME Globex close convention, DST-aware). An overnight
equity-futures session that generates PnL at 22:30 UTC in July (17:30 CDT)
would be charged to a *different* UTC day than PnL generated at 02:00 UTC
the next morning (21:00 CDT the prior evening) — even though both events
belong to the same Apex trading day.

Effect: the *largest winning day* appears smaller and the *total net
profit* denominator includes extra zero-PnL buckets. Both errors bias the
30%-rule ratio **downward**, hiding a real concentration risk. The
runtime reports "OK" or "WARNING" when the true state is "VIOLATION",
and the operator flies blind until Apex itself closes the eval.

**Fix.** Added `apex_trading_day_iso()` in
`apex_predator/core/consistency_guard.py`. Uses
`zoneinfo.ZoneInfo("America/Chicago")` to compute the 17:00 local
rollover in DST-aware fashion, with a fixed 23:00-UTC fallback when
`zoneinfo` is unavailable (wrong by ≤ 1h in summer, never splits RTH).
`run_apex_live.py` now calls this helper; `utc_today_iso()` stays with a
deprecation note for backwards compatibility.

**Tests.** `TestApexTradingDayIso` (11 tests): CDT + CST before and after
rollover, exact boundary at 17:00 CT, one-second-before boundary,
overnight-session co-location, naive-datetime coercion, `ZoneInfo`-absent
fallback, and an explicit diff-test confirming the two helpers disagree
on an evening-session timestamp (the bug the fix closes).

---

## B2 — Live-mode gate on `TrailingDDTracker`

**Finding.** `run_apex_live.py` runs a tick-precise trailing-DD tracker
**when one is supplied via the constructor kwarg**. When the kwarg is
omitted (which is the default), the runtime falls back to
`build_apex_eval_snapshot()` — a bar-level proxy that does **not**
implement the Apex freeze rule (once `peak >= start + cap`, the floor
locks at `start` forever). The fallback silently under-protects: a live
account that has climbed above the initial cap will compute a floor that
keeps trailing the peak down, so a normal retrace through the *correct*
frozen floor appears safe when it is actually a bust.

Effect: a runtime constructed without a tracker in `--live` mode is a
footgun. The operator has to remember to pass the tracker; nothing in
the framework enforces it.

**Fix.** `ApexRuntime.__init__` now raises `RuntimeError` when
`cfg.live=True AND cfg.dry_run=False AND trailing_dd_tracker is None`.
The error message names the missing wiring explicitly and points at the
module. Dry-run, paper-sim, and unit tests stay permissive (the proxy is
acceptable for those modes).

**Tests.** `TestLiveModeTrackerGate` (4 tests): live without tracker
raises, live with tracker builds cleanly, dry-run without tracker builds
cleanly, `live=True + dry_run=True` builds cleanly (dry_run wins).

---

## B3 — VIOLATION enforcement (advisory → pause)

**Finding.** When the consistency guard returned
`ConsistencyStatus.VIOLATION`, the runtime sent an alert and wrote a
structured log line. Neither action prevented new trades. A bot already
concentrated on its largest winning day could continue to open positions
until the operator noticed the alert in Discord/Slack and manually
paused. For an automated system, "notify and keep trading" is not
enforcement — it is a different kind of silent failure.

Effect: the guard was correctly detecting the risk but the runtime was
not acting on it. Tests covered the detection path but not an
enforcement path (because none existed).

**Fix.** On `VIOLATION`, the runtime now synthesizes a
`KillVerdict(action=PAUSE_NEW_ENTRIES, severity=CRITICAL, scope="tier_a")`
and feeds it through the existing `apply_verdict` dispatch path, which
flips `bot.state.is_paused = True` on every tier-A bot. Existing
positions are **not** flattened — they close on their own signals — but
new entries are blocked until the operator clears the violation (close
the bucket, bank the win, or `ConsistencyGuard.reset()` for a fresh
eval). The verdict is also appended to the tick's verdict log so audit
history captures the enforcement.

**Tests.** `TestConsistencyViolationPauses` (2 tests): pre-seeded
VIOLATION fires PAUSE on the tick and persists in `runtime.jsonl`;
pre-seeded WARNING does NOT fire PAUSE.

---

## Residual risks (accepted, documented)

The following HIGH findings from the Red Team are accepted as known
residual risks and tracked for future work. None of them is a BLOCKER
because the D-series closure brings the runtime to a safer baseline than
v0.1.56 across every regime tested.

### R1 — Logical equity vs broker MTM

The tracker consumes `sum(bot.state.equity)` — a logical figure maintained
by the bot's own PnL book. Apex accounts for MTM at broker level
(unrealized + realized + funding + fees). A prolonged disconnect between
these two could drift the floor calculation from what Apex sees. Fix:
wire a broker-side equity reader (venue-specific; IBKR account value,
Tastytrade balance) as the canonical source when creds are present.
Tracked for v0.2.x.

### R2 — Tick-interval latency

The runtime polls on a 5-second tick by default. A fast retrace during
that window could cross the floor before the next update. Apex's own
tick enforcement is unknown; likely sub-second. The tracker can latch on
the *next* tick after the breach, not during. Mitigation: tighter tick
in live mode (1s), plus a stop-buffer so the local floor fires before
Apex's does. Tracked for v0.2.x.

### R3 — Freeze-rule re-entrancy

The tracker freezes when `peak >= start + cap`. If equity subsequently
dips and then climbs *through* that peak again, the frozen floor stays.
This is correct per Apex rules. The risk is that if the tracker's state
file is ever accidentally deleted or the operator re-inits with a larger
`trailing_dd_cap_usd`, the freeze is lost and the floor resumes
trailing. Mitigation: immutable audit log on tracker state changes, and
a reset-confirmation path that requires operator acknowledgement.
Tracked.

### R4 — Session-day math vs weekends / holidays

The 30% rule buckets by Apex trading day. Weekends and US holidays
don't exist in the calendar; the `apex_trading_day_iso` helper keys
a Saturday-morning timestamp to "Saturday" which Apex probably ignores.
Not wrong, but not perfectly aligned. Mitigation: a CME-calendar-aware
version that rounds to the next trading day. Tracked as nice-to-have.

---

## Coverage delta

Before v0.1.58:
- `tests/test_consistency_guard.py` — 32 tests (guard logic, no
  session-day coverage).
- `tests/test_run_apex_live.py` — 60 tests (D2+D3 integration but no
  live-mode gate, no enforcement path).

After v0.1.58:
- `tests/test_consistency_guard.py` — **43 tests** (+11 `TestApexTradingDayIso`).
- `tests/test_run_apex_live.py` — **66 tests** (+4 `TestLiveModeTrackerGate`,
  +2 `TestConsistencyViolationPauses`).

All 133 D-series tests pass on Python 3.14.4 / Windows / apex_predator
as of 2026-04-24.

---

## Quick-reference commands

```bash
# Single-module check
python -m ruff check apex_predator/core/consistency_guard.py \
                     apex_predator/scripts/run_apex_live.py

# D-series regression
python -m pytest \
    apex_predator/tests/test_run_apex_live.py \
    apex_predator/tests/test_consistency_guard.py \
    apex_predator/tests/test_trailing_dd_tracker.py \
    apex_predator/tests/test_kill_switch_latch.py \
    -x -q

# Chaos drills
python -m apex_predator.scripts.chaos_drills

# Kill-switch latch state
type apex_predator\state\kill_switch_latch.json

# Clear a tripped latch (requires operator name)
python -m apex_predator.scripts.clear_kill_switch --confirm --operator <name>
```
