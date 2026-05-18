# Prop-Firm + Small-Capital Live Cutover Plan

**Status:** Historical planning snapshot from 2026-05-09. Operator approved a controlled Tradovate reactivation path for prop-fund testing with winning strategies in that planning window.

This document records the un-dormancy path for that prop-firm futures testing lane. It does not place trades by itself. Tradovate remains dormant by default unless the live process explicitly sets `ETA_TRADOVATE_ENABLED=1`.

> **Safety note:** This plan governs the separate futures prop-ladder
> controlled dry-run lane centered on `volume_profile_mnq`. It does not
> authorize the Diamond/Wave-25 launch lane by itself. Keep checking
> `python -m eta_engine.scripts.prop_launch_check --json` for the separate
> launch-candidate cutover verdict.

## Activation Gate

Tradovate was dormant under the 2026-04-24 broker mandate. The approved reactivation is now narrow:

- Scope: prop-fund testing for winning futures strategies, starting with `volume_profile_mnq`.
- Enable flag: `ETA_TRADOVATE_ENABLED=1`.
- Default behavior: if the flag is absent, Tradovate stays in `DORMANT_BROKERS` and futures route through IBKR/Tastytrade.
- Required before live prop orders: firm account ID, Tradovate credentials, `TRADOVATE_LIVE`/demo setting, broker-router config, bracket/stop protection, daily-loss guard, and operator-watched first fills.
- Do not use offshore venues for US-person live routing.

## Prop Firm Pick

Primary: **BluSky Trading**.

Why: BluSky has the clearest official language found for ETA's use case: automated trading with bots is allowed, including semi-automated and fully automated systems.

Secondary: **My Funded Futures**.

Why: My Funded Futures officially allows tailored automated trading strategies, with clear restrictions: no HFT, no simulated-fill exploitation, CME rules required live, and no collaborative/copy-trading abuse. It also documents Tradovate login support.

Watchlist only:

- **Elite Trader Funding:** promising because several restrictions were removed in the 2025 policy update, but I did not find a clear official REST+WS custom-bot permission source. Use only after written confirmation.
- **Tradeify:** allows sole-owned bots, but the agreement restricts using the same bot across multiple firms. Do not use for dual-prop replication without written approval.
- **Topstep:** API automation is allowed, but VPS/remote-server execution is prohibited, which conflicts with ETA's VPS runtime.
- **Apex Trader Funding:** no-go for this use case because official rules prohibit automation/algorithm usage.

Official policy evidence:

- BluSky automation: https://blog.blusky.pro/blusky-blog/attention-prop-firm-traders
- My Funded Futures automation policy: https://help.myfundedfutures.com/en/articles/8444599-fair-play-and-prohibited-trading-practices
- My Funded Futures Tradovate login: https://help.myfundedfutures.com/en/articles/8445591-tradovate-login-instructions
- Apex prohibited activities: https://apextraderfunding.com/help-center/getting-started/prohibited-activities/
- Topstep API/VPS restriction: https://help.topstep.com/en/articles/11187768-topstepx-api-access
- Tradeify funded trader agreement: https://tradeify.co/funded-trader-agreement

## Strategy Ranking For Prop Test

Readiness snapshots now expose the capital-priority order directly:
equity-index futures first, commodities second, rates/FX third, CME crypto
futures fourth, and spot crypto last. Broker priority is IBKR first, then
Tradovate only when explicitly enabled for the prop-test process, then
Tastytrade, then Alpaca for spot-crypto paper/personal lanes.

| Bot | Use | Reason |
|---|---|---|
| `volume_profile_mnq` | Primary prop lane | Current strict-gate pass, `sh_def +2.86`, `n=2916`, MNQ fits prop account structure |
| `volume_profile_nq` | Runner slot 1 | Near-strict Nasdaq upscale lane, `sh_def +2.08`, `n=3073`, but NQ waits for prop buffer |
| `rsi_mr_mnq_v2` | Runner slot 2 | MNQ runner-up, split-stable but still watch-only until deflated Sharpe improves |
| `mym_sweep_reclaim` | Runner slot 3 | Dow micro diversification candidate, strong per-trade result but only `n=11` |
| `mes_sweep_reclaim_v2` / `mnq_anchor_sweep` | Reserve runners | S&P micro / additional MNQ research lanes; no prop route until strict evidence improves |
| `ng_sweep_reclaim` / `eur_sweep_reclaim` | Research watch | Commodity/FX lanes need clean 5m data, event filters, and rollover validation before promotion |
| `mbt_funding_basis` | Later CME crypto-futures lane | Futures contract, not spot crypto, but still lower priority than index/commodity work |
| `sol_optimized` | Non-prop diversifier | Alpaca/personal crypto lane, not a futures prop lane |

Within this controlled futures prop-ladder lane, any initial prop-test capital
goes to `volume_profile_mnq` only. Other strategies keep optimizing in
paper/Kaizen until their live-fill evidence is strong enough. This does not
mean the separate Diamond/Wave-25 launch lane is `GO`.

## Automated Ladder And Hard Gate

The futures ladder is the automated ranking surface:

```powershell
cd C:\EvolutionaryTradingAlgo
python -m eta_engine.scripts.futures_prop_ladder --json
python -m eta_engine.scripts.prop_strategy_promotion_audit --json
```

Current expected mode before funding/API unlock is
`FULLY_AUTOMATED_PAPER_PROP_HELD`: the system keeps optimizing in paper,
keeps `volume_profile_mnq` as the primary, and holds 2-3 runner-up slots for
Nasdaq/S&P/Dow minis and micros.

The prop-live gate is the hard latch:

```powershell
cd C:\EvolutionaryTradingAlgo
python -m eta_engine.scripts.closed_trade_ledger
python -m eta_engine.scripts.broker_bracket_audit
python -m eta_engine.scripts.prop_strategy_promotion_audit --json
python -m eta_engine.scripts.prop_live_readiness_gate --json
python -m eta_engine.scripts.prop_operator_checklist
```

It must report `READY_FOR_CONTROLLED_PROP_DRY_RUN` before any prop route edit.
It intentionally blocks if any of these are dirty or missing: Tradovate
cutover readiness, `volume_profile_mnq` live eligibility, router cleanliness,
broker-native bracket/OCO proof, live fleet broker surfaces, or schema-backed
closed-trade outcomes.

Scope note: `prop_live_readiness_gate` and `prop_operator_checklist` are the
futures prop-ladder controlled dry-run lane for `volume_profile_mnq`. They are
not the Diamond/Wave-25 launch gate. Use the parallel launch surface below
when the question is broader launch readiness:

```powershell
cd C:\EvolutionaryTradingAlgo
python -m eta_engine.scripts.prop_launch_check --json
```

## Current Code Path

The repo now supports the first activation layer:

```powershell
$env:ETA_TRADOVATE_ENABLED = "1"
$env:TRADOVATE_ACCOUNT_ID = "<firm account id>"
$env:TRADOVATE_LIVE = "0"   # demo/sim first; set to 1 only after demo smoke is clean
```

Tradovate order payloads now use the configured `accountId` instead of defaulting to `0`. Broker routing config can carry prop account aliases for controlled testing:

```yaml
prop_accounts:
  blusky_50k:
    venue: tradovate
    env: demo
    account_id_env: BLUSKY_TRADOVATE_ACCOUNT_ID
    creds_env_prefix: BLUSKY_
    bot_policy: explicit_allow
    policy_source: https://blog.blusky.pro/blusky-blog/attention-prop-firm-traders
  mffu_50k:
    venue: tradovate
    env: demo
    account_id_env: MFFU_TRADOVATE_ACCOUNT_ID
    creds_env_prefix: MFFU_
    bot_policy: automation_allowed_with_limits
    policy_source: https://help.myfundedfutures.com/en/articles/8444599-fair-play-and-prohibited-trading-practices

bots:
  volume_profile_mnq:
    venue: tradovate
    account_alias: blusky_50k
```

## Cutover Phases

DORMANT context: every Tradovate step below stays behind the default
dormancy gate until a dedicated prop-test process sets
`ETA_TRADOVATE_ENABLED=1`.

Phase 1: Single-account Tradovate demo/sim smoke.

- Enable `ETA_TRADOVATE_ENABLED=1`.
- Seed one account ID and credentials.
- Route only `volume_profile_mnq`.
- Submit demo/sim orders with brackets only.
- Verify order result, fills, broker reconciliation, account ID, and kill-switch behavior.

Phase 2: Second account account-aware smoke.

- Add `mffu_50k` credentials and account ID.
- Repeat the same demo/sim smoke on My Funded Futures.
- Confirm both firms still allow the exact automation pattern in writing or current official docs.

Phase 3: Dual-prop replication.

- Add `routing: replicate` only after both single-account lanes pass.
- Per-account daily loss and trailing drawdown must be independent.
- Failure on one account must not block the other.
- Tradovate platform-wide outage remains shared risk; later diversification should use a non-Tradovate venue/API.

Phase 4: Live watched cutover.

- Start with one MNQ contract.
- Operator watches first 10 fills.
- Pause after any drawdown above 50% of the trailing buffer.
- Keep all non-winning strategies in paper mode.

## Operator Checklist

- Use `docs/TRADOVATE_API_DEPOSIT_PREP.md` as the day-by-day runbook for the
  personal Tradovate API deposit, API add-on, OAuth smoke, readiness check,
  and first dry-run sequence.
- Before funding, run:
  `python -m eta_engine.scripts.tradovate_prop_readiness --phase predeposit`.
- Keep the futures ladder current:
  `python -m eta_engine.scripts.futures_prop_ladder`.
- Keep the primary promotion audit current when checking why
  `volume_profile_mnq` is still held:
  `python -m eta_engine.scripts.prop_strategy_promotion_audit`.
- Keep the closed-trade ledger current:
  `python -m eta_engine.scripts.closed_trade_ledger`.
- Keep broker-native bracket/OCO proof current:
  `python -m eta_engine.scripts.broker_bracket_audit`.
- If the audit flags an open IBKR futures position that already has a
  broker-native TP/SL OCO visible in TWS/IB Gateway, record the short-lived
  manual proof latch:
  `python -m eta_engine.scripts.broker_bracket_audit --ack-manual-oco --symbol MNQM6 --venue ibkr --operator edward --expires-hours 24 --confirm`.
  Do not run this if the broker-side OCO is not actually visible.
- Treat the consolidated gate as the final go/no-go:
  `python -m eta_engine.scripts.prop_live_readiness_gate`.
- Use the compact operator checklist for the exact remaining manual commands:
  `python -m eta_engine.scripts.prop_operator_checklist`.
- Use the parallel Diamond/Wave-25 launch gate for the separate launch story:
  `python -m eta_engine.scripts.prop_launch_check --json`.
- After funding/API activation, require:
  `python -m eta_engine.scripts.tradovate_prop_readiness --phase cutover`
  to report `READY_FOR_DRY_RUN` before editing any winning-bot route.
- Create BluSky and My Funded Futures accounts only after confirming their current automation language.
- Capture the account IDs and Tradovate credential source for each.
- Keep `TRADOVATE_LIVE=0` until demo/sim smoke passes.
- Do not route Apex or Topstep with the VPS bot.
- Keep the 12-bot paper soak and Kaizen reports running in parallel.
