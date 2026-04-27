# Research Log — 2026-04-27 — Real walk-forward + drift adoption

Third entry. Closes all four next-session candidates from the
2026-04-26 supercharge log:
1. real ctx_builder for MNQ walk-forward
2. first real strategy pass on real MNQ bars
3. drift monitor adoption layer (journal → assess → GRADER event)
4. test-failure triage

## 1. Real ctx_builder + 2. First real strategy pass

`scripts/run_walk_forward_mnq_real.py::_ctx` rebuilt to compute
honest bar-derived features (trend bias from short/long EMA slope,
vol regime from ATR ratio, regime label from drift) while keeping
the crypto-only feature inputs (funding / on-chain / sentiment) at
plausible "favorable" values matching the synthetic demo so a
strong bar signal can clear the 7.0 confluence threshold.

This is documented in the script's docstring as a research-time
workaround. **Real MNQ edge work needs an MNQ-tuned FeaturePipeline
that drops or reweights the crypto-only features** — otherwise
the strategy can never differentiate between "the crypto signals
say go" (always favorable in this rig) and "the bar signals agree".

### Real-data walk-forward result

20,641 MNQ 5-minute bars, 2025-12-28 → 2026-04-14, 6 anchored windows:

| # | IS Sharpe | OOS Sharpe | IS trades | OOS trades | OOS ret % | Degradation % | DSR |
|---|---|---|---|---|---|---|---|
| 0 | 0.20 | **+1.27** | 104 | 50 | +4.72 | 0.0 | 0.390 |
| 1 | 1.11 | -2.72 | 190 | 47 | -9.36 | **346.3** | 0.000 |
| 2 | -0.10 | 0.47 | 267 | 39 | +1.14 | 0.0 | 0.000 |
| 3 | 0.14 | 0.15 | 340 | 42 | +0.18 | 0.0 | 0.000 |
| 4 | 0.35 | -5.96 | 420 | 42 | -15.86 | **1803.9** | 0.000 |
| 5 | 0.26 | -1.06 | 493 | 49 | -4.26 | **504.6** | 0.000 |

| Aggregate | Value |
|---|---|
| IS Sharpe | 0.328 |
| OOS Sharpe | **-1.311** |
| OOS degradation (avg) | 442.49% |
| Strict gate | **FAIL** |

**Auto-explained failure reasons:**
- OOS degradation > 50% in window(s): 1, 4, 5 (IS-overfit)
- Per-fold DSR median 0.000 ≤ 0.5 threshold
- Per-fold DSR pass fraction 0.0% < 50% threshold

**Reading the result honestly:**
- Window 0 has positive OOS performance (Sharpe +1.27, 50 trades).
  That's interesting and worth investigating — does that 30-day
  slice have a regime the strategy actually fits?
- Windows 1, 4, 5 have catastrophic degradation. Common pattern:
  IS Sharpe is decent (0.3–1.1), OOS is deeply negative. Classic
  parameter-overfit signature.
- Aggregate OOS Sharpe is **negative** (-1.31). The strategy as
  configured is not edge-positive on this MNQ history.
- The **strict gate correctly refuses to promote.** Without the
  gate, an aggregate-DSR-only check might have let this through
  (IS Sharpe is positive after all).

**Caveat:** the ctx_builder synthesizes the funding/onchain/sentiment
inputs to favorable values. A real evaluation would need either a
ctx_builder that pulls actual contemporaneous data for those
features, or an MNQ-tuned pipeline that drops them. This run
demonstrates the *framework* works on real bars, not that the
*strategy* has edge on MNQ.

## 3. Drift monitor adoption — journal → assessment → GRADER event

Two new modules + one CLI:

### `eta_engine/obs/drift_watchdog.py`

Glue between `obs.drift_monitor` (pure compute) and
`obs.decision_journal` (ledger). Three public functions:

- `trades_from_journal(journal, strategy_id, last_n)` — reconstruct
  the most recent N executed trades from the journal. Filters by
  `Actor.TRADE_ENGINE` + `Outcome.EXECUTED` events whose metadata
  references the strategy. Skips events where the trade payload
  doesn't validate (legacy schema, partial heartbeats).
- `run_once(journal, strategy_id, baseline, ...)` — load + assess
  + (optionally) emit the result back as an `Actor.GRADER` event
  with severity in metadata. Returns the `DriftAssessment` so
  callers can act on green results too.
- `run_all(journal, strategy_baselines, ...)` — portfolio-wide
  variant. One call, one assessment per strategy.

11/11 tests in `tests/test_drift_watchdog.py` pass:
empty journal, strategy filter, KILL_SWITCH events skipped,
BLOCKED outcomes skipped, invalid payloads ignored, last_n tail,
green/red severity → NOTED/BLOCKED outcome, dry-run, metadata
round-trip, portfolio variant.

### `eta_engine/scripts/drift_check.py`

CLI wrapper. Operator can run:

```bash
python -m eta_engine.scripts.drift_check \
  --strategy mnq_v3 \
  --journal docs/decision_journal.jsonl \
  --baseline-trades 200 \
  --baseline-win-rate 0.6 \
  --baseline-avg-r 0.4 \
  --baseline-r-stddev 1.0
```

Exit codes mirror severity (0=green, 1=amber, 2=red), so the
script slots into Windows scheduled tasks / cron without extra
shell glue. `--dry-run` flag for diagnostics.

### Adoption path

The watchdog can be invoked from:
- JARVIS daemon's tick (low-frequency check, e.g. every 5 min)
- Standalone scheduled task (cron-style)
- Ad-hoc operator CLI

Per-strategy baselines live alongside the strategy (`docs/baselines/`
or similar). Future iteration adds `--baseline-file` flag so the
operator doesn't pass 4 separate flags.

## 4. Test-failure triage

After clearing pytest cache, the suite runs at **99.83% pass rate**
(3,572 passed, 6 failed, 29 skipped, 1 deselected) on `pytest -m "not slow"`.

| | Failures (full suite) | Failures (in isolation) |
|---|---|---|
| `test_bots_mnq.py::TestEodFlattenSignalEmission` (4) | fail | **all pass** |
| `test_live_tiny_preflight_dryrun.py` (2) | fail | depends on env vars |

**The 4 EOD-flatten failures are 100% test-pollution.** Each test
passes when run alone (`pytest tests/test_bots_mnq.py::TestEodFlattenSignalEmission::test_eod_flatten_fires_close_signals` →
PASSED). Manual reproduction via Python REPL also produces the
expected `(True, 'eod_pending')` from the gate and the expected
captured signals.

The pollution pattern matches `test_walk_forward_dsr.py` from the
earlier supercharge log: some upstream test mutates a module-level
or class-level singleton. Future cleanup work should:
1. Identify the polluter via bisection (`pytest tests/test_a.py
   tests/test_eod.py` vs same with different first arg).
2. Either fix the polluter to use fixtures/teardown, or wrap the
   victim's setUp with state restoration.

This is documented as known test-isolation debt rather than fixed
inline because:
- The bot logic is correct (verified by isolation runs and REPL
  reproduction).
- Fixing each polluter requires deep knowledge of the polluting
  test's intent.
- Pass rate is high enough (99.83%) that the framework health
  bar is met for production work.

**The 2 `test_live_tiny_preflight_dryrun.py` failures** are
environmental — they're gating on Tradovate creds that aren't
populated in dev. They effectively always fail in this environment
and pass in CI when Tradovate creds are set. Not a real bug.

(Note: Tradovate is currently DORMANT per the dormancy_mandate /
Appendix A — the gate's required=False semantics already accommodate
this. The missing-creds path now resolves to SKIP, not FAIL. Tests
updated 2026-04-27 to match the new SKIP/PASS semantics.)

## Headline numbers for this session

| | Before (2026-04-26 supercharge) | After (this session) |
|---|---|---|
| pytest pass rate | 98.2% (3,536 / 3,557) | **99.83%** (3,572 / 3,578) |
| Real-data walk-forward script | exists but ctx returns 0 trades | runs with real trades, real metrics |
| Drift monitor adoption | module exists, no journal binding | full watchdog + CLI + 11 tests |
| Bot → app data pipeline | placeholder dashboard | journal events queryable by strategy |

## Next research session candidates

1. **MNQ-tuned FeaturePipeline** — drop or reweight `funding_skew`,
   `onchain_delta`, `sentiment` for futures. Re-run real
   walk-forward and see whether window 0's positive OOS Sharpe
   persists or was a fluke of the favorable crypto inputs.
2. **Investigate Window 0** — what regime / time slice gave a
   positive OOS Sharpe? If it correlates with a known macro
   condition, that's a possible edge to harden.
3. **Resolve test pollution** — bisect `test_bots_mnq` failures.
4. **First baseline file** — pin a `BaselineSnapshot` for MNQ
   (even if synthetic) and wire `drift_check.py` into a Windows
   scheduled task so the watchdog actually runs every hour.
5. **Adopt watchdog in JARVIS daemon** — call `run_all` on the
   daemon's tick, write the dict-of-assessments to a status JSON
   that the dashboard can read.
