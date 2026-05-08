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
    # Perp funding research must use the explicit BTC-PERP/ETH-PERP specs below,
    # not these CME futures symbols.
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
    # Explicit perpetual-swap specs for funding-cost accounting only.
    # These keep CME BTC/ETH futures non-funding while preserving a
    # deliberate perp path for research/backtests that model funding.
    "BTC-PERP": InstrumentSpec("BTC-PERP", tick_size=0.01, point_value=1.0, commission_rt=0.0,
                               half_spread_ticks=2.0, base_slip_ticks=3.0,
                               overnight_slip_mult=1.0,
                               is_perpetual=True),
    "ETH-PERP": InstrumentSpec("ETH-PERP", tick_size=0.01, point_value=1.0, commission_rt=0.0,
                               half_spread_ticks=2.0, base_slip_ticks=3.0,
                               overnight_slip_mult=1.0,
                               is_perpetual=True),
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
    # Micro Dow (added 2026-05-07): tick=1.0pt, $0.50/pt -> $0.50 tick value.
    # Commission scales with the smaller contract; IBKR retail is ~$1.40 RT
    # for micros vs $4.00 for full-size equity-index.
    "MYM":  InstrumentSpec("MYM",  tick_size=1.0,  point_value=0.5,   commission_rt=1.40,
                           half_spread_ticks=0.5, base_slip_ticks=2.0),
    "MYM1": InstrumentSpec("MYM1", tick_size=1.0,  point_value=0.5,   commission_rt=1.40,
                           half_spread_ticks=0.5, base_slip_ticks=2.0),
}

# Crypto spot taker fee (round-trip) as fraction of notional.  Applied
# inside fill sim as: commission = notional * CRYPTO_TAKER_FEE_RT.
CRYPTO_SPOT_TAKER_FEE_RT: float = 0.0010  # 10 bps RT (5 bps each side, retail Coinbase Pro / Kraken)
CRYPTO_SPOT_SYMBOLS: frozenset[str] = frozenset({"SOL", "XRP"})


def _strip_front_month_suffix(s: str) -> str:
    """Strip the trailing ``1`` that distinguishes the front-month
    convention (``MNQ1`` for the active MNQ contract) from the bare
    instrument root (``MNQ``).

    Both forms appear in the codebase: the broker-side trade routing
    uses ``MNQ1`` (continuous front-month) while bot configs and the
    audit engine sometimes use just ``MNQ``. Without this normalization,
    ``get_spec("MNQ")`` (no suffix) and ``get_spec("MNQ1")`` (suffixed)
    can return different specs -- the latter hits the spec table, the
    former hits the conservative default.
    """
    s = s.upper()
    if s.endswith("1") and s[:-1] in _SPECS:
        return s[:-1]
    return s


def get_spec(symbol: str) -> InstrumentSpec:
    """Return the spec for symbol or a conservative default.

    NOTE on multi-venue ambiguity: for symbols that exist on multiple
    venues with different multipliers (BTC and ETH most notably -- CME
    Bitcoin Futures has point_value=$5/pt while spot BTC on Alpaca
    paper has point_value=1.0), this returns the FIRST matching entry
    in ``_SPECS`` (the CME futures spec). When the caller routes the
    trade to spot, use ``effective_point_value(symbol, route="spot")``
    instead -- that helper resolves the ambiguity safely.
    """
    s = symbol.upper()
    # First try direct lookup. Then try the suffixed form (e.g. "YM" ->
    # check if "YM1" exists). This handles the asymmetry where some
    # specs are keyed by front-month form ("YM1") and some by bare
    # form ("MNQ", "BTC"), with callers passing either.
    if s in _SPECS:
        return _SPECS[s]
    suffixed = f"{s}1"
    if suffixed in _SPECS:
        return _SPECS[suffixed]
    stripped = _strip_front_month_suffix(s)
    if stripped != s and stripped in _SPECS:
        return _SPECS[stripped]
    return InstrumentSpec(
        symbol=s, tick_size=0.25, point_value=1.0, commission_rt=4.0,
        half_spread_ticks=2.0, base_slip_ticks=3.0,
    )


# Crypto roots that, when traded on a SPOT venue (Alpaca paper, Coinbase,
# etc.), have point_value=1.0 -- i.e. qty * price already equals the USD
# notional and there is no contract multiplier to layer on. This set is
# the union of ``_root()``-normalized identifiers used elsewhere in the
# engine. MBT and MET are intentionally NOT in this set: those are CME
# crypto-MICRO futures whose multipliers ($0.10/pt) live in ``_SPECS``.
_SPOT_CRYPTO_ROOTS: frozenset[str] = frozenset({
    "BTC", "ETH", "SOL", "XRP", "AVAX", "LINK", "DOGE",
})


def effective_point_value(symbol: str, *, route: str = "auto") -> float:
    """Return the contract multiplier the engine should use for THIS
    trade's notional / PnL math.

    Resolves the multi-venue ambiguity that has bitten the codebase
    repeatedly (see 2026-05-07 bracket_sizing fix + supervisor PnL fix):
    ``get_spec("BTC")`` returns the CME Bitcoin Futures spec
    (point_value=$5/pt) but the supervisor's BTC bots route through
    Alpaca SPOT where the right multiplier is 1.0. Using the wrong
    multiplier silently inflates / deflates PnL by 5x for BTC, 50x for
    ETH, etc.

    ``route`` overrides:
      * "spot"    -> 1.0 for any spot-crypto root, else fall through to
                     get_spec(symbol).point_value.
      * "futures" -> always defer to get_spec(symbol).point_value.
      * "auto"    -> default. For roots in ``_SPOT_CRYPTO_ROOTS`` returns
                     1.0 (the supervisor's BTC/ETH/SOL bots all route
                     through Alpaca spot). For everything else, defer
                     to get_spec.

    Why "auto" defaults to spot for the crypto roots: the *current*
    fleet routes those tickers through Alpaca paper. If a future bot
    routes BTC through CME Bitcoin Futures, it MUST pass route="futures"
    explicitly so this helper doesn't silently assume spot.

    The function never returns 0.0 (would zero out PnL); falls back to
    1.0 if the spec lookup throws.
    """
    s = symbol.upper()
    root = s.lstrip("/").rstrip("0123456789")
    for suffix in ("USDT", "USD"):
        if root.endswith(suffix):
            root = root[: -len(suffix)] or root

    # ``route="spot"`` AND known spot-crypto root: definitive 1.0.
    # ``route="auto"`` AND known spot-crypto root: also 1.0 (the current
    # fleet wires BTC/ETH/SOL bots through Alpaca spot). For any other
    # combination we fall through to the spec table.
    if route in ("auto", "spot") and root in _SPOT_CRYPTO_ROOTS:
        return 1.0

    try:
        spec = get_spec(symbol)
        pv = float(getattr(spec, "point_value", 0.0) or 0.0)
        if pv > 0:
            return pv
    except Exception:  # noqa: BLE001 -- conservative default
        pass
    return 1.0


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
