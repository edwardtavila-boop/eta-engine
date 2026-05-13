"""Tests for the wave-20 prop drawdown + consistency guard."""

# ruff: noqa: N802, PLR2004
from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _trade(bot_id: str, pnl: float, days_ago: int = 0, idx: int = 0, base_dt: datetime | None = None) -> dict:
    """Build a synthetic trade-close record."""
    if base_dt is None:
        base_dt = datetime.now(UTC)
    ts = (base_dt - timedelta(days=days_ago)).isoformat()
    return {
        "bot_id": bot_id,
        "signal_id": f"{bot_id}_{idx}",
        "ts": ts,
        "realized_r": 0.5,
        "extra": {"realized_pnl": pnl, "side": "BUY"},
    }


# ────────────────────────────────────────────────────────────────────
# OK signal
# ────────────────────────────────────────────────────────────────────


def test_OK_when_no_prop_ready_bots() -> None:
    """No PROP_READY = no live exposure = OK + 'guard idle'."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    receipt = dg.compute_guard(
        trades=[],
        prop_ready_bots=[],
        account_size=50_000.0,
    )
    assert receipt.signal == "OK"
    assert "guard idle" in receipt.rationale


def test_OK_when_pnl_well_within_all_limits() -> None:
    """Healthy day, healthy total, balanced consistency -> OK."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    # 10 trades over 5 days, each +$50 → total +$500, daily +$100, well-distributed
    trades = []
    for d in range(5):
        for i in range(2):
            trades.append(_trade("m2k_sweep_reclaim", pnl=50.0, days_ago=d, idx=d * 10 + i))
    receipt = dg.compute_guard(
        trades=trades,
        prop_ready_bots=["m2k_sweep_reclaim"],
        account_size=50_000.0,
    )
    assert receipt.signal == "OK"
    assert receipt.daily_dd_check.status == "OK"
    assert receipt.static_dd_check.status == "OK"


# ────────────────────────────────────────────────────────────────────
# HALT signals
# ────────────────────────────────────────────────────────────────────


def test_HALT_when_daily_dd_breached() -> None:
    """Today's loss exceeds 3% daily DD ($1500 on $50K) -> HALT."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    # Today: lose $1600 (over $1500 limit)
    trades = [_trade("m2k_sweep_reclaim", pnl=-1600.0, days_ago=0, idx=0)]
    receipt = dg.compute_guard(
        trades=trades,
        prop_ready_bots=["m2k_sweep_reclaim"],
        account_size=50_000.0,
    )
    assert receipt.signal == "HALT"
    assert receipt.daily_dd_check.status == "HALT"
    assert "BREACHED" in receipt.daily_dd_check.rationale


def test_HALT_when_static_dd_breached() -> None:
    """Cumulative loss exceeds 5% static DD ($2500) -> HALT."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    # Spread the loss across multiple days so daily DD doesn't trip
    trades = [
        _trade("m2k_sweep_reclaim", pnl=-300.0, days_ago=d, idx=d)
        for d in range(10)  # 10 × -$300 = -$3000 total
    ]
    receipt = dg.compute_guard(
        trades=trades,
        prop_ready_bots=["m2k_sweep_reclaim"],
        account_size=50_000.0,
    )
    assert receipt.signal == "HALT"
    assert receipt.static_dd_check.status == "HALT"


def test_HALT_when_consistency_rule_breached() -> None:
    """Single-day profit > 30% of total profit -> HALT (eval would fail)."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    # 1 day with +$1000, 4 days with +$50 each = $1200 total
    # Best day = $1000 / $1200 = 83.3% (> 30% limit)
    trades = [_trade("m2k_sweep_reclaim", pnl=1000.0, days_ago=0, idx=0)]
    for d in range(1, 5):
        trades.append(_trade("m2k_sweep_reclaim", pnl=50.0, days_ago=d, idx=d))
    receipt = dg.compute_guard(
        trades=trades,
        prop_ready_bots=["m2k_sweep_reclaim"],
        account_size=50_000.0,
    )
    assert receipt.signal == "HALT"
    assert receipt.consistency_check.status == "HALT"


# ────────────────────────────────────────────────────────────────────
# WATCH signals
# ────────────────────────────────────────────────────────────────────


def test_WATCH_when_daily_dd_buffer_under_25pct() -> None:
    """Daily DD buffer < 25% of limit triggers WATCH (not yet HALT)."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    # Lose $1200 today (limit $1500); buffer $300 = 20% of limit < 25%
    trades = [_trade("m2k_sweep_reclaim", pnl=-1200.0, days_ago=0, idx=0)]
    receipt = dg.compute_guard(
        trades=trades,
        prop_ready_bots=["m2k_sweep_reclaim"],
        account_size=50_000.0,
    )
    assert receipt.signal == "WATCH"
    assert receipt.daily_dd_check.status == "WATCH"


def test_WATCH_when_consistency_approaching_threshold() -> None:
    """Consistency ratio just under 30% but > 22.5% (75% to limit) -> WATCH."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    # Best day +$280, total +$1000 → 28% ratio (just below 30% limit,
    # well above 75% utilization → WATCH band)
    trades = [_trade("m2k_sweep_reclaim", pnl=280.0, days_ago=0, idx=0)]
    for d in range(1, 5):
        trades.append(_trade("m2k_sweep_reclaim", pnl=180.0, days_ago=d, idx=d))
    receipt = dg.compute_guard(
        trades=trades,
        prop_ready_bots=["m2k_sweep_reclaim"],
        account_size=50_000.0,
    )
    assert receipt.signal == "WATCH"
    assert receipt.consistency_check.status == "WATCH"


# ────────────────────────────────────────────────────────────────────
# Worst-of-many semantics
# ────────────────────────────────────────────────────────────────────


def test_HALT_dominates_WATCH_signal() -> None:
    """If one rule is HALT and another is WATCH, master signal is HALT."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    # Today: -$1600 (daily HALT) AND consistency: doesn't matter
    trades = [_trade("m2k_sweep_reclaim", pnl=-1600.0, days_ago=0, idx=0)]
    receipt = dg.compute_guard(
        trades=trades,
        prop_ready_bots=["m2k_sweep_reclaim"],
        account_size=50_000.0,
    )
    assert receipt.signal == "HALT"


# ────────────────────────────────────────────────────────────────────
# Quarantined records are excluded
# ────────────────────────────────────────────────────────────────────


def test_quarantined_records_excluded_from_pnl() -> None:
    """diamond_data_sanitizer-quarantined records (poison PnL) must not
    affect the prop guard's drawdown calculations.

    Use a 5-day balanced PnL distribution so the consistency rule
    doesn't independently trip — we want to verify ONLY the
    quarantine exclusion path."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    # Poison: -$99,999 on day 0 (quarantined, must be excluded)
    poison = _trade("m2k_sweep_reclaim", pnl=-99_999.0, days_ago=0, idx=0)
    poison["_sanitizer_quarantined"] = True
    # 5 clean days of +$20 each (total +$100, max-day = 20% of total)
    trades = [poison] + [_trade("m2k_sweep_reclaim", pnl=20.0, days_ago=d, idx=10 + d) for d in range(5)]
    receipt = dg.compute_guard(
        trades=trades,
        prop_ready_bots=["m2k_sweep_reclaim"],
        account_size=50_000.0,
    )
    # Poison excluded → total = $100 (not -$99,899), no rule trips → OK
    assert receipt.signal == "OK"
    assert receipt.total_pnl_usd == 100.0
    assert receipt.daily_pnl_usd == 20.0  # only today's clean +$20


# ────────────────────────────────────────────────────────────────────
# Configurable thresholds
# ────────────────────────────────────────────────────────────────────


def test_thresholds_are_configurable() -> None:
    """Operator can pass custom DD limits for different prop firms."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    trades = [_trade("m2k_sweep_reclaim", pnl=-2000.0, days_ago=0, idx=0)]
    # With default 3% daily DD on $50K → $1500 limit → HALT
    r1 = dg.compute_guard(
        trades=trades,
        prop_ready_bots=["m2k_sweep_reclaim"],
        account_size=50_000.0,
    )
    assert r1.signal == "HALT"
    # With looser 5% daily DD → $2500 limit → still WATCH (buf $500/2500=20%)
    r2 = dg.compute_guard(
        trades=trades,
        prop_ready_bots=["m2k_sweep_reclaim"],
        account_size=50_000.0,
        daily_dd_pct=0.05,
    )
    assert r2.signal == "WATCH"
