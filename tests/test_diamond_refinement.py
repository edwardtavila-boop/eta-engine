"""Diamond refinement layer — authenticity audit, falsification
watchdog, and combined-notional sizing enforcement.

This is the "1000-fold refinement" companion to test_diamond_protection.py.
Protection prevents auto-disable from killing a diamond.  Refinement
catches the cases where a diamond ISN'T actually a diamond (cubic
zirconia) or is approaching its falsification threshold (watchdog) or
would breach correlated-underlying sizing limits (portfolio limits).
"""
# ruff: noqa: N802, PLR2004
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.feeds.capital_allocator import DIAMOND_BOTS
from eta_engine.scripts.diamond_authenticity_audit import (
    _assess as authenticity_assess,
)
from eta_engine.scripts.diamond_authenticity_audit import (
    _bootstrap_ci,
    _mc_shuffle_p_value,
)
from eta_engine.scripts.diamond_falsification_watchdog import (
    RETIREMENT_THRESHOLDS_USD,
    DiamondStatus,
    _classify,
)
from eta_engine.scripts.diamond_falsification_watchdog import (
    _evaluate as watchdog_evaluate,
)
from eta_engine.strategies.l2_portfolio_limits import (
    DEFAULT_MAX_COMBINED_UNITS_PER_GROUP,
    _group_existing_units,
    _resolve_underlying_group,
    check_portfolio_limits,
)

# ────────────────────────────────────────────────────────────────────
# Combined-notional sizing — operator commitment now code-enforced
# ────────────────────────────────────────────────────────────────────


def test_underlying_groups_cover_diamond_correlations() -> None:
    """All 8 diamond symbols must resolve to a tracked group."""
    diamond_symbols = {
        "mnq_futures_sage": "MNQ1",
        "nq_futures_sage": "NQ1",
        "cl_momentum": "CL1",
        "mcl_sweep_reclaim": "MCL1",
        "mgc_sweep_reclaim": "MGC1",
        "gc_momentum": "GC1",
        "cl_macro": "CL1",
        # eur_sweep_reclaim (6E) intentionally has no group — diversifier
    }
    for bot, sym in diamond_symbols.items():
        group, units = _resolve_underlying_group(sym)
        assert group is not None, f"{bot} symbol {sym} not in any group"
        assert units > 0


def test_resolve_underlying_group_known_symbols() -> None:
    """Spot-check the group resolution math."""
    assert _resolve_underlying_group("MNQ") == ("NASDAQ", 1.0)
    assert _resolve_underlying_group("NQ") == ("NASDAQ", 10.0)
    assert _resolve_underlying_group("MNQM6") == ("NASDAQ", 1.0)
    assert _resolve_underlying_group("MCL") == ("CRUDE", 1.0)
    assert _resolve_underlying_group("CL") == ("CRUDE", 10.0)
    assert _resolve_underlying_group("MGC") == ("GOLD", 1.0)
    assert _resolve_underlying_group("GC") == ("GOLD", 10.0)
    # case-insensitive
    assert _resolve_underlying_group("mnq")[0] == "NASDAQ"


def test_resolve_unknown_symbol_returns_none() -> None:
    assert _resolve_underlying_group("XYZ") == (None, 0.0)
    assert _resolve_underlying_group("6E")[0] is None  # FX diversifier


def test_group_existing_units_sums_correctly() -> None:
    """1 MNQ long + 1 NQ long = 11 NASDAQ units."""
    positions = {("MNQ", "LONG"): 1, ("NQ", "LONG"): 1, ("MCL", "LONG"): 1}
    assert _group_existing_units(positions, "NASDAQ", side="LONG") == 11.0
    assert _group_existing_units(positions, "NASDAQ", side="SHORT") == 0.0
    assert _group_existing_units(positions, "CRUDE", side="LONG") == 1.0


def test_combined_notional_blocks_third_nq_when_mnq_open(
        tmp_path: Path) -> None:
    """MNQ + NQ should count as ONE NASDAQ bet (cap=1 unit by default).
    With 1 MNQ open already, a new 1-NQ entry (10 units) must block."""
    fills = tmp_path / "fills.jsonl"
    fills.write_text(json.dumps({
        "ts": datetime.now(UTC).isoformat(),
        "signal_id": "MNQ-LONG-1",
        "symbol": "MNQ",
        "side": "LONG",
        "qty_filled": 1,
        "exit_reason": "ENTRY",
    }) + "\n", encoding="utf-8")
    decision = check_portfolio_limits(
        symbol="NQ", side="LONG", qty=1,
        _fill_path=fills, _log_path=tmp_path / "plim.jsonl",
    )
    assert decision.blocked is True
    assert "combined_underlying_exceeded" in decision.reason
    assert decision.detail.get("group") == "NASDAQ"


def test_combined_notional_allows_offsetting_short(tmp_path: Path) -> None:
    """1 MNQ LONG should NOT block a new MNQ SHORT (hedge case).
    Group cap is per-side, so SHORT-side has its own 1-unit budget.
    Note: hedging via a FULL NQ would still be blocked because 1 NQ =
    10 NASDAQ-equivalents, busting the SHORT-side cap of 1 unit."""
    fills = tmp_path / "fills.jsonl"
    fills.write_text(json.dumps({
        "ts": datetime.now(UTC).isoformat(),
        "signal_id": "MNQ-LONG-1",
        "symbol": "MNQ",
        "side": "LONG",
        "qty_filled": 1,
        "exit_reason": "ENTRY",
    }) + "\n", encoding="utf-8")
    decision = check_portfolio_limits(
        symbol="MNQ", side="SHORT", qty=1,
        _fill_path=fills, _log_path=tmp_path / "plim.jsonl",
    )
    assert decision.blocked is False


def test_combined_notional_crude_blocks_mcl_when_cl_open(
        tmp_path: Path) -> None:
    """Crude cap is 2 MCL-equivalents.  1 full CL = 10 units, already
    over cap → any new MCL LONG must be blocked by the combined-units
    rule (NOT by same-side stacking since the symbol is different)."""
    fills = tmp_path / "fills.jsonl"
    # 1 full CL LONG (10 NASDAQ-equiv units)
    fills.write_text(json.dumps({
        "ts": datetime.now(UTC).isoformat(),
        "signal_id": "CL-LONG-1",
        "symbol": "CL",
        "side": "LONG",
        "qty_filled": 1,
        "exit_reason": "ENTRY",
    }) + "\n", encoding="utf-8")
    decision = check_portfolio_limits(
        symbol="MCL", side="LONG", qty=1,
        _fill_path=fills, _log_path=tmp_path / "plim.jsonl",
    )
    # CL alone is already 10 units > 2 cap; MCL adds 1 more → still blocked
    assert decision.blocked is True
    assert decision.detail.get("group") == "CRUDE"


def test_combined_notional_eur_has_no_group(tmp_path: Path) -> None:
    """6E (Euro) is a diversifier — not in any group, so combined-
    notional check is bypassed."""
    fills = tmp_path / "fills.jsonl"
    fills.write_text("", encoding="utf-8")
    decision = check_portfolio_limits(
        symbol="6E", side="LONG", qty=1,
        _fill_path=fills, _log_path=tmp_path / "plim.jsonl",
    )
    assert decision.blocked is False


def test_default_caps_match_diamond_memo() -> None:
    """The default caps in DEFAULT_MAX_COMBINED_UNITS_PER_GROUP must
    match the operator's diamond decision memo (2026-05-12).  Any
    change here is a deliberate sizing-rule revision."""
    assert DEFAULT_MAX_COMBINED_UNITS_PER_GROUP["NASDAQ"] == 1.0
    assert DEFAULT_MAX_COMBINED_UNITS_PER_GROUP["CRUDE"] == 2.0
    assert DEFAULT_MAX_COMBINED_UNITS_PER_GROUP["GOLD"] == 1.0


# ────────────────────────────────────────────────────────────────────
# Authenticity audit — bootstrap / MC primitives
# ────────────────────────────────────────────────────────────────────


def test_bootstrap_ci_positive_distribution() -> None:
    """A clearly-positive sample should produce a CI well above zero."""
    samples = [10.0, 15.0, 20.0, 12.0, 8.0, 18.0, 22.0, 14.0, 11.0,
                17.0, 9.0, 16.0, 13.0, 19.0, 21.0]
    lo, hi = _bootstrap_ci(samples, n_resamples=500)
    assert lo > 0
    assert hi > lo
    assert 10 < (lo + hi) / 2 < 20  # mean roughly preserved


def test_bootstrap_ci_zero_centered_includes_zero() -> None:
    samples = [-10, -5, -2, 0, 2, 5, 10, -8, 8, -3, 3, -7, 7, 1, -1]
    lo, hi = _bootstrap_ci(samples, n_resamples=500)
    # Should bracket zero (no clear edge)
    assert lo < 0 < hi


def test_mc_shuffle_p_value_unbiased_sample_high_p() -> None:
    """Symmetric ±10 sample — random sign-shuffle should easily match
    the observed mean of zero → high p-value."""
    samples = [10, -10, 10, -10, 10, -10, 10, -10, 10, -10]
    p = _mc_shuffle_p_value(samples, n_shuffles=500)
    assert p > 0.5  # very high — null is fine


def test_mc_shuffle_p_value_strongly_biased_low_p() -> None:
    """All-positive sample — sign-shuffle rarely matches → low p."""
    samples = [5] * 20
    p = _mc_shuffle_p_value(samples, n_shuffles=500)
    # Even the most-positive shuffle (all +5) achieves the observed
    # mean exactly once, so p ~ 1/n_shuffles
    assert p < 0.02


# ────────────────────────────────────────────────────────────────────
# Authenticity audit — verdict logic
# ────────────────────────────────────────────────────────────────────


def test_audit_returns_one_report_per_diamond() -> None:
    """The audit must produce exactly one verdict per diamond, even
    when source ledgers are missing/empty."""
    from eta_engine.scripts.diamond_authenticity_audit import run_audit
    summary = run_audit()
    assert summary["n_diamonds"] == len(DIAMOND_BOTS)
    assert len(summary["reports"]) == len(DIAMOND_BOTS)
    bot_ids = {r["bot_id"] for r in summary["reports"]}
    assert bot_ids == DIAMOND_BOTS


def test_audit_detects_scale_bug_in_eur_sweep() -> None:
    """The eur_sweep_reclaim ledger record has a scale bug ($20M).
    The audit must flag it as CUBIC_ZIRCONIA or INCONCLUSIVE — never
    GENUINE."""
    rep = authenticity_assess("eur_sweep_reclaim")
    assert rep.verdict != "GENUINE", (
        "eur_sweep_reclaim has a known ledger scale bug; "
        "GENUINE verdict would be wrong"
    )


def test_audit_inconclusive_for_low_sample(monkeypatch) -> None:
    """When n_trades < 20, verdict should be INCONCLUSIVE or LAB_GROWN —
    never GENUINE (we cannot statistically confirm an edge on tiny n)."""
    # cl_macro currently has n=2 in the ledger
    rep = authenticity_assess("cl_macro")
    assert rep.verdict in ("INCONCLUSIVE", "LAB_GROWN")


# ────────────────────────────────────────────────────────────────────
# Falsification watchdog
# ────────────────────────────────────────────────────────────────────


def test_watchdog_thresholds_match_diamond_memo() -> None:
    """The retirement thresholds must match the operator's 2026-05-12
    decision memo exactly.  Any drift is a deliberate edit."""
    assert RETIREMENT_THRESHOLDS_USD["mnq_futures_sage"] == -5000.0
    assert RETIREMENT_THRESHOLDS_USD["nq_futures_sage"] == -1500.0
    assert RETIREMENT_THRESHOLDS_USD["cl_momentum"] == -1500.0
    assert RETIREMENT_THRESHOLDS_USD["mcl_sweep_reclaim"] == -1500.0
    assert RETIREMENT_THRESHOLDS_USD["mgc_sweep_reclaim"] == -600.0
    assert RETIREMENT_THRESHOLDS_USD["eur_sweep_reclaim"] == -300.0
    assert RETIREMENT_THRESHOLDS_USD["gc_momentum"] == -200.0
    assert RETIREMENT_THRESHOLDS_USD["cl_macro"] == -1000.0


def test_watchdog_has_threshold_for_every_diamond() -> None:
    """No diamond can lack a falsification threshold — that would be
    a silent gap in the watchdog."""
    missing = DIAMOND_BOTS - set(RETIREMENT_THRESHOLDS_USD.keys())
    assert not missing, f"diamonds without thresholds: {missing}"


def test_watchdog_classification_buckets() -> None:
    """Classification logic: HEALTHY > 50%, WATCH 20-50%, WARN <20%,
    CRITICAL <= 0."""
    s = DiamondStatus(bot_id="test", retirement_threshold=-1000.0,
                       buffer_usd=600.0)  # 60% of |threshold|
    _classify(s)
    assert s.classification == "HEALTHY"

    s = DiamondStatus(bot_id="test", retirement_threshold=-1000.0,
                       buffer_usd=300.0)  # 30%
    _classify(s)
    assert s.classification == "WATCH"

    s = DiamondStatus(bot_id="test", retirement_threshold=-1000.0,
                       buffer_usd=150.0)  # 15%
    _classify(s)
    assert s.classification == "WARN"

    s = DiamondStatus(bot_id="test", retirement_threshold=-1000.0,
                       buffer_usd=-50.0)  # breached
    _classify(s)
    assert s.classification == "CRITICAL"


def test_watchdog_handles_missing_ledger() -> None:
    """Watchdog must not crash when the ledger is missing — returns
    INCONCLUSIVE with a note."""
    s = watchdog_evaluate("mnq_futures_sage", ledger=None)
    assert s.classification == "INCONCLUSIVE"
    assert any("missing" in n for n in s.notes)


def test_watchdog_handles_scale_bug() -> None:
    """When a bot's ledger shows the scale-bug signature ($100k+ per
    trade), the watchdog should refuse to classify rather than report
    a misleading buffer."""
    fake_ledger = {
        "per_bot": {
            "eur_sweep_reclaim": {
                "closed_trade_count": 280,
                "total_realized_pnl": 20_973_625.0,
                "win_rate_pct": 70.0,
            },
        },
    }
    s = watchdog_evaluate("eur_sweep_reclaim", ledger=fake_ledger)
    assert s.classification == "INCONCLUSIVE"
    assert any("SCALE_BUG" in n for n in s.notes)


def test_watchdog_classification_with_synthetic_healthy_bot() -> None:
    """Bot with lifetime P&L well above its retirement floor → HEALTHY."""
    fake_ledger = {
        "per_bot": {
            "mnq_futures_sage": {
                "closed_trade_count": 1000,
                "total_realized_pnl": 8000.0,   # well above -$5,000
                "win_rate_pct": 55.0,
            },
        },
    }
    s = watchdog_evaluate("mnq_futures_sage", ledger=fake_ledger)
    assert s.classification == "HEALTHY"
    assert s.buffer_usd is not None
    # buffer = 8000 - (-5000) = +13000; threshold magnitude = 5000
    # → pct = 13000/5000 = 260% → HEALTHY
    assert s.buffer_usd == 13000.0


def test_watchdog_classification_breached_floor_is_critical() -> None:
    """P&L below the floor → CRITICAL — operator paging signal."""
    fake_ledger = {
        "per_bot": {
            "gc_momentum": {  # threshold -$200 (FRAGILE)
                "closed_trade_count": 50,
                "total_realized_pnl": -250.0,  # below floor by $50
                "win_rate_pct": 30.0,
            },
        },
    }
    s = watchdog_evaluate("gc_momentum", ledger=fake_ledger)
    assert s.classification == "CRITICAL"
    # buffer = -250 - (-200) = -50
    assert s.buffer_usd == -50.0


def test_watchdog_run_produces_full_report() -> None:
    """End-to-end run must produce a report dict with every diamond
    and a classification_counts roll-up."""
    from eta_engine.scripts.diamond_falsification_watchdog import (
        run_watchdog,
    )
    report = run_watchdog()
    assert report["n_diamonds"] == len(DIAMOND_BOTS)
    assert len(report["statuses"]) == len(DIAMOND_BOTS)
    assert "classification_counts" in report
    counts = report["classification_counts"]
    total = sum(counts.values())
    assert total == len(DIAMOND_BOTS)


# ────────────────────────────────────────────────────────────────────
# Cross-module sanity
# ────────────────────────────────────────────────────────────────────


def test_diamond_set_is_consistent_across_modules() -> None:
    """DIAMOND_BOTS (capital_allocator), RETIREMENT_THRESHOLDS_USD
    (watchdog), and the EXPECTED_DIAMONDS in test_diamond_protection
    must all enumerate the same 8 bots."""
    assert set(RETIREMENT_THRESHOLDS_USD.keys()) == DIAMOND_BOTS
