# IBKR Pro Level 2 + CME — data inventory & upgrade gaps

**Status:** 2026-05-08. Operator unlocked IBKR Pro Level 2 (Depth of
Market, "Booktrader") and CME real-time market data. Audit what we
currently consume vs what L2/CME unlocks vs what we'd need to build
to actually leverage it.

> **Historical snapshot note:** This inventory captures a 2026-05-08 data
> access planning state. Treat its live-cutover timing and un-dormancy
> references as historical context, and defer to current ETA
> launch/readiness surfaces plus current broker-routing policy before acting.

---

## What we currently have ✓

### Bar-based historical data (canonical at `mnq_data/history/`)

| Asset class | Symbols | Timeframes | Provider chain |
|---|---|---|---|
| Equity index futures | MNQ, NQ, ES, MES, M2K, RTY, YM, MYM | 1m / 5m / 1h / 4h / D | TWS API (`fetch_tws_historical_bars.py`) + yfinance composite (`fetch_index_futures_bars.py`) |
| Commodity futures | GC, MGC, CL, MCL, NG | 5m / 1h / D | TWS API back-fetch |
| Rates / FX | ZN, 6E, M6E | 5m / 1h | TWS API back-fetch |
| Crypto futures (CME) | MBT, MET | 5m / 1h / D | TWS API + Client Portal Gateway |
| Crypto spot | BTC, ETH, SOL | 1m / 5m / 1h / D | Coinbase (`fetch_btc_bars.py`) + IBKR (`fetch_ibkr_crypto_bars.py`) + Alpaca |
| Auxiliary feeds | DXY, BTCONCHAIN, FEAR_GREED, ETF flows, funding | D | Multiple yfinance + onchain + farside |

**Bar schema:** `timestamp_utc, epoch_s, open, high, low, close, volume,
session`. Aggregated trade volume — **no bid/ask split** in the bar
data.

### Real-time data path (currently wired)

- `feeds/bar_accumulator.py` — IBKR `reqMktData` calls for Level 1
  quotes (best bid/ask + last trade)
- `feeds/composite_feed.py` — multi-broker live data aggregator
- TWS API client (`venues/ibkr_live.py`) — order routing only, NO L2
  depth code path
- Client Portal Gateway path (`scripts/fetch_mbt_met_bars.py`) — REST
  bars only

### Strategy data dependencies (current)

12-bot active pin consumes ONLY bar data. The richest bar-level
intelligence is in `strategies/smc_primitives.py`:
- `find_equal_levels` — equal highs/lows from bar tops/bottoms
- `detect_liquidity_sweep` — bar-only proxy (wick through equal level + body closes back inside)
- `detect_displacement` — body >= mult × median body
- `detect_fvg` — fair-value-gap from 3 consecutive bars
- `detect_break_of_structure` — swing high/low from bars
- `detect_order_block` — last opposing bar before a BOS

**Every "liquidity"-flavored detector is currently a BAR PROXY** for
something L2 could measure directly.

---

## What L2 + CME real-time unlocks ✗

### Data classes we're NOT capturing today

| Data class | What it adds | Strategies that benefit |
|---|---|---|
| **Depth of Market (10 levels deep)** | bid/ask quantities at 5-10 price levels above and below NBBO | sweep_reclaim, anchor_sweep, volume_profile, all SMC detectors |
| **Order book imbalance** | sum(bid_qty) ÷ sum(ask_qty) at top-N levels | momentum / scalping bots; pre-entry regime filter |
| **Tick-by-tick trades** | every trade (price, size, aggressor side) | footprint charts, true volume profile, iceberg detection |
| **Bid/ask volume split per bar** | buy-aggressor vs sell-aggressor volume | volume_profile gets actual buy/sell pressure instead of net |
| **Spread time series** | live bid-ask spread track | spread-regime filter (skip wide-spread minutes) |
| **Quote-update rate** | quotes per second per symbol | "thin book" detection — avoid trading when no liquidity |
| **NBBO changes** | every best-bid / best-ask refresh | latency-sensitive scalping; reject signals if NBBO drifted before fill |

### Strategies in the current 12-bot pin that would directly benefit

1. **volume_profile_mnq** (STRICT-GATE PASS, sh_def +2.91) —
   bar-volume-derived POC could become true bid/ask-split footprint.
   Already the strongest edge; L2 could tighten entries further.
2. **sweep_reclaim family** (mcl, mym, ng, m2k, eur_sweep_reclaim) —
   wick% is a proxy; L2 shows the ACTUAL stop-orders that got swept.
   Reduces false signals when wick is technical noise.
3. **mnq_anchor_sweep** — named-anchor sweeps (PDH/PDL/PMH/PML/ONH/
   ONL) currently confirmed by bar action; L2 would verify the
   anchor had liquidity sitting on it BEFORE the sweep.
4. **rsi_mr_mnq_v2** — RSI/BB mean-reversion gets noisy near
   thin-book moments; spread / quote-rate filter would cull bad
   entries.
5. **mbt_funding_basis** — basis is a slow signal; book imbalance
   gives faster confirmation that the basis dislocation is real.
6. **sol_optimized** (Alpaca crypto) — different venue, but
   conceptually same: order-book imbalance on Alpaca is its own
   feed gap (Alpaca offers crypto L2 via stream).

### New strategy categories L2 enables

- **Order-flow / footprint scalping** — short-horizon entries off
  imbalance + aggressor flips
- **Iceberg detection** — same price prints repeatedly with the same
  qty refilling on the bid (or ask)
- **Stop-run hunting** — see clustered stops on the book before the
  market reaches them
- **Liquidity-driven exits** — close early when the favorable side
  of the book thins

---

## What's missing — concrete gaps

### Code gaps

| Component | Status | Effort to add |
|---|---|---|
| IBKR `reqMktDepth` wiring in `venues/ibkr_live.py` | **MISSING** | 2-3 days |
| Tick capture (`reqTickByTickData`) in bar_accumulator | **MISSING** | 1-2 days |
| Storage format for depth snapshots (parquet/sqlite) | **MISSING** | 1 day |
| Storage format for tick stream (append-only jsonl or parquet) | **MISSING** | 1 day |
| `feeds/orderbook_feed.py` — live depth aggregator | **MISSING** | 2 days |
| Strategy module: `book_imbalance_strategy.py` | **MISSING** | 3-5 days |
| Strategy module: `footprint_strategy.py` | **MISSING** | 1 week |
| Bar-builder extension: per-bar bid/ask volume split | **MISSING** | 2 days |
| L2 backtest harness (replay book snapshots) | **MISSING** | 3-5 days |

### Data gaps

- **Zero tick-by-tick history.** TWS API supports
  `reqHistoricalTicks` for limited windows; we don't call it.
- **Zero depth snapshots.** L2 is realtime-only; we'd need to start
  capturing now to build any history.
- **No bid/ask volume split in historical bars.** Every backtest
  treats volume as one aggregate number.
- **No spread tracking.** We don't know historical bid-ask spread by
  minute, by session, by symbol.
- **No quote-update-rate metric** to gate strategies during thin-book
  windows.

### Subscription gaps to verify

The operator says they have Level 2 + CME on IBKR Pro. Confirm
during setup:

- **CME Real-Time (Globex + Floor)** — full eMini / Micro suite at
  realtime: $11/month per exchange waived above threshold
- **CME Depth of Book** — separate add-on if not bundled, ~$15/mo
- **NYMEX / COMEX realtime** — for CL, MCL, NG, GC, MGC
- **CBOT realtime** — for ZN, ZB, YM, MYM
- **ICE Forex realtime** — for 6E, M6E

If any of these are NOT yet activated in the IBKR account, the
strategies that depend on them will silently get 15-min delayed
data and degrade quietly.

---

## Recommended upgrade path

### Phase 1 — capture (do this FIRST, before strategy changes)

1. **Add `reqTickByTickData` capture** in `bar_accumulator.py`. Persist
   ticks to `mnq_data/ticks/<SYMBOL>_<DATE>.jsonl`. Volume tag
   includes aggressor side. ~1 week of tick data is enough to start
   bid/ask-split bar reconstruction.
2. **Add `reqMktDepth` snapshot loop** (e.g. once per second) for
   top 5 levels. Persist to `mnq_data/depth/<SYMBOL>_<DATE>.jsonl`.
   This is the only way to get ANY depth history — start ASAP.
3. **Add spread tracker** to write per-minute spread medians /
   quartiles to `mnq_data/spread/<SYMBOL>_<DATE>.csv`.

### Phase 2 — bar enrichment (no strategy changes yet)

4. **Bar builder upgrade**: reconstruct bars from tick stream with
   bid/ask volume split. New schema:
   `timestamp_utc, epoch_s, o, h, l, c, vol_total, vol_buy, vol_sell, session`.
5. **Backfill on the historical bars** by replaying ticks for the
   window where ticks exist. Old bars stay at `vol_buy=vol_sell=NaN`
   (sentinel).

### Phase 3 — strategy upgrades (after Phase 1+2 collect data)

6. **volume_profile_mnq → v2** consuming buy/sell-split volume.
   Should preserve strict-gate pass while reducing false-POC pulls.
7. **sweep_reclaim family → v2** confirming wick sweep with actual
   stop-cluster L2 data at the swept level.
8. **anchor_sweep → v2** with pre-touch depth check on the anchor.

### Phase 4 — new strategies

9. **`book_imbalance_strategy.py`** — entry when top-3-level bid/ask
   imbalance > threshold for N consecutive ticks. Backtest on the
   tick history accumulated in Phase 1.
10. **`spread_regime_filter`** — global gate that pauses ALL strategies
    when spread > N×median (e.g. 10s, FOMC announcements).

### Phase 5 — production

11. **L2 backtest harness** — replay depth snapshots through Phase 3
    strategies for honest pre-live evaluation.

---

## Quick wins available IMMEDIATELY (no L2 build required)

These don't need new infra; they leverage what we already have but
turn on data we may already be receiving:

- **Confirm CME / NYMEX / COMEX / CBOT / ICE realtime is ACTIVE** on
  the IBKR Pro account. If any are still delayed, the live supervisor
  is making decisions on 15-min stale prices on those symbols —
  silently bad. Verify via `bar_accumulator` log: look for
  `delayed_market_data=False` on each subscribed contract.
- **Add IBKR Pro Level 1 NBBO logging** even without L2 build. Tick-
  by-tick best bid/ask is cheap and gives us spread + quote-rate
  immediately. Cost: ~50 lines in `bar_accumulator.py`.
- **Set up Level 2 viewer for OPERATOR EYES** (TWS BookTrader window)
  on MNQ during live trading — operator sees the depth even before
  ETA does. Manual circuit breaker if book looks broken.

---

## Effort summary

| Phase | Time | Operator-visible win |
|---|---|---|
| Verify subscriptions active | 1 hour | No silent delayed data |
| Phase 1 capture | 1 week | Build the tick + depth history that doesn't exist yet |
| Phase 2 bar enrichment | 1 week | Existing 12-bot pin can opt-in to buy/sell split |
| Phase 3 strategy upgrades | 2-3 weeks | volume_profile_mnq v2 + sweep_reclaim v2 |
| Phase 4 new strategies | 3-4 weeks | book_imbalance + spread_regime |
| Phase 5 L2 backtest | 2 weeks | Honest pre-live eval of L2 strategies |

**Total ~2-3 months of focused work** to fully leverage L2 + CME.

**But:** Phase 1 alone (~1 week) starts capturing irreplaceable
history. Every day we don't capture is a day's worth of tick + depth
data lost forever. **Recommend starting Phase 1 immediately**, in
parallel with the 7-day paper-soak + prop-eval window.

---

## What does NOT change

The current 12-bot pin keeps trading on bar data. The audit-confirmed
edge (volume_profile_mnq sh_def +2.91, etc.) is real on OHLCV alone.
L2 makes edges TIGHTER, not from-nothing — these strategies stay
profitable on bars while we build the L2 layer.

> **Dormancy reminder:** The historical Day-7 live cutover plan routed
> orders via the prop-firm Tradovate path (BluSky + Elite, both
> bot-friendly per `dormancy_mandate.md` Appendix A). The L2 data
> upgrade described here is parallel to that: market data into ETA
> from IBKR Pro, orders out via the prop-firm lane. The Tradovate
> references in this doc are scoped to that older un-dormancy path
> already governed in `PROP_FIRM_LIVE_CUTOVER_PLAN.md` — different
> lanes (data vs orders), shared historical un-dormancy gate.
