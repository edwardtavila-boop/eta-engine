# mnq_orb_v2 paper-soak checklist

Start: **2026-05-01**  End: **2026-05-14**
Sessions: **10**
Strategy: **mnq_orb_v2** (MNQ1/5m)
Venue: **ibkr_paper** (DUQ***9869)
Expected trades: **5-10**

## Operator pre-flight

- [ ] IBKR Client Portal Gateway is running on 127.0.0.1:5000
- [ ] Account `DUQ***9869` is a *paper* account (DUH/DU prefix)
- [ ] /MNQ contract roll: confirm IBKR_CONID_MNQ is the active month
- [ ] Risk caps confirmed: 1% per trade, $250 daily-loss circuit breaker
- [ ] EOD flatten time set to 15:55 ET in the live runner

## During the soak

- [ ] Day 1 — first fire matches a backtest re-run on the same bars
- [ ] Day 3 — daily R-PnL inside `pinned_baseline.avg_r ± 1σ`
- [ ] Day 7 — running win rate within ±10pp of pinned baseline
- [ ] Day 14 — total trades inside the 5-10 band

## After the soak

- [ ] Promote to next risk tier ONLY if all four checkpoints passed
- [ ] Append the run summary to `docs/research_log/`
- [ ] If failed: file an incident in `docs/incidents/` and DO NOT
      ship to live without a signed-off remediation
