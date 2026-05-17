# BTC Paper Snapshots

This folder contains checked-in historical BTC paper-trading snapshots from
older paper soak, audit, and journal runs.

These files are not authoritative live runtime state.

Some files here, including `btc_paper_journal.jsonl`, were captured before the
canonical runtime-state cleanup and should be treated as archived evidence
rather than current operator truth.

Use the canonical workspace runtime surfaces instead:

- `var/eta_engine/state/btc_paper/`
- `var/eta_engine/state/broker_fleet/`

If a snapshot here disagrees with current runtime state, trust the canonical
runtime state and treat this folder as stale historical evidence.
