"""EVOLUTIONARY TRADING ALGO  //  tests.test_strategies_engine_adapter.

Unit tests for :mod:`eta_engine.strategies.engine_adapter` -- the
bridge that lets the live bot loop talk to the pure-function policy
router.
"""

from __future__ import annotations

import pytest

from eta_engine.bots.base_bot import SignalType
from eta_engine.strategies.engine_adapter import (
    DEFAULT_BUFFER_BARS,
    RouterAdapter,
    bar_from_dict,
    context_from_dict,
    has_eligibility_for,
    strategy_signal_to_bot_signal,
)
from eta_engine.strategies.models import (
    Bar,
    Side,
    StrategyId,
    StrategySignal,
)
from eta_engine.strategies.policy_router import RouterDecision

# ---------------------------------------------------------------------------
# bar_from_dict
# ---------------------------------------------------------------------------


class TestBarFromDict:
    def test_canonical_keys(self) -> None:
        bar = bar_from_dict(
            {"ts": 100, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0},
        )
        assert isinstance(bar, Bar)
        assert bar.ts == 100
        assert bar.open == 1.0
        assert bar.high == 2.0
        assert bar.low == 0.5
        assert bar.close == 1.5
        assert bar.volume == 10.0

    def test_short_keys(self) -> None:
        bar = bar_from_dict({"t": 5, "o": 1, "h": 2, "l": 0, "c": 1, "v": 100})
        assert bar.ts == 5
        assert bar.close == 1.0

    def test_ts_fallback_when_missing(self) -> None:
        bar = bar_from_dict(
            {"open": 1, "high": 2, "low": 0, "close": 1},
            ts_fallback=42,
        )
        assert bar.ts == 42

    def test_volume_defaults_to_zero(self) -> None:
        bar = bar_from_dict({"ts": 1, "open": 1, "high": 2, "low": 0, "close": 1.5})
        assert bar.volume == 0.0

    def test_missing_close_raises(self) -> None:
        with pytest.raises(ValueError, match="missing or non-numeric OHLC"):
            bar_from_dict({"ts": 1, "open": 1, "high": 2, "low": 0})

    def test_non_numeric_raises(self) -> None:
        with pytest.raises(ValueError, match="missing or non-numeric OHLC"):
            bar_from_dict({"ts": 1, "open": "abc", "high": 2, "low": 0, "close": 1})

    def test_non_numeric_ts_falls_back(self) -> None:
        bar = bar_from_dict(
            {"ts": "abc", "open": 1, "high": 2, "low": 0, "close": 1},
            ts_fallback=7,
        )
        assert bar.ts == 7


# ---------------------------------------------------------------------------
# context_from_dict
# ---------------------------------------------------------------------------


class TestContextFromDict:
    def test_defaults_when_dict_empty(self) -> None:
        ctx = context_from_dict({})
        assert ctx.regime_label == "TRANSITION"
        assert ctx.confluence_score == 5.0
        assert ctx.vol_z == 0.0
        assert ctx.trend_bias is Side.FLAT
        assert ctx.htf_bias is Side.FLAT
        assert ctx.kill_switch_active is False
        assert ctx.session_allows_entries is True

    def test_reads_regime_label_directly(self) -> None:
        ctx = context_from_dict({"regime_label": "TRENDING"})
        assert ctx.regime_label == "TRENDING"

    def test_reads_regime_enum(self) -> None:
        class FakeRegime:
            value = "HIGH_VOL"

        ctx = context_from_dict({"regime": FakeRegime()})
        assert ctx.regime_label == "HIGH_VOL"

    def test_reads_regime_string(self) -> None:
        ctx = context_from_dict({"regime": "RANGING"})
        assert ctx.regime_label == "RANGING"

    def test_reads_confluence_and_vol_z(self) -> None:
        ctx = context_from_dict({"confluence_score": 8.5, "vol_z": 2.2})
        assert ctx.confluence_score == 8.5
        assert ctx.vol_z == 2.2

    def test_reads_htf_bias_string(self) -> None:
        ctx = context_from_dict({"htf_bias": "long"})
        assert ctx.htf_bias is Side.LONG
        ctx2 = context_from_dict({"htf_bias": "SELL"})
        assert ctx2.htf_bias is Side.SHORT

    def test_reads_htf_bias_enum(self) -> None:
        ctx = context_from_dict({"htf_bias": Side.SHORT})
        assert ctx.htf_bias is Side.SHORT

    def test_explicit_kill_switch_overrides_dict_default(self) -> None:
        ctx = context_from_dict({}, kill_switch_active=True)
        assert ctx.kill_switch_active is True

    def test_dict_kill_switch_wins_over_kwarg(self) -> None:
        ctx = context_from_dict({"kill_switch_active": False}, kill_switch_active=True)
        # The bar dict's value takes precedence because we trust upstream input
        assert ctx.kill_switch_active is False

    def test_overrides_last_write_wins(self) -> None:
        ctx = context_from_dict(
            {"regime_label": "TRENDING"},
            overrides={"regime_label": "CRISIS"},
        )
        assert ctx.regime_label == "CRISIS"


# ---------------------------------------------------------------------------
# strategy_signal_to_bot_signal
# ---------------------------------------------------------------------------


class TestStrategySignalToBotSignal:
    def test_actionable_long_converts(self) -> None:
        sig = StrategySignal(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            side=Side.LONG,
            entry=100.0,
            stop=95.0,
            target=115.0,
            confidence=7.0,
            risk_mult=1.0,
            rationale_tags=("liquidity_sweep",),
        )
        out = strategy_signal_to_bot_signal(sig, "MNQ")
        assert out is not None
        assert out.type is SignalType.LONG
        assert out.symbol == "MNQ"
        assert out.price == 100.0
        assert out.confidence == 7.0
        assert out.meta["setup"] == "liquidity_sweep_displacement"
        assert out.meta["stop_distance"] == pytest.approx(5.0)
        assert out.meta["target"] == 115.0
        assert out.meta["rationale_tags"] == ["liquidity_sweep"]

    def test_actionable_short_converts(self) -> None:
        sig = StrategySignal(
            strategy=StrategyId.OB_BREAKER_RETEST,
            side=Side.SHORT,
            entry=200.0,
            stop=210.0,
            target=180.0,
            confidence=6.0,
            risk_mult=1.0,
        )
        out = strategy_signal_to_bot_signal(sig, "NQ")
        assert out is not None
        assert out.type is SignalType.SHORT

    def test_flat_returns_none(self) -> None:
        sig = StrategySignal(
            strategy=StrategyId.FVG_FILL_CONFLUENCE,
            side=Side.FLAT,
            confidence=5.0,
            risk_mult=1.0,
        )
        assert strategy_signal_to_bot_signal(sig, "MNQ") is None

    def test_zero_confidence_returns_none(self) -> None:
        sig = StrategySignal(
            strategy=StrategyId.FVG_FILL_CONFLUENCE,
            side=Side.LONG,
            confidence=0.0,
            risk_mult=1.0,
        )
        assert strategy_signal_to_bot_signal(sig, "MNQ") is None

    def test_zero_risk_mult_returns_none(self) -> None:
        sig = StrategySignal(
            strategy=StrategyId.FVG_FILL_CONFLUENCE,
            side=Side.LONG,
            confidence=5.0,
            risk_mult=0.0,
        )
        assert strategy_signal_to_bot_signal(sig, "MNQ") is None

    def test_price_fallback_when_entry_zero(self) -> None:
        sig = StrategySignal(
            strategy=StrategyId.MTF_TREND_FOLLOWING,
            side=Side.LONG,
            entry=0.0,
            stop=95.0,
            target=115.0,
            confidence=5.0,
            risk_mult=1.0,
        )
        out = strategy_signal_to_bot_signal(sig, "MNQ", price_fallback=101.25)
        assert out is not None
        assert out.price == 101.25

    def test_meta_propagates_strategy_meta(self) -> None:
        sig = StrategySignal(
            strategy=StrategyId.OB_BREAKER_RETEST,
            side=Side.LONG,
            entry=100.0,
            stop=95.0,
            target=115.0,
            confidence=5.0,
            risk_mult=1.0,
            meta={"bos_pivot": 99.5},
        )
        out = strategy_signal_to_bot_signal(sig, "MNQ")
        assert out is not None
        assert out.meta["strategy_meta"] == {"bos_pivot": 99.5}


# ---------------------------------------------------------------------------
# has_eligibility_for
# ---------------------------------------------------------------------------


class TestHasEligibilityFor:
    def test_known_assets_true(self) -> None:
        for asset in ("MNQ", "NQ", "BTC", "ETH", "SOL", "XRP", "PORTFOLIO"):
            assert has_eligibility_for(asset)

    def test_lower_case_ok(self) -> None:
        assert has_eligibility_for("mnq")

    def test_unknown_asset_false(self) -> None:
        assert has_eligibility_for("DOGE") is False


# ---------------------------------------------------------------------------
# RouterAdapter
# ---------------------------------------------------------------------------


def _bar_dict(ts: int, close: float = 100.0) -> dict[str, float]:
    return {
        "ts": ts,
        "open": close - 0.5,
        "high": close + 0.5,
        "low": close - 1.0,
        "close": close,
        "volume": 100.0,
    }


class TestRouterAdapterBasics:
    def test_init_upper_cases_asset(self) -> None:
        adapter = RouterAdapter(asset="mnq")
        assert adapter.asset == "MNQ"

    def test_default_max_bars(self) -> None:
        adapter = RouterAdapter(asset="MNQ")
        assert adapter.max_bars == DEFAULT_BUFFER_BARS

    def test_init_rejects_small_buffer(self) -> None:
        with pytest.raises(ValueError, match="max_bars"):
            RouterAdapter(asset="MNQ", max_bars=1)

    def test_buffered_count_starts_zero(self) -> None:
        assert RouterAdapter(asset="MNQ").buffered_count == 0

    def test_bars_property_is_defensive_copy(self) -> None:
        adapter = RouterAdapter(asset="MNQ")
        adapter.push_bar(_bar_dict(0))
        snapshot = adapter.bars
        snapshot.clear()
        assert adapter.buffered_count == 1


class TestRouterAdapterBuffer:
    def test_push_appends_bar(self) -> None:
        adapter = RouterAdapter(asset="MNQ")
        adapter.push_bar(_bar_dict(0))
        adapter.push_bar(_bar_dict(1))
        assert adapter.buffered_count == 2

    def test_push_respects_max_bars(self) -> None:
        adapter = RouterAdapter(asset="MNQ", max_bars=5)
        for i in range(10):
            adapter.push_bar(_bar_dict(i))
        assert adapter.buffered_count == 5
        assert adapter.bars[0].ts == 5
        assert adapter.bars[-1].ts == 9

    def test_seed_bulk_loads_history(self) -> None:
        adapter = RouterAdapter(asset="MNQ", max_bars=100)
        adapter.seed(_bar_dict(i) for i in range(50))
        assert adapter.buffered_count == 50
        # Seed does not dispatch
        assert adapter.last_decision is None

    def test_reset_clears_buffer_and_decision(self) -> None:
        adapter = RouterAdapter(asset="MNQ")
        adapter.push_bar(_bar_dict(0))
        adapter.reset()
        assert adapter.buffered_count == 0
        assert adapter.last_decision is None


class TestRouterAdapterDispatch:
    def test_push_bar_flat_bars_returns_none_signal(self) -> None:
        """With boring flat bars no strategy should fire."""
        adapter = RouterAdapter(asset="MNQ", max_bars=50)
        adapter.seed(_bar_dict(i) for i in range(50))
        out = adapter.push_bar(_bar_dict(50))
        assert out is None
        # But a decision was recorded
        assert isinstance(adapter.last_decision, RouterDecision)
        assert adapter.last_decision.asset == "MNQ"

    def test_push_bar_records_last_decision(self) -> None:
        adapter = RouterAdapter(asset="MNQ")
        for i in range(10):
            adapter.push_bar(_bar_dict(i))
        assert adapter.last_decision is not None
        # MNQ has 4 eligible strategies
        assert len(adapter.last_decision.candidates) == 4

    def test_kill_switch_propagates_to_context(self) -> None:
        """When kill_switch_active=True, strategies must return flat."""
        adapter = RouterAdapter(asset="MNQ", kill_switch_active=True)
        for i in range(50):
            adapter.push_bar(_bar_dict(i))
        last = adapter.last_decision
        assert last is not None
        assert last.winner.is_actionable is False

    def test_session_closed_propagates_to_context(self) -> None:
        adapter = RouterAdapter(asset="MNQ", session_allows_entries=False)
        for i in range(50):
            adapter.push_bar(_bar_dict(i))
        last = adapter.last_decision
        assert last is not None
        assert last.winner.is_actionable is False


class TestRouterAdapterWithStubRegistry:
    """Inject a fake registry to force a winner and verify end-to-end mapping."""

    def test_stub_winner_converts_to_bot_signal(self) -> None:
        def fake_long(_b: list[Bar], _c: object) -> StrategySignal:
            return StrategySignal(
                strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                side=Side.LONG,
                entry=101.0,
                stop=99.0,
                target=105.0,
                confidence=7.0,
                risk_mult=1.0,
                rationale_tags=("stub_winner",),
            )

        adapter = RouterAdapter(
            asset="MNQ",
            max_bars=10,
            registry={StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fake_long},
            eligibility={"MNQ": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)},
        )
        sig = adapter.push_bar(_bar_dict(0, close=100.0))
        assert sig is not None
        assert sig.type is SignalType.LONG
        assert sig.price == 101.0
        assert sig.meta["setup"] == "liquidity_sweep_displacement"
        assert sig.meta["stop_distance"] == pytest.approx(2.0)
        assert "stub_winner" in sig.meta["rationale_tags"]

    def test_stub_flat_returns_none(self) -> None:
        def fake_flat(_b: list[Bar], _c: object) -> StrategySignal:
            return StrategySignal(
                strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                side=Side.FLAT,
                rationale_tags=("flat_stub",),
            )

        adapter = RouterAdapter(
            asset="MNQ",
            registry={StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fake_flat},
            eligibility={"MNQ": (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)},
        )
        sig = adapter.push_bar(_bar_dict(0))
        assert sig is None
        assert adapter.last_decision is not None
        assert adapter.last_decision.winner.side is Side.FLAT

    def test_bar_ts_fallback_when_missing(self) -> None:
        """Bars without ts get auto-numbered via the adapter's counter."""
        adapter = RouterAdapter(asset="MNQ", max_bars=10)
        # bars without ts keys
        adapter.push_bar({"open": 1, "high": 2, "low": 0, "close": 1})
        adapter.push_bar({"open": 1, "high": 2, "low": 0, "close": 1})
        # Internally the adapter assigns ts=0, 1
        ts_values = [b.ts for b in adapter.bars]
        assert ts_values == [0, 1]


class TestRegistryKillSwitchChokepoint:
    """The risk-sage chokepoint (2026-04-27) — when bot_id is set,
    push_bar must short-circuit to None for any bot whose registry
    extras carry ``deactivated=True``. The xrp_perp registry entry
    is used as the canonical fixture since the operator muted it
    for "no news feed" reasons in the same review."""

    def test_deactivated_bot_short_circuits_to_none(self) -> None:
        # xrp_perp is muted in the registry — adapter must return None
        # without buffering a dispatch decision.
        adapter = RouterAdapter(asset="MNQ", bot_id="xrp_perp")
        out = adapter.push_bar(_bar_dict(0))
        assert out is None
        assert adapter.last_decision is None, "muted bots must not record a dispatch decision either"

    def test_active_bot_runs_normally(self) -> None:
        # Pin history:
        # - mnq_futures (deactivated DIAMOND CUT 2026-05-02)
        # - mnq_futures_sage (sidecar deactivated 2026-05-05 after
        #   elite-gate found severe overfit, decay -79%)
        # - mnq_anchor_sweep (gate-cleared all 5 lights 2026-05-05,
        #   promoted to paper_soak — currently the canonical active
        #   MNQ bot for adapter dispatch tests).
        adapter = RouterAdapter(asset="MNQ", bot_id="mnq_anchor_sweep")
        adapter.push_bar(_bar_dict(0))
        # The dispatch path runs; last_decision is populated.
        assert adapter.last_decision is not None

    def test_unset_bot_id_runs_normally(self) -> None:
        # bot_id=None preserves the legacy code path used by every
        # backtest and unit test that doesn't route through registry.
        adapter = RouterAdapter(asset="MNQ", bot_id=None)
        adapter.push_bar(_bar_dict(0))
        assert adapter.last_decision is not None

    def test_unknown_bot_id_short_circuits_to_none(self) -> None:
        # is_bot_active returns False for unknown bot_ids — same
        # as a deactivated bot, the adapter must short-circuit.
        # Better to fail-shut on a typo than silently route.
        adapter = RouterAdapter(asset="MNQ", bot_id="does_not_exist_xyz")
        out = adapter.push_bar(_bar_dict(0))
        assert out is None
        assert adapter.last_decision is None
