"""Tests for core.kill_switch_runtime."""

from __future__ import annotations

import pytest
import yaml

from eta_engine.core.kill_switch_runtime import (
    ApexEvalSnapshot,
    ApexTickCadenceError,
    BotSnapshot,
    CorrelationSnapshot,
    FundingSnapshot,
    KillAction,
    KillSeverity,
    KillSwitch,
    PortfolioSnapshot,
    validate_apex_tick_cadence,
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
                "nq": {"max_loss_usd": 1200, "consecutive_losses": 3},
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
        total_equity_usd=total,
        peak_equity_usd=peak,
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
    bot = BotSnapshot(
        name="mnq", tier="A", equity_usd=1000, peak_equity_usd=5000, session_realized_pnl_usd=-4000
    )  # also tripped per-bucket
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
            "BTC-ETH": 0.91,
            "BTC-SOL": 0.88,
            "ETH-SOL": 0.90,
            "SOL-XRP": 0.86,
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
        name="mnq",
        tier="A",
        equity_usd=4500,
        peak_equity_usd=5000,
        session_realized_pnl_usd=-500.01,
    )
    v = ks.evaluate(bots=[bot], portfolio=_portfolio())
    hits = [x for x in v if x.action is KillAction.FLATTEN_BOT]
    assert hits and hits[0].scope == "bot:mnq"


def test_tier_b_max_loss_pct_trips():
    ks = KillSwitch(_cfg())
    bot = BotSnapshot(
        name="eth_perp",
        tier="B",
        equity_usd=900,
        peak_equity_usd=1000,  # 10% loss
        session_realized_pnl_usd=-100,
    )
    v = ks.evaluate(bots=[bot], portfolio=_portfolio())
    hits = [x for x in v if x.action is KillAction.FLATTEN_BOT and x.scope == "bot:eth_perp"]
    assert hits


def test_consecutive_loss_trip_fires_on_either_tier():
    ks = KillSwitch(_cfg())
    bot = BotSnapshot(
        name="mnq",
        tier="A",
        equity_usd=5000,
        peak_equity_usd=5000,
        session_realized_pnl_usd=-10,
        consecutive_losses=3,
    )
    v = ks.evaluate(bots=[bot], portfolio=_portfolio())
    hits = [x for x in v if x.action is KillAction.FLATTEN_BOT]
    assert hits
    assert any("consecutive" in h.reason.lower() for h in hits)


def test_no_trip_returns_single_continue():
    ks = KillSwitch(_cfg())
    bot = BotSnapshot(
        name="mnq",
        tier="A",
        equity_usd=5000,
        peak_equity_usd=5000,
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


# --------------------------------------------------------------------------- #
# R2 closure: validate_apex_tick_cadence
# --------------------------------------------------------------------------- #
# Enforces tick_interval_s * max_usd_move_per_sec * safety_factor <= cushion_usd
# in live mode. Non-live runs are exempt.


class TestValidateApexTickCadence:
    def test_noop_when_not_live(self):
        # Clearly-unsafe cadence but live=False -> silent pass.
        validate_apex_tick_cadence(
            tick_interval_s=60.0,
            cushion_usd=100.0,
            max_usd_move_per_sec=300.0,
            live=False,
        )

    def test_safe_cadence_live_passes(self):
        # tick=1s * move=300 * safety=2 = 600, cushion=1000 -> OK
        validate_apex_tick_cadence(
            tick_interval_s=1.0,
            cushion_usd=1000.0,
            max_usd_move_per_sec=300.0,
            safety_factor=2.0,
            live=True,
        )

    def test_unsafe_cadence_live_raises(self):
        # tick=5s * move=300 * safety=2 = 3000, cushion=500 -> FAIL
        with pytest.raises(ApexTickCadenceError, match="tick cadence too slow"):
            validate_apex_tick_cadence(
                tick_interval_s=5.0,
                cushion_usd=500.0,
                max_usd_move_per_sec=300.0,
                safety_factor=2.0,
                live=True,
            )

    def test_error_message_mentions_remediation(self):
        with pytest.raises(ApexTickCadenceError) as exc_info:
            validate_apex_tick_cadence(
                tick_interval_s=5.0,
                cushion_usd=500.0,
                live=True,
            )
        msg = str(exc_info.value)
        assert "tick_interval_s" in msg
        assert "cushion_usd" in msg
        # operator needs actionable guidance in the traceback
        assert "configs/kill_switch.yaml" in msg

    def test_exact_boundary_passes(self):
        # tick=1s * move=250 * safety=2 = 500 == cushion -> pass (<=)
        validate_apex_tick_cadence(
            tick_interval_s=1.0,
            cushion_usd=500.0,
            max_usd_move_per_sec=250.0,
            safety_factor=2.0,
            live=True,
        )

    def test_exact_boundary_plus_one_fails(self):
        # tick=1s * move=250.01 * safety=2 = 500.02 > cushion=500 -> fail
        with pytest.raises(ApexTickCadenceError):
            validate_apex_tick_cadence(
                tick_interval_s=1.0,
                cushion_usd=500.0,
                max_usd_move_per_sec=250.01,
                safety_factor=2.0,
                live=True,
            )

    def test_zero_tick_interval_raises_value_error(self):
        with pytest.raises(ValueError, match="tick_interval_s"):
            validate_apex_tick_cadence(
                tick_interval_s=0.0,
                cushion_usd=500.0,
                live=True,
            )

    def test_negative_cushion_raises_value_error(self):
        with pytest.raises(ValueError, match="cushion_usd"):
            validate_apex_tick_cadence(
                tick_interval_s=1.0,
                cushion_usd=-1.0,
                live=True,
            )

    def test_zero_max_move_raises_value_error(self):
        with pytest.raises(ValueError, match="max_usd_move_per_sec"):
            validate_apex_tick_cadence(
                tick_interval_s=1.0,
                cushion_usd=500.0,
                max_usd_move_per_sec=0.0,
                live=True,
            )

    def test_negative_safety_factor_raises_value_error(self):
        with pytest.raises(ValueError, match="safety_factor"):
            validate_apex_tick_cadence(
                tick_interval_s=1.0,
                cushion_usd=500.0,
                safety_factor=-1.0,
                live=True,
            )

    def test_default_cushion_matches_typical_apex_config(self):
        """sanity: our canonical kill_switch.yaml cushion=$500 should be safe
        at the new default tick_interval_s=1.0 with worst-case 250/sec.
        If this ever fails, the canonical config needs a re-tune."""
        validate_apex_tick_cadence(
            tick_interval_s=1.0,
            cushion_usd=500.0,
            max_usd_move_per_sec=250.0,  # slightly conservative vs the 300 default
            safety_factor=1.0,
            live=True,
        )

    def test_preemptive_cushion_zero_in_paper_still_safe(self):
        """_cfg_factory test fixture uses cushion_usd=0 with live=False.
        That should not raise because the validator no-ops when live=False."""
        validate_apex_tick_cadence(
            tick_interval_s=0.0001,
            cushion_usd=0.0001,
            live=False,
        )
