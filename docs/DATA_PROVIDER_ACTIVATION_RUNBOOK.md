# Data Provider Activation Runbook

Updated: 2026-05-14

This runbook is the operator checklist for reaching the lean-serious data tier:
reliable IBKR futures data, local capture, free macro/event context, realized
outcomes, and optional paid news/depth once the core feed is proven.

## Current State

- IBKR Gateway: recovered locally on `127.0.0.1:4002`.
- IBKR API handshake: healthy in `var/eta_engine/state/tws_watchdog.json`.
- IBKR market data entitlement probe: blocked by IBKR Error 354 for CME, NYMEX, COMEX, CBOT, and MNQ depth.
- Symbol intelligence lake: active at `C:\EvolutionaryTradingAlgo\var\eta_engine\data_lake`.
- Symbol intelligence snapshot: active at `C:\EvolutionaryTradingAlgo\var\eta_engine\state\symbol_intelligence_latest.json`.
- Current symbol intelligence status: AMBER after bootstrapping existing CSV bars, event calendar, decision journal, and realized outcomes.

## Prove The Feed

Run these from `C:\EvolutionaryTradingAlgo\eta_engine`:

```powershell
python -m eta_engine.scripts.verify_ibkr_subscriptions --json
python -m eta_engine.scripts.symbol_intelligence_audit --json --write --bootstrap-existing
python -m eta_engine.scripts.data_health_check --json
```

Pass criteria before serious prop/live testing:

- `verify_ibkr_subscriptions`: CME/MNQ top-of-book is realtime.
- `verify_ibkr_subscriptions`: depth is green only if Level 2 is intentionally subscribed.
- `symbol_intelligence_audit`: `MNQ1` is GREEN.
- `data_health_check`: `volume_profile_mnq`, `mnq_futures_sage`, and runner-up futures bots stay GREEN.

## IBKR Paper Market Data Fix

If the verifier reports Error 354:

1. Log into the live account Client Portal.
2. Open the user menu, then `Settings`.
3. Under `Account Settings`, open `Account Configuration`.
4. Open `Paper Trading Account`.
5. Set `Share real-time market data with your paper trading account?` to `Yes`.
6. Select the live username whose data should be shared.
7. Save.
8. Log out of the paper account and IB Gateway.
9. Log back into the paper account.
10. Rerun `python -m eta_engine.scripts.verify_ibkr_subscriptions --json`.

Do not assume the portal checkbox worked until the verifier is green.

## Subscription Priority

Priority order for this project:

1. CME Group top-of-book Level 1. Required for MNQ/NQ/ES/MES/YM/MYM futures testing.
2. CBOT/COMEX/NYMEX top-of-book only when those strategies are actively promoted.
3. Level 2 depth only after the Level 1 verifier is green and the local capture job is writing book records.
4. Paid news/calendar only after the core broker feed and local lake are stable.

Level 2 is useful for order-book imbalance, spoofing/absorption studies, slippage modeling,
and better execution diagnostics. It is not required for the current volume-profile MNQ
priority lane unless we are actively testing L2 features.

## Local Data Spine

The canonical joined record format is `eta_engine.data.symbol_intel.SymbolIntelRecord`.

Record types:

- `bar`: price history and future live bar capture.
- `tick`: future tick capture.
- `book`: future Level 2/order-book capture.
- `news`: future news vendor capture.
- `macro_event`: event calendar and macro risk windows.
- `decision`: Jarvis and supervisor decisions.
- `outcome`: realized trade outcomes.
- `quality`: audit and reconciliation stamps.

Canonical write path:

```text
C:\EvolutionaryTradingAlgo\var\eta_engine\data_lake
```

Never write production data to OneDrive, `%LOCALAPPDATA%`, `C:\mnq_data`, or old Firm paths.

## What Is Missing

Current missing items after the 2026-05-14 bootstrap:

- `NQ1`, `MES1`, and `YM1`: realized outcomes are missing from the joined symbol-intel lake.
- `ES1`: Jarvis decisions and realized outcomes are missing from the joined symbol-intel lake.
- All priority symbols: paid news records are not connected yet.
- All priority symbols: Level 2/book records are not connected yet.
- IBKR market-data verifier still fails until account-side data sharing/subscription is fixed.

## Activation Sequence

1. Fix IBKR Error 354 in Client Portal.
2. Rerun the IBKR subscription verifier.
3. Keep Level 2 disabled unless the L1 verifier is green.
4. Run the symbol-intelligence bootstrap audit.
5. Promote only strategies whose symbol coverage is GREEN or intentionally accepted AMBER.
6. Add paid news only after the broker feed is stable and the dashboard is consuming the symbol-intel snapshot.

## Dashboard Contract

Dashboards should read:

```text
C:\EvolutionaryTradingAlgo\var\eta_engine\state\symbol_intelligence_latest.json
```

The snapshot is operator-facing and safe to expose. It contains coverage and missing-data state,
not private broker credentials or account secrets.
