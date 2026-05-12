"""Tests for the wave-7 dual-basis (USD + R) diamond falsification watchdog.

The watchdog must report TWO classifications per diamond and take the
WORST as the canonical verdict.  This catches:

  - USD bleed where strategy R-edge is fine (sizing issue, not strategy)
  - Strategy R-decay where USD looks fine (scale-bug / data-quality issue)

Pre-wave-7 the watchdog was USD-only; cl_momentum and gc_momentum
flagged CRITICAL on -$4,645 / -$650 USD even though their R-multiples
(-1.71R / +0.24R) were inside the strategy-health envelope.
"""
# ruff: noqa: N802, PLR2004, SLF001
from __future__ import annotations

from eta_engine.scripts import diamond_falsification_watchdog as wd

# ────────────────────────────────────────────────────────────────────
# _classify_buffer
# ────────────────────────────────────────────────────────────────────


def test_buffer_below_zero_is_CRITICAL() -> None:
    assert wd._classify_buffer(-100.0, -200.0) == "CRITICAL"


def test_buffer_exactly_zero_is_CRITICAL() -> None:
    """Zero buffer means we're at the floor; treat as breached."""
    assert wd._classify_buffer(0.0, -200.0) == "CRITICAL"


def test_buffer_under_20_pct_is_WARN() -> None:
    # threshold magnitude 200; 20% of 200 = 40 → anything under 40 is WARN
    assert wd._classify_buffer(35.0, -200.0) == "WARN"


def test_buffer_20_to_50_pct_is_WATCH() -> None:
    assert wd._classify_buffer(60.0, -200.0) == "WATCH"   # 30%
    assert wd._classify_buffer(99.0, -200.0) == "WATCH"   # 49.5%


def test_buffer_above_50_pct_is_HEALTHY() -> None:
    assert wd._classify_buffer(150.0, -200.0) == "HEALTHY"
    assert wd._classify_buffer(5000.0, -200.0) == "HEALTHY"


def test_buffer_none_is_INCONCLUSIVE() -> None:
    assert wd._classify_buffer(None, -200.0) == "INCONCLUSIVE"
    assert wd._classify_buffer(50.0, None) == "INCONCLUSIVE"


# ────────────────────────────────────────────────────────────────────
# _worst_of (USD + R classification combiner)
# ────────────────────────────────────────────────────────────────────


def test_worst_of_picks_critical_over_healthy() -> None:
    assert wd._worst_of("CRITICAL", "HEALTHY") == "CRITICAL"
    assert wd._worst_of("HEALTHY", "CRITICAL") == "CRITICAL"


def test_worst_of_inconclusive_defers_to_other_basis() -> None:
    """When one basis has no verdict (data quality), the OTHER basis wins.
    This is the wave-7 fix: pre-wave-7 a single INCONCLUSIVE made the
    whole bot INCONCLUSIVE, hiding bots whose R-strategy was clearly
    healthy but whose USD ledger tripped on a scale-bug."""
    assert wd._worst_of("INCONCLUSIVE", "HEALTHY") == "HEALTHY"
    assert wd._worst_of("HEALTHY", "INCONCLUSIVE") == "HEALTHY"
    assert wd._worst_of("INCONCLUSIVE", "CRITICAL") == "CRITICAL"


def test_worst_of_both_inconclusive_stays_inconclusive() -> None:
    """No evidence either side → can't classify."""
    assert wd._worst_of("INCONCLUSIVE", "INCONCLUSIVE") == "INCONCLUSIVE"


def test_worst_of_order_warn_watch_healthy() -> None:
    """WARN beats WATCH beats HEALTHY."""
    assert wd._worst_of("WARN", "WATCH") == "WARN"
    assert wd._worst_of("WATCH", "HEALTHY") == "WATCH"
    assert wd._worst_of("WARN", "HEALTHY") == "WARN"


# ────────────────────────────────────────────────────────────────────
# _evaluate end-to-end with both bases populated
# ────────────────────────────────────────────────────────────────────


def _make_ledger(bot_id: str, total_pnl: float, cum_r: float,
                 n_trades: int = 50) -> dict:
    """Build a minimal closed_trade_ledger dict for one bot."""
    return {
        "per_bot": {
            bot_id: {
                "total_realized_pnl": total_pnl,
                "cumulative_r": cum_r,
                "closed_trade_count": n_trades,
            },
        },
    }


def test_evaluate_usd_critical_but_r_healthy_overall_is_CRITICAL() -> None:
    """This is the cl_momentum / gc_momentum case as of 2026-05-12:
    USD says CRITICAL (sizing pulled the dollars below the floor) but
    R-multiples say HEALTHY (strategy edge is fine).  Worst-of-both
    keeps the bot under CRITICAL so the operator surfaces the sizing
    issue, not silently passes it."""
    bot = "gc_momentum"  # USD floor -$200, R floor -3.0R
    ledger = _make_ledger(bot, total_pnl=-650.0, cum_r=+0.24, n_trades=8)
    s = wd._evaluate(bot, ledger)
    assert s.classification_usd == "CRITICAL"
    assert s.classification_r == "HEALTHY"
    assert s.classification == "CRITICAL"  # worst wins


def test_evaluate_both_healthy_stays_healthy() -> None:
    bot = "m2k_sweep_reclaim"  # USD -$800, R -20R
    ledger = _make_ledger(bot, total_pnl=+1760.0, cum_r=+533.0, n_trades=1151)
    s = wd._evaluate(bot, ledger)
    assert s.classification_usd == "HEALTHY"
    assert s.classification_r == "HEALTHY"
    assert s.classification == "HEALTHY"


def test_evaluate_r_basis_used_when_usd_scale_bug() -> None:
    """When per-trade USD exceeds $5,000 (scale-bug heuristic), the USD
    classification is INCONCLUSIVE but the R classification still
    drives the verdict. Pre-wave-7 the entire bot was marked
    INCONCLUSIVE; the wave-7 fix lets the R basis carry the verdict."""
    bot = "eur_sweep_reclaim"  # USD -$300, R -10R
    # Pretend a scale bug inflated the USD lifetime to $200k on n=50
    # trades = $4,000/trade — under the $5k threshold so NOT scale-bug
    # suspect. Use $500k / 50 = $10k/trade (above threshold).
    ledger = _make_ledger(bot, total_pnl=500_000.0, cum_r=+50.0, n_trades=50)
    s = wd._evaluate(bot, ledger)
    assert s.classification_usd == "INCONCLUSIVE"
    assert s.classification_r == "HEALTHY"
    assert s.classification == "HEALTHY"  # R wins when USD scale-bugged
    assert any("SCALE_BUG_SUSPECTED" in n for n in s.notes)


def test_evaluate_r_threshold_breach_is_CRITICAL() -> None:
    """Strategy R-decay below the R threshold: CRITICAL even with USD
    looking fine (e.g., quarantined USD = $0 but R is bleeding)."""
    bot = "eur_sweep_reclaim"  # R floor -10R
    # USD looks fine ($0 above -$300 → HEALTHY), R below -10R floor
    ledger = _make_ledger(bot, total_pnl=0.0, cum_r=-15.0, n_trades=50)
    s = wd._evaluate(bot, ledger)
    assert s.classification_usd == "HEALTHY"
    assert s.classification_r == "CRITICAL"
    assert s.classification == "CRITICAL"


def test_evaluate_missing_bot_in_ledger_is_INCONCLUSIVE() -> None:
    bot = "m2k_sweep_reclaim"
    ledger = {"per_bot": {}}  # no entry for this bot
    s = wd._evaluate(bot, ledger)
    assert s.classification == "INCONCLUSIVE"
    assert any("bot not in ledger" in n for n in s.notes)


def test_evaluate_unknown_bot_returns_INCONCLUSIVE() -> None:
    """Bots not in either RETIREMENT_THRESHOLDS_USD or _R must
    INCONCLUSIVE — we can't classify what we have no floor for."""
    s = wd._evaluate("nonexistent_bot", _make_ledger("nonexistent_bot", 0, 0))
    assert s.classification == "INCONCLUSIVE"
    assert any("no retirement threshold" in n for n in s.notes)


# ────────────────────────────────────────────────────────────────────
# Threshold dict completeness
# ────────────────────────────────────────────────────────────────────


def test_every_diamond_has_an_R_threshold() -> None:
    """Every member of DIAMOND_BOTS must have a RETIREMENT_THRESHOLDS_R
    entry, otherwise the wave-7 dual-basis logic silently degrades to
    USD-only for that bot."""
    from eta_engine.feeds.capital_allocator import DIAMOND_BOTS

    missing = [b for b in DIAMOND_BOTS if b not in wd.RETIREMENT_THRESHOLDS_R]
    assert not missing, (
        f"diamonds missing from RETIREMENT_THRESHOLDS_R: {sorted(missing)}"
    )


def test_every_diamond_has_a_USD_threshold() -> None:
    """Same coverage check on the legacy USD threshold dict."""
    from eta_engine.feeds.capital_allocator import DIAMOND_BOTS

    missing = [
        b for b in DIAMOND_BOTS if b not in wd.RETIREMENT_THRESHOLDS_USD
    ]
    assert not missing, (
        f"diamonds missing from RETIREMENT_THRESHOLDS_USD: {sorted(missing)}"
    )


def test_R_thresholds_are_all_negative() -> None:
    """Sanity: R retirement floors are losses, not gains."""
    for bot, threshold in wd.RETIREMENT_THRESHOLDS_R.items():
        assert threshold < 0, (
            f"R threshold for {bot} must be negative (a loss floor), "
            f"got {threshold}"
        )
