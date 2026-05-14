# Symbol Intelligence Data Flywheel Design

Date: 2026-05-14
Workspace: `C:\EvolutionaryTradingAlgo`
Lane: `eta_engine`
Status: design anchor ready for operator review

## Goal

Turn ETA's scattered market data, broker fills, Jarvis decisions, news, macro
events, and trade outcomes into one ticker-relative memory layer.

The goal is not to buy every feed or make news a magic signal. The goal is to
make every strategy smarter over time by answering, for each symbol and trade:

- What was price doing before, during, and after the decision?
- What market regime was active?
- What news or macro event was near the decision?
- What did Jarvis believe, approve, deny, or skip?
- Did the outcome prove the belief useful, useless, or dangerous?

## Existing Surfaces To Preserve

ETA already has useful parts of this system:

- `eta_engine/data/requirements.py` declares per-bot data needs.
- `eta_engine/data/library.py` discovers canonical CSV datasets.
- `eta_engine/data/audit.py` reports missing critical and optional data.
- `eta_engine/data/event_calendar.py` reads operator-curated macro events.
- `eta_engine/scripts/data_health_check.py` summarizes bot data coverage.
- `eta_engine/scripts/hydrate_canonical_market_data.py` imports local futures
  and crypto data into canonical roots.
- `eta_engine/scripts/l2_news_blackout.py` blocks risky scheduled-event windows.
- `eta_engine/scripts/closed_trade_ledger.py` and runtime journals hold
  post-trade evidence.
- Runtime state under `C:\EvolutionaryTradingAlgo\var\eta_engine\state` already
  includes decision journals, calibration labels, Jarvis traces, and close
  evidence.

The flywheel should extend these surfaces rather than replace them.

## Provider Roles

### Broker truth

IBKR and Tastytrade remain execution truth. They supply account state, fills,
positions, bracket status, session PnL, and live trading readiness.

They should not become the only research warehouse. IBKR itself documents
traffic and historical-data limits because it is a broker, not a specialized
market-data vendor. Broker data is authoritative for what ETA actually traded;
research-grade market history should be independently stored and reconciled.

### Futures research truth

Databento remains dormant until explicitly activated, but it is the strongest
fit for futures research once the operator approves. It supports trade, top of
book, depth, order-book, definitions, statistics, status, and OHLCV schemas.

Recommended activation order:

1. OHLCV and trades for `MNQ`, `NQ`, `ES`, `MES`, `YM`, `MYM`.
2. Definitions and statistics for contract roll, tick size, and open interest.
3. MBP-1 or BBO for spread and top-of-book quality.
4. MBP-10 or MBO only after a strategy proves it needs depth or queue features.

### Crypto and equity context

Alpaca-style market data is useful for equities, ETFs, crypto, and news context.
It should enrich ETA's view of broad risk appetite and public-market catalysts,
not override futures broker truth.

### News and macro truth

Macro and scheduled-event data should be treated as a risk filter first:

- CPI, FOMC, NFP, Fed speakers, EIA, OPEC, Treasury auctions, and major expiry
  windows can force no-entry or reduced-risk windows.
- News headlines should be stored with source, timestamp, tickers/entities,
  urgency, sentiment, dedupe hash, and whether the item arrived before or after
  ETA's decision.
- Sentiment is only trusted after post-trade attribution proves that it improves
  outcomes for a specific symbol and strategy.

FRED and Nasdaq Data Link are useful for macro time series. Benzinga-style news
feeds are useful for event calendars and real-time market stories if the
operator chooses a paid news source. SEC EDGAR matters for equity/ETF and public
company catalysts, but is secondary to futures execution today.

## Architecture

### 1. Canonical data lake

Create a new canonical runtime data root:

`C:\EvolutionaryTradingAlgo\var\eta_engine\data_lake`

Suggested partition shape:

- `bars/<source>/<symbol>/<timeframe>/<yyyy-mm-dd>.jsonl`
- `ticks/<source>/<symbol>/<yyyy-mm-dd>.jsonl`
- `book/<source>/<symbol>/<yyyy-mm-dd>.jsonl`
- `events/<source>/<yyyy-mm-dd>.jsonl`
- `news/<source>/<yyyy-mm-dd>.jsonl`
- `decisions/<symbol>/<yyyy-mm-dd>.jsonl`
- `outcomes/<symbol>/<yyyy-mm-dd>.jsonl`
- `quality/<yyyy-mm-dd>.json`

The existing CSV library stays intact. The data lake becomes the append-only
runtime/research memory that can later compact to parquet.

### 2. Normalized event schema

Use one lightweight envelope for every record:

```json
{
  "schema": "eta.symbol_intel.v1",
  "record_type": "bar|tick|book|news|macro_event|decision|outcome|quality",
  "ts_utc": "2026-05-14T14:30:00+00:00",
  "symbol": "MNQ",
  "source": "ibkr|tastytrade|databento|alpaca|fred|benzinga|operator|jarvis",
  "payload": {},
  "quality": {
    "confidence": 0.0,
    "latency_ms": null,
    "is_stale": false,
    "is_reconciled": false
  }
}
```

This avoids one-off file formats while letting each source keep its native
payload inside `payload`.

### 3. Symbol feature snapshots

Build a derived snapshot per symbol and decision time:

- Returns over 1m, 5m, 15m, 1h, and session.
- ATR, realized volatility, VWAP distance, and volume profile location.
- Prior high/low, overnight high/low, RTH open range, and liquidity sweep zones.
- Correlation context: `ES`, `NQ`, `YM`, `VIX`, `DXY`, rates, BTC/ETH where
  relevant.
- Event proximity: next high-impact event and minutes since latest headline.
- Broker execution context: spread, fill quality, slippage, open exposure, and
  bracket coverage.

These snapshots become the common input for research, Jarvis explanations, and
post-trade learning.

### 4. Post-trade attribution

Every closed trade should produce an outcome record:

- Entry time, exit time, entry price, exit price, side, qty, strategy, and venue.
- Realized PnL, R multiple, MFE, MAE, time in trade, and slippage.
- Nearby news/macro events before entry and before exit.
- Regime at entry and regime at exit.
- Whether the trade respected or violated blackout, trend, volatility, and
  correlation filters.

This is where ETA gets smarter: a feed only earns influence if it repeatedly
improves decisions after being measured against closed trades.

## Data Quality Firewall

Every collector must emit quality evidence before data is allowed into research:

- Stale check: newest record is within the expected delay window.
- Gap check: missing bars are counted by session and symbol.
- Duplicate check: repeated timestamps and impossible OHLC bars are quarantined.
- Roll check: futures symbols map to the expected active contract.
- Source check: broker fills reconcile against broker open/close evidence.
- Calendar check: high-impact scheduled events are current, not stale seed data.
- Drift check: primary and secondary feeds disagreeing beyond threshold is
  surfaced as an operator warning, not silently averaged.

Bad data should become a visible warning, not a hidden trading input.

## Data Flow

1. Collect raw source records into `data_lake`.
2. Normalize records into the common envelope.
3. Run quality checks and write daily quality snapshots.
4. Build symbol feature snapshots for active strategy symbols.
5. Join feature snapshots to Jarvis decisions and broker outcomes.
6. Feed clean outcomes back into strategy supercharge, retune queue, dashboard,
   and Jarvis explainers.

## First Implementation Slice

The first slice should be deliberately small:

1. Add `SymbolIntelRecord` and `SymbolIntelStore`.
2. Add a read-only audit that reports existing data coverage plus missing
   symbol-intelligence components for `MNQ`, `NQ`, `ES`, `MES`, `YM`, and `MYM`.
3. Backfill decision and outcome records from existing journals without changing
   live routing.
4. Add daily quality snapshot output under the canonical `var` path.
5. Surface the score on the ops dashboard as "Data Intelligence" without
   affecting trade decisions.

Only after this passes should ETA add new paid collectors.

## Explicit Non-Goals

- Do not activate Databento without an operator-approved code and docs change.
- Do not buy or wire Level 2/Level 3 depth just because it exists.
- Do not make sentiment a live approval signal until post-trade attribution
  proves value.
- Do not write data outside `C:\EvolutionaryTradingAlgo`.
- Do not modify broker routing or live trade permissions in this data batch.

## Testing

Minimum verification for the first slice:

- Unit tests for schema serialization and append/read behavior.
- Unit tests that bad paths cannot escape `C:\EvolutionaryTradingAlgo`.
- Fixture tests for stale, gap, duplicate, and source disagreement warnings.
- A smoke test that reads current state journals and emits a quality snapshot.
- Dashboard/API test that the new data-intelligence score is read-only.

## Success Criteria

The batch is successful when ETA can show, for each priority futures symbol:

- Which price, event, news, decision, and outcome datasets exist.
- Which feeds are stale, missing, or low-confidence.
- Which strategy decisions have complete context attached.
- Which closed trades have enough evidence for attribution.
- What data would most improve `volume_profile_mnq` and 2-3 futures runner-ups.

That gives ETA a compounding data advantage without risking the live execution
lane.
