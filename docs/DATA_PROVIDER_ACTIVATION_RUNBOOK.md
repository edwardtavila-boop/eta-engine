# Data Provider Activation Runbook

Updated: 2026-05-14

## Rule

No paid/live provider is activated unless the operator approves a paired code
and docs change. Broker routing remains unchanged unless the task explicitly
targets broker routing.

The VPS is the 24/7 IBKR Gateway authority. A local or home-desktop Gateway
session is a bug, not a fallback. If Gateway recovery is needed, run it on the
VPS lane and keep local Gateway tasks disabled.

## Phase A - Internal Truth First

- Run `python -m eta_engine.scripts.symbol_intelligence_audit --json --write`.
- Verify `var/eta_engine/state/symbol_intelligence_latest.json`.
- Verify close/outcome backfill before adding new feeds.
- Confirm the symbol-intelligence lake root is `var/eta_engine/data_lake`.
- Keep all writes under `C:\EvolutionaryTradingAlgo`.

## Phase B - IBKR Real-Time Capture

- Confirm CME Group Level 1 is active for MNQ/NQ/ES/MES.
- Confirm the paper account shares live market data.
- Capture L1 top-of-book, ticks, spread, and quote rate.
- Store normalized records in `var/eta_engine/data_lake`.
- Do not enable strategy decisions from L2/order-flow features yet.

## Phase C - Databento Historical Research

- Operator must approve Databento activation.
- Start with usage-based or Standard.
- Pull OHLCV/trades/definitions/statistics for MNQ/NQ/ES/MES/YM/MYM.
- Store normalized records in `var/eta_engine/data_lake`.
- Compare against IBKR bars before research use.

## Phase D - Macro And News

- Use FRED/BLS/EIA/Treasury for public macro series and event context.
- Use Benzinga-style paid news only after event storage and dedupe exist.
- Sentiment remains advisory until post-trade attribution shows edge.
- SEC/EDGAR and filings are equity/ETF context first, not futures execution truth.

## Phase E - Order Flow

- Capture before trading.
- Use order-flow features first as filters.
- Promote to strategy input only after closed-trade attribution improves.
- Keep Level 2 optional until Level 1 capture and broker stability are green.

## Current Verification Commands

Run from `C:\EvolutionaryTradingAlgo\eta_engine`:

```powershell
python -m eta_engine.scripts.verify_ibkr_subscriptions --json
python -m eta_engine.scripts.symbol_intelligence_audit --json --write --bootstrap-existing
python -m eta_engine.scripts.data_health_check --json
```

The current first-pass symbol-intelligence target is GREEN for `MNQ1`, then
GREEN or intentionally accepted AMBER for the runner-up futures symbols.
