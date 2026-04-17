"""EVOLUTIONARY TRADING ALGO  //  strategies.engine_adapter.

Bridge between the live bot handlers (``on_bar(dict)`` / ``on_signal``
in :mod:`eta_engine.bots.base_bot`) and the pure-function
:mod:`eta_engine.strategies.policy_router`.

Bots work with pydantic ``Signal`` + ``dict`` bars. The strategies
package works with frozen dataclass :class:`Bar` + :class:`StrategySignal`.
This adapter converts between the two worlds *without* either side
having to know about the other:

* pure conversion helpers (``bar_from_dict``, ``context_from_dict``,
  ``strategy_signal_to_bot_signal``) -- fully deterministic, no state,
  easy to unit test.
* :class:`RouterAdapter` -- thin stateful wrapper holding a rolling
  bar buffer and the last :class:`RouterDecision` so a bot can call
  ``push_bar`` once per OHLC and get back a ``Signal | None`` ready
  for its ``on_signal`` hook.

The adapter is **import-light**. It does not touch pydantic, torch,
web3, or any network primitives so it can run in the hot trading
loop without GIL or allocation surprises.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from eta_engine.bots.base_bot import Signal, SignalType
from eta_engine.strategies.eta_policy import StrategyContext
from eta_engine.strategies.models import Bar, Side, StrategySignal
from eta_engine.strategies.policy_router import (
    DEFAULT_ELIGIBILITY,
    RouterDecision,
    dispatch,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from eta_engine.strategies.decision_sink import RouterDecisionSink
    from eta_engine.strategies.models import StrategyId

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default rolling buffer cap. ``mtf_trend_following`` needs a 200-period MA
#: plus BOS lookback; 300 bars gives us headroom without holding a full day.
DEFAULT_BUFFER_BARS: int = 300

#: Keys on a dict-bar that the adapter recognises. First one present wins,
#: so callers can feed either ``open``/``o`` / etc. interchangeably.
_OPEN_KEYS: tuple[str, ...] = ("open", "o")
_HIGH_KEYS: tuple[str, ...] = ("high", "h")
_LOW_KEYS: tuple[str, ...] = ("low", "l")
_CLOSE_KEYS: tuple[str, ...] = ("close", "c")
_VOLUME_KEYS: tuple[str, ...] = ("volume", "v", "vol")
_TS_KEYS: tuple[str, ...] = ("ts", "timestamp", "t", "time")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _first_present(d: dict[str, Any], keys: tuple[str, ...], default: Any) -> Any:  # noqa: ANN401
    for k in keys:
        if k in d:
            return d[k]
    return default


def bar_from_dict(d: dict[str, Any], *, ts_fallback: int = 0) -> Bar:
    """Convert a bot-style ``dict`` bar into a frozen :class:`Bar`.

    Missing ``ts`` falls back to ``ts_fallback`` (callers can inject a
    monotonic counter). Missing OHLC raises :class:`ValueError`. Missing
    volume defaults to 0.0 since most bot dicts include it optionally.
    """
    try:
        o = float(_first_present(d, _OPEN_KEYS, _MISSING))
        h = float(_first_present(d, _HIGH_KEYS, _MISSING))
        lo = float(_first_present(d, _LOW_KEYS, _MISSING))
        c = float(_first_present(d, _CLOSE_KEYS, _MISSING))
    except (TypeError, ValueError) as err:
        raise ValueError(f"bar dict missing or non-numeric OHLC: {d!r}") from err
    v = float(_first_present(d, _VOLUME_KEYS, 0.0))
    ts_raw = _first_present(d, _TS_KEYS, ts_fallback)
    try:
        ts = int(ts_raw)
    except (TypeError, ValueError):
        ts = ts_fallback
    return Bar(ts=ts, open=o, high=h, low=lo, close=c, volume=v)


_MISSING: object = object()  # sentinel for required-field detection


def context_from_dict(
    d: dict[str, Any],
    *,
    kill_switch_active: bool = False,
    session_allows_entries: bool = True,
    overrides: dict[str, Any] | None = None,
) -> StrategyContext:
    """Build a :class:`StrategyContext` from a bar dict + explicit flags.

    The bot's ``on_bar`` dict typically contains ``regime`` / ``adx_14``
    / ``confluence_score`` / ``vol_z`` / ``htf_bias`` produced upstream by
    :mod:`eta_engine.core.confluence_scorer` and friends. This helper
    picks them up with sensible defaults so an under-populated dict never
    crashes the strategy stack.

    ``overrides`` is a last-wins dict for callers who want to inject test
    scenarios without mutating the bar.
    """
    base: dict[str, Any] = {
        "regime_label": _pick_regime_label(d),
        "confluence_score": float(d.get("confluence_score", 5.0)),
        "vol_z": float(d.get("vol_z", 0.0)),
        "trend_bias": _pick_side(d.get("trend_bias")),
        "session_allows_entries": bool(
            d.get("session_allows_entries", session_allows_entries),
        ),
        "kill_switch_active": bool(d.get("kill_switch_active", kill_switch_active)),
        "htf_bias": _pick_side(d.get("htf_bias")),
    }
    if overrides:
        base.update(overrides)
    return StrategyContext(**base)


def _pick_regime_label(d: dict[str, Any]) -> str:
    if "regime_label" in d:
        return str(d["regime_label"])
    regime = d.get("regime")
    if regime is None:
        return "TRANSITION"
    # Support both Enum and str
    if hasattr(regime, "value"):
        return str(regime.value)
    return str(regime)


def _pick_side(raw: Any) -> Side:  # noqa: ANN401 - user-facing coercion
    if raw is None:
        return Side.FLAT
    if isinstance(raw, Side):
        return raw
    text = str(raw).upper()
    if text in ("LONG", "BUY", "UP"):
        return Side.LONG
    if text in ("SHORT", "SELL", "DOWN"):
        return Side.SHORT
    return Side.FLAT


def strategy_signal_to_bot_signal(
    signal: StrategySignal,
    symbol: str,
    *,
    price_fallback: float = 0.0,
) -> Signal | None:
    """Convert a :class:`StrategySignal` into the bot's pydantic ``Signal``.

    Returns ``None`` when the strategy signal is not actionable (flat,
    zero confidence, or kill-switch muted). The returned ``Signal``
    carries the rationale + per-strategy stop distance inside ``meta``
    so :meth:`MnqBot._size_from_signal` can compute contracts without
    re-running the detectors.
    """
    if not signal.is_actionable:
        return None
    sig_type = _side_to_signal_type(signal.side)
    if sig_type is None:
        return None
    price = signal.entry if signal.entry > 0.0 else price_fallback
    stop_distance = abs(signal.entry - signal.stop) if signal.stop > 0.0 else 0.0
    meta: dict[str, Any] = {
        "setup": signal.strategy.value,
        "strategy": signal.strategy.value,
        "risk_mult": signal.risk_mult,
        "rr": signal.rr,
        "rationale_tags": list(signal.rationale_tags),
    }
    if stop_distance > 0.0:
        meta["stop_distance"] = stop_distance
    if signal.target > 0.0:
        meta["target"] = signal.target
    if signal.stop > 0.0:
        meta["stop"] = signal.stop
    if signal.meta:
        meta["strategy_meta"] = dict(signal.meta)
    return Signal(
        type=sig_type,
        symbol=symbol,
        price=price,
        size=0.0,  # let _size_from_signal decide from stop_distance
        confidence=signal.confidence,
        meta=meta,
    )


def _side_to_signal_type(side: Side) -> SignalType | None:
    if side is Side.LONG:
        return SignalType.LONG
    if side is Side.SHORT:
        return SignalType.SHORT
    return None


# ---------------------------------------------------------------------------
# Stateful adapter
# ---------------------------------------------------------------------------


@dataclass
class RouterAdapter:
    """Bot-facing wrapper around :func:`policy_router.dispatch`.

    Holds a rolling bar buffer so a bot can feed one dict-bar per tick
    and get back either ``None`` (no trade) or a ready-to-route
    :class:`Signal`. The last :class:`RouterDecision` is kept for
    observability (dashboards, decision-journal payloads).

    The adapter is not thread-safe by itself; wrap access in your own
    asyncio lock if multiple coroutines could call ``push_bar``. In the
    common single-consumer bot loop no locking is needed.
    """

    asset: str
    max_bars: int = DEFAULT_BUFFER_BARS
    eligibility: dict[str, tuple[StrategyId, ...]] | None = None
    registry: dict[StrategyId, Callable[..., StrategySignal]] | None = None
    kill_switch_active: bool = False
    session_allows_entries: bool = True
    #: Optional sink that writes every dispatch to the decision journal.
    #: When set, ``push_bar`` invokes ``decision_sink.emit(last_decision)``
    #: after the router runs. Defaults to ``None`` so bots without an
    #: observability stack pay zero overhead.
    decision_sink: RouterDecisionSink | None = None

    # private
    _bars: deque[Bar] = field(init=False, repr=False)
    _last_decision: RouterDecision | None = field(default=None, init=False, repr=False)
    _ts_counter: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_bars < 2:
            raise ValueError(f"max_bars must be >= 2, got {self.max_bars}")
        self.asset = self.asset.upper()
        self._bars = deque(maxlen=self.max_bars)

    # ── Buffer lifecycle ──

    @property
    def bars(self) -> list[Bar]:
        """Defensive copy so the caller can't mutate buffer state."""
        return list(self._bars)

    @property
    def buffered_count(self) -> int:
        return len(self._bars)

    @property
    def last_decision(self) -> RouterDecision | None:
        return self._last_decision

    def reset(self) -> None:
        """Clear buffer + last-decision. Call on bot restart / regime flip."""
        self._bars.clear()
        self._last_decision = None
        self._ts_counter = 0

    def seed(self, bar_dicts: Iterable[dict[str, Any]]) -> None:
        """Bulk-load historical bars before going live. Does not dispatch."""
        for d in bar_dicts:
            self._append_bar_dict(d)

    # ── Main entry ──

    def push_bar(self, bar_dict: dict[str, Any]) -> Signal | None:
        """Append ``bar_dict`` to the buffer, dispatch, and map the result.

        Returns a bot-ready :class:`Signal` when the winning strategy is
        actionable, else ``None``. Either way ``last_decision`` is
        updated so observability code can audit every tick.

        When :attr:`decision_sink` is wired, every dispatch is pushed
        into the decision journal before the signal is returned. Sink
        failures are swallowed so an observability issue cannot crash
        the hot trading loop.
        """
        self._append_bar_dict(bar_dict)
        ctx = context_from_dict(
            bar_dict,
            kill_switch_active=self.kill_switch_active,
            session_allows_entries=self.session_allows_entries,
        )
        decision = dispatch(
            self.asset,
            list(self._bars),
            ctx,
            eligibility=self.eligibility,
            registry=self.registry,
        )
        self._last_decision = decision
        if self.decision_sink is not None:
            # Emission failures are already swallowed inside the sink.
            self.decision_sink.emit(decision)
        price = float(bar_dict.get("close", 0.0))
        return strategy_signal_to_bot_signal(
            decision.winner,
            symbol=self.asset,
            price_fallback=price,
        )

    # ── Internal ──

    def _append_bar_dict(self, d: dict[str, Any]) -> None:
        bar = bar_from_dict(d, ts_fallback=self._ts_counter)
        self._bars.append(bar)
        self._ts_counter += 1


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def has_eligibility_for(asset: str) -> bool:
    """True if :data:`DEFAULT_ELIGIBILITY` has an explicit row for ``asset``.

    Lets callers decide whether to build a :class:`RouterAdapter` at all
    for exotic symbols that would otherwise fall through to the unknown
    fallback four-strategy basket.
    """
    return asset.upper() in DEFAULT_ELIGIBILITY


__all__ = [
    "DEFAULT_BUFFER_BARS",
    "RouterAdapter",
    "bar_from_dict",
    "context_from_dict",
    "has_eligibility_for",
    "strategy_signal_to_bot_signal",
]
