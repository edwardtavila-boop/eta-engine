# Broker Connection Sweep

This folder holds historical checked-in broker/exchange connection probe
artifacts.

Fresh local probe runs now write canonical runtime outputs under
`var/eta_engine/state/broker_connections/`. Checked-in JSON files here are
historical snapshots only and should not be re-staged into source history.

## Purpose

- Probe configured brokers without placing trades.
- Report supported venues as `READY`, `STUBBED`, or `DEGRADED`.
- Report unsupported broker names explicitly as `UNAVAILABLE`.
- Keep a timestamped JSON artifact plus a `*_latest.json` snapshot for
  automation, without using checked-in `docs/` as the live truth surface.

## Primary entry point

- Module: `python -m eta_engine.scripts.connect_brokers`

## Common usage

Run these from the repo root:

```powershell
python -m eta_engine.scripts.connect_brokers --probe
python -m eta_engine.scripts.connect_brokers --brokers ibkr tastytrade bybit okx
python -m eta_engine.scripts.connect_brokers --config .\config.json --json
python -m eta_engine.scripts.connect_brokers --reconnect ibkr
```

> **Dormancy note (2026-04-24):** Tradovate is currently DORMANT (funding-blocked).
> The active futures brokers are **IBKR (primary) + Tastytrade (fallback)**. The
> sweep will still probe `tradovate` if you pass it explicitly, but it is
> deliberately excluded from the default broker list. Single-point-of-truth for
> re-enablement: `eta_engine/venues/router.py` `DORMANT_BROKERS` frozenset.

## Output

- Canonical report directory: `var/eta_engine/state/broker_connections/`
- This `docs/broker_connections/` folder is historical/reference only
- Latest JSON snapshot: `broker_connections_latest.json`
- Timestamped artifact: `broker_connections_YYYYMMDDTHHMMSSZ.json`

## Config shapes

The sweep reads the configured broker list from these fields when present:

- top-level `brokers`
- top-level `venues`
- `execution.brokers`
- `execution.futures.broker_primary` / `broker_backup`
- `execution.futures.broker_backups`
- `execution.crypto.exchange_primary` / `exchange_backups`

## Notes

- Missing credentials do not fail the sweep; they are reported as `STUBBED`.
- A real connectivity failure on a supported venue is reported as `FAILED`.
- Tastytrade and IBKR are paper-gated adapters. Missing credentials report as
  `STUBBED`; ready paper configuration reports as `READY`.
- If the venue exists but the adapter has not been implemented yet, the sweep reports `UNAVAILABLE`.
