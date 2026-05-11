# L2 Supervisor Wiring — Minimal Operator Diff

The L2 supercharge infrastructure is fully built and tested. The only remaining code change to make it active in live trading is a 3-call integration into the live order supervisor.

This is **intentionally additive** — no existing supervisor logic changes, only two hook calls (block on gate, log signal/fill) bracket the existing place_order path.

## The diff (3 places in `jarvis_strategy_supervisor.py`)

### 1. Import (one line at the top of the module)

```python
from eta_engine.scripts import l2_supervisor_hooks as l2hooks
```

### 2. Pre-trade gate (before `_venue.place_order(...)`)

Find the block around line ~1085 (`_run_on_live_ibkr_loop(_venue.place_order(_req), timeout=30.0)`). Insert immediately before the call:

```python
# L2 supercharge: circuit breaker (disk RED/CRITICAL or capture RED blocks)
if not l2hooks.pre_trade_check(bot, rec):
    _rollback_recorded_entry("blocked_by_l2_trading_gate")
    return None

# Existing line:
_result = _run_on_live_ibkr_loop(_venue.place_order(_req), timeout=30.0)
```

### 3. Signal log emission (after successful order ack)

In the same block, after `_result` is checked and the status is OK (`OPEN` / `PARTIAL` / `FILLED`):

```python
# Existing line ~1108:
_ok_statuses = {"OPEN", "PARTIAL", "FILLED"}
if (
    _result.status.value in _ok_statuses
    and bot.open_position is not None
):
    bot.open_position["broker_bracket"] = True
    # ... existing bracket setup ...

    # L2 supercharge: persist signal for fill audit + calibration
    l2hooks.record_signal(bot, rec, _result)
```

### 4. Fill log emission (in the IBKR execution event handler)

Find the supervisor's IBKR fill handler (typically `_on_execution` or similar — the function that receives `executionEvent` from ib_insync). At the entry point of the handler:

```python
def _on_execution(self, trade, fill):
    # ... existing handling ...

    # L2 supercharge: persist fill for slip audit + calibration
    l2hooks.record_fill(
        signal_id=trade.order.orderRef or fill.execution.orderRef or "",
        broker_exec_id=fill.execution.execId,
        exit_reason=_classify_exit_reason(trade, fill),  # operator-implemented
        side="LONG" if trade.order.action == "BUY" else "SHORT",
        actual_fill_price=float(fill.execution.price),
        qty_filled=int(fill.execution.shares),
        commission_usd=float(fill.commissionReport.commission or 0),
    )

    # ... rest of existing handler ...
```

`_classify_exit_reason` is operator-implemented one-liner mapping IBKR fill metadata to one of:
- `ENTRY` — the entry leg of a bracket
- `TARGET` — bracket TP touched
- `STOP` — bracket SL touched
- `TIMEOUT` — manual close via supervisor logic
- `CANCEL` — operator or supervisor canceled

If exact classification is unclear, defaulting to `TIMEOUT` is safe — it loses some calibration signal but doesn't corrupt the audit.

## What happens after wiring

```
session start
    ↓
session_start_hook() → marks captures_expected("MNQ")
    ↓
strategy emits signal → record_signal() → l2_signal_log.jsonl
    ↓
order placement gated by pre_trade_check()
    ↓ (if not blocked)
broker placement (existing code)
    ↓
fill events → record_fill() → broker_fills.jsonl
    ↓
[daily cron] l2_fill_audit → realized slip per session bucket
    ↓
[daily cron] l2_confidence_calibration → Brier score
    ↓
[daily cron] l2_promotion_evaluator → promotion verdict
    ↓
[daily cron] l2_registry_adapter → verdict_cache.json
    ↓
operator dashboard shows L2 bots alongside legacy fleet
```

## Failure modes — fail-OPEN by design

Every hook function wraps its inner call in `try/except` and:
- `pre_trade_check`: on exception → returns `True` (don't block trading)
- `record_signal`: on exception → logs to stderr, continues
- `record_fill`: on exception → logs to stderr, continues

Rationale: observability bugs should never take down live trading. The audit pipeline degrades to "missing data" if hooks break; the trading path stays intact.

## Testing the wiring

After adding the 3 (4 with the import) lines:

```bash
# Smoke test: emit one signal + one fill in test mode
python -c "
from eta_engine.scripts import l2_supervisor_hooks as h
from dataclasses import dataclass
@dataclass
class Bot: bot_id='test'; strategy_id='manual_test'; symbol='MNQ'
@dataclass
class Rec: signal_id='manual-1'; symbol='MNQ'; side='BUY'; qty=1; entry_price=29270.0; stop_price=29268.0; target_price=29274.0; confidence=0.5; rationale='wiring test'
h.record_signal(Bot(), Rec())
h.record_fill(signal_id='manual-1', broker_exec_id='test', exit_reason='TARGET', side='LONG', actual_fill_price=29274.0, qty_filled=1)
print('wiring OK')
"

# Verify both logs got written
ls -la logs/eta_engine/l2_signal_log.jsonl logs/eta_engine/broker_fills.jsonl
python -m eta_engine.scripts.l2_fill_audit --days 1
```

If `l2_fill_audit` reports `n_observations >= 1`, the wiring is live.
