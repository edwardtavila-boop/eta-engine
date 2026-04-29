"""
EVOLUTIONARY TRADING ALGO  //  venues.cme_mapping
=====================================
Crypto-perp -> CME-futures translation table.

Operator mandate M2 (2026-04-26): US persons cannot legally route live
orders to Bybit / OKX / Deribit / Hyperliquid / other non-FCM venues.
The router (eta_engine.venues.router) hard-refuses such orders.

This module documents the substitution path: instead of trading
``BTCUSDT`` perp on Bybit, the bot trades ``MBT`` (CME Micro Bitcoin)
through IBKR. All four crypto perps in the production fleet have CME
futures equivalents that are routable through IBKR / Tastytrade and
qualify as Section 1256 contracts (60/40 long/short capital-gains
treatment, 1099-B reporting).

Usage
-----

    from eta_engine.venues.cme_mapping import to_cme

    cme_symbol = to_cme("BTCUSDT")          # -> "MBT" (micro)
    cme_symbol = to_cme("BTCUSDT", micro=False)  # -> "BTC" (full-size)

The translation is intentionally separate from the router so that:

* Bots can keep their internal ``self.config.symbol`` nomenclature
  (BTCUSDT, ETHUSDT, ...) for backtest replay against historical
  Bybit/OKX tape, while live orders translate to CME at routing time.
* The mapping table itself is unit-testable in isolation and audited
  against CME's product master.
* When CME lists new crypto futures (or delists), changes happen in
  exactly one place.

Contract reference
------------------

============   ===================================  =============================
Bot symbol     Micro CME equivalent (preferred)     Full-size CME equivalent
============   ===================================  =============================
BTCUSDT        MBT  (Micro Bitcoin, 0.1 BTC)        BTC  (Bitcoin, 5 BTC)
ETHUSDT        MET  (Micro Ether, 0.1 ETH)          ETH  (Ether, 50 ETH)
SOLUSDT        SOL  (Solana, listed 2025-03-17)     -- (no separate full-size)
XRPUSDT        XRP  (XRP, listed 2025-05-19)        -- (no separate full-size)
============   ===================================  =============================

Notional reference (illustrative, refresh from CME daily settle):

    MBT @ BTC=$95k  -> ~$9,500 / contract
    MET @ ETH=$3.3k -> ~$330   / contract
    BTC @ BTC=$95k  -> ~$475,000 / contract  (use only with size to match)
    ETH @ ETH=$3.3k -> ~$165,000 / contract
"""

from __future__ import annotations

CRYPTO_PERP_TO_CME_MICRO: dict[str, str] = {
    "BTCUSDT": "MBT",
    "ETHUSDT": "MET",
    "SOLUSDT": "SOL",
    "XRPUSDT": "XRP",
}

CRYPTO_PERP_TO_CME_FULL: dict[str, str] = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    # SOL + XRP have no separate full-size contract on CME (yet) -- the
    # listed contract IS the standard size. Fall through to the micro
    # mapping for those.
}


def to_cme(symbol: str, *, micro: bool = True) -> str | None:
    """Translate a crypto perp symbol to its CME futures equivalent.

    Parameters
    ----------
    symbol:
        Bot-internal perp symbol, e.g. ``"BTCUSDT"``. Case-insensitive.
    micro:
        When True (default), prefer the micro contract (MBT, MET, SOL,
        XRP). When False, prefer the full-size contract (BTC, ETH);
        falls back to micro for symbols that have no separate full-size
        listing.

    Returns
    -------
    str | None
        CME contract code, or ``None`` if no equivalent exists.
    """
    norm = symbol.strip().upper()
    if micro:
        return CRYPTO_PERP_TO_CME_MICRO.get(norm)
    return CRYPTO_PERP_TO_CME_FULL.get(norm) or CRYPTO_PERP_TO_CME_MICRO.get(norm)


def from_cme(cme_symbol: str) -> str | None:
    """Reverse-translate a CME futures code to its crypto perp symbol.

    Useful when the journal stores the bot's internal symbol but the
    venue confirmation came back with the CME code. Returns ``None`` if
    the input is not a tracked CME crypto futures code.
    """
    norm = cme_symbol.strip().upper()
    for perp, cme in CRYPTO_PERP_TO_CME_MICRO.items():
        if cme == norm:
            return perp
    for perp, cme in CRYPTO_PERP_TO_CME_FULL.items():
        if cme == norm:
            return perp
    return None


def is_crypto_perp(symbol: str) -> bool:
    """True if symbol is a tracked crypto perp with a CME equivalent."""
    return symbol.strip().upper() in CRYPTO_PERP_TO_CME_MICRO
