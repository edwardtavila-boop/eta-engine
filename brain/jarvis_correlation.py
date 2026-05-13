"""Cross-bot correlation awareness for JARVIS (Tier-2 #5, 2026-04-27).

When `MnqBot` and `NqBot` both want to enter long at the same bar,
they're effectively the same trade -- correlation between MNQ and NQ
is ~0.99. JARVIS's existing per-bot ``_ask_jarvis()`` calls don't see
the correlation, so without something like this module the fleet ends
up doubly-exposed when a regime aligns the two.

This module exposes a single function::

    should_throttle_for_correlation(symbol, fleet_positions) -> CapDecision

which any consumer (bot pre-flight, JARVIS evaluate_request override,
risk overlay) can call BEFORE submitting an order. Returns a
multiplier (1.0 = no throttle, 0.0 = full block) plus a reason code.

Correlation matrix is hardcoded for the 7 fleet symbols; revisit
quarterly with realized correlations from the burn-in journal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# Pairwise correlation matrix. Symmetric; only the upper triangle is
# stored. Values are 90-day rolling abs-correlation of daily returns,
# rounded for clarity. Update from realized data quarterly.
_CORRELATIONS: dict[tuple[str, str], float] = {
    ("MNQ", "NQ"): 0.99,  # same instrument, different sizes
    ("BTCUSDT", "ETHUSDT"): 0.85,
    ("BTCUSDT", "SOLUSDT"): 0.78,
    ("BTCUSDT", "XRPUSDT"): 0.55,
    ("ETHUSDT", "SOLUSDT"): 0.90,
    ("ETHUSDT", "XRPUSDT"): 0.55,
    ("SOLUSDT", "XRPUSDT"): 0.50,
    # Crypto vs equity index -- low (regime-dependent, this is the long-run average)
    ("MNQ", "BTCUSDT"): 0.30,
    ("MNQ", "ETHUSDT"): 0.30,
    ("MNQ", "SOLUSDT"): 0.25,
    ("MNQ", "XRPUSDT"): 0.15,
    ("NQ", "BTCUSDT"): 0.30,
    ("NQ", "ETHUSDT"): 0.30,
    ("NQ", "SOLUSDT"): 0.25,
    ("NQ", "XRPUSDT"): 0.15,
}

# CME-translated equivalents -- treat MBT identically to BTCUSDT, etc.
# This ensures the correlation logic works after the M2 router translation.
_SYMBOL_ALIASES: dict[str, str] = {
    "MBT": "BTCUSDT",
    "BTC": "BTCUSDT",
    "MET": "ETHUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
}


def _canon(sym: str) -> str:
    """Normalize a symbol to its canonical form for correlation lookup."""
    norm = sym.strip().upper()
    return _SYMBOL_ALIASES.get(norm, norm)


def correlation(a: str, b: str) -> float:
    """Pairwise correlation between two symbols, normalized.

    Returns 1.0 if a == b, 0.0 if no entry exists.
    """
    ca, cb = _canon(a), _canon(b)
    if ca == cb:
        return 1.0
    key1 = (ca, cb)
    key2 = (cb, ca)
    return _CORRELATIONS.get(key1, _CORRELATIONS.get(key2, 0.0))


@dataclass
class CapDecision:
    cap_mult: float  # multiplier to apply to the order qty (1.0 = full)
    reason_code: str
    detail: str


# How aggressively to throttle when correlation is high. Tuneable.
_HIGH_CORR_THRESHOLD = 0.80  # >= this -> treat as "same trade"
_MED_CORR_THRESHOLD = 0.50  # >= this -> moderate throttle


def should_throttle_for_correlation(
    incoming_symbol: str,
    fleet_positions: Mapping[str, float],
) -> CapDecision:
    """Decide whether to throttle a new entry given current fleet state.

    Parameters
    ----------
    incoming_symbol:
        The symbol the bot wants to open a NEW position in.
    fleet_positions:
        Mapping of symbol -> signed-units of currently-open exposure.
        Positive = long, negative = short, 0 = flat.

    Returns
    -------
    CapDecision with cap_mult in [0.0, 1.0]:
      * 1.0  -- no correlated exposure, fire at full size
      * 0.5  -- moderate correlation with an existing position; halve
      * 0.0  -- already maxed out by a highly correlated position; defer

    Examples
    --------
    >>> p = {"MNQ": 2.0}                                        # already long MNQ
    >>> d = should_throttle_for_correlation("NQ", p)
    >>> assert d.cap_mult == 0.0    # NQ is ~0.99 corr to MNQ -> defer
    """
    inc = _canon(incoming_symbol)

    # Already long/short the SAME symbol => not this module's concern;
    # individual bots handle stacking limits internally.
    if (
        inc in (_canon(s) for s in fleet_positions if fleet_positions[s] != 0)
        and fleet_positions.get(incoming_symbol, 0) != 0
    ):
        return CapDecision(
            cap_mult=1.0,
            reason_code="same_symbol_already_open",
            detail="bot is already in this symbol; correlation throttle defers to bot's own stacking logic",
        )

    max_corr = 0.0
    binding_other: str | None = None
    for other_sym, qty in fleet_positions.items():
        if qty == 0:
            continue
        c = correlation(incoming_symbol, other_sym)
        if c > max_corr:
            max_corr = c
            binding_other = other_sym

    if max_corr >= _HIGH_CORR_THRESHOLD:
        return CapDecision(
            cap_mult=0.0,
            reason_code="high_corr_block",
            detail=f"already in {binding_other} (corr {max_corr:.2f}) -- entering "
            f"{incoming_symbol} would double the same trade",
        )
    if max_corr >= _MED_CORR_THRESHOLD:
        return CapDecision(
            cap_mult=0.5,
            reason_code="med_corr_throttle",
            detail=f"correlated exposure in {binding_other} (corr {max_corr:.2f}); sizing {incoming_symbol} at 0.5x",
        )
    return CapDecision(
        cap_mult=1.0,
        reason_code="no_corr_throttle",
        detail=f"max correlation with open positions = {max_corr:.2f} (under {_MED_CORR_THRESHOLD})",
    )
