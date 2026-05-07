"""Per-instrument contract specs and realistic-fill defaults.

Single source of truth for tick size, point value, commissions, typical
half-spread, and base slippage assumptions. Used by realistic_fill_sim
and paper_trade_sim so that paper-soak numbers reflect what an actual
broker would charge on the same fills.

Verified against CME contract specifications and typical retail
broker commission schedules (IBKR / Tastytrade) as of 2026-Q2.

Numbers are deliberately conservative: where two values are plausible
the model picks the slightly worse one so paper PnL trends toward
under-stating live performance, not over-stating it.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    symbol: str
    tick_size: float          # Minimum price increment (in instrument's price units)
    point_value: float        # USD value per 1.0 of price (per contract / per unit)
    commission_rt: float      # USD per round-trip (open + close), per contract
    half_spread_ticks: float  # Typical bid-ask half-spread in ticks (RTH)
    base_slip_ticks: float    # Baseline stop-fill slippage in ticks (RTH)
    overnight_slip_mult: float = 2.0  # Multiplier on slippage outside RTH session
    fast_bar_slip_mult: float = 1.5   # When body/range > 0.8
    thin_volume_slip_mult: float = 1.3  # When bar_vol < median_20
    is_perpetual: bool = False        # True for crypto perp swaps that pay funding
                                      # every 8h. CME BTC/ETH futures do NOT pay
                                      # funding — but until the engine routes
                                      # perp orders separately from CME futures
                                      # the funding_ledger module treats BTC/ETH
                                      # entries as perps when explicitly invoked.
                                      # TODO: split perp symbols (e.g. "BTC-PERP",
                                      # "ETH-PERP") from CME futures so this
                                      # flag can be set per-venue.

    @property
    def tick_value_usd(self) -> float:
        return self.tick_size * self.point_value


# Verified specs.  point_value is USD per 1.0 INDEX POINT, not per tick.
# (CME futures: divide by ticks_per_point to get tick value.)
_SPECS: dict[str, InstrumentSpec] = {
    # CME Equity Index Futures
    "MNQ":  InstrumentSpec("MNQ",  tick_size=0.25, point_value=2.0,   commission_rt=1.40,
                           half_spread_ticks=0.5, base_slip_ticks=2.0),
    "MNQ1": InstrumentSpec("MNQ1", tick_size=0.25, point_value=2.0,   commission_rt=1.40,
                           half_spread_ticks=0.5, base_slip_ticks=2.0),
    "NQ":   InstrumentSpec("NQ",   tick_size=0.25, point_value=20.0,  commission_rt=4.00,
                           half_spread_ticks=0.5, base_slip_ticks=1.5),
    "NQ1":  InstrumentSpec("NQ1",  tick_size=0.25, point_value=20.0,  commission_rt=4.00,
                           half_spread_ticks=0.5, base_slip_ticks=1.5),
    "ES":   InstrumentSpec("ES",   tick_size=0.25, point_value=50.0,  commission_rt=4.00,
                           half_spread_ticks=0.5, base_slip_ticks=1.0),
    "ES1":  InstrumentSpec("ES1",  tick_size=0.25, point_value=50.0,  commission_rt=4.00,
                           half_spread_ticks=0.5, base_slip_ticks=1.0),
    "MES":  InstrumentSpec("MES",  tick_size=0.25, point_value=5.0,   commission_rt=1.40,
                           half_spread_ticks=0.5, base_slip_ticks=1.5),
    "RTY":  InstrumentSpec("RTY",  tick_size=0.10, point_value=50.0,  commission_rt=4.00,
                           half_spread_ticks=0.5, base_slip_ticks=2.0),
    "M2K":  InstrumentSpec("M2K",  tick_size=0.10, point_value=5.0,   commission_rt=1.40,
                           half_spread_ticks=0.5, base_slip_ticks=2.0),
    # CME Metals
    "GC":   InstrumentSpec("GC",   tick_size=0.10, point_value=100.0, commission_rt=4.00,
                           half_spread_ticks=1.0, base_slip_ticks=2.0),
    "MGC":  InstrumentSpec("MGC",  tick_size=0.10, point_value=10.0,  commission_rt=1.40,
                           half_spread_ticks=1.0, base_slip_ticks=2.0),
    # NYMEX Energy
    "CL":   InstrumentSpec("CL",   tick_size=0.01, point_value=1000.0, commission_rt=4.50,
                           half_spread_ticks=1.0, base_slip_ticks=2.0),
    "MCL":  InstrumentSpec("MCL",  tick_size=0.01, point_value=100.0,  commission_rt=1.40,
                           half_spread_ticks=1.0, base_slip_ticks=2.0),
    "NG":   InstrumentSpec("NG",   tick_size=0.001, point_value=10000.0, commission_rt=4.50,
                           half_spread_ticks=1.0, base_slip_ticks=3.0),
    # CME FX
    "6E":   InstrumentSpec("6E",   tick_size=0.00005, point_value=125000.0, commission_rt=4.00,
                           half_spread_ticks=1.0, base_slip_ticks=1.5),
    "M6E":  InstrumentSpec("M6E",  tick_size=0.0001,  point_value=12500.0,  commission_rt=1.40,
                           half_spread_ticks=1.0, base_slip_ticks=1.5),
    # CBOT Rates
    "ZN":   InstrumentSpec("ZN",   tick_size=0.015625, point_value=1000.0, commission_rt=4.00,
                           half_spread_ticks=1.0, base_slip_ticks=1.0),
    # CME Crypto Futures.  BTC/ETH large contracts; MBT/MET micros.
    # is_perpetual=False — CME futures are NOT perpetual swaps, they
    # do NOT pay funding.  The funding_ledger will not charge funding on
    # these symbols.  Alpaca spot crypto (BTC, ETH, SOL, XRP) still routes
    # through the spot fallback specs below.
    "BTC":  InstrumentSpec("BTC",  tick_size=5.0,  point_value=5.0,   commission_rt=11.00,
                           half_spread_ticks=2.0, base_slip_ticks=2.0,
                           overnight_slip_mult=1.2,
                           is_perpetual=False),
    "MBT":  InstrumentSpec("MBT",  tick_size=5.0,  point_value=0.10,  commission_rt=2.50,
                           half_spread_ticks=2.0, base_slip_ticks=2.0,
                           overnight_slip_mult=1.2),
    "ETH":  InstrumentSpec("ETH",  tick_size=0.50, point_value=50.0,  commission_rt=11.00,
                           half_spread_ticks=2.0, base_slip_ticks=2.0,
                           overnight_slip_mult=1.2,
                           is_perpetual=False),
    "MET":  InstrumentSpec("MET",  tick_size=0.50, point_value=0.10,  commission_rt=2.50,
                           half_spread_ticks=2.0, base_slip_ticks=2.0,
                           overnight_slip_mult=1.2),
    # Crypto spot fallbacks (treat as 1x notional, taker fee ~5bps RT)
    # tick_size=0.01 dollars; point_value=$1 per $1 of price per 1 unit.
    # PnL math: pnl_usd = (exit_price - entry_price) * qty.  Commission =
    # commission_rt is computed as bps_taker_rt * notional inside fill sim.
    "SOL":  InstrumentSpec("SOL",  tick_size=0.01, point_value=1.0,   commission_rt=0.0,
                           half_spread_ticks=2.0, base_slip_ticks=3.0,
                           overnight_slip_mult=1.0),
    "XRP":  InstrumentSpec("XRP",  tick_size=0.0001, point_value=1.0, commission_rt=0.0,
                           half_spread_ticks=2.0, base_slip_ticks=3.0,
                           overnight_slip_mult=1.0),
    # CME Solana and XRP micro futures — not yet listed; specs TBD.
    # When available, add as "SOL_FUT" or "XRP_FUT" to avoid collision
    # with spot fallback specs above.
    # Front-month suffixed aliases.  The data library uses "GC1/CL1/..."
    # for the active front-month contract; the registry uses the same
    # naming.  Without these aliases, get_spec(symbol) falls back to a
    # default point_value=1.0 — and a 1.0 multiplier on contracts whose
    # real multiplier is 100-125,000 produces catastrophic sizing bugs
    # (saw $-866K loss on 8 6E trades in a 90d harness before the fix).
    # Bug discovery: 2026-05-05 elite-gate sweep on commodities/forex.
    "GC1":  InstrumentSpec("GC1",  tick_size=0.10, point_value=100.0, commission_rt=4.00,
                           half_spread_ticks=1.0, base_slip_ticks=2.0),
    "CL1":  InstrumentSpec("CL1",  tick_size=0.01, point_value=1000.0, commission_rt=4.50,
                           half_spread_ticks=1.0, base_slip_ticks=2.0),
    "NG1":  InstrumentSpec("NG1",  tick_size=0.001, point_value=10000.0, commission_rt=4.50,
                           half_spread_ticks=1.0, base_slip_ticks=3.0),
    "6E1":  InstrumentSpec("6E1",  tick_size=0.00005, point_value=125000.0, commission_rt=4.00,
                           half_spread_ticks=1.0, base_slip_ticks=1.5),
    "ZN1":  InstrumentSpec("ZN1",  tick_size=0.015625, point_value=1000.0, commission_rt=4.00,
                           half_spread_ticks=1.0, base_slip_ticks=1.0),
    "M2K1": InstrumentSpec("M2K1", tick_size=0.10, point_value=5.0,   commission_rt=1.40,
                           half_spread_ticks=0.5, base_slip_ticks=2.0),
    "YM1":  InstrumentSpec("YM1",  tick_size=1.0,  point_value=5.0,   commission_rt=4.00,
                           half_spread_ticks=0.5, base_slip_ticks=2.0),
}

# Crypto spot taker fee (round-trip) as fraction of notional.  Applied
# inside fill sim as: commission = notional * CRYPTO_TAKER_FEE_RT.
CRYPTO_SPOT_TAKER_FEE_RT: float = 0.0010  # 10 bps RT (5 bps each side, retail Coinbase Pro / Kraken)
CRYPTO_SPOT_SYMBOLS: frozenset[str] = frozenset({"SOL", "XRP"})


def get_spec(symbol: str) -> InstrumentSpec:
    """Return the spec for symbol or a conservative default."""
    s = symbol.upper()
    if s in _SPECS:
        return _SPECS[s]
    return InstrumentSpec(
        symbol=s, tick_size=0.25, point_value=1.0, commission_rt=4.0,
        half_spread_ticks=2.0, base_slip_ticks=3.0,
    )


def is_rth_session(ts_iso: str, instrument: str) -> bool:
    """Heuristic RTH detection from an ISO timestamp.

    For US equity-index futures: 09:30-16:00 ET (UTC-5/-4 depending on DST).
    For metals / energy: 24x5 — always RTH unless weekend.
    For crypto: always RTH (24x7).

    Caller passes UTC timestamps; this is a coarse heuristic, not a
    holiday calendar.  Used only for tagging trades to a session bucket
    and for applying overnight slip multipliers — not for entry gating.
    """
    s = instrument.upper()
    crypto_24x7 = {"BTC", "MBT", "ETH", "MET", "SOL", "XRP"}
    metals_energy_24x5 = {"GC", "MGC", "CL", "MCL", "NG", "ZN", "6E", "M6E"}
    if s in crypto_24x7:
        return True
    if s in metals_energy_24x5:
        # 24x5: only flag weekend gaps as non-RTH
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
            return dt.weekday() < 5
        except (ValueError, TypeError):
            return True
    # US equity index: assume UTC ts; RTH is 13:30 - 21:00 UTC (winter) or 12:30 - 20:00 UTC (summer)
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        if dt.weekday() >= 5:
            return False
        # Use a lenient envelope to cover both DST regimes.
        h = dt.hour + dt.minute / 60.0
        return 13.0 <= h <= 21.0
    except (ValueError, TypeError):
        return True
