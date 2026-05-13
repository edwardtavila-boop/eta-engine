# Post-Launch Day 1 Debrief — Template

**Date:** YYYY-MM-DD
**Operator:** Edward
**Eval account:** BluSky 50K (or whichever)
**Bots opted EVAL_LIVE:** _list bot_ids_

Copy this template to `docs/POSTLAUNCH_DAY1_<YYYY-MM-DD>.md` at EOD
and fill it in. The structure forces honesty.

---

## Top-line numbers

Pull from `python -m eta_engine.scripts.prop_launch_check` at EOD:

- **Daily PnL:** $___
- **Daily DD buffer remaining:** $___ / $1,500 (used ___ %)
- **Static DD buffer remaining:** $___ / $2,500 (used ___ %)
- **Single-day-share ratio:** _% of week-to-date profit_
- **Live trades closed today:** ___
- **Paper trades closed today:** ___ (parallel paper soak)
- **Shadow signals (observed but not taken):** ___
- **Cumulative R today:** ___
- **Cumulative USD today:** ___

## What happened (chronological, ET)

| Time | Event |
|---|---|
| 09:25 | First-light check verdict: ___ |
| 09:30 | RTH open; first signal at ___ |
| 10:00 | _e.g. mnq_futures_sage fill: BUY 1 @ $___, exit @ $___, R=____ |
| 12:00 | _lunch check, DD buffer at $____ |
| 14:00 | _e.g. WATCH triggered at $___ daily PnL; supervisor halved sizes_ |
| 16:00 | _RTH close; ____ trades closed for the session_ |
| 16:30 | _EOD review_ |

## Gate decisions

Pull from supervisor heartbeat or the alerts log:

- **target=live:** ___ trades
- **target=paper:** ___ trades (route_reasons: ____)
- **target=reject:** ___ signals (reject_reasons: ____)
- **Most common paper-route reason:** _e.g. lifecycle_eval_paper (X), soft_dd (Y)_

## Drawdown guard transitions

- HALT events today: ___ (timestamps + rationale)
- WATCH events today: ___ (timestamps + rationale)
- WATCH→OK clearings today: ___

## Telegram alerts received

- Total: ___ (or _0_ — channels not configured)
- Severity breakdown: RED=___, YELLOW=___, INFO=___
- Did you respond to any? (Y/N) — _details_

## Surprises

What happened that I didn't expect?

- _Slippage on mnq_futures_sage: paper expected $0.50/fill, live got $____
- _Lag between signal and broker confirm: ___ ms_
- _Bot fired more/fewer times than I expected because ____

(If a surprise is large, mark it for investigation — file a follow-up
TODO at the end of this doc.)

## What worked

- _e.g. wave-25 gate correctly routed mnq_futures_sage to live, mes_v2 to paper_
- _e.g. drawdown guard caught a brewing consistency-rule issue at 14:32 and shifted to WATCH_
- _e.g. Telegram delivered HALT alert within 30 seconds of trigger_

## What didn't work

- _e.g. supervisor heartbeat stalled at 11:47 for 4 minutes — task restart? operator review?_
- _e.g. first-light check pushed at 09:25:14, three minutes late — cron drift?_
- _e.g. unexpected qty=0.5 fill on a bot I didn't expect (see MES_V2_SIZING_FORENSIC)_

## Operator decisions for tomorrow

- [ ] Adjust bot lifecycle (promote / demote / retire)?
- [ ] Tighten / loosen daily DD floor?
- [ ] Investigate any specific bot's fills?
- [ ] Change risk_per_trade_pct?
- [ ] File any kaizen tasks?

## Falsification check (pre-committed before launch)

The pre-launch quant review committed to specific falsification
criteria. Honest check today:

- [ ] mnq_futures_sage delivered >0R live today (yes/no)
- [ ] No single-trade USD loss > $250 (yes/no)
- [ ] No single-day-share ratio > 30% (yes/no)
- [ ] At least 1 live trade closed (proves the gate fires) (yes/no)

If 3+ of the above are NO, the strategy may not be working as
expected. Don't panic on day 1 — but flag for review.

## Follow-up TODOs

- [ ] _e.g. investigate the 11:47 supervisor stall_
- [ ] _e.g. confirm Telegram chat ID is correct for ETA_TELEGRAM_CHAT_ID_

---

## Quick checks to run before bed

```powershell
# 1. Final launch-check snapshot
ssh forex-vps "cd C:\EvolutionaryTradingAlgo\eta_engine && python -m eta_engine.scripts.prop_launch_check --json" > debriefs/launch_check_$(date +%Y%m%d_EOD).json

# 2. Drift detector — once live + paper diverge, this populates
ssh forex-vps "cd C:\EvolutionaryTradingAlgo\eta_engine && python -m eta_engine.scripts.diamond_live_paper_drift --json"

# 3. Today's alert log
ssh forex-vps "powershell -Command \"Get-Content 'C:\EvolutionaryTradingAlgo\logs\eta_engine\alerts_log.jsonl' -Tail 50\""

# 4. Sleep — overnight cron + tomorrow's first-light will handle the rest
```

---

## Filed by

- Operator signature: ____________
- Doc commit: `git add docs/POSTLAUNCH_DAY1_<YYYY-MM-DD>.md && git commit -m "docs(launch): day-1 debrief"`
