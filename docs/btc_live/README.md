# BTC Live Docs Surface

This directory contains checked-in BTC paper/live artifacts kept for audit,
debugging, and historical reference.

These files are not the authoritative live runtime surface.

Some top-level checked-in snapshots here, including files like
`btc_live_latest.json` and `btc_live_gate_decision.json`, were captured during
earlier runs and may still contain legacy path strings from pre-canonical
migrations. Treat them as archived evidence, not as current configuration or
runtime truth.

Current authoritative BTC runtime paths live under the canonical workspace
state tree:

- `var/eta_engine/state/btc_live/`
- `var/eta_engine/state/broker_fleet/`
- `var/eta_engine/state/btc_paper/`

Before using any file in `docs/btc_live/` for an operator decision, compare it
against the matching canonical runtime state or regenerate it from current
runtime data.
