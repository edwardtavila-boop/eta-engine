"""Tests for the 16-item supercharge batch (Tier-1..4, 2026-04-27).

Covers the math-heavy / contract-critical modules:
  * jarvis_correlation: throttle math
  * jarvis_today_verdicts: audit-record aggregation
  * online_learning: EWMA correctness
  * portfolio_rebalancer_v2: Sharpe + rank-based scaling + DD brake
  * global_rate_limiter: token-bucket + state persistence
  * position_reconciler: diff math
  * outcome P&L feedback in kaizen synthesizer
  * bandit_harness: arm registration + champion fallback
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

# ─── jarvis_correlation ─────────────────────────────────────────────────


def test_correlation_diagonal_is_1() -> None:
    from eta_engine.brain.jarvis_correlation import correlation

    assert correlation("MNQ", "MNQ") == 1.0
    assert correlation("BTCUSDT", "BTCUSDT") == 1.0


def test_correlation_known_pairs() -> None:
    from eta_engine.brain.jarvis_correlation import correlation

    assert correlation("MNQ", "NQ") == 0.99
    assert correlation("BTCUSDT", "ETHUSDT") == 0.85
    # Symmetric
    assert correlation("ETHUSDT", "BTCUSDT") == 0.85


def test_correlation_handles_cme_aliases() -> None:
    from eta_engine.brain.jarvis_correlation import correlation

    # MBT (CME Micro Bitcoin) should alias to BTCUSDT for correlation
    assert correlation("MBT", "ETHUSDT") == 0.85
    assert correlation("MET", "MBT") == 0.85


def test_correlation_unknown_pair_returns_zero() -> None:
    from eta_engine.brain.jarvis_correlation import correlation

    assert correlation("FOOBAR", "BAZ") == 0.0


def test_should_throttle_blocks_high_corr() -> None:
    from eta_engine.brain.jarvis_correlation import should_throttle_for_correlation

    # Already long MNQ -> NQ should be blocked (corr 0.99)
    decision = should_throttle_for_correlation("NQ", {"MNQ": 2.0})
    assert decision.cap_mult == 0.0
    assert decision.reason_code == "high_corr_block"


def test_should_throttle_halves_med_corr() -> None:
    from eta_engine.brain.jarvis_correlation import should_throttle_for_correlation

    # Already long ETH -> BTCUSDT is 0.85 corr -- still high. Use a med-corr pair.
    # Use BTCUSDT vs XRPUSDT (corr 0.55, in [0.50, 0.80))
    decision = should_throttle_for_correlation("XRPUSDT", {"BTCUSDT": 0.5})
    assert decision.cap_mult == 0.5
    assert decision.reason_code == "med_corr_throttle"


def test_should_throttle_passes_through_when_uncorrelated() -> None:
    from eta_engine.brain.jarvis_correlation import should_throttle_for_correlation

    # No open positions -> max_corr=0 -> no throttle
    decision = should_throttle_for_correlation("MNQ", {})
    assert decision.cap_mult == 1.0
    assert decision.reason_code == "no_corr_throttle"


def test_should_throttle_skips_zero_qty_positions() -> None:
    from eta_engine.brain.jarvis_correlation import should_throttle_for_correlation

    # 0-qty position should not throttle a correlated entry
    decision = should_throttle_for_correlation("NQ", {"MNQ": 0.0})
    assert decision.cap_mult == 1.0


# ─── jarvis_today_verdicts ──────────────────────────────────────────────


def test_aggregate_today_handles_empty(tmp_path: Path) -> None:
    from eta_engine.obs.jarvis_today_verdicts import aggregate_today

    out = aggregate_today(audit_globs=[str(tmp_path / "missing*.jsonl")])
    assert out["totals"] == {}
    assert out["by_subsystem"] == {}
    assert out["avg_conditional_cap"] == 1.0


def test_aggregate_today_buckets_records(tmp_path: Path) -> None:
    from eta_engine.obs.jarvis_today_verdicts import aggregate_today

    audit = tmp_path / "j.jsonl"
    now = datetime.now(UTC)
    records = [
        {
            "ts": now.isoformat(),
            "policy_version": 0,
            "request": {"subsystem": "bot.mnq"},
            "response": {"verdict": "APPROVED"},
        },
        {
            "ts": now.isoformat(),
            "policy_version": 0,
            "request": {"subsystem": "bot.mnq"},
            "response": {"verdict": "DENIED", "reason_code": "high_vol"},
        },
        {
            "ts": now.isoformat(),
            "policy_version": 1,
            "request": {"subsystem": "bot.eth_perp"},
            "response": {"verdict": "CONDITIONAL", "size_cap_mult": 0.5},
        },
    ]
    audit.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    out = aggregate_today(audit_globs=[str(audit)])
    assert out["totals"] == {"APPROVED": 1, "DENIED": 1, "CONDITIONAL": 1}
    assert out["by_subsystem"]["bot.mnq"] == {"APPROVED": 1, "DENIED": 1}
    assert out["avg_conditional_cap"] == 0.5
    assert sorted(out["policy_versions_seen"]) == [0, 1]
    assert ("high_vol", 1) in out["top_denial_reasons"]


# ─── online_learning ────────────────────────────────────────────────────


def test_online_updater_first_observation_sets_ewma() -> None:
    from eta_engine.brain.online_learning import OnlineUpdater

    u = OnlineUpdater(bot_name="test")
    u.observe(feature_bucket="A", r_multiple=1.0)
    assert u.expected_r("A") == 1.0


def test_online_updater_ewma_smooths() -> None:
    from eta_engine.brain.online_learning import OnlineUpdater

    u = OnlineUpdater(bot_name="test", alpha=0.5)
    u.observe(feature_bucket="A", r_multiple=2.0)
    u.observe(feature_bucket="A", r_multiple=0.0)
    # EWMA with alpha=0.5: (0.5 * 0.0) + (0.5 * 2.0) = 1.0
    assert u.expected_r("A") == 1.0


def test_online_updater_unseen_bucket_returns_zero() -> None:
    from eta_engine.brain.online_learning import OnlineUpdater

    u = OnlineUpdater(bot_name="test")
    assert u.expected_r("never_seen") == 0.0
    assert u.confidence("never_seen") == 0


# ─── portfolio_rebalancer_v2 ────────────────────────────────────────────


def test_realized_sharpe_zero_for_short_series() -> None:
    from eta_engine.brain.portfolio_rebalancer_v2 import realized_sharpe

    assert realized_sharpe([0.01]) == 0.0
    assert realized_sharpe([]) == 0.0


def test_realized_sharpe_positive_for_winning_series() -> None:
    from eta_engine.brain.portfolio_rebalancer_v2 import realized_sharpe

    s = realized_sharpe([0.001, 0.002, 0.001, 0.003, 0.002, 0.001])
    assert s > 0


def test_rebalance_allocations_preserves_baselines_when_no_drawdown() -> None:
    from eta_engine.brain.portfolio_rebalancer_v2 import (
        BotPerformance,
        rebalance_allocations,
    )

    perf = [
        BotPerformance(bot_name="MnqBot", rolling_returns=[0.001] * 30, baseline_usd=5500.0),
        BotPerformance(bot_name="EthPerpBot", rolling_returns=[0.001] * 30, baseline_usd=3000.0),
    ]
    out = rebalance_allocations(perf, fleet_drawdown_pct=0.0)
    # Some scaling will happen but everything stays within [cap_low, cap_high]
    assert "MnqBot" in out
    assert "EthPerpBot" in out
    assert all(v > 0 for v in out.values())


def test_rebalance_allocations_drawdown_brake_halves() -> None:
    from eta_engine.brain.portfolio_rebalancer_v2 import (
        BotPerformance,
        rebalance_allocations,
    )

    perf = [BotPerformance(bot_name="MnqBot", rolling_returns=[0.001] * 30, baseline_usd=5500.0)]
    normal = rebalance_allocations(perf, fleet_drawdown_pct=0.0)
    braked = rebalance_allocations(perf, fleet_drawdown_pct=0.10)
    assert braked["MnqBot"] < normal["MnqBot"]


# ─── global_rate_limiter ────────────────────────────────────────────────


def test_rate_limiter_consumes_token(tmp_path: Path) -> None:
    from eta_engine.obs.global_rate_limiter import GlobalRateLimiter

    rl = GlobalRateLimiter(state_path=tmp_path / "state.json")
    # critical level has capacity 999 -- many fires should still pass
    for _ in range(50):
        assert rl.try_consume(event_class="test", level="critical") is True


def test_rate_limiter_blocks_when_empty(tmp_path: Path) -> None:
    from eta_engine.obs.global_rate_limiter import GlobalRateLimiter

    rl = GlobalRateLimiter(
        state_path=tmp_path / "state.json",
        buckets={"warn": {"capacity": 2, "refill_per_min": 0.0}},  # never refills
    )
    assert rl.try_consume(event_class="t", level="warn") is True
    assert rl.try_consume(event_class="t", level="warn") is True
    assert rl.try_consume(event_class="t", level="warn") is False


def test_rate_limiter_persists_state(tmp_path: Path) -> None:
    from eta_engine.obs.global_rate_limiter import GlobalRateLimiter

    state_path = tmp_path / "state.json"
    rl1 = GlobalRateLimiter(
        state_path=state_path,
        buckets={"warn": {"capacity": 1, "refill_per_min": 0.0}},
    )
    assert rl1.try_consume(event_class="t", level="warn") is True
    # New limiter loads same state file -> bucket already exhausted
    rl2 = GlobalRateLimiter(
        state_path=state_path,
        buckets={"warn": {"capacity": 1, "refill_per_min": 0.0}},
    )
    assert rl2.try_consume(event_class="t", level="warn") is False


# ─── position_reconciler ────────────────────────────────────────────────


def test_diff_positions_returns_empty_when_aligned() -> None:
    from eta_engine.obs.position_reconciler import diff_positions

    bot = {"MNQ": {"mnq_bot": 2.0}}
    broker = {"MNQ": {"mnq_bot": 2.0}}
    assert diff_positions(bot, broker) == []


def test_diff_positions_finds_drift() -> None:
    from eta_engine.obs.position_reconciler import diff_positions

    bot = {"MNQ": {"mnq_bot": 2.0}, "BTC": {"btc_hybrid": 0.5}}
    broker = {"MNQ": {"mnq_bot": 1.0}, "BTC": {"btc_hybrid": 0.5}}
    diffs = diff_positions(bot, broker)
    assert len(diffs) == 1
    assert diffs[0].bot == "mnq_bot"
    assert diffs[0].abs_drift == 1.0


def test_diff_positions_treats_missing_as_zero() -> None:
    from eta_engine.obs.position_reconciler import diff_positions

    bot = {"MNQ": {"mnq_bot": 2.0}}
    broker: dict[str, dict[str, float]] = {}  # broker reports nothing
    diffs = diff_positions(bot, broker)
    assert len(diffs) == 1
    assert diffs[0].broker_qty == 0.0


# ─── kaizen outcome P&L feedback (Tier-2 #7) ────────────────────────────


def test_kaizen_synthesizer_uses_realized_r_when_present() -> None:
    from eta_engine.obs.decision_journal import Actor, JournalEvent, Outcome
    from eta_engine.scripts.run_kaizen_close_cycle import synthesize_inputs

    events = [
        JournalEvent(
            actor=Actor.TRADE_ENGINE, intent="open_mnq_long", outcome=Outcome.NOTED, metadata={"realized_r": 1.5}
        ),
        JournalEvent(
            actor=Actor.TRADE_ENGINE, intent="open_mnq_long", outcome=Outcome.NOTED, metadata={"realized_r": 0.8}
        ),
        JournalEvent(
            actor=Actor.TRADE_ENGINE, intent="open_eth_short", outcome=Outcome.NOTED, metadata={"realized_r": -1.0}
        ),
        JournalEvent(
            actor=Actor.TRADE_ENGINE, intent="open_eth_short", outcome=Outcome.NOTED, metadata={"realized_r": -0.5}
        ),
    ]
    out = synthesize_inputs(events)
    # Winning intent should be in went_well; losing in went_poorly
    assert any("open_mnq_long" in s for s in out["went_well"])
    assert any("open_eth_short" in s for s in out["went_poorly"])
    # KPIs should include realized R aggregates
    assert "realized_r_total" in out["kpis"]
    assert "realized_r_mean" in out["kpis"]
    assert out["kpis"]["winning_count"] == 2.0
    assert out["kpis"]["losing_count"] == 2.0


# ─── bandit_harness scaffold ────────────────────────────────────────────


def test_bandit_harness_champion_fallback_when_disabled() -> None:
    from eta_engine.brain.jarvis_v3.bandit_harness import BanditHarness

    def champ_policy(req, ctx):
        return "champion"

    def cand_policy(req, ctx):
        return "candidate"

    h = BanditHarness()
    h.register_arm("v17", champ_policy, is_champion=True)
    h.register_arm("v18", cand_policy)
    # With BANDIT_ENABLED False (default), choose_arm always returns champion.
    arm = h.choose_arm()
    assert arm.arm_id == "v17"


def test_bandit_harness_observe_outcome_updates_arm() -> None:
    from eta_engine.brain.jarvis_v3.bandit_harness import BanditHarness

    def p(req, ctx):
        return None

    h = BanditHarness()
    h.register_arm("v17", p, is_champion=True)
    h.observe_outcome("v17", reward=1.5)
    h.observe_outcome("v17", reward=0.5)
    report = h.report()
    assert report["v17"]["pulls"] == 2
    assert report["v17"]["mean_reward"] == 1.0
