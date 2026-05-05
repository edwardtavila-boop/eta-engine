# IBKR Data Feed Lazy Import Note

`scripts/data_feeds.py` keeps `ib_insync` as a lazy import inside
`IbkrDataFeed._ensure_connected()`.

That is intentional:

- Non-IBKR feeds can import and run even when `ib_insync` or TWS is unavailable.
- The first IBKR bar request is serialized by `_connect_lock`, so concurrent
  futures bots share one connection attempt instead of racing or deadlocking.
- The mocked mid-session test exercises this path without touching a live IBKR
  account.

Eager import would only be safer for a fail-fast deployment profile where the
operator wants the process to refuse startup unless IBKR dependencies are
installed. For ETA's composite feed mode, lazy import is safer because Yahoo or
Coinbase can continue serving data while IBKR is unavailable or intentionally
not configured.
