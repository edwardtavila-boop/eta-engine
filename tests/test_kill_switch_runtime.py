"""Tests for core.kill_switch_runtime."""

from __future__ import annotations

import yaml

from eta_engine.core.kill_switch_runtime import (
    ApexEvalSnapshot,
    BotSnapshot,
    CorrelationSnapshot,
    FundingSnapshot,
    KillAction,
    KillSeverity,
    KillSwitch,
    PortfolioSnapshot,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _cfg() -> dict:
    return {
        "global": {
            "daily_loss_cap_pct_of_portfolio": 6.0,
            "max_drawdown_kill_pct_of_portfolio": 20.0,
        },
        "tier_a": {
            "apex_eval_preemptive": {"cushion_usd": 500},
            "per_bucket": {
                "mnq": {"max_loss_usd": 500, "consecutive_losses": 3},
                "nq":  {"max_loss_usd": 1200, "consecutive_losses": 3},
            },
        },
        "tier_b": {
            "correlation_kill": {
                "enabled": True,
                "threshold_abs_corr": 0.85,
                "pairs_required": 4,
            },
            "funding_veto": {"soft_threshold_bps": 20, "hard_threshold_bps": 50},
            "per_bucket": {
                "eth_perp": {"max_loss_pct": 10.0, "consecutive_losses": 5},
                "sol_perp": {"max_loss_pct": 10.0, "consecutive_losses": 5},
            },
        },
    }


def _portfolio(total=7000, peak=7000, pnl=0.0):
    return PortfolioSnapshot(
        total_equity_usd=total, peak_equity_usd=peak,
        daily_realized_pnl_usd=pnl,
    )


# --------------------------------------------------------------------------- #
# Global-trip precedence
# --------------------------------------------------------------------------- #
def test_global_dd_cap_triggers_flatten_all():
    ks = KillSwitch(_cfg())
    # 25% DD >= 20% cap
    p = _portfolio(total=7500, peak=10000)
    v = ks.evaluate(bots=[], portfolio=p)
    assert len(v) == 1
    assert v[0].action is KillAction.FLATTEN_ALL
    assert v[0].severity is KillSeverity.CRITICAL
    assert v[0].scope == "global"


def test_global_daily_loss_cap_triggers_flatten_all():
    ks = KillSwitch(_cfg())
    # 7% daily loss vs 6% cap on 10k peak
    p = _portfolio(total=10000, peak=10000, pnl=-700.0)
    v = ks.evaluate(bots=[], portfolio=p)
    assert v[0].action is KillAction.FLATTEN_ALL
    assert "daily loss" in v[0].reason.lower()


def test_global_trip_supersedes_other_verdicts():
    """If global trips, we must not emit per-bot verdicts too."""
    ks = KillSwitch(_cfg())
    p = _portfolio(total=7500, peak=10000)  # DD 25% > 20% cap
    bot = BotSnapshot(name="mnq", tier="A", equity_usd=1000, peak_equity_usd=5000,
                      session_realized_pnl_usd=-4000)  # also tripped per-bucket
    v = ks.evaluate(bots=[bot], portfolio=p)
    assert len(v) == 1, "global trip should short-circuit"
    assert v[0].action is KillAction.FLATTEN_ALL


# --------------------------------------------------------------------------- #
# Apex preempt
# --------------------------------------------------------------------------- #
def test_apex_preempt_fires_when_cushion_below_threshold():
    ks = KillSwitch(_cfg())
    p = _portfolio()
    ae = ApexEvalSnapshot(trailing_dd_limit_usd=2500, distance_to_limit_usd=400)
    v = ks.evaluate(bots=[], portfolio=p, apex_eval=ae)
    assert any(x.action is KillAction.FLATTEN_TIER_A_PREEMPTIVE for x in v)


def test_apex_preempt_silent_when_cushion_above_threshold():
    ks = KillSwitch(_cfg())
    p = _portfolio()
    ae = ApexEvalSnapshot(trailing_dd_limit_usd=2500, distance_to_limit_usd=2000)
    v = ks.evaluate(bots=[], portfolio=p, apex_eval=ae)
    assert not any(x.action is KillAction.FLATTEN_TIER_A_PREEMPTIVE for x in v)


# --------------------------------------------------------------------------- #
# Correlation kill (Tier-B only)
# --------------------------------------------------------------------------- #
def test_correlation_kill_fires_on_n_pairs_above_threshold():
    ks = KillSwitch(_cfg())
    p = _portfolio()
    c = CorrelationSnapshot(
        pair_abs_corr={
            "BTC-ETH": 0.91, "BTC-SOL": 0.88, "ETH-SOL": 0.90, "SOL-XRP": 0.86,
        }
    )
    v = ks.evaluate(bots=[], portfolio=p, correlations=c)
    hits = [x for x in v if x.action is KillAction.FLATTEN_TIER_B]
    assert len(hits) == 1
    assert hits[0].scope == "tier_b"


def test_correlation_kill_silent_when_below_required_pairs():
    ks = KillSwitch(_cfg())
    p = _portfolio()
    c = CorrelationSnapshot(pair_abs_corr={"BTC-ETH": 0.9, "BTC-SOL": 0.9})  # only 2
    v = ks.evaluate(bots=[], portfolio=p, correlations=c)
    assert not any(x.action is KillAction.FLATTEN_TIER_B for x in v)


def test_correlation_kill_respects_enabled_flag():
    cfg = _cfg()
    cfg["tier_b"]["correlation_kill"]["enabled"] = False
    ks = KillSwitch(cfg)
    c = CorrelationSnapshot(
        pair_abs_corr={f"X-{i}": 0.99 for i in range(10)}  # would otherwise fire
    )
    v = ks.evaluate(bots=[], portfolio=_portfolio(), correlations=c)
    assert not any(x.action is KillAction.FLATTEN_TIER_B for x in v)


# --------------------------------------------------------------------------- #
# Funding veto
# --------------------------------------------------------------------------- #
def test_funding_hard_threshold_flattens_bot():
    ks = KillSwitch(_cfg())
    f = FundingSnapshot(symbol_to_bps={"ETHUSDT": 60.0})
    v = ks.evaluate(bots=[], portfolio=_portfolio(), funding=f)
    hits = [x for x in v if x.action is KillAction.FLATTEN_BOT]
    assert len(hits) == 1
    assert "ETHUSDT" in hits[0].reason


def test_funding_soft_threshold_halves_size():
    ks = KillSwitch(_cfg())
    f = FundingSnapshot(symbol_to_bps={"SOLUSDT": 25.0})
    v = ks.evaluate(bots=[], portfolio=_portfolio(), funding=f)
    hits = [x for x in v if x.action is KillAction.HALVE_SIZE]
    assert len(hits) == 1


def test_funding_below_soft_is_silent():
    ks = KillSwitch(_cfg())
    f = FundingSnapshot(symbol_to_bps={"SOLUSDT": 5.0})
    v = ks.evaluate(bots=[], portfolio=_portfolio(), funding=f)
    assert all(x.action is KillAction.CONTINUE for x in v)


# --------------------------------------------------------------------------- #
# Per-bucket bot trip-wires
# --------------------------------------------------------------------------- #
def test_tier_a_max_loss_usd_trips():
    ks = KillSwitch(_cfg())
    bot = BotSnapshot(
        name="mnq", tier="A",
        equity_usd=4500, peak_equity_usd=5000,
        session_realized_pnl_usd=-500.01,
    )
    v = ks.evaluate(bots=[bot], portfolio=_portfolio())
    hits = [x for x in v if x.action is KillAction.FLATTEN_BOT]
    assert hits and hits[0].scope == "bot:mnq"


def test_tier_b_max_loss_pct_trips():
    ks = KillSwitch(_cfg())
    bot = BotSnapshot(
        name="eth_perp", tier="B",
        equity_usd=900, peak_equity_usd=1000,  # 10% loss
        session_realized_pnl_usd=-100,
    )
    v = ks.evaluate(bots=[bot], portfolio=_portfolio())
    hits = [x for x in v if x.action is KillAction.FLATTEN_BOT and x.scope == "bot:eth_perp"]
    assert hits


def test_consecutive_loss_trip_fires_on_either_tier():
    ks = KillSwitch(_cfg())
    bot = BotSnapshot(
        name="mnq", tier="A",
        equity_usd=5000, peak_equity_usd=5000,
        session_realized_pnl_usd=-10, consecutive_losses=3,
    )
    v = ks.evaluate(bots=[bot], portfolio=_portfolio())
    hits = [x for x in v if x.action is KillAction.FLATTEN_BOT]
    assert hits
    assert any("consecutive" in h.reason.lower() for h in hits)


def test_no_trip_returns_single_continue():
    ks = KillSwitch(_cfg())
    bot = BotSnapshot(
        name="mnq", tier="A",
        equity_usd=5000, peak_equity_usd=5000,
    )
    v = ks.evaluate(bots=[bot], portfolio=_portfolio())
    assert len(v) == 1
    assert v[0].action is KillAction.CONTINUE


# --------------------------------------------------------------------------- #
# YAML ingestion
# --------------------------------------------------------------------------- #
def test_from_yaml_parses_real_config(tmp_path):
    p = tmp_path / "ks.yaml"
    p.write_text(yaml.safe_dump(_cfg()), encoding="utf-8")
    ks = KillSwitch.from_yaml(p)
    bot = BotSnapshot(name="mnq", tier="A", equity_usd=5000, peak_equity_usd=5000)
    v = ks.evaluate(bots=[bot], portfolio=_portfolio())
    assert v[0].action is KillAction.CONTINUE


def test_from_yaml_tolerates_empty_file(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    ks = KillSwitch.from_yaml(p)
    # No rules → everything passes.
    v = ks.evaluate(bots=[], portfolio=_portfolio())
    assert v and v[0].action is KillAction.CONTINUE
