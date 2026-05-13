"""
Defensive sanitizer for trade_closes records.

Several legacy bots (notably ``mnq_futures_sage``) intermittently write
the wrong value into the canonical ``realized_r`` field — most commonly
the tick count (e.g. ``realized_r=69`` for a $17.25 PnL on MNQ, where 69
is the tick count and the real R is ~0.86) or the raw dollar PnL.

These bad values, if read at face value, blow up two systems:
  1. ``anomaly_watcher._detect_suspicious_win`` fires "+69R trade!"
     critical alerts, which then trigger Hermes auto-investigation.
  2. ``pnl_summary`` rolls them into the MTD R total, masking real PnL.

This module centralises the detection + repair logic so both readers
(and any future ones) share the same sanity floor.

What we do
----------

For each record:
  1. Read ``realized_r`` as the bot wrote it.
  2. If ``|r| <= 20`` (a generous trader-realistic ceiling per single
     trade), trust it as-is.
  3. Otherwise classify as ``SUSPECT`` and try to RECOVER:
     a. If the record carries ``extra.realized_pnl`` AND a known
        futures ``extra.symbol``, recompute R = pnl_usd / $-per-R.
     b. If recovery yields ``|r| <= 20`` again, use the recovered R.
     c. Otherwise return ``None`` so downstream code skips the row.

What we explicitly do NOT do
----------------------------

* Mutate the original JSONL on disk. The bad value stays in the file
  as forensic evidence; only the in-memory read is sanitized.
* Silently downgrade values just because they're "big" — high-R wins
  do happen (a 7R or 10R trade is legitimate), so the threshold is
  set at the ceiling where realistic-but-extreme bleeds into
  obvious-bug territory.

Public interface
----------------

* ``sanitize_r(rec)`` → ``float | None`` — sanitized R value, or None
  if the value is unrecoverable (and the row should be skipped).
* ``classify(rec)`` → ``("clean" | "recovered" | "suspect", r)`` for
  diagnostics + audit logging.
"""

from __future__ import annotations

from typing import Any

# Realistic ceiling for a single trade in R units. Per-trade R values
# this high are still possible (a runner, a gap fill, etc.) but anything
# above is almost certainly a unit-confusion bug.
R_SANITY_CEILING = 20.0

# Dollar value per 1R for known futures roots. Used to recompute R
# from realized_pnl when the realized_r field is suspect.
_DOLLAR_PER_R_BY_ROOT = {
    "MNQ": 20.0,
    "MES": 12.5,
    "MGC": 10.0,
    "MCL": 10.0,
    "M6E": 6.25,
    "MYM": 5.0,
    "MBT": 25.0,
    "NQ": 200.0,
    "ES": 125.0,
    "GC": 100.0,
    "CL": 100.0,
    "6E": 62.50,
    "NG": 100.0,
    "ZB": 1000.0,
    "ZN": 1000.0,
}


def _symbol_root(symbol: str | None) -> str | None:
    """Strip stray contract suffix from a symbol to find the root.

    ``MNQ1``, ``MNQM6``, ``MNQ`` → ``MNQ``. ``BTC``, ``ETH`` → unchanged
    (not in the dollar_per_r table, so recovery skips them).
    """
    if not symbol:
        return None
    sym = str(symbol).upper().strip()
    # Try longest known root prefix first so "M6E" beats "M".
    for root in sorted(_DOLLAR_PER_R_BY_ROOT.keys(), key=len, reverse=True):
        if sym.startswith(root):
            return root
    return None


def _extract_raw_r(rec: dict[str, Any]) -> float | None:
    """Read the bot-written realized_r value, no sanity applied."""
    raw = rec.get("realized_r")
    if raw is None:
        raw = rec.get("r", rec.get("r_value"))
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _recover_r_from_pnl(rec: dict[str, Any]) -> float | None:
    """Recompute R from ``extra.realized_pnl`` and ``extra.symbol``.

    Returns None when:
      * ``extra`` is missing or not a dict
      * ``realized_pnl`` is missing / non-numeric
      * symbol is unknown (not in _DOLLAR_PER_R_BY_ROOT)
    """
    extra = rec.get("extra")
    if not isinstance(extra, dict):
        return None
    pnl_raw = extra.get("realized_pnl")
    try:
        pnl = float(pnl_raw) if pnl_raw is not None else None
    except (TypeError, ValueError):
        return None
    if pnl is None:
        return None
    root = _symbol_root(extra.get("symbol"))
    if root is None:
        return None
    dollar_per_r = _DOLLAR_PER_R_BY_ROOT.get(root)
    if not dollar_per_r:
        return None
    return pnl / dollar_per_r


def classify(rec: dict[str, Any]) -> tuple[str, float | None]:
    """Return ``(status, r)`` for diagnostics.

    Status:
      * ``"clean"``       — original value within R_SANITY_CEILING
      * ``"recovered"``   — original outside ceiling, recomputed from PnL
      * ``"suspect"``     — original outside ceiling, no recovery possible
      * ``"none"``        — no usable r at all
    """
    raw = _extract_raw_r(rec)
    if raw is None:
        return ("none", None)
    if abs(raw) <= R_SANITY_CEILING:
        return ("clean", raw)
    # Beyond the ceiling — try to recover
    recovered = _recover_r_from_pnl(rec)
    if recovered is not None and abs(recovered) <= R_SANITY_CEILING:
        return ("recovered", recovered)
    return ("suspect", raw)


def sanitize_r(rec: dict[str, Any]) -> float | None:
    """Sanitized R value for downstream readers (anomaly_watcher, pnl_summary).

    Returns:
      * the original value if clean
      * the recovered value if recovery worked
      * None if the value is suspect-and-unrecoverable (caller skips row)
    """
    status, value = classify(rec)
    if status == "suspect":
        return None
    return value
