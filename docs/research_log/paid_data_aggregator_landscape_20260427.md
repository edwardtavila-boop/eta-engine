# Paid data aggregator landscape — scoping + integration plan, 2026-04-27

After the 2026-04-27 supercharge thread (commit 973a6aa) proved
that post-hoc filtering / sizing layers cannot lift a sample-
specific result, the next genuine source of OOS-Sharpe lift is
**new uncorrelated information**. We have:

* 5y BTC OHLCV history (Coinbase REST) ✅
* 5y BTC 8h funding history (BitMEX) ✅
* Daily ETF flows (Farside) ✅
* Daily ETH ETF flows (Farside) ✅
* Daily F&G index (alternative.me) ✅
* LTH-supply proxy from Mayer Multiple ✅

We do NOT have, and need, for the next leg up:

* **Open Interest history** (US-friendly historical OI is short-tail
  on free APIs — OKX gives ~3 months, BitMEX gives only current spot)
* **Liquidation history** (where stops are pulled = where price
  moves; this is the strongest leverage signal)
* **Order-flow / cumulative volume delta** (CVD shows whether
  aggression is buying or selling)
* **Order book depth snapshots** (real-time liquidity walls)
* **Real on-chain flows** (exchange reserves, LTH/STH SOPR, MVRV)

Free public endpoints don't give us a usable history of any of
these. The path is a paid aggregator. This doc captures the
candidates with rough specs, pricing posture, and the integration
plan the codebase already supports.

## Aggregator candidates (2026-04-27 snapshot)

### CryptoQuant (https://cryptoquant.com)
**Strength:** on-chain + exchange flows. The strongest
"institutional" data set. Exchange reserves, miner flows,
whale-wallet movements, SOPR, MVRV — the full quant on-chain
toolkit.
**Pricing:** ~$29-99/mo for retail tier; full API access ~$199+/mo.
**Coverage:** historical back to 2017 for most metrics.
**Format:** REST API, JSON, well-documented.
**Integration cost:** Low — endpoints are clean, schemas stable.
**Recommended for:** real on-chain feeds (exchange reserves, LTH
supply) + exchange flow data.

### Glassnode (https://glassnode.com)
**Strength:** Same domain as CryptoQuant, slightly different
metric set. Strong reputation among quant funds.
**Pricing:** ~$29 advanced, ~$99 professional, custom for
institutional. T1 metrics on free tier.
**Coverage:** historical back to genesis for many metrics.
**Format:** REST API, JSON.
**Integration cost:** Low.
**Recommended for:** alternative source for cross-validation
with CryptoQuant; HODL waves; supply distribution by age.

### Coinglass (https://coinglass.com)
**Strength:** **OI, funding, liquidations across all exchanges**
in one place. The natural fit for our gap. Aggregates Binance,
Bybit, OKX, BitMEX, etc. into a single API.
**Pricing:** Free tier with 5 req/min limit; paid ~$29-79/mo for
higher rates + historical. Crypto-only payments accepted.
**Coverage:** OI back to 2020, liquidations back to 2021 on most
metrics.
**Format:** REST API, JSON. Some rate-limit gotchas on free tier.
**Integration cost:** Low-medium. Documentation is thinner than
CryptoQuant / Glassnode but the schemas are recoverable.
**Recommended for:** OI + liquidation data — the highest-leverage
gap right now.

### CoinMetrics (https://coinmetrics.io)
**Strength:** "Datonomy" institutional-grade metrics, market-
microstructure focus.
**Pricing:** Community tier free (some restrictions); paid
starts ~$200+/mo.
**Coverage:** Network-data back to genesis; market-data ~5y.
**Format:** REST API, JSON, also CSV bulk dumps.
**Integration cost:** Low.
**Recommended for:** cross-validation; alternative to Glassnode.

### Kaiko (https://kaiko.com)
**Strength:** Tick-level order book + trade data. Genuinely
high-end market-microstructure tier.
**Pricing:** Institutional-only. $1k+/mo realistically.
**Coverage:** all major exchanges, 5y+ tick data.
**Recommended for:** order-flow / CVD / depth — the long-term
ambition tier. Not the next step.

## Recommended sequencing for ETA

| Priority | Source | Cost | Why |
|---|---|---|---|
| **1** | **Coinglass paid** | ~$29-79/mo | Direct fix for OI + liquidations gap; aggregated across exchanges |
| 2 | CryptoQuant retail | ~$29-99/mo | Real on-chain (exchange reserves, MVRV, SOPR) replaces our Mayer-Multiple proxy |
| 3 | Glassnode advanced | ~$29/mo | Cross-validation; HODL waves are unique |
| 4 (long-term) | Kaiko | $1k+/mo | Order flow + depth — the genuine alpha edge tier |

## Integration plan — provider pattern is already there

The codebase already follows a consistent **provider attachment
pattern** that makes adding a new aggregator trivial:

```python
class CoinglassOIProvider:
    """Daily / 1h Open Interest from Coinglass aggregated API."""

    def __init__(self, csv_path_or_api: Path | str | CoinglassClient) -> None:
        # Load cached history if path; else live API client
        ...

    def __call__(self, bar: BarData) -> float:
        """Return OI in USD billions at-or-before bar.timestamp."""
        ...
```

Then strategies attach via:
```python
strategy.attach_oi_provider(CoinglassOIProvider(csv_path))
```

Existing providers (`EtfFlowProvider`, `FundingRateProvider`,
`FearGreedProvider`, `LthProxyProvider`) already follow exactly
this shape. Adding Coinglass is a ~150-line file: a fetcher
script (`scripts/fetch_btc_oi_coinglass.py`) + a provider class
(`strategies/coinglass_providers.py`).

## Concrete next steps when budget approves

1. **Subscribe to Coinglass paid tier** (or Tatum / similar that
   re-sells Coinglass data with more generous rate-limits).
2. Build `scripts/fetch_btc_oi_coinglass.py` mirroring the
   structure of `fetch_btc_funding_extended.py`. Output schema:
   `time, oi_btc, oi_usd, funding_aggregate, liquidations_long,
   liquidations_short`.
3. Run the fetcher to backfill 5y of OI + liquidations to
   `C:/crypto_data/history/BTCOI_1h.csv` (and 8h variant).
4. Add `CoinglassOIProvider` + `LiquidationProvider` to
   `strategies/macro_confluence_providers.py`.
5. Add new filter knobs to `MacroConfluenceConfig`:
   - `require_oi_alignment: bool` — long requires OI rising
     (new money in); short requires OI falling (capitulation).
   - `liquidation_signal_threshold: float` — fire only after a
     significant liquidation event (price often reverts).
6. Walk-forward sweep across the new feature stack on 5y BTC.

The **architecture is ready**; the **gating constraint is the
data subscription**. ~$30-99/mo unlocks the next wave of research.

## Related current state

* `_fetch_chunk_bitmex` now powers the 5y funding history → covers
  the funding-rate side of the data gap.
* `FundingDivergenceStrategy` (commit forthcoming) is the first
  regime-invariant strategy; uses funding alone, no OI yet.
* When OI data lands, the natural next strategy is a paired
  divergence: short when funding extreme + OI rising (overheated +
  position-stuffed); long when funding extreme + OI falling
  (capitulation + position-clearing).

## Bottom line for the user

Free public US-friendly historical OI / liquidations / on-chain
data is **not enough** for the next leg of OOS-Sharpe lift. The
gap is real and structural — Binance / Bybit are US-blocked, and
US-friendly free APIs only give recent windows.

A **single ~$30-99/mo paid subscription (Coinglass)** unlocks the
biggest gap in our data set right now. The codebase is ready to
absorb it via the existing provider attachment pattern. ~150 lines
of new code + 5y of new historical data + a sweep would let us
test whether OI + liquidations carry edge that's truly
uncorrelated with the price-EMA mechanics that have so far
defined our strategy library.

This is the next concrete decision point: does the user want to
authorize a paid data subscription so this thread of research can
continue?
