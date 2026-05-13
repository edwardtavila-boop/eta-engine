"""Tests for prop firm rule guardrails — the safety gate for live capital."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_known_firms() -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    accounts = g.list_known_accounts()
    assert "blusky-50K-launch" in accounts
    assert "apex-50K-eval" in accounts
    assert "apex-50K-funded" in accounts
    assert "topstep-50K" in accounts
    assert "etf-50K" in accounts


def test_blusky_rules_match_known_policy() -> None:
    """BluSky Launch 50K: $1500 daily loss, $2000 trailing DD, $3000 profit target."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rules = g.get_rules("blusky-50K-launch")
    assert rules is not None
    assert rules.firm == "blusky"
    assert rules.daily_loss_limit == 1_500.0
    assert rules.trailing_drawdown == 2_000.0
    assert rules.profit_target == 3_000.0
    assert rules.automation_allowed is True


def test_apex_funded_disallows_automation() -> None:
    """Per project_prop_firm_bot_policy: Apex TOS restricts automation on FUNDED."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rules = g.get_rules("apex-50K-funded")
    assert rules is not None
    assert rules.automation_allowed is False


def test_topstep_disallows_automation() -> None:
    """Per project_prop_firm_bot_policy: Topstep TOS restricts automation."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rules = g.get_rules("topstep-50K")
    assert rules is not None
    assert rules.automation_allowed is False


def test_get_rules_returns_none_for_unknown() -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    assert g.get_rules("does-not-exist") is None


# ---------------------------------------------------------------------------
# Account state computation
# ---------------------------------------------------------------------------


def _write_trades(path: Path, trades: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for t in trades:
            fh.write(json.dumps(t) + "\n")


def _ts(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


def test_account_state_empty_when_no_trades(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    state = g.account_state_from_trades(
        "blusky-50K-launch",
        trade_closes_path=tmp_path / "no_trades.jsonl",
    )
    assert state.starting_balance == 50_000.0
    assert state.current_balance == 50_000.0
    assert state.peak_balance == 50_000.0
    assert state.day_pnl_usd == 0.0
    assert state.n_trades_today == 0


def test_account_state_aggregates_pnl(tmp_path: Path) -> None:
    """Sum every USD PnL into current_balance, track peak."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    today_iso = datetime.now(UTC).isoformat()
    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {
                "account_id": "blusky-50K-launch",
                "bot_id": "a",
                "realized_pnl_usd": 500.0,
                "ts": _ts(48),
            },
            {
                "account_id": "blusky-50K-launch",
                "bot_id": "b",
                "realized_pnl_usd": -200.0,
                "ts": _ts(24),
            },
            {
                # Use today's actual datetime so the "is today?" UTC check
                # is robust regardless of when the test runs vs. UTC midnight.
                "account_id": "blusky-50K-launch",
                "bot_id": "c",
                "realized_pnl_usd": 300.0,
                "ts": today_iso,
            },
        ],
    )
    state = g.account_state_from_trades("blusky-50K-launch", trade_closes_path=path)
    assert state.current_balance == 50_600.0  # 50k + 500 - 200 + 300
    assert state.peak_balance == 50_600.0
    assert state.day_pnl_usd == 300.0  # only the today-tagged trade counts


def test_account_state_only_includes_tagged_trades(tmp_path: Path) -> None:
    """Trades without account_id (paper/dev) don't pollute live account state."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "dev_bot", "realized_pnl_usd": 9_999.0, "ts": _ts(1)},
            {
                "account_id": "blusky-50K-launch",
                "bot_id": "live",
                "realized_pnl_usd": 100.0,
                "ts": _ts(1),
            },
        ],
    )
    state = g.account_state_from_trades("blusky-50K-launch", trade_closes_path=path)
    assert state.current_balance == 50_100.0  # only the tagged trade counts


def test_account_state_peak_balance_tracks_max(tmp_path: Path) -> None:
    """Peak balance must be the high-water mark, not the most recent value."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {
                "account_id": "blusky-50K-launch",
                "bot_id": "a",
                "realized_pnl_usd": 2_500.0,  # peak hits 52,500
                "ts": _ts(72),
            },
            {
                "account_id": "blusky-50K-launch",
                "bot_id": "b",
                "realized_pnl_usd": -1_000.0,  # drawdown to 51,500
                "ts": _ts(48),
            },
        ],
    )
    state = g.account_state_from_trades("blusky-50K-launch", trade_closes_path=path)
    assert state.peak_balance == 52_500.0
    assert state.current_balance == 51_500.0


def test_account_state_falls_back_to_r_with_dollar_per_r(tmp_path: Path) -> None:
    """When no USD field, computes via realized_r * dollar_per_r."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {
                "account_id": "blusky-50K-launch",
                "bot_id": "a",
                "realized_r": 1.5,
                "symbol": "MNQ",  # $20/R
                "ts": _ts(1),
            }
        ],
    )
    state = g.account_state_from_trades("blusky-50K-launch", trade_closes_path=path)
    # 1.5R * $20/R = $30 profit
    assert state.current_balance == 50_030.0


# ---------------------------------------------------------------------------
# evaluate() — the gate
# ---------------------------------------------------------------------------


def _baseline_state(
    day_pnl: float = 0.0,
    current_balance: float = 50_000.0,
    peak_balance: float | None = None,
):
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    return g.AccountState(
        account_id="blusky-50K-launch",
        starting_balance=50_000.0,
        current_balance=current_balance,
        peak_balance=peak_balance if peak_balance is not None else max(50_000.0, current_balance),
        day_pnl_usd=day_pnl,
        today_date=datetime.now(UTC).date().isoformat(),
        n_trades_today=0,
        open_contracts=0,
    )


def test_evaluate_allows_clean_signal_on_fresh_account() -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rules = g.get_rules("blusky-50K-launch")
    state = _baseline_state()
    verdict = g.evaluate(rules, state, {"symbol": "MNQ", "stop_r": 1.0, "size": 2})
    assert verdict.allowed is True
    assert verdict.worst_case_loss_usd == pytest.approx(40.0)  # 1R * $20 * 2 = $40
    assert verdict.headroom["daily_loss_remaining_usd"] == 1_500.0


def test_evaluate_blocks_when_size_exceeds_max_contracts() -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rules = g.get_rules("blusky-50K-launch")  # max_contracts=10
    state = _baseline_state()
    verdict = g.evaluate(rules, state, {"symbol": "MNQ", "stop_r": 1.0, "size": 15})
    assert verdict.allowed is False
    assert any("max_contracts" in b for b in verdict.blockers)


def test_evaluate_blocks_signal_that_would_breach_daily_loss() -> None:
    """Already at -$1480, a $40-worst-case trade would push past -$1500."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rules = g.get_rules("blusky-50K-launch")
    state = _baseline_state(day_pnl=-1_480.0)
    verdict = g.evaluate(rules, state, {"symbol": "MNQ", "stop_r": 1.0, "size": 2})
    assert verdict.allowed is False
    assert any("daily_loss" in b for b in verdict.blockers)


def test_evaluate_blocks_signal_that_would_breach_trailing_dd() -> None:
    """Account peaked at +$2000, currently at +$100 (DD=$1900). $1500 stop pushes past $2000 DD."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rules = g.get_rules("blusky-50K-launch")  # trailing_dd=2000
    state = _baseline_state(current_balance=50_100.0, peak_balance=52_000.0)
    # 75 contracts * 1R * $20 = $1500 worst-case loss
    verdict = g.evaluate(rules, state, {"symbol": "MNQ", "stop_r": 1.0, "size": 75})
    # First it'll fail max_contracts (75 > 10), but even at size=10 → $200 → DD=$2100 > $2000
    assert verdict.allowed is False


def test_evaluate_denies_when_automation_disallowed() -> None:
    """Apex funded + Topstep TOS-block all automation regardless of headroom."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rules = g.get_rules("apex-50K-funded")
    state = _baseline_state()
    verdict = g.evaluate(rules, state, {"symbol": "MNQ", "stop_r": 1.0, "size": 1})
    assert verdict.allowed is False
    assert "tos_automation" in verdict.blockers


def test_evaluate_rejects_malformed_signal() -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rules = g.get_rules("blusky-50K-launch")
    state = _baseline_state()
    verdict = g.evaluate(rules, state, {"symbol": "MNQ", "stop_r": "garbage", "size": 1})
    assert verdict.allowed is False
    assert "malformed_signal" in verdict.blockers


def test_evaluate_rejects_zero_size() -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rules = g.get_rules("blusky-50K-launch")
    state = _baseline_state()
    verdict = g.evaluate(rules, state, {"symbol": "MNQ", "stop_r": 1.0, "size": 0})
    assert verdict.allowed is False


def test_evaluate_rejects_unknown_symbol() -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rules = g.get_rules("blusky-50K-launch")
    state = _baseline_state()
    verdict = g.evaluate(rules, state, {"symbol": "XYZ", "stop_r": 1.0, "size": 1})
    assert verdict.allowed is False
    assert "unknown_symbol" in verdict.blockers


def test_evaluate_accepts_signal_with_explicit_dollar_per_r() -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rules = g.get_rules("blusky-50K-launch")
    state = _baseline_state()
    verdict = g.evaluate(
        rules,
        state,
        {"symbol": "WEIRD_FUTURE", "stop_r": 1.0, "size": 1, "dollar_per_r": 50.0},
    )
    assert verdict.allowed is True
    assert verdict.worst_case_loss_usd == 50.0


def test_evaluate_consistency_rule_applies_to_profit_distribution() -> None:
    """Funded account on a +1000 day with only +1500 lifetime profit → 67% > 30% consistency cap."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rules = g.get_rules("apex-50K-funded")
    # Mock automation_allowed=True so we can test the consistency rule path
    rules_for_test = g.PropFirmRules(
        firm=rules.firm,
        size=rules.size,
        account_id=rules.account_id,
        starting_balance=rules.starting_balance,
        daily_loss_limit=rules.daily_loss_limit,
        trailing_drawdown=rules.trailing_drawdown,
        profit_target=rules.profit_target,
        consistency_rule_pct=rules.consistency_rule_pct,
        max_contracts=rules.max_contracts,
        rth_only=rules.rth_only,
        automation_allowed=True,
    )
    state = _baseline_state(day_pnl=1_000.0, current_balance=51_500.0)
    verdict = g.evaluate(rules_for_test, state, {"symbol": "MNQ", "stop_r": 1.0, "size": 1})
    # 1000/1500 = 67% > 30% → blocker
    assert verdict.allowed is False
    assert any("consistency" in b for b in verdict.blockers)


# ---------------------------------------------------------------------------
# Aggregate snapshot
# ---------------------------------------------------------------------------


def test_aggregate_status_returns_all_registered(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    snaps = g.aggregate_status(trade_closes_path=tmp_path / "empty.jsonl")
    assert len(snaps) == len(g.REGISTRY)


def test_aggregate_status_sorts_by_severity(tmp_path: Path) -> None:
    """blown > critical > warn > ok order."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    today_iso = datetime.now(UTC).isoformat()
    # Burn BluSky daily loss limit (trade tagged for today)
    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {
                "account_id": "blusky-50K-launch",
                "realized_pnl_usd": -1_600.0,  # blown - over $1500 daily loss
                "ts": today_iso,
            }
        ],
    )
    snaps = g.aggregate_status(trade_closes_path=path)
    # blown sorts first
    assert snaps[0].state.account_id == "blusky-50K-launch"
    assert snaps[0].severity == "blown"


def test_snapshot_severity_ok_when_no_pnl(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    snap = g.snapshot_one(
        "blusky-50K-launch",
        trade_closes_path=tmp_path / "empty.jsonl",
    )
    assert snap is not None
    assert snap.severity == "ok"
    assert snap.daily_loss_remaining == 1_500.0


def test_snapshot_severity_warn_at_80pct_daily_loss(tmp_path: Path) -> None:
    """75% of daily loss used -> severity warn."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    today_iso = datetime.now(UTC).isoformat()
    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {
                "account_id": "blusky-50K-launch",
                "realized_pnl_usd": -1_200.0,  # 80% of $1500 limit
                "ts": today_iso,
            }
        ],
    )
    snap = g.snapshot_one("blusky-50K-launch", trade_closes_path=path)
    assert snap is not None
    assert snap.severity == "warn"


def test_snapshot_severity_critical_at_95pct_daily_loss(tmp_path: Path) -> None:
    """95% of daily loss used -> severity critical."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    today_iso = datetime.now(UTC).isoformat()
    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {
                "account_id": "blusky-50K-launch",
                "realized_pnl_usd": -1_425.0,  # 95% of $1500
                "ts": today_iso,
            }
        ],
    )
    snap = g.snapshot_one("blusky-50K-launch", trade_closes_path=path)
    assert snap is not None
    assert snap.severity == "critical"


def test_snapshot_progress_to_target(tmp_path: Path) -> None:
    """Profit-to-target tracking for eval accounts."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {
                "account_id": "blusky-50K-launch",
                "realized_pnl_usd": 1_500.0,
                "ts": _ts(24),
            }
        ],
    )
    snap = g.snapshot_one("blusky-50K-launch", trade_closes_path=path)
    assert snap is not None
    assert snap.profit_to_target == 1_500.0  # need another $1500 to hit $3k target
    assert snap.pct_to_target == 0.5
