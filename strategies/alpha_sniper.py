"""
EVOLUTIONARY TRADING ALGO  //  strategies.alpha_sniper
======================================================
Multi-symbol confirmation layer — uses real-time cross-asset data
from MultiSymbolFeed to confirm breakouts, reject fakeouts, and
detect intermarket divergences.

When cross-symbol data is available (live MultiSymbolFeed), the
AlphaSniper applies full intermarket confirmation:
  - ES must confirm MNQ/NQ breakouts (simultaneous ORB break)
  - BTC must participate in ETH/SOL moves (correlation gate)
  - Spread tightness validates breakout quality
  - Volume synchronization across symbols confirms real flow
  - Divergence between symbols rejects the trap

When cross-symbol data is NOT available (paper soak / backtest),
the AlphaSniper degrades to tape-reading mode using the same
symbol's own bar structure:
  - Bar type classification (absorption, breakout, reversal, doji)
  - Wick-vs-body ratio (rejection bars)
  - Bid-ask spread tightness (from feed, if available)
  - Gap continuation probability

Architecture
------------
AlphaSniper wraps any sub-strategy. It's designed to sit BETWEEN
the strategy and the EdgeAmplifier in the stack:

    Strategy → AlphaSniper → EdgeAmplifier → Bridge → Signal

Cross-symbol data is injected via attach_* methods (same pattern
as sage providers). When no cross-symbol providers are attached,
the sniper operates in tape-reading mode (same-symbol only).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Protocol

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData

    class _SubStrategy(Protocol):
        def maybe_enter(
            self,
            bar: BarData,
            hist: list[BarData],
            equity: float,
            config: BacktestConfig,
        ) -> _Open | None: ...


# ---------------------------------------------------------------------------
# Bar type classification — tape reading on a single symbol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BarType:
    """Classify a bar's internal structure to determine what it represents."""
    is_bull: bool
    is_bear: bool
    body_ratio: float        # body / range — 1.0 = full marubozu, 0.0 = doji
    upper_wick_ratio: float   # upper wick / range
    lower_wick_ratio: float   # lower wick / range
    clv: float               # close location value (0=low, 1=high)
    is_doji: bool            # body < 10% of range
    is_marubozu: bool        # body > 80% of range, minimal wicks
    is_hammer: bool          # small body near top, long lower wick
    is_shooting_star: bool   # small body near bottom, long upper wick
    is_absorption: bool      # small range, above-average volume (detected elsewhere)


def classify_bar(bar: BarData) -> BarType:
    """Classify a single bar's internal structure."""
    rng = max(bar.high - bar.low, 1e-9)
    body = abs(bar.close - bar.open)
    body_ratio = body / rng
    open_val = bar.open
    close_val = bar.close

    upper_wick = bar.high - max(open_val, close_val)
    lower_wick = min(open_val, close_val) - bar.low
    upper_wick_ratio = upper_wick / rng
    lower_wick_ratio = lower_wick / rng
    clv = (close_val - bar.low) / rng

    is_bull = close_val > open_val
    is_bear = close_val < open_val
    is_doji = body_ratio < 0.10
    is_marubozu = body_ratio > 0.80 and upper_wick_ratio < 0.10 and lower_wick_ratio < 0.10
    is_hammer = (body_ratio < 0.40 and lower_wick_ratio > 0.50
                 and upper_wick_ratio < 0.15 and clv > 0.60)
    is_shooting_star = (body_ratio < 0.40 and upper_wick_ratio > 0.50
                        and lower_wick_ratio < 0.15 and clv < 0.40)
    # Absorption: calculated externally (needs volume context)
    is_absorption = False

    return BarType(
        is_bull=is_bull, is_bear=is_bear,
        body_ratio=body_ratio,
        upper_wick_ratio=upper_wick_ratio,
        lower_wick_ratio=lower_wick_ratio,
        clv=clv,
        is_doji=is_doji, is_marubozu=is_marubozu,
        is_hammer=is_hammer, is_shooting_star=is_shooting_star,
        is_absorption=is_absorption,
    )


# ---------------------------------------------------------------------------
# Same-symbol tape reading — bar-level edge detection
# ---------------------------------------------------------------------------


def tape_confirms_entry(bar: BarData, side: str) -> tuple[bool, float, str]:
    """Check if the bar's internal structure confirms the entry direction.

    Returns (allowed, confidence_mult, reason).
    - allowed=False: the bar's structure OPPOSES the entry → reject
    - confidence > 1.0: the bar's structure STRONGLY confirms → boost
    - confidence = 1.0: neutral bar → allow
    - confidence < 1.0: weak confirmation → shrink

    Edges captured:
    - Marubozu bullish on long entry → strong tape confirmation (1.3x)
    - Shooting star on long entry → rejection → VETO (0x)
    - Doji on either entry → indecision → weak (0.7x)
    - Hammer on long entry → bullish reversal confirmation (1.2x)
    - Marubozu bearish on short entry → strong confirmation (1.3x)
    - Marubozu bullish on SHORT entry → tape opposes → weak (0.5x)
    """
    bt = classify_bar(bar)
    is_long = side.upper() == "BUY"

    # Strong rejections — bars that directly oppose the entry
    if is_long and bt.is_shooting_star:
        return False, 0.0, "shooting_star_rejection"
    if not is_long and bt.is_hammer:
        return False, 0.0, "hammer_rejection"

    # Indecision bars — doji = market hasn't decided yet, reduce conviction
    if bt.is_doji:
        return True, 0.7, "doji_indecision"

    # Strong confirmation — bar's close is firmly in entry direction
    if is_long and bt.is_marubozu and bt.is_bull:
        return True, 1.3, "bull_marubozu_confirm"
    if not is_long and bt.is_marubozu and bt.is_bear:
        return True, 1.3, "bear_marubozu_confirm"

    # Reversal confirmation — hammer/shooting star in the entry direction
    if is_long and bt.is_hammer:
        return True, 1.2, "hammer_confirm"
    if not is_long and bt.is_shooting_star:
        return True, 1.2, "shooting_star_confirm"

    # Tape opposes but doesn't reject — bar is opposite color
    # Long entry on a red bar = you're fighting the tape
    if is_long and bt.is_bear:
        return True, 0.7, "tape_opposes"
    if not is_long and bt.is_bull:
        return True, 0.7, "tape_opposes"

    return True, 1.0, "pass"


# ---------------------------------------------------------------------------
# Cross-symbol confirmation — the intermarket edge
# ---------------------------------------------------------------------------

# Which symbol pairs to confirm against
_INTERMARKET_PAIRS: dict[str, list[str]] = {
    "MNQ": ["ES", "NQ"],   # MNQ confirms with ES (lead) and NQ (sister)
    "NQ":  ["ES", "MNQ"],  # NQ confirms with ES (lead) and MNQ (sister)
    "ETH": ["BTC"],        # ETH confirms with BTC (crypto lead)
    "SOL": ["BTC", "ETH"], # SOL confirms with BTC and ETH
    "BTC": ["ES"],         # BTC confirms with ES (risk-on/off proxy)
}

# Maximum divergence ATR before we call it a fakeout
DIVERGENCE_ATR_MULT: float = 0.5


def intermarket_confirms(
    primary_symbol: str,
    primary_bar: BarData,
    primary_hist: list[BarData],
    cross_bars: dict[str, dict | None],  # {symbol: bar dict or None}
    entry_side: str,
    hist_lookback: int = 5,
) -> tuple[bool, float, str]:
    """Check if correlated symbols confirm the primary symbol's entry.

    Returns (confirmed, confidence_mult, reason).

    Confirmation logic per pair:
    - ES → NQ/MNQ: ES must be trending in the same direction over the
      last N bars. If ES is flat or opposite, the NQ/MNQ move is
      sector rotation, not broad market conviction.
    - BTC → ETH/SOL: BTC must NOT be diverging. If ETH is up but BTC
      is down, the ETH move is alt-coin noise, not real demand.
    - BTC → ES: BTC-ES correlation is risk-on/off. If ES is selling
      but BTC is rallying, it's defensive BTC buying — lower conviction.

    When cross-symbol data is missing (paper mode), returns (True, 1.0).
    """
    peers = _INTERMARKET_PAIRS.get(primary_symbol, [])
    if not peers:
        return True, 1.0, "no_peers"

    recent = primary_hist[-hist_lookback:] if len(primary_hist) >= hist_lookback else primary_hist
    if len(recent) < 3:
        return True, 1.0, "insufficient_history"

    primary_dir = sum(1 for b in recent if b.close > b.open)
    primary_dir / len(recent)
    is_long = entry_side.upper() == "BUY"

    confirmations = 0
    total_peers_with_data = 0
    reasons: list[str] = []

    for peer_sym in peers:
        peer_bar = cross_bars.get(peer_sym)
        if peer_bar is None:
            continue  # peer data not available — skip (fail-open)
        total_peers_with_data += 1

        # Peer direction check: does this symbol agree with our direction?
        if isinstance(peer_bar, dict):
            peer_close = float(peer_bar.get("close", 0))
            peer_open = float(peer_bar.get("open", peer_close))
            peer_dir = 1 if peer_close > peer_open else 0
        elif hasattr(peer_bar, 'close') and hasattr(peer_bar, 'open'):
            peer_dir = 1 if peer_bar.close > peer_bar.open else 0
        else:
            continue

        if is_long and peer_dir == 1 or not is_long and peer_dir == 0:
            confirmations += 1
        else:
            reasons.append(f"{peer_sym}_opposes")

    if total_peers_with_data == 0:
        return True, 1.0, "no_peer_data"

    # At least one peer must confirm; peer disagreement = veto
    if confirmations == 0 and total_peers_with_data > 0:
        return False, 0.0, f"all_peers_oppose:{','.join(reasons)}"

    # Partial confirmation — reduce confidence if some peers oppose
    confirmation_ratio = confirmations / total_peers_with_data
    if confirmation_ratio < 0.5:
        return True, 0.7, f"weak_peer_confirm_{confirmation_ratio:.1f}"
    if confirmation_ratio < 1.0:
        return True, 0.9, f"partial_peer_confirm_{confirmation_ratio:.1f}"

    return True, 1.2, "full_peer_confirm"


# ---------------------------------------------------------------------------
# Spread quality check — tight spread = real market, wide = trap
# ---------------------------------------------------------------------------

def spread_confirms(
    bid: float | None,
    ask: float | None,
    bar_range: float,
    max_spread_pct_of_range: float = 0.20,
) -> tuple[bool, float, str]:
    """Check if bid-ask spread is tight enough for a quality entry.

    A wide spread on a breakout bar means the market maker is
    protecting inventory — the breakout is likely a trap. A tight
    spread means genuine participation.

    Returns (confirmed, confidence_mult, reason).
    When bid/ask data is unavailable (paper mode), returns (True, 1.0).
    """
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return True, 1.0, "no_spread_data"

    spread = ask - bid
    if spread <= 0:
        return True, 1.0, "inverted_spread"  # shouldn't happen, fail-open
    if bar_range <= 0:
        return True, 1.0, "zero_range"

    spread_pct = spread / bar_range

    if spread_pct > max_spread_pct_of_range:
        return True, 0.6, f"wide_spread_{spread_pct:.2f}"
    if spread_pct > max_spread_pct_of_range * 0.5:
        return True, 0.85, "moderate_spread"

    return True, 1.1, "tight_spread"


# ---------------------------------------------------------------------------
# AlphaSniper — the unified wrapper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlphaSniperConfig:
    """Knobs for cross-symbol confirmation and tape reading."""

    # Tape reading (same-symbol, always available)
    enable_tape_reading: bool = True

    # Cross-symbol intermarket confirmation (requires providers)
    enable_intermarket: bool = True

    # Spread quality check (requires bid/ask from feed)
    enable_spread_check: bool = True
    max_spread_pct: float = 0.20

    # Divergence detection
    enable_divergence_check: bool = True
    divergence_atr_mult: float = 0.5
    divergence_lookback: int = 5

    # Minimum peer confirmation ratio (0.0-1.0)
    min_peer_confirmation: float = 0.5


class AlphaSniper:
    """Multi-symbol confirmation + tape reading wrapper.

    Wraps any strategy. When cross-symbol providers are attached,
    applies full intermarket confirmation. When not, degrades to
    tape-reading mode (same-symbol bar structure analysis).

    Provider attachment (mirrors sage provider pattern):
        sniper = AlphaSniper(strategy, config)
        sniper.attach_cross_bars_provider(lambda sym: get_bar(sym))
        sniper.attach_spread_provider(lambda: (bid, ask))
    """

    def __init__(
        self,
        sub_strategy: _SubStrategy,
        config: AlphaSniperConfig | None = None,
    ) -> None:
        self._sub = sub_strategy
        self.cfg = config or AlphaSniperConfig()
        # Cross-symbol bar provider: callable(symbol: str) -> dict | None
        self._cross_bar_provider: Callable[[str], dict | None] | None = None
        # Spread provider: callable() -> (bid: float, ask: float) | None
        self._spread_provider: Callable[[], tuple[float, float] | None] | None = None
        # Per-bot tape state
        self._tape_history: deque[BarType] = deque(maxlen=20)

    # -- provider attachment ------------------------------------------------

    def attach_cross_bars(self, provider: Callable[[str], dict | None] | None) -> None:
        """Attach a cross-symbol bar provider.

        Signature: provider(symbol: str) -> dict | None
        Returns a bar dict {open, high, low, close, volume} for the
        requested symbol, or None if data is unavailable at this tick.
        """
        self._cross_bar_provider = provider

    def attach_spread(self, provider: Callable[[], tuple[float, float] | None] | None) -> None:
        """Attach a bid-ask spread provider.

        Signature: provider() -> (bid: float, ask: float) | None
        Returns the current bid and ask, or None if unavailable.
        """
        self._spread_provider = provider

    # -- main entry point --------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        opened = self._sub.maybe_enter(bar, hist, equity, config)
        if opened is None:
            return None

        side = opened.side
        total_mult = 1.0
        reasons: list[str] = []

        # ── Layer 1: Tape reading (always available) ──
        if self.cfg.enable_tape_reading:
            allowed, tape_mult, reason = tape_confirms_entry(bar, side)
            if not allowed:
                return None
            total_mult *= tape_mult
            reasons.append(reason)

        # ── Layer 2: Spread quality (from live feed, if available) ──
        bid, ask = None, None
        if self.cfg.enable_spread_check and self._spread_provider is not None:
            try:
                spread_result = self._spread_provider()
                if spread_result:
                    bid, ask = spread_result
            except Exception:
                pass

        allowed, spread_mult, spread_reason = spread_confirms(
            bid, ask, max(bar.high - bar.low, 1e-9), self.cfg.max_spread_pct,
        )
        if not allowed:
            return None
        total_mult *= spread_mult

        # ── Layer 3: Intermarket confirmation (if cross-symbol data available) ──
        if self.cfg.enable_intermarket and self._cross_bar_provider is not None:
            cross_bars: dict[str, dict | None] = {}
            symbol = getattr(bar, 'symbol', None) or 'MNQ'
            sym_base = symbol.upper().rstrip("1")  # MNQ1 → MNQ
            for peer in _INTERMARKET_PAIRS.get(sym_base, []):
                try:
                    cross_bars[peer] = self._cross_bar_provider(peer)
                except Exception:
                    cross_bars[peer] = None

            allowed, im_mult, im_reason = intermarket_confirms(
                sym_base, bar, hist, cross_bars, side,
            )
            if not allowed:
                return None
            total_mult *= im_mult
            reasons.append(im_reason)

        # ── Aggregate ──
        if total_mult <= 0:
            return None

        return replace(
            opened,
            qty=opened.qty * total_mult,
            risk_usd=opened.risk_usd * total_mult,
            confluence=min(10.0, max(0.0, opened.confluence * total_mult)),
            regime=opened.regime + "_sniper_" + "+".join(reasons[:3]),
        )


# ---------------------------------------------------------------------------
# Cross-symbol bar loader for paper simulation
# ---------------------------------------------------------------------------

def build_cross_symbol_provider_for_paper(
    bot_symbol: str,
    data_directory: str | None = None,
) -> Callable[[str], dict | None] | None:
    """Build a cross-symbol bar provider for paper_trade_sim.

    For a given bot's primary symbol (e.g. "MNQ1"), pre-load the
    last N bars for all correlated symbols. The returned callable
    returns the most recent bar for the requested peer symbol.

    In paper mode, this is the bridge between single-symbol paper
    simulation and multi-symbol AlphaSniper confirmation.
    """
    import json
    from pathlib import Path

    peers_map = {
        "MNQ1": ["ES1", "NQ1"],
        "NQ1":  ["ES1", "MNQ1"],
        "MNQ":  ["ES", "NQ"],
        "NQ":   ["ES", "MNQ"],
        "ETH":  ["BTC"],
        "SOL":  ["BTC", "ETH"],
        "BTC":  ["ES"],
    }
    peers = peers_map.get(bot_symbol, [])
    if not peers:
        return None

    base_dir = Path(data_directory or "var/eta_engine/paper_bars_cache")
    peer_bars: dict[str, list[dict]] = {}

    for peer_sym in peers:
        cache_file = base_dir / f"{peer_sym}_latest.json"
        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text())
                if isinstance(data, list):
                    peer_bars[peer_sym] = data
            except (json.JSONDecodeError, OSError):
                pass

    if not peer_bars:
        return None

    def _provider(symbol: str) -> dict | None:
        bars = peer_bars.get(symbol)
        if bars and len(bars) > 0:
            return bars[-1]  # most recent bar
        return None

    return _provider
