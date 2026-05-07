"""Tests for strategies.per_bot_registry — the per-bot strategy catalog."""

from __future__ import annotations

import pytest

from eta_engine.scripts import workspace_roots
from eta_engine.strategies.per_bot_registry import (
    ASSIGNMENTS,
    StrategyAssignment,
    all_assignments,
    bots,
    get_for_bot,
    is_bot_active,
    summary_markdown,
)


def test_assignments_is_a_tuple_of_assignments() -> None:
    assert isinstance(ASSIGNMENTS, tuple)
    assert all(isinstance(a, StrategyAssignment) for a in ASSIGNMENTS)
    assert len(ASSIGNMENTS) >= 1


def test_every_assignment_is_immutable() -> None:
    """frozen=True must hold — operators must edit the registry source,
    not mutate at runtime."""
    a = ASSIGNMENTS[0]
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError on dataclass(frozen=True)
        a.bot_id = "tampered"  # type: ignore[misc]


def test_bot_ids_are_unique() -> None:
    ids = [a.bot_id for a in ASSIGNMENTS]
    assert len(ids) == len(set(ids)), f"duplicate bot_ids: {ids}"


def test_strategy_ids_are_unique() -> None:
    sids = [a.strategy_id for a in ASSIGNMENTS]
    assert len(sids) == len(set(sids)), f"duplicate strategy_ids: {sids}"


def test_get_for_bot_returns_match() -> None:
    a = get_for_bot("mnq_futures")
    assert a is not None
    assert a.bot_id == "mnq_futures"
    assert a.symbol == "MNQ1"


def test_get_for_bot_returns_none_when_unknown() -> None:
    assert get_for_bot("does_not_exist") is None


def test_all_assignments_returns_full_list() -> None:
    out = all_assignments()
    assert len(out) == len(ASSIGNMENTS)
    assert all(isinstance(a, StrategyAssignment) for a in out)


def test_bots_helper_lists_all_ids() -> None:
    assert bots() == [a.bot_id for a in ASSIGNMENTS]


def test_summary_markdown_includes_every_bot() -> None:
    md = summary_markdown()
    for a in ASSIGNMENTS:
        assert a.bot_id in md
        assert a.strategy_id in md
    assert md.count("|") > 8  # has table rows


def test_thresholds_in_valid_range() -> None:
    # Strategy kinds that have their own filtering and ignore the
    # confluence threshold entirely. Adding a new self-contained
    # strategy here is the one-line change to keep the registry tests
    # green for it.
    _IGNORES_THRESHOLD = {  # noqa: N806 - module-style constant inside test fn
        "orb", "drb", "grid", "crypto_orb",
        "crypto_trend", "crypto_meanrev", "crypto_scalp",
        "sage_consensus", "orb_sage_gated", "crypto_regime_trend",
        "crypto_macro_confluence", "sage_daily_gated", "ensemble_voting",
        # Foundation strategies (2026-04-27): compression-breakout
        # and sweep-reclaim use their own internal triggers (BB-width
        # percentile + ATR-MA / wick + reclaim) and don't read the
        # confluence threshold.
        "compression_breakout", "sweep_reclaim",
        # Confluence scorecard uses internal scorecard_config
        # (min_score, factor EMAs, A+ multiplier) and ignores
        # the basic confluence threshold entirely.
        "confluence_scorecard",
        "mtf_scalp",
        # Anchor-sweep (2026-05-04): named-anchor variant of sweep_reclaim
        # for MNQ/NQ. Self-contained — wick-pierce + close-reclaim of
        # PDH/PDL/PMH/PML/ONH/ONL is the entire trigger.
        "anchor_sweep",
        # CME crypto micro futures (2026-05-07): MBT/MET strategies that
        # have their own internal triggers (basis-premium z-score, overnight
        # gap detection, opening-range breakout) — they don't read the
        # generic confluence threshold.
        "mbt_funding_basis",
        "mbt_overnight_gap",
        "met_rth_orb",
    }
    for a in ASSIGNMENTS:
        if a.strategy_kind in _IGNORES_THRESHOLD:
            continue
        assert 0.0 < a.confluence_threshold <= 10.0, (
            f"{a.bot_id} threshold {a.confluence_threshold} out of (0, 10]"
        )


def test_window_step_consistent() -> None:
    for a in ASSIGNMENTS:
        assert a.step_days > 0, f"{a.bot_id} step_days must be positive"
        assert a.step_days <= a.window_days, (
            f"{a.bot_id} step_days {a.step_days} > window_days {a.window_days}"
        )


def test_scorer_name_is_known() -> None:
    valid = {"global", "mnq", "btc"}
    for a in ASSIGNMENTS:
        assert a.scorer_name in valid, (
            f"{a.bot_id} unknown scorer {a.scorer_name!r}; add to "
            f"_resolve_scorer in run_research_grid + this allowlist "
            f"before registering"
        )


def test_block_regimes_is_frozenset() -> None:
    for a in ASSIGNMENTS:
        assert isinstance(a.block_regimes, frozenset), (
            f"{a.bot_id} block_regimes must be frozenset (immutability)"
        )


def test_rationale_is_substantive() -> None:
    """Rationale exists for a reason — every assignment must justify
    itself in at least 50 chars so future readers know why."""
    for a in ASSIGNMENTS:
        assert len(a.rationale) >= 50, (
            f"{a.bot_id} rationale too short: {a.rationale!r}"
        )


def test_known_bots_present() -> None:
    """Every bot in eta_engine/bots/ should have a registry entry.
    Smoke-test the most prominent ones; full coverage is enforced
    by registry-vs-bots-dir audit when added."""
    for required in ("mnq_futures", "nq_futures", "btc_hybrid"):
        assert get_for_bot(required) is not None, (
            f"{required} missing from registry"
        )


def test_btc_etf_assignments_use_canonical_history_root() -> None:
    expected = str(workspace_roots.MNQ_HISTORY_ROOT / "BTC_ETF_FLOWS.csv")
    for bot_id in ("btc_sage_daily_etf", "btc_ensemble_2of3", "btc_regime_trend_etf"):
        assignment = get_for_bot(bot_id)
        assert assignment is not None
        assert assignment.extras.get("etf_csv_path") == expected


def test_elite_scoreboard_deactivates_decayed_legacy_lanes() -> None:
    expected_replacements = {
        "eth_perp": "eth_sage_daily",
        "btc_hybrid": "btc_hybrid_sage",
        "eth_compression": "compression on ETH 1h has no edge",
    }
    for bot_id, reason_fragment in expected_replacements.items():
        assignment = get_for_bot(bot_id)
        assert assignment is not None
        assert is_bot_active(bot_id) is False
        assert assignment.extras.get("deactivated_on") == "2026-05-05"
        reason = str(assignment.extras.get("deactivated_reason", ""))
        assert "elite_scoreboard 2026-05-05" in reason
        assert reason_fragment in reason
