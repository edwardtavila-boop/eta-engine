"""Tests for sage_daily_gated, ensemble_voting, drawdown_aware_sizing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.backtest.engine import _Open
from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.crypto_macro_confluence_strategy import (
    CryptoMacroConfluenceConfig,
    MacroConfluenceConfig,
)
from eta_engine.strategies.crypto_regime_trend_strategy import CryptoRegimeTrendConfig
from eta_engine.strategies.drawdown_aware_sizing import (
    DrawdownAwareSizingConfig,
    DrawdownAwareSizingStrategy,
)
from eta_engine.strategies.ensemble_voting_strategy import (
    EnsembleVotingConfig,
    EnsembleVotingStrategy,
)
from eta_engine.strategies.sage_daily_gated_strategy import (
    SageDailyGatedConfig,
    SageDailyGatedStrategy,
    SageDailyVerdict,
)


def _bar(idx: int, *, h: float, low: float, c: float | None = None,
         v: float = 1000.0) -> BarData:
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=idx)
    c = c if c is not None else (h + low) / 2
    return BarData(
        timestamp=ts, symbol="BTC", open=(h + low) / 2,
        high=h, low=low, close=c, volume=v,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="BTC", initial_equity=10_000.0,
        risk_per_trade_pct=0.01, confluence_threshold=0.0,
        max_trades_per_day=10,
    )


def _open(side: str, entry: float, qty: float, risk: float) -> _Open:
    """Factory for _Open instances in voting tests."""
    return _Open(
        entry_bar=_bar(0, h=entry + 1, low=entry - 1, c=entry),
        side=side, qty=qty, entry_price=entry,
        stop=entry - 1.0 if side == "BUY" else entry + 1.0,
        target=entry + 3.0 if side == "BUY" else entry - 3.0,
        risk_usd=risk, confluence=10.0, leverage=1.0,
        regime="test",
    )


# ---------------------------------------------------------------------------
# SageDailyGatedStrategy
# ---------------------------------------------------------------------------


def _make_sage_strat() -> SageDailyGatedStrategy:
    base_cfg = CryptoMacroConfluenceConfig(
        base=CryptoRegimeTrendConfig(
            regime_ema=20, pullback_ema=5, warmup_bars=25,
            atr_period=5, min_bars_between_trades=0,
            pullback_tolerance_pct=2.0, max_trades_per_day=100,
        ),
        filters=MacroConfluenceConfig(),  # no underlying filters
    )
    return SageDailyGatedStrategy(SageDailyGatedConfig(
        base=base_cfg, min_daily_conviction=0.30, strict_mode=False,
    ))


def _setup_uptrend(s: SageDailyGatedStrategy, n: int = 35) -> list[BarData]:
    cfg = _config()
    hist: list[BarData] = []
    for i in range(n):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    return hist


def test_sage_daily_no_provider_passes_through() -> None:
    """No daily verdict provider → underlying strategy fires unchanged."""
    s = _make_sage_strat()
    cfg = _config()
    hist = _setup_uptrend(s)
    pull_ema = s._base._base._pullback_ema
    assert pull_ema is not None
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"


def test_sage_daily_long_passes_when_sage_aligned() -> None:
    s = _make_sage_strat()
    cfg = _config()
    s.attach_daily_verdict_provider(
        lambda d: SageDailyVerdict(direction="long", conviction=0.6, composite=0.6),
    )
    hist = _setup_uptrend(s)
    pull_ema = s._base._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert "sage_daily_long" in out.regime


def test_sage_daily_long_blocked_when_sage_says_short() -> None:
    s = _make_sage_strat()
    cfg = _config()
    s.attach_daily_verdict_provider(
        lambda d: SageDailyVerdict(direction="short", conviction=0.7, composite=-0.7),
    )
    hist = _setup_uptrend(s)
    pull_ema = s._base._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is None


def test_sage_daily_low_conviction_falls_through() -> None:
    """Below min_daily_conviction → sage too uncertain to veto."""
    s = _make_sage_strat()
    cfg = _config()
    # Strong short bias but very low conviction → should fall through
    s.attach_daily_verdict_provider(
        lambda d: SageDailyVerdict(direction="short", conviction=0.10, composite=-0.10),
    )
    hist = _setup_uptrend(s)
    pull_ema = s._base._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None  # should fire — sage too uncertain


def test_sage_daily_strict_mode_blocks_neutral() -> None:
    """strict_mode=True → neutral sage vetoes (no neutral fall-through)."""
    cfg_obj = SageDailyGatedConfig(
        base=CryptoMacroConfluenceConfig(
            base=CryptoRegimeTrendConfig(
                regime_ema=20, pullback_ema=5, warmup_bars=25,
                atr_period=5, min_bars_between_trades=0,
                pullback_tolerance_pct=2.0, max_trades_per_day=100,
            ),
            filters=MacroConfluenceConfig(),
        ),
        min_daily_conviction=0.30, strict_mode=True,
    )
    s = SageDailyGatedStrategy(cfg_obj)
    s.attach_daily_verdict_provider(
        lambda d: SageDailyVerdict(direction="neutral", conviction=0.50, composite=0.0),
    )
    cfg = _config()
    hist = _setup_uptrend(s)
    pull_ema = s._base._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is None


# ---------------------------------------------------------------------------
# EnsembleVotingStrategy
# ---------------------------------------------------------------------------


class _StubStrategy:
    """Stub sub-strategy that returns a pre-canned _Open or None."""

    def __init__(self, response: _Open | None) -> None:
        self._response = response

    def maybe_enter(self, bar, hist, equity, config):  # type: ignore[no-untyped-def]
        return self._response


def test_ensemble_requires_min_agreement() -> None:
    """With min_agreement=2, single-strategy fire alone should not fire."""
    s = EnsembleVotingStrategy(
        sub_strategies=[
            ("a", _StubStrategy(_open("BUY", 100.0, 1.0, 100.0))),
            ("b", _StubStrategy(None)),
            ("c", _StubStrategy(None)),
        ],
        config=EnsembleVotingConfig(min_agreement_count=2),
    )
    out = s.maybe_enter(_bar(0, h=100, low=99, c=99.5), [], 10_000.0, _config())
    assert out is None


def test_ensemble_fires_on_two_agreeing() -> None:
    s = EnsembleVotingStrategy(
        sub_strategies=[
            ("a", _StubStrategy(_open("BUY", 100.0, 1.0, 100.0))),
            ("b", _StubStrategy(_open("BUY", 102.0, 1.0, 100.0))),
            ("c", _StubStrategy(None)),
        ],
        # Default composition_mode flipped to "elect_one" (winner-takes-
        # bracket; avoids geometrically-incoherent averaged stops). This
        # test verifies the average-of-entries semantic, so set the
        # legacy "average" mode explicitly. A separate test should
        # cover elect_one.
        config=EnsembleVotingConfig(min_agreement_count=2, composition_mode="average"),
    )
    out = s.maybe_enter(_bar(0, h=100, low=99, c=99.5), [], 10_000.0, _config())
    assert out is not None
    assert out.side == "BUY"
    # Average entry: (100 + 102) / 2 = 101
    assert out.entry_price == pytest.approx(101.0)
    assert "ensemble_buy" in out.regime


def test_ensemble_no_consensus_when_split() -> None:
    """One BUY, one SELL, one None — no side has 2 votes."""
    s = EnsembleVotingStrategy(
        sub_strategies=[
            ("a", _StubStrategy(_open("BUY", 100.0, 1.0, 100.0))),
            ("b", _StubStrategy(_open("SELL", 100.0, 1.0, 100.0))),
            ("c", _StubStrategy(None)),
        ],
        config=EnsembleVotingConfig(min_agreement_count=2),
    )
    out = s.maybe_enter(_bar(0, h=100, low=99, c=99.5), [], 10_000.0, _config())
    assert out is None


def test_ensemble_size_by_agreement_scales_qty() -> None:
    """When 3/3 agree and size_by_agreement=True, qty scales 3/2 = 1.5x."""
    s = EnsembleVotingStrategy(
        sub_strategies=[
            ("a", _StubStrategy(_open("BUY", 100.0, 2.0, 100.0))),
            ("b", _StubStrategy(_open("BUY", 100.0, 2.0, 100.0))),
            ("c", _StubStrategy(_open("BUY", 100.0, 2.0, 100.0))),
        ],
        config=EnsembleVotingConfig(
            min_agreement_count=2, size_by_agreement=True,
            max_size_multiplier=2.0,
        ),
    )
    out = s.maybe_enter(_bar(0, h=100, low=99, c=99.5), [], 10_000.0, _config())
    assert out is not None
    # avg qty = 2.0; scale = 3/2 = 1.5; result = 3.0
    assert out.qty == pytest.approx(3.0, rel=1e-3)


def test_ensemble_confidence_weighting_biases_entry_toward_stronger_vote() -> None:
    """Confidence-weighting should pull aggregate entry toward stronger proposal."""
    s = EnsembleVotingStrategy(
        sub_strategies=[
            ("trend_a", _StubStrategy(_open("BUY", 100.0, 1.0, 100.0))),
            ("trend_b", _StubStrategy(_open("BUY", 110.0, 1.0, 300.0))),
        ],
        config=EnsembleVotingConfig(
            min_agreement_count=2,
            use_confidence_weighting=True,
            use_regime_router=False,
        ),
    )
    out = s.maybe_enter(_bar(0, h=101, low=99, c=100.0), [], 10_000.0, _config())
    assert out is not None
    # Arithmetic mean would be 105; weighted aggregate should lean higher.
    assert out.entry_price > 105.0


def test_ensemble_fail_safe_abstains_on_toxic_wick_bar() -> None:
    """Adversarial fail-safe should block entries on toxic wick conditions."""
    s = EnsembleVotingStrategy(
        sub_strategies=[
            ("trend_a", _StubStrategy(_open("BUY", 100.0, 1.0, 100.0))),
            ("trend_b", _StubStrategy(_open("BUY", 100.0, 1.0, 100.0))),
        ],
        config=EnsembleVotingConfig(min_agreement_count=2, enable_fail_safe=True),
    )
    hist = [
        _bar(i, h=101.0 + i * 0.1, low=99.0 + i * 0.1, c=100.0 + i * 0.1)
        for i in range(8)
    ]
    # Tiny body, huge wicks -> should trigger abstain.
    toxic = _bar(99, h=112.0, low=88.0, c=100.01)
    out = s.maybe_enter(toxic, hist, 10_000.0, _config())
    assert out is None


def test_ensemble_empty_substrategies_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        EnsembleVotingStrategy(sub_strategies=[])


def test_ensemble_min_above_count_rejected() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        EnsembleVotingStrategy(
            sub_strategies=[("a", _StubStrategy(None))],
            config=EnsembleVotingConfig(min_agreement_count=3),
        )


# ---------------------------------------------------------------------------
# DrawdownAwareSizingStrategy
# ---------------------------------------------------------------------------


def test_dd_sizing_no_drawdown_passes_through() -> None:
    """At equity = HWM, multiplier = 1.0 → opened unchanged."""
    sub = _StubStrategy(_open("BUY", 100.0, 5.0, 50.0))
    s = DrawdownAwareSizingStrategy(sub, DrawdownAwareSizingConfig())
    out = s.maybe_enter(_bar(0, h=100, low=99, c=99.5), [], 10_000.0, _config())
    assert out is not None
    assert out.qty == pytest.approx(5.0)
    assert out.risk_usd == pytest.approx(50.0)


def test_dd_sizing_reduces_size_on_drawdown() -> None:
    """Equity drops 20% from HWM → multiplier ≈ 1 - 0.5*0.2 = 0.9."""
    sub = _StubStrategy(_open("BUY", 100.0, 10.0, 100.0))
    s = DrawdownAwareSizingStrategy(
        sub, DrawdownAwareSizingConfig(drawdown_penalty=0.5),
    )
    # Bar 1: at HWM
    s.maybe_enter(_bar(0, h=100, low=99, c=99.5), [], 10_000.0, _config())
    # Bar 2: equity drops 20%
    out = s.maybe_enter(_bar(1, h=100, low=99, c=99.5), [], 8_000.0, _config())
    assert out is not None
    # multiplier = 1 - 0.5 * 0.2 = 0.9 → qty = 10 * 0.9 = 9
    assert out.qty == pytest.approx(9.0, rel=1e-3)
    assert "dd0.20" in out.regime
    assert "mult0.90" in out.regime


def test_dd_sizing_floor_caps_min_multiplier() -> None:
    """At extreme drawdown, multiplier floors at min_size_multiplier."""
    sub = _StubStrategy(_open("BUY", 100.0, 10.0, 100.0))
    s = DrawdownAwareSizingStrategy(
        sub,
        DrawdownAwareSizingConfig(drawdown_penalty=2.0, min_size_multiplier=0.25),
    )
    # Equity at HWM
    s.maybe_enter(_bar(0, h=100, low=99, c=99.5), [], 10_000.0, _config())
    # 80% drawdown — raw multiplier would be 1 - 2*0.8 = -0.6
    out = s.maybe_enter(_bar(1, h=100, low=99, c=99.5), [], 2_000.0, _config())
    assert out is not None
    # Floored at 0.25
    assert out.qty == pytest.approx(2.5)


def test_dd_sizing_doesnt_amplify_above_baseline() -> None:
    """No upward amplification — multiplier capped at 1.0 even at HWM."""
    sub = _StubStrategy(_open("BUY", 100.0, 10.0, 100.0))
    s = DrawdownAwareSizingStrategy(sub, DrawdownAwareSizingConfig())
    # Track equity rising from 10k to 12k (new HWM)
    s.maybe_enter(_bar(0, h=100, low=99, c=99.5), [], 10_000.0, _config())
    out = s.maybe_enter(_bar(1, h=100, low=99, c=99.5), [], 12_000.0, _config())
    assert out is not None
    # New HWM, multiplier = 1.0 → qty unchanged at 10
    assert out.qty == pytest.approx(10.0)
