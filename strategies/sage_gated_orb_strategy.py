"""
EVOLUTIONARY TRADING ALGO  //  strategies.sage_gated_orb_strategy
==================================================================
ORB with a sage-consensus overlay on the breakout direction, plus
classic ORB scale-out management and a VIX-spike entry filter.

Why
---
The 2026-04-27 sweep promoted ``mnq_orb_v1`` on agg OOS Sharpe
+0.80 / DSR median 0.52. Solid but not spectacular. The dominant
loss mode in real MNQ tape is FALSE BREAKOUTS — a 5m bar punches
through the opening range, fires the entry, then immediately
reverses.

Sage gives us a cheap second opinion. When all 22 market-theory
schools collectively say "NO this is not a real breakout" — i.e.
their composite_bias is NEUTRAL or opposite the breakout side —
the overlay vetoes the entry. When sage agrees with the breakout
direction, the trade fires as before.

The overlay is OPT-IN per ORBConfig (see ``sage_overlay_enabled``)
and uses the same SageConsensusConfig knobs as the standalone
sage strategy. Operators tune the threshold and the strategy stays
ORB-the-strategy-they-promoted, just with a quality filter.

2026-05-07 redesign — scale-out + VIX filter
---------------------------------------------
The 2026-05-07 fleet audit verdict on ``mnq_futures_sage`` flagged
expR 0.064 over 1156 trades — edge exists but the 95% CI is
[0.006, 0.122], i.e. fragile. Two operator-driven adds:

1. **Scale-out at 1.5R, runner to 3.5R**. Classic ORB management.
   Cuts naive RR=3.5 down to RR=3.0 net, but lifts realized
   win-rate by locking in a partial when price first reaches 1.5R.
   The runner moves stop to breakeven so the overall trade can no
   longer lose after the partial fires. Implemented via the
   ``partial_target`` field on ``_Open`` — engine does the
   bookkeeping, strategy emits the level.
2. **VIX-spike filter**. ORBs trap on high-VIX bars (failed
   breakouts on volatile open). Block new entries when the current
   VIX 5m close exceeds a rolling-percentile threshold (default
   p90 over 252 bars ≈ one trading day).

Both are configurable and default-on for the ``mnq_futures_sage``
production wiring; ablation runs flip ``enable_vix_filter=False``
or ``enable_scale_out=False``.

Composition
-----------
This is a *composition* strategy: it embeds an ORBStrategy and
delegates to it. When ORB returns an _Open, we run sage; if sage
disagrees, we reject by returning None and rolling back the ORB
day-state so the strategy gets to fire later in the day if a
better breakout appears.

Test surface stays small: the overlay reuses the existing ORB
tests as-is (delegation is invisible) and adds a few targeted
overlay-specific tests.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eta_engine.strategies.orb_strategy import ORBConfig, ORBStrategy
from eta_engine.strategies.sage_consensus_strategy import SageConsensusConfig

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SageGatedORBConfig:
    """Combined ORB + sage-overlay config + scale-out + VIX filter."""

    orb: ORBConfig = field(default_factory=ORBConfig)
    sage: SageConsensusConfig = field(default_factory=SageConsensusConfig)
    # Disable the overlay and behave identically to plain ORB. Useful
    # for ablation tests / "what does the gate cost vs. add?" runs.
    overlay_enabled: bool = True

    # ── Scale-out / partial-exit fields (Option A) ──
    # When ``enable_scale_out`` is True, the strategy sets
    # ``_Open.partial_target`` at ``rr_partial`` × stop distance and
    # the runner rides to the ORB target (typically rr_target=3.5).
    # ``partial_qty_frac`` of the size exits at the partial level;
    # the engine moves stop to breakeven on the runner.
    enable_scale_out: bool = True
    rr_partial: float = 1.5  # partial fires at +1.5R from entry
    partial_qty_frac: float = 0.5  # 50% off at partial

    # ── VIX-spike filter ──
    # Block new entries when the current VIX 5m close exceeds a
    # rolling-percentile threshold. This is the "no-trade if
    # VIX_5m > p90" filter from the 2026-05-07 audit. The provider
    # is a callable injected at construction (preferred — keeps the
    # strategy I/O-free) or, when None, a class-level CSV cache
    # loads from the canonical mnq_data/history/VIX_5m.csv path.
    enable_vix_filter: bool = True
    vix_lookback_bars: int = 252
    vix_pct_threshold: float = 0.90


# ---------------------------------------------------------------------------
# VIX provider — lazy class-cache fallback
# ---------------------------------------------------------------------------


# Default canonical path for VIX 5m bars. The class-cache fallback
# loads this once; tests inject a callable instead.
_DEFAULT_VIX_CSV = Path(
    r"C:\EvolutionaryTradingAlgo\mnq_data\history\VIX_5m.csv",
)

# Cache: ts (epoch seconds) → close. Loaded once per process by
# ``_load_vix_csv`` and reused. Tests should bypass this entirely
# by attaching a callable provider.
_VIX_CSV_CACHE: dict[int, float] = {}
_VIX_CSV_LOADED: bool = False


def _load_vix_csv(path: Path = _DEFAULT_VIX_CSV) -> dict[int, float]:
    """Load the VIX 5m CSV into a {epoch_seconds: close} map.

    The CSV header is ``time,open,high,low,close,volume`` where
    ``time`` is epoch seconds (UTC). Idempotent — successive calls
    return the cached map. Returns an empty dict if the path is
    missing (preserves fail-open semantics for the filter).
    """
    global _VIX_CSV_LOADED  # noqa: PLW0603 — process-global cache
    if _VIX_CSV_LOADED:
        return _VIX_CSV_CACHE
    if not path.exists():
        _VIX_CSV_LOADED = True
        return _VIX_CSV_CACHE
    try:
        with path.open("r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts = int(row["time"])
                    close = float(row["close"])
                except (ValueError, KeyError, TypeError):
                    continue
                _VIX_CSV_CACHE[ts] = close
    except OSError:
        # Disk error: leave cache empty, filter degrades to no-op.
        pass
    _VIX_CSV_LOADED = True
    return _VIX_CSV_CACHE


def _csv_vix_provider(bar: BarData) -> float | None:
    """Return the VIX close at-or-just-before ``bar.timestamp``.

    Looks up the bar's epoch-second timestamp in the loaded CSV.
    When VIX has no bar at that exact minute (holiday, late-quote),
    walks back up to 5 minutes to find the most recent close.
    Returns None when no recent VIX bar exists — the filter then
    degrades to "allow entry" (fail-open).
    """
    cache = _load_vix_csv()
    if not cache:
        return None
    ts = int(bar.timestamp.timestamp())
    # Most VIX 5m bars align on :00, :05, :10. Round down to a 5-min
    # boundary, then walk back up to 6 boundaries (= 30 min) to handle
    # gaps. 30m is conservative — anything older than that is stale.
    aligned = ts - (ts % 300)
    for delta in range(0, 1801, 300):  # 0, 300, 600, ..., 1800
        v = cache.get(aligned - delta)
        if v is not None:
            return v
    return None


# Type alias so callers (and tests) can wire a Callable cleanly.
VixProvider = "Callable[[BarData], float | None]"


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


def _bar_to_dict(b: BarData) -> dict[str, Any]:
    """Sage bar dict shape (mirrors sage_consensus_strategy._bar_to_dict)."""
    return {
        "ts": b.timestamp.isoformat(),
        "timestamp": b.timestamp,
        "open": float(b.open),
        "high": float(b.high),
        "low": float(b.low),
        "close": float(b.close),
        "volume": float(b.volume),
    }


class SageGatedORBStrategy:
    """ORB with a multi-school veto, scale-out management, and VIX filter."""

    def __init__(
        self,
        config: SageGatedORBConfig | None = None,
        *,
        vix_provider: Callable[[BarData], float | None] | None = None,
    ) -> None:
        self.cfg = config or SageGatedORBConfig()
        # Embed a real ORBStrategy. We DELEGATE the breakout logic
        # entirely; this class is a wrapper that filters its output.
        self._orb = ORBStrategy(self.cfg.orb)
        # VIX provider: caller-supplied callable wins; otherwise fall
        # back to the canonical CSV-cache provider. Test suites pass
        # a callable so tests don't depend on a CSV being present.
        self._vix_provider: Callable[[BarData], float | None] | None = (
            vix_provider if vix_provider is not None else _csv_vix_provider
        )
        # Per-process VIX history buffer for the rolling percentile.
        # Sized to vix_lookback_bars; new closes append, oldest evict.
        self._vix_history: list[float] = []
        self._last_vix_ts: datetime | None = None

        # ── Audit counters ──
        # Counted across the strategy lifetime so walk-forward / lab
        # runs can post-mortem how often each gate fired. Read by
        # callers via the property accessors below.
        self._n_vix_filtered: int = 0
        self._n_partial_exits_emitted: int = 0
        # Note: ``_n_runners_reached_target`` is not derivable from
        # within ``maybe_enter`` (the engine owns exits). It's a
        # placeholder for a future trade-close listener; kept here so
        # the symbol exists for audits + the test that touches it.
        self._n_runners_reached_target: int = 0

    # -- proxy: ES provider attachment --------------------------------------

    def attach_es_provider(self, provider: Any) -> None:  # noqa: ANN401 - provider is duck-typed
        """Forward to the embedded ORBStrategy."""
        self._orb.attach_es_provider(provider)

    def attach_vix_provider(
        self, provider: Callable[[BarData], float | None] | None,
    ) -> None:
        """Wire (or detach) a VIX provider after construction.

        Live runners attach a real-time provider; backtests use the
        CSV-cache default. Pass None to fall back to the default.
        """
        self._vix_provider = provider if provider is not None else _csv_vix_provider

    # -- audit accessors ------------------------------------------------------

    @property
    def n_vix_filtered(self) -> int:
        return self._n_vix_filtered

    @property
    def n_partial_exits_emitted(self) -> int:
        return self._n_partial_exits_emitted

    @property
    def n_runners_reached_target(self) -> int:
        return self._n_runners_reached_target

    # -- main entry point -----------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        """Run ORB; gate on sage consensus + VIX; emit scale-out target."""
        opened = self._orb.maybe_enter(bar, hist, equity, config)
        if opened is None:
            return None  # ORB itself rejected — nothing to gate

        # ── VIX filter ──
        # Run AFTER ORB so the day-state is in the same "would have
        # fired" position; if VIX vetoes we roll back the day-state
        # (same pattern as the sage veto) so a later non-spike bar
        # can still take the trade.
        if self.cfg.enable_vix_filter and self._vix_blocks_entry(bar):
            self._n_vix_filtered += 1
            self._reset_orb_day_state()
            return None

        if not self.cfg.overlay_enabled:
            # Ablation mode: skip sage but still apply scale-out + VIX.
            return self._maybe_attach_scale_out(opened)

        # Run sage on the same hist
        from eta_engine.brain.jarvis_v3.sage.base import MarketContext
        from eta_engine.brain.jarvis_v3.sage.consultation import consult_sage

        bars_dicts = [_bar_to_dict(b) for b in hist[-self.cfg.sage.sage_lookback_bars:]]
        if len(bars_dicts) < 25:
            # Not enough bars for the regime detector — let ORB fire as
            # if the overlay were off. Failing-closed here would silence
            # the strategy entirely on the first 24 bars of every run.
            return self._maybe_attach_scale_out(opened)

        ctx = MarketContext(
            bars=bars_dicts,
            side="long" if opened.side == "BUY" else "short",
            entry_price=float(bar.close),
            symbol=bar.symbol,
            instrument_class=self.cfg.sage.instrument_class,
        )
        try:
            report = consult_sage(
                ctx,
                enabled=(
                    set(self.cfg.sage.enabled_schools)
                    if self.cfg.sage.enabled_schools else None
                ),
                parallel=False,
                use_cache=True,
                apply_edge_weights=self.cfg.sage.apply_edge_weights,
            )
        except Exception:  # noqa: BLE001 — sage isolation
            # Sage failure shouldn't kill the strategy; fail-OPEN so
            # the breakout still fires (consistent with ORB behaviour).
            return self._maybe_attach_scale_out(opened)

        # Gate: composite_bias must match the breakout side AND
        # conviction >= threshold AND alignment_score >= threshold.
        # alignment_score uses ctx.side so already side-correct.
        bias_str = report.composite_bias.value
        bias_aligned = (
            (opened.side == "BUY" and bias_str == "long")
            or (opened.side == "SELL" and bias_str == "short")
        )
        if not bias_aligned:
            self._reset_orb_day_state()
            return None
        if report.conviction < self.cfg.sage.min_conviction:
            self._reset_orb_day_state()
            return None
        if report.alignment_score < self.cfg.sage.min_alignment:
            self._reset_orb_day_state()
            return None

        # Trade survived — annotate regime so the audit trail tells us
        # WHY it fired (sage-confirmed vs naked ORB).
        from dataclasses import replace
        confirmed = replace(opened, regime="orb_sage_confirmed")
        return self._maybe_attach_scale_out(confirmed)

    # -- helpers --------------------------------------------------------------

    def _vix_blocks_entry(self, bar: BarData) -> bool:
        """True when current VIX 5m > rolling p(threshold) of last N bars.

        Provider returns ``None`` when VIX has no recent bar — that's
        treated as "filter disabled for this bar" (fail-open). Buffer
        warmup (< vix_lookback_bars samples) also fails open: the
        percentile would be unreliable on a tiny window.
        """
        provider = self._vix_provider
        if provider is None:
            return False
        try:
            current = provider(bar)
        except Exception:  # noqa: BLE001 — provider isolation
            return False
        if current is None:
            return False
        # Keep history monotonic per bar timestamp; live runs may call
        # maybe_enter multiple times per minute, so dedupe on the
        # timestamp field rather than blindly appending.
        if self._last_vix_ts is None or bar.timestamp != self._last_vix_ts:
            self._vix_history.append(current)
            self._last_vix_ts = bar.timestamp
            # Bound the buffer to the lookback window.
            if len(self._vix_history) > self.cfg.vix_lookback_bars:
                # Trim from the front; preserves O(1) amortized cost.
                excess = len(self._vix_history) - self.cfg.vix_lookback_bars
                del self._vix_history[:excess]

        if len(self._vix_history) < self.cfg.vix_lookback_bars:
            return False  # warmup — not enough samples
        # p(threshold) over the in-window samples. Implemented inline
        # so the strategy stays numpy-free (matches the rest of the
        # ORB strategy file). The percentile uses the simple
        # nearest-rank method, which is fine for entry filtering.
        sorted_vix = sorted(self._vix_history)
        idx = int(self.cfg.vix_pct_threshold * (len(sorted_vix) - 1))
        threshold = sorted_vix[idx]
        return current > threshold

    def _maybe_attach_scale_out(self, opened: _Open) -> _Open:
        """Set ``partial_target`` on the trade when scale-out is enabled.

        The partial price = entry ± rr_partial × stop_dist. This sits
        BETWEEN entry and the runner target by construction, satisfying
        the engine's _Open invariant. The engine handles partial-fire +
        stop-to-BE bookkeeping; the strategy only emits the level.
        """
        if not self.cfg.enable_scale_out:
            return opened
        from dataclasses import replace
        stop_dist = abs(opened.entry_price - opened.stop)
        if stop_dist <= 0:
            return opened
        if opened.side == "BUY":
            partial = opened.entry_price + self.cfg.rr_partial * stop_dist
            # Don't set partial when target sits below the partial level
            # (rare; happens when rr_target < rr_partial in misconfigured
            # ablation runs). Skip the scale-out instead of raising.
            if partial >= opened.target:
                return opened
        else:  # SELL
            partial = opened.entry_price - self.cfg.rr_partial * stop_dist
            if partial <= opened.target:
                return opened
        self._n_partial_exits_emitted += 1
        return replace(
            opened,
            partial_target=partial,
            partial_qty_frac=self.cfg.partial_qty_frac,
        )

    def _reset_orb_day_state(self) -> None:
        """Roll back the ORB's ``breakout_taken`` latch + trade count.

        ORB's _Open path increments trades_today and flips
        breakout_taken ON the entry — but we just rejected the entry,
        so those flags must come back off or we lose the rest of the
        day to a single sage veto.
        """
        if self._orb._day is None:
            return
        self._orb._day.breakout_taken = False
        self._orb._day.trades_today = max(0, self._orb._day.trades_today - 1)
