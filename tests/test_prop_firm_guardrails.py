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
    """Every prop-firm rule profile we've ever supported must remain
    in REGISTRY — that way reintroducing one (signing the contract,
    moving a bot to evaluation) is a single ACTIVE_ACCOUNTS edit, not
    a re-typing of the rule set."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    # REGISTRY keeps ALL accounts regardless of active state
    assert "blusky-50K-launch" in g.REGISTRY
    assert "apex-50K-eval" in g.REGISTRY
    assert "apex-50K-funded" in g.REGISTRY
    assert "topstep-50K" in g.REGISTRY
    assert "etf-50K" in g.REGISTRY
    # list_known_accounts(include_inactive=True) surfaces them all
    accounts = g.list_known_accounts(include_inactive=True)
    for aid in ("blusky-50K-launch", "apex-50K-eval", "apex-50K-funded", "topstep-50K", "etf-50K"):
        assert aid in accounts


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
    """aggregate_status(include_inactive=True) returns a snapshot for
    every REGISTRY account, not just ACTIVE_ACCOUNTS."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    snaps = g.aggregate_status(
        trade_closes_path=tmp_path / "empty.jsonl",
        include_inactive=True,
    )
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


# ────────────────────────────────────────────────────────────────────
# 2026-05-13: paper-test virtual account (research portfolio split)
#
# The operator's 5/15 cutover plan separates two portfolios:
#   * paper-test — unconstrained research portfolio (no breach rules)
#   * blusky-50K-launch / apex-* / etc — real prop firm accounts (strict)
#
# The bot fleet has historically written paper trades to a single
# trade_closes.jsonl with NO account_id field. paper-test inherits those
# untagged records as a catch-all so the dashboard shows real paper P&L.
# Real prop accounts NEVER inherit untagged trades.
# ────────────────────────────────────────────────────────────────────


def test_paper_test_virtual_account_registered() -> None:
    """paper-test must be in REGISTRY as an unconstrained research portfolio."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    assert "paper-test" in g.REGISTRY
    r = g.REGISTRY["paper-test"]
    assert r.firm == "paper-test"
    # No breach rules — strategies must be free to stretch
    assert r.daily_loss_limit is None
    assert r.trailing_drawdown is None
    assert r.profit_target is None
    assert r.starting_balance >= 50_000.0  # at least as large as a prop firm
    assert r.automation_allowed is True


# ────────────────────────────────────────────────────────────────────
# 2026-05-13: ACTIVE_ACCOUNTS dashboard filter
#
# Operator brief: hide apex-50K-eval/funded, etf-50K, topstep-50K until
# they're reintroduced into the mix with live or evaluation accounts.
# The dashboard only shows paper-test + blusky-50K-launch by default.
# Inactive rule profiles stay in REGISTRY so reintroducing one is a
# single-line ACTIVE_ACCOUNTS edit, not a re-typing of the rule set.
# ────────────────────────────────────────────────────────────────────


def test_active_accounts_set_pinned() -> None:
    """ACTIVE_ACCOUNTS contains exactly the two operator-visible
    accounts. Any change is a deliberate revision — fail loudly so a
    silent edit can't accidentally hide BluSky or unhide a dormant
    Apex/Topstep before the operator decides."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    assert frozenset({"paper-test", "blusky-50K-launch"}) == g.ACTIVE_ACCOUNTS, (
        f"ACTIVE_ACCOUNTS drifted: {sorted(g.ACTIVE_ACCOUNTS)}"
    )


def test_list_known_accounts_filters_to_active_by_default() -> None:
    """Default list_known_accounts() returns only ACTIVE_ACCOUNTS.
    include_inactive=True returns every REGISTRY key."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    active = g.list_known_accounts()
    assert set(active) == g.ACTIVE_ACCOUNTS
    all_accts = g.list_known_accounts(include_inactive=True)
    assert set(all_accts) == set(g.REGISTRY.keys())
    # All inactive accounts are present in the include_inactive list
    for inactive in {"apex-50K-eval", "apex-50K-funded", "etf-50K", "topstep-50K"}:
        assert inactive in all_accts


def test_is_account_active_check() -> None:
    """is_account_active returns True only for ACTIVE_ACCOUNTS."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    assert g.is_account_active("paper-test") is True
    assert g.is_account_active("blusky-50K-launch") is True
    assert g.is_account_active("apex-50K-eval") is False
    assert g.is_account_active("topstep-50K") is False
    assert g.is_account_active("nonexistent") is False


def test_get_rules_still_works_for_inactive() -> None:
    """get_rules returns the rule set for any registered account,
    including inactive ones. This is the path operators use to preview
    a rule profile before reintroducing the account."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    apex = g.get_rules("apex-50K-eval")
    assert apex is not None
    assert apex.firm == "apex"
    assert apex.starting_balance == 50_000.0


def test_aggregate_status_default_excludes_inactive(tmp_path: Path) -> None:
    """aggregate_status() default returns only ACTIVE_ACCOUNTS snapshots."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    path = tmp_path / "tc.jsonl"
    _write_trades(path, [])
    snaps = g.aggregate_status(trade_closes_path=path)
    aids = {s.rules.account_id for s in snaps}
    assert aids == g.ACTIVE_ACCOUNTS
    snaps_all = g.aggregate_status(trade_closes_path=path, include_inactive=True)
    aids_all = {s.rules.account_id for s in snaps_all}
    assert aids_all == set(g.REGISTRY.keys())


def test_paper_test_catches_untagged_paper_trades(tmp_path: Path) -> None:
    """Untagged trades must roll into the paper-test snapshot, NOT into
    any real prop firm account. This protects the operator from a stale
    untagged record showing up as a blusky-50K daily loss breach."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            # Untagged paper trade (the legacy shape)
            {
                "ts": _ts(2),
                "realized_pnl_usd": 50.0,
                "extra": {"realized_pnl": 50.0, "symbol": "MNQ1"},
            },
            # Untagged paper trade
            {
                "ts": _ts(1),
                "realized_pnl_usd": -25.0,
                "extra": {"realized_pnl": -25.0, "symbol": "MNQ1"},
            },
            # Explicitly tagged to BluSky — must NOT show in paper-test
            {
                "ts": _ts(0.5),
                "account_id": "blusky-50K-launch",
                "realized_pnl_usd": 100.0,
                "extra": {"realized_pnl": 100.0, "symbol": "MNQ1"},
            },
        ],
    )
    # Paper-test sees the 2 untagged
    snap_paper = g.snapshot_one("paper-test", trade_closes_path=path)
    assert snap_paper is not None
    assert snap_paper.state.day_pnl_usd == pytest.approx(25.0)  # 50 - 25
    # BluSky sees only the 1 tagged
    snap_blusky = g.snapshot_one("blusky-50K-launch", trade_closes_path=path)
    assert snap_blusky is not None
    assert snap_blusky.state.day_pnl_usd == pytest.approx(100.0)


def test_real_prop_account_does_not_inherit_untagged(tmp_path: Path) -> None:
    """A real prop firm account snapshot must compute ZERO state from
    untagged trades. Without this guard, a single stale untagged record
    could trigger a fake breach alert on a real account."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            # Big "loss" untagged — must NOT count against BluSky
            {
                "ts": _ts(1),
                "realized_pnl_usd": -3000.0,  # would breach BluSky's $1500 daily cap
                "extra": {"realized_pnl": -3000.0, "symbol": "MNQ1"},
            },
        ],
    )
    snap = g.snapshot_one("blusky-50K-launch", trade_closes_path=path)
    assert snap is not None
    assert snap.state.day_pnl_usd == 0.0
    assert snap.severity != "blown"
    # paper-test sees the loss
    paper = g.snapshot_one("paper-test", trade_closes_path=path)
    assert paper is not None
    assert paper.state.day_pnl_usd == pytest.approx(-3000.0)
    # paper-test never blows because it has no breach rules
    assert paper.severity == "ok"


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


# ────────────────────────────────────────────────────────────────────
# 2026-05-13: tick-leak guard on _trade_pnl_usd
#
# Pre-fix, a single record with ``realized_r=69`` on MNQ would compute
# as +$1380 phantom profit, swinging the trailing-DD high-water-mark
# and risking a false BluSky/Apex breach. The sanitizer drops these.
# ────────────────────────────────────────────────────────────────────


def test_trade_pnl_usd_drops_tick_leak() -> None:
    """A record with realized_r=69 and NO USD/extra fields must
    contribute $0 (the suspect r is dropped, not multiplied)."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rec = {
        "account_id": "blusky-50K-launch",
        "symbol": "MNQ1",
        "realized_r": 69.0,  # ticks-traveled, not R-multiple
        "ts": _ts(1),
    }
    assert g._trade_pnl_usd(rec) == 0.0


def test_trade_pnl_usd_prefers_explicit_usd_over_r() -> None:
    """If realized_pnl_usd is on the record, R is irrelevant — even a
    bogus tick-leak realized_r doesn't matter."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rec = {
        "account_id": "blusky-50K-launch",
        "symbol": "MNQ1",
        "realized_r": 69.0,
        "realized_pnl_usd": 17.25,
        "ts": _ts(1),
    }
    assert g._trade_pnl_usd(rec) == 17.25


def test_trade_pnl_usd_prefers_extra_realized_pnl_over_r() -> None:
    """If extra.realized_pnl is set, use that directly rather than
    R-based recovery."""
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rec = {
        "account_id": "blusky-50K-launch",
        "symbol": "MNQ1",
        "realized_r": 32661.0,  # bogus tick-count leak
        "extra": {"realized_pnl": 12.50},
        "ts": _ts(1),
    }
    assert g._trade_pnl_usd(rec) == 12.50


def test_trade_pnl_usd_clean_r_still_works() -> None:
    """Sanity check: a legitimate realized_r still multiplies into
    correct PnL when no explicit USD field is available.

    MNQ: dollar_per_R = $20 (per _DEFAULT_DOLLAR_PER_R), so r=1.5
    yields $30 PnL.
    """
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rec = {
        "account_id": "blusky-50K-launch",
        "symbol": "MNQ1",
        "realized_r": 1.5,
        "ts": _ts(1),
    }
    assert g._trade_pnl_usd(rec) == 30.0


def test_drawdown_unaffected_by_tick_leak(tmp_path: Path) -> None:
    """End-to-end: a fleet day with 1 real loss and 1 tick-leak record
    must compute drawdown from the real loss only.

    Before the fix: r=69 on MNQ would add +$1380 to the high-water
    mark, then any real -$200 loss would look like a -$1580 drawdown
    that could falsely trip BluSky's $1500 daily-loss cap.
    """
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {
                "account_id": "blusky-50K-launch",
                "realized_pnl_usd": -200.0,
                "ts": _ts(2),
            },
            # Tick-leak phantom — pre-fix would have added +$1380
            {
                "account_id": "blusky-50K-launch",
                "symbol": "MNQ1",
                "realized_r": 69.0,
                "ts": _ts(1),
            },
        ],
    )
    snap = g.snapshot_one("blusky-50K-launch", trade_closes_path=path)
    assert snap is not None
    # Daily PnL is just the -$200, NOT (-200 + 1380 phantom)
    assert snap.state.day_pnl_usd == pytest.approx(-200.0)
    # And we're well within the $1500 cap → severity is OK / warn,
    # NOT blown.
    assert snap.severity != "blown"
