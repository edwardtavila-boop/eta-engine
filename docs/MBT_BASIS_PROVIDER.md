# MBT Basis Provider — Wiring Guide

Status: scaffolded 2026-05-07. Production default = `log_return_fallback`.
Real basis provider (`cme_basis`) is implemented but un-fired pending a
maintained BTC spot feed.

## Why this matters

`MBTFundingBasisStrategy` is named for a basis-premium fade: short the
CME Micro Bitcoin future when its premium over BTC spot is rich, expect
the premium to decay. That is the *intended* mechanic.

Until this scaffolding landed, no `basis_provider` was wired, so the
strategy was running on a silent fallback: `(close - prev_close) / prev_close`
scaled to bps. That is **not** basis. It is a one-bar log return — a
short-side momentum-fade z-filter, not a basis-decay trade. Walk-forward
results produced in that mode validated a different mechanism than the
strategy name implies.

The devil's-advocate finding here: anyone reading the registry entry,
or any backtest report, would assume "basis fade" — and they would be
wrong. The strategy was misleading-by-default.

This module makes the choice **explicit**:

* The registry entry now carries `basis_provider_kind`. The current value
  documents which proxy is in use.
* Three providers are defined in `feeds/cme_basis_provider.py`:
  * `LogReturnFallbackProvider` — names the silent fallback so audits
    can confirm "we are deliberately on the proxy".
  * `CMEBasisProvider` — the real implementation. Reads BTC spot from a
    CSV (or callable) and returns `(MBT_close - BTC_spot) / BTC_spot * 10_000`.
  * `MockBasisProvider` — deterministic, used in tests.
* The bridge in `strategies/registry_strategy_bridge.py` instantiates a
  provider per the registry's `basis_provider_kind` and passes it to
  `MBTFundingBasisStrategy(cfg, basis_provider=provider)`.

## Wiring options

In `strategies/per_bot_registry.py`, the `mbt_funding_basis` extras dict
controls which provider is wired:

```python
extras={
    ...,
    "basis_provider_kind": "log_return_fallback",
    # Optional: override the default BTC spot CSV path for `cme_basis`:
    # "basis_spot_csv": r"C:\path\to\BTC_5m.csv",
}
```

Recognized values:

| `basis_provider_kind`     | Behavior                                                                                                                                                       |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `internal_log_return`     | No provider attached. Strategy uses its built-in silent fallback. Equivalent to legacy behavior; useful only for explicit "leave it alone" reproducibility.    |
| `log_return_fallback`     | Wires `LogReturnFallbackProvider`. Behaviorally identical to `internal_log_return` but the bridge audit log shows a real provider object — honest naming.      |
| `cme_basis`               | Wires `CMEBasisProvider` against `data/crypto/history/BTC_5m.csv` (or a custom path via `basis_spot_csv`). **Soft-fails to None** if the spot CSV is missing.  |

## Recommended evolution

```
log_return_fallback         <-- TODAY (production default)
            |
            v
coinbase_btc_spot           <-- INTERIM
   - drop maintained        Once `scripts/fetch_crypto_bars_coinbase.py`
     BTC_5m.csv into         is on the daily Kaizen schedule and the
     data/crypto/history/    BTC_5m.csv is fresh (<= 5m staleness),
   - flip basis_provider_    flip the registry to `cme_basis` pointed
     kind to `cme_basis`     at that file. This is the *interim*
                             provider — Coinbase != CME settlement,
                             but it's a trustworthy public spot tape.
            |
            v
cme_brr                     <-- CANONICAL
   - subscribe to CME data  Once a CME data feed is operationalized,
     feed publishing the     replace the CSV-backed `CMEBasisProvider`
     CF Bitcoin Reference    with one that resolves spot via the BRR
     Rate (BRR)              ticker. This is the "right" spot reference
   - implement a callable    for CME-listed futures because BRR is what
     spot source             those contracts settle to at expiry.
   - keep `cme_basis` as
     the kind, swap the
     underlying source
```

The provider interface is `Callable[[BarData], float | None]`, so
swapping spot sources is a one-line change at the construction site —
no strategy edits required.

## How to flip the switch

1. Confirm `data/crypto/history/BTC_5m.csv` is fresh (last row is within
   one bar of the latest MBT bar you intend to score).
2. In `strategies/per_bot_registry.py`, change the `mbt_funding_basis`
   extras to `"basis_provider_kind": "cme_basis"`.
3. Re-run walk-forward validation. Existing log-return-mode results are
   no longer comparable — they were scoring a different mechanism.
4. Update `STRATEGY_OPTIMIZATION_ROADMAP.md` to flag the regime change.

## Generalization to MET (ETH micro future)

`CMEBasisProvider` is symbol-agnostic. To attach the same pattern to
`met_*` strategies:

1. Add `MET ETH spot CSV` (e.g. `data/crypto/history/ETH_5m.csv`).
2. Add a parallel registry extras knob, e.g. `met_basis_provider_kind`.
3. In the bridge's `met_*` dispatch branch, build the provider via
   `build_basis_provider("cme_basis", spot_csv=...)` (or extend the
   factory with an `eth` kind that defaults to `DEFAULT_ETH_SPOT_CSV`).
4. The strategy class needs to accept a `basis_provider` parameter the
   same way `MBTFundingBasisStrategy` already does.

## Open follow-ups (for `cme_basis` to actually fire)

- **Data source:** populate or refresh `data/crypto/history/BTC_5m.csv`.
  `scripts/fetch_crypto_bars_coinbase.py` is the existing fetcher;
  hook it into the Kaizen daily run (or a dedicated cron) so the file
  doesn't drift.
- **Skew tolerance:** `CMEBasisProvider`'s `max_lookup_skew_seconds`
  defaults to 300s (one 5m bar). For lower-resolution spot data raise
  this knob; for tick-level cleanup tighten it.
- **Walk-forward re-validation:** after flipping to `cme_basis`, the
  prior log-return walk-forward is invalid. Re-run before any promotion
  past `research_candidate`.
- **CME BRR feed:** decide whether to bring up a direct CME data feed
  for the BRR (canonical) or stay on Coinbase as the interim reference.
