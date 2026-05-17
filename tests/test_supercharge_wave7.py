"""Tests for wave-7 (audit-driven supercharge, 2026-04-27).

Covers the math + scaffolds shipped to close the audit gaps:
  * Monte Carlo stress test
  * Slippage tracker
  * Macro calendar
  * Sentiment + on-chain enrichers (file roundtrip)
  * Order flow + volume profile math
  * Performance metrics + DSR
  * Latency tracker
  * Correlation regime detector
  * Pyramid planner
  * Basis tracker
  * Market impact estimator
  * Operator override
  * Filter bandit
  * RL trading env smoke
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

# ─── Monte Carlo stress ─────────────────────────────────────────────


def test_monte_carlo_runs_with_synthetic_distribution() -> None:
    from eta_engine.scripts.monte_carlo_stress import (
        run_stress,
        synthetic_r_distribution,
    )

    samples = synthetic_r_distribution(n=200)
    report = run_stress(samples, paths=200, n_trades=30)
    assert report.paths == 200
    assert report.realized_r_sample_size == 200
    assert report.p05_max_dd_usd >= 0
    assert report.p95_max_dd_usd >= report.p50_max_dd_usd >= report.p25_max_dd_usd >= report.p05_max_dd_usd


def test_monte_carlo_blowup_pct_in_valid_range() -> None:
    from eta_engine.scripts.monte_carlo_stress import (
        run_stress,
        synthetic_r_distribution,
    )

    samples = synthetic_r_distribution(win_rate=0.10, avg_winner_r=0.5, avg_loser_r=-1.5, n=100)
    report = run_stress(samples, paths=100, n_trades=50)
    assert 0 <= report.pct_paths_blown_up <= 100
    # Highly negative-EV distribution -> should blow up frequently
    assert report.pct_paths_blown_up > 10  # at least 10% should blow at this configuration


def test_monte_carlo_load_returns_empty_when_no_journal(tmp_path: Path) -> None:
    from eta_engine.scripts.monte_carlo_stress import load_realized_r_samples

    samples = load_realized_r_samples(tmp_path / "missing.jsonl")
    assert samples == []


# ─── Slippage tracker ───────────────────────────────────────────────


def test_slippage_tracker_resolves_buy_side(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.obs import slippage_tracker as st

    monkeypatch.setattr(st, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(st, "PENDING_PATH", tmp_path / "pending.json")

    st.record_expected(order_id="o1", symbol="MNQ", side="buy", expected_price=21450.0, ts=1000.0)
    event = st.record_realized(order_id="o1", realized_price=21451.5, ts=1000.5)
    assert event is not None
    assert event.slippage_abs == 1.5  # buy paid 1.5 more
    assert event.slippage_bps > 0
    assert event.latency_ms == 500.0


def test_slippage_tracker_resolves_sell_side(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.obs import slippage_tracker as st

    monkeypatch.setattr(st, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(st, "PENDING_PATH", tmp_path / "pending.json")

    st.record_expected(order_id="o2", symbol="MNQ", side="sell", expected_price=21500.0, ts=2000.0)
    event = st.record_realized(order_id="o2", realized_price=21498.5, ts=2000.2)
    assert event is not None
    # SELL got LESS than expected -> slippage is positive (worse for trader)
    assert event.slippage_abs == 1.5


def test_slippage_unknown_order_id_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.obs import slippage_tracker as st

    monkeypatch.setattr(st, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(st, "PENDING_PATH", tmp_path / "pending.json")
    out = st.record_realized(order_id="never-recorded", realized_price=100.0, ts=1.0)
    assert out is None


# ─── Macro calendar ────────────────────────────────────────────────


def test_macro_calendar_detects_fomc_window() -> None:
    from eta_engine.brain.jarvis_v3.macro_calendar import (
        MacroEvent,
        MacroEventKind,
        is_within_event_window,
    )

    fomc = MacroEvent(MacroEventKind.FOMC, datetime(2026, 6, 17, 18, 0, tzinfo=UTC), "FOMC Jun", "high")
    # 15 min before -> in window
    when = datetime(2026, 6, 17, 17, 45, tzinfo=UTC)
    e = is_within_event_window(when, events=[fomc])
    assert e is not None
    assert e.kind == MacroEventKind.FOMC


def test_macro_calendar_outside_window_returns_none() -> None:
    from eta_engine.brain.jarvis_v3.macro_calendar import (
        MacroEvent,
        MacroEventKind,
        is_within_event_window,
    )

    fomc = MacroEvent(MacroEventKind.FOMC, datetime(2026, 6, 17, 18, 0, tzinfo=UTC), "FOMC Jun", "high")
    when = datetime(2026, 6, 17, 14, 0, tzinfo=UTC)  # 4h before
    e = is_within_event_window(when, events=[fomc])
    assert e is None


def test_macro_calendar_presser_uses_wider_window() -> None:
    """FOMC_PRESSER default window is 60 min (vs 30 for FOMC release)."""
    from eta_engine.brain.jarvis_v3.macro_calendar import (
        MacroEvent,
        MacroEventKind,
        is_within_event_window,
    )

    presser = MacroEvent(MacroEventKind.FOMC_PRESSER, datetime(2026, 6, 17, 18, 30, tzinfo=UTC), "Powell Jun", "high")
    # 50 min before -> in window for PRESSER (window=60)
    when = datetime(2026, 6, 17, 17, 40, tzinfo=UTC)
    assert is_within_event_window(when, events=[presser]) is not None


# ─── Sentiment + on-chain file roundtrip ───────────────────────────


def test_sentiment_snapshot_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import sentiment_score as ss

    monkeypatch.setattr(ss, "SENTIMENT_DIR", tmp_path)
    snap = ss.SentimentSnapshot(
        symbol="MNQ",
        ts=datetime.now(UTC),
        composite=0.5,
        news_score=0.6,
        social_score=0.4,
        volume_z=1.5,
        n_news_articles=20,
        n_social_mentions=500,
    )
    ss.write_snapshot(snap)
    out = ss.current_snapshot("MNQ")
    assert out is not None
    assert out.composite == 0.5
    assert out.is_stale is False


def test_sentiment_confluence_modifier_aligns() -> None:
    from eta_engine.brain.jarvis_v3.sentiment_score import (
        SentimentSnapshot,
        confluence_modifier,
    )

    snap = SentimentSnapshot(
        symbol="MNQ",
        ts=datetime.now(UTC),
        composite=0.6,
        news_score=0.6,
        social_score=0.6,
        volume_z=1.0,
        n_news_articles=10,
        n_social_mentions=100,
    )
    # Long bias + bullish sentiment -> positive modifier
    mod = confluence_modifier(snap, direction="long", weight=1.0)
    assert mod > 0
    # Short bias + bullish sentiment -> negative modifier
    mod = confluence_modifier(snap, direction="short", weight=1.0)
    assert mod < 0


def test_sentiment_confluence_returns_zero_for_stale_or_none() -> None:
    from eta_engine.brain.jarvis_v3.sentiment_score import confluence_modifier

    assert confluence_modifier(None, direction="long") == 0.0


def test_onchain_snapshot_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import onchain_enricher as oc

    monkeypatch.setattr(oc, "ONCHAIN_DIR", tmp_path)
    snap = oc.OnchainSnapshot(
        symbol="BTCUSDT",
        ts=datetime.now(UTC),
        funding_rate_8h=-0.001,
        open_interest_usd=15_000_000_000.0,
        net_exchange_flow_usd=-50_000_000.0,
        whale_tx_count_24h=80,
        btc_dominance_pct=53.0,
        realized_vol_30d=0.55,
    )
    oc.write_snapshot(snap)
    out = oc.current_snapshot("BTCUSDT")
    assert out is not None
    assert out.funding_rate_8h == -0.001


def test_onchain_confluence_signals_negative_funding_bullish_for_long() -> None:
    from eta_engine.brain.jarvis_v3.onchain_enricher import (
        OnchainSnapshot,
        confluence_signal,
    )

    snap = OnchainSnapshot(
        symbol="BTCUSDT",
        ts=datetime.now(UTC),
        funding_rate_8h=-0.0015,
        open_interest_usd=None,
        net_exchange_flow_usd=None,
        whale_tx_count_24h=None,
        btc_dominance_pct=None,
        realized_vol_30d=None,
    )
    signals = confluence_signal(snap, direction="long")
    assert "funding" in signals
    assert signals["funding"] > 0


# ─── Order flow + volume profile ───────────────────────────────────


def test_compute_flow_series_basic() -> None:
    from eta_engine.core.order_flow import FlowBar, compute_flow_series

    bars = [
        FlowBar(ts_ms=1, open=100, high=101, low=99, close=100.5, buy_volume=100, sell_volume=80),
        FlowBar(ts_ms=2, open=100.5, high=102, low=100, close=101.5, buy_volume=150, sell_volume=70),
        FlowBar(ts_ms=3, open=101.5, high=103, low=101, close=102.5, buy_volume=120, sell_volume=90),
    ]
    series = compute_flow_series(bars)
    assert series.cumulative_delta == [20, 100, 130]
    assert len(series.absorption) == 3
    assert len(series.divergences) == 3


def test_cumulative_delta_alignment_long_with_rising_cd() -> None:
    from eta_engine.core.order_flow import FlowBar, compute_flow_series, cumulative_delta_alignment

    bars = [
        FlowBar(ts_ms=i, open=100, high=101, low=99, close=100.5, buy_volume=100 + i * 5, sell_volume=80)
        for i in range(15)
    ]
    series = compute_flow_series(bars)
    score = cumulative_delta_alignment(series, direction="long")
    assert score > 0  # rising delta + long direction = positive alignment


def test_volume_profile_finds_poc() -> None:
    from eta_engine.core.volume_profile import compute_profile

    buckets = {100.0: 50, 101.0: 200, 102.0: 80, 103.0: 30, 104.0: 10}
    profile = compute_profile(buckets)
    assert profile.poc == 101.0
    assert profile.val <= 101.0 <= profile.vah
    assert profile.total_volume == 370


def test_volume_profile_handles_empty() -> None:
    from eta_engine.core.volume_profile import compute_profile

    profile = compute_profile({})
    assert profile.poc == 0.0
    assert profile.total_volume == 0.0


def test_position_relative_to_value_area() -> None:
    from eta_engine.core.volume_profile import compute_profile, position_relative_to_value_area

    buckets = {100.0: 50, 101.0: 200, 102.0: 80}
    profile = compute_profile(buckets)
    assert position_relative_to_value_area(99.0, profile) == "below_val"
    assert position_relative_to_value_area(101.0, profile) == "in_value"
    assert position_relative_to_value_area(105.0, profile) == "above_vah"


# ─── Performance metrics + DSR ─────────────────────────────────────


def test_perf_metrics_winning_distribution() -> None:
    from eta_engine.obs.performance_metrics import compute_metrics

    rs = [1.5, -1.0, 2.0, -1.0, 1.0, 1.5, -1.0, 2.5, 1.0, -0.5] * 5
    m = compute_metrics(r_multiples=rs)
    assert m.n_trades == 50
    assert 0 < m.win_rate < 1
    assert m.expectancy_r > 0
    assert m.sharpe > 0


def test_perf_metrics_handles_empty() -> None:
    from eta_engine.obs.performance_metrics import compute_metrics

    m = compute_metrics(r_multiples=[])
    assert m.n_trades == 0
    assert m.sharpe == 0.0


def test_psr_higher_for_consistent_winner() -> None:
    from eta_engine.obs.performance_metrics import probabilistic_sharpe_ratio

    consistent = [0.5, 0.4, 0.6, 0.5, 0.5, 0.4, 0.5, 0.6, 0.5, 0.5] * 5
    erratic = [3.0, -2.5, 4.0, -3.0, 3.5, -2.0, 0.0, 0.0, 1.0, -1.0] * 5
    psr_consistent = probabilistic_sharpe_ratio(consistent, target_sharpe=0.0)
    psr_erratic = probabilistic_sharpe_ratio(erratic, target_sharpe=0.0)
    # Consistent series should yield much higher PSR
    assert psr_consistent > psr_erratic


# ─── Latency tracker ───────────────────────────────────────────────


def test_latency_timer_records_deltas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.obs import latency_tracker as lt

    monkeypatch.setattr(lt, "EVENTS_PATH", tmp_path / "events.jsonl")
    timer = lt.LatencyTimer(signal_id="sig-test")
    timer.mark("signal_emitted")
    timer.mark("jarvis_verdict")
    timer.mark("order_submitted")
    path = timer.finalize()
    assert path.exists()
    # Deltas should exist (in some order)
    assert len(timer.event.deltas_ms) >= 2
    assert timer.event.total_ms >= 0


def test_latency_daily_summary_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.obs import latency_tracker as lt

    monkeypatch.setattr(lt, "EVENTS_PATH", tmp_path / "missing.jsonl")
    summary = lt.daily_summary()
    assert summary["n"] == 0


# ─── Correlation regime detector ───────────────────────────────────


def test_corr_regime_detects_material_shift() -> None:
    from eta_engine.brain.jarvis_v3.corr_regime_detector import detect_shifts

    baseline = {"MNQ|NQ": 0.99, "BTCUSDT|ETHUSDT": 0.85}
    rolling = {"MNQ|NQ": 0.99, "BTCUSDT|ETHUSDT": 0.50}  # crypto decoupling
    shifts = detect_shifts(rolling, baseline)
    assert len(shifts) == 1
    assert shifts[0].pair == "BTCUSDT|ETHUSDT"
    assert shifts[0].severity in ("material", "extreme")


def test_corr_regime_no_shift_when_stable() -> None:
    from eta_engine.brain.jarvis_v3.corr_regime_detector import detect_shifts

    baseline = {"MNQ|NQ": 0.99}
    rolling = {"MNQ|NQ": 0.97}
    assert detect_shifts(rolling, baseline) == []


def test_corr_regime_load_baseline_falls_back_to_legacy_correlation_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from eta_engine.brain.jarvis_v3 import corr_regime_detector as crd

    canonical = tmp_path / "var" / "eta_engine" / "state" / "correlation"
    legacy = tmp_path / "eta_engine" / "state" / "correlation"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "learned.json").write_text(
        '{"pairs":{"MNQ|NQ":0.88,"BTCUSDT|ETHUSDT":0.64}}',
        encoding="utf-8",
    )

    monkeypatch.setattr(crd.workspace_roots, "ETA_CORRELATION_ARTIFACT_DIR", canonical)
    monkeypatch.setattr(crd.workspace_roots, "ETA_LEGACY_CORRELATION_ARTIFACT_DIR", legacy)

    assert crd.load_baseline() == {
        "MNQ|NQ": 0.88,
        "BTCUSDT|ETHUSDT": 0.64,
    }


def test_corr_regime_write_shift_report_uses_canonical_runtime_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from eta_engine.brain.jarvis_v3 import corr_regime_detector as crd

    out_dir = tmp_path / "var" / "eta_engine" / "state" / "correlation_regime"
    monkeypatch.setattr(crd.workspace_roots, "ETA_CORRELATION_REGIME_DIR", out_dir)

    path = crd.write_shift_report(
        [
            crd.CorrRegimeShift(
                pair="BTCUSDT|ETHUSDT",
                baseline=0.85,
                rolling=0.52,
                delta=-0.33,
                severity="extreme",
            )
        ]
    )

    assert path.parent == out_dir
    assert path.exists()
    assert "BTCUSDT|ETHUSDT" in path.read_text(encoding="utf-8")


# ─── Pyramid planner ───────────────────────────────────────────────


def test_pyramid_blocked_when_max_adds_reached() -> None:
    from eta_engine.brain.pyramid_planner import (
        PyramidPlan,
        PyramidState,
        can_add_now,
    )

    plan = PyramidPlan(max_adds=2)
    state = PyramidState(adds_so_far=2)
    decision = can_add_now(plan=plan, state=state, current_price=100.0, direction="long")
    assert decision.allowed is False
    assert decision.reason_code == "max_adds_reached"


def test_pyramid_blocked_when_too_soon() -> None:
    from eta_engine.brain.pyramid_planner import (
        PyramidPlan,
        PyramidState,
        can_add_now,
    )

    plan = PyramidPlan(min_minutes_between=20)
    state = PyramidState(
        adds_so_far=1,
        last_add_ts=datetime.now(UTC) - timedelta(minutes=5),
        last_add_price=100.0,
        initial_entry_price=100.0,
        initial_stop_distance_r=1.0,
    )
    decision = can_add_now(plan=plan, state=state, current_price=102.0, direction="long")
    assert decision.allowed is False
    assert decision.reason_code == "too_soon"


def test_pyramid_allowed_with_sufficient_progress() -> None:
    from eta_engine.brain.pyramid_planner import (
        PyramidPlan,
        PyramidState,
        can_add_now,
    )

    plan = PyramidPlan(max_adds=3, min_minutes_between=10, min_progress_r=1.0)
    state = PyramidState(
        adds_so_far=1,
        last_add_ts=datetime.now(UTC) - timedelta(minutes=30),
        last_add_price=100.0,
        initial_entry_price=99.0,
        initial_stop_distance_r=1.0,
    )
    decision = can_add_now(plan=plan, state=state, current_price=101.5, direction="long")
    assert decision.allowed is True


# ─── Basis tracker ─────────────────────────────────────────────────


def test_basis_regime_label_classifies_correctly() -> None:
    from eta_engine.obs.basis_tracker import BasisSnapshot, regime_label

    backwardation = BasisSnapshot(
        symbol="MBT",
        ts=datetime.now(UTC),
        spot_price=95000.0,
        futures_price=94800.0,
        basis_pct=-0.0021,
        days_to_expiry=30,
        annualized_basis=-0.025,
    )
    assert regime_label(backwardation) == "BACKWARDATION"

    normal = BasisSnapshot(
        symbol="MBT",
        ts=datetime.now(UTC),
        spot_price=95000.0,
        futures_price=95200.0,
        basis_pct=0.0021,
        days_to_expiry=30,
        annualized_basis=0.025,
    )
    assert regime_label(normal) == "NORMAL"

    steep = BasisSnapshot(
        symbol="MBT",
        ts=datetime.now(UTC),
        spot_price=95000.0,
        futures_price=96500.0,
        basis_pct=0.0158,
        days_to_expiry=30,
        annualized_basis=0.20,
    )
    assert regime_label(steep) == "STEEP_CONTANGO"


# ─── Market impact ─────────────────────────────────────────────────


def test_market_impact_increases_with_size() -> None:
    from eta_engine.core.market_impact import estimate_impact_bps

    bps_small = estimate_impact_bps(symbol="MBT", qty=1)
    bps_large = estimate_impact_bps(symbol="MBT", qty=100)
    assert bps_large > bps_small


def test_market_impact_zero_for_unknown_symbol() -> None:
    from eta_engine.core.market_impact import estimate_impact_bps

    assert estimate_impact_bps(symbol="WAT", qty=10) == 0.0


def test_is_size_too_aggressive_classifies() -> None:
    from eta_engine.core.market_impact import is_size_too_aggressive

    # Small size on liquid product -> not aggressive
    assert is_size_too_aggressive(symbol="MNQ", qty=2) is False
    # Big size on thin product (XRP, ADV=3000): qty=1000 gives ~15.5 bps,
    # well above the 10 bps default threshold.
    assert is_size_too_aggressive(symbol="XRP", qty=1000) is True
    # Borderline case: lower threshold flips a smaller qty into "aggressive"
    assert is_size_too_aggressive(symbol="XRP", qty=200, threshold_bps=5.0) is True


# ─── Operator override ─────────────────────────────────────────────


def test_operator_override_default_is_normal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.obs import operator_override as oo

    monkeypatch.setattr(oo, "OVERRIDE_PATH", tmp_path / "override.json")
    state = oo.get_state()
    assert state.level == oo.OverrideLevel.NORMAL


def test_operator_override_set_and_get(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.obs import operator_override as oo

    monkeypatch.setattr(oo, "OVERRIDE_PATH", tmp_path / "override.json")
    oo.set_state(oo.OverrideLevel.SOFT_PAUSE, reason="test pause")
    state = oo.get_state()
    assert state.level == oo.OverrideLevel.SOFT_PAUSE
    assert state.reason == "test pause"


def test_operator_override_is_paused_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.obs import operator_override as oo

    monkeypatch.setattr(oo, "OVERRIDE_PATH", tmp_path / "override.json")
    assert oo.is_paused() is False
    oo.set_state(oo.OverrideLevel.SOFT_PAUSE, reason="x")
    assert oo.is_paused() is True
    assert oo.is_paused(hard_only=True) is False
    oo.set_state(oo.OverrideLevel.HARD_PAUSE, reason="x")
    assert oo.is_paused(hard_only=True) is True


# ─── Filter bandit ─────────────────────────────────────────────────


def test_filter_bandit_chooses_best_arm_after_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3.filter_bandit import FilterBandit

    fb = FilterBandit(epsilon=0.0, state_path=tmp_path / "post.json")  # 0% explore
    fb.register("good", lambda **_: True)
    fb.register("bad", lambda **_: True)
    # Feed positive rewards to "good", negative to "bad"
    for _ in range(20):
        fb.observe_outcome("good", 1.5)
        fb.observe_outcome("bad", -1.0)
    # Greedy choice -> good
    _, used = fb.choose_filter_check()
    assert used == "good"


def test_filter_bandit_persists_across_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3.filter_bandit import FilterBandit

    state_path = tmp_path / "post.json"
    fb1 = FilterBandit(state_path=state_path)
    fb1.register("arm1", lambda **_: True)
    fb1.observe_outcome("arm1", 1.0)
    fb2 = FilterBandit(state_path=state_path)
    report = fb2.report()
    arm = next((a for a in report if a["arm"] == "arm1"), None)
    assert arm is not None
    assert arm["pulls"] == 1


# ─── RL env smoke ──────────────────────────────────────────────────


def test_rl_env_quick_smoke_runs() -> None:
    from eta_engine.brain.jarvis_v3.rl_env import quick_smoke

    out = quick_smoke()
    # Should produce valid numbers, no crash
    assert "total_reward" in out
    assert "final_equity" in out
    assert isinstance(out["final_equity"], (int, float))


def test_rl_env_step_returns_correct_shape() -> None:
    from eta_engine.brain.jarvis_v3.rl_env import (
        EtaTradingEnv,
        EtaTradingEnvSpec,
        RLAction,
    )

    env = EtaTradingEnv(EtaTradingEnvSpec(max_steps=5))
    env.reset()
    obs, reward, done, info = env.step(RLAction.APPROVED_FULL)
    assert isinstance(obs, list)
    assert len(obs) == 17
    assert isinstance(reward, float)
    assert isinstance(done, bool)
    assert "equity" in info
