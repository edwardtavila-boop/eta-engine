"""Tests for diamond_leaderboard — wave-15 PROP_READY competition."""

# ruff: noqa: N802, PLR2004, SLF001
from __future__ import annotations

import json
from pathlib import Path

# ────────────────────────────────────────────────────────────────────
# Multipliers
# ────────────────────────────────────────────────────────────────────


def test_dual_basis_healthy_returns_one() -> None:
    from eta_engine.scripts import diamond_leaderboard as lb

    assert lb._dual_basis_multiplier("HEALTHY", "HEALTHY") == 1.0


def test_dual_basis_takes_weakest_link() -> None:
    """USD HEALTHY but R CRITICAL → multiplier driven by the weak side."""
    from eta_engine.scripts import diamond_leaderboard as lb

    assert lb._dual_basis_multiplier("HEALTHY", "CRITICAL") == 0.0
    assert lb._dual_basis_multiplier("CRITICAL", "HEALTHY") == 0.0


def test_sizing_breached_punishes_score() -> None:
    from eta_engine.scripts import diamond_leaderboard as lb

    assert lb._sizing_multiplier("SIZING_BREACHED") == 0.3
    assert lb._sizing_multiplier("SIZING_OK") == 1.0


def test_symmetry_bonus_rewards_both_sides_working() -> None:
    """SYMMETRIC > LONG_DOMINANT > LONG_ONLY_EDGE > BIDIRECTIONAL_LOSS."""
    from eta_engine.scripts import diamond_leaderboard as lb

    sym = lb._symmetry_bonus("SYMMETRIC")
    dom = lb._symmetry_bonus("LONG_DOMINANT")
    only = lb._symmetry_bonus("LONG_ONLY_EDGE")
    loss = lb._symmetry_bonus("BIDIRECTIONAL_LOSS")
    assert sym > dom > only > loss


def test_temporal_multiplier_caps_at_five_days() -> None:
    from eta_engine.scripts import diamond_leaderboard as lb

    assert lb._temporal_multiplier(5) == 1.0
    assert lb._temporal_multiplier(10) == 1.0
    assert lb._temporal_multiplier(2) == 0.4
    assert lb._temporal_multiplier(0) == 0.0


# ────────────────────────────────────────────────────────────────────
# PROP_READY eligibility + designation
# ────────────────────────────────────────────────────────────────────


def _entry(
    bot_id: str,
    n: int,
    avg_r: float,
    composite: float,
    usd_cls: str = "HEALTHY",
    r_cls: str = "HEALTHY",
    sizing_verdict: str = "SIZING_OK",
) -> object:
    """Build a leaderboard entry for testing _evaluate_prop_ready."""
    from eta_engine.scripts import diamond_leaderboard as lb

    e = lb.LeaderboardEntry(bot_id=bot_id)
    e.n_trades = n
    e.avg_r = avg_r
    e.composite_score = composite
    e.sources = {
        "watchdog_classification_usd": usd_cls,
        "watchdog_classification_r": r_cls,
        "sizing_verdict": sizing_verdict,
    }
    return e


def test_top_three_eligible_get_prop_ready() -> None:
    from eta_engine.scripts import diamond_leaderboard as lb

    entries = [
        _entry("a", n=200, avg_r=0.5, composite=10.0),
        _entry("b", n=200, avg_r=0.4, composite=8.0),
        _entry("c", n=200, avg_r=0.3, composite=6.0),
        _entry("d", n=200, avg_r=0.25, composite=5.0),
    ]
    lb._evaluate_prop_ready(entries)
    by_id = {e.bot_id: e for e in entries}
    assert by_id["a"].prop_ready
    assert by_id["b"].prop_ready
    assert by_id["c"].prop_ready
    assert not by_id["d"].prop_ready
    # Ranks are 1..N by composite descending
    assert by_id["a"].rank == 1
    assert by_id["d"].rank == 4


def test_low_n_disqualifies_from_prop_ready() -> None:
    """n_trades < MIN_PROP_READY_N=100 → DQ even if composite tops the list."""
    from eta_engine.scripts import diamond_leaderboard as lb

    entries = [
        _entry("tiny_but_strong", n=50, avg_r=2.0, composite=14.0),
        _entry("normal", n=200, avg_r=0.3, composite=4.0),
    ]
    lb._evaluate_prop_ready(entries)
    by_id = {e.bot_id: e for e in entries}
    assert not by_id["tiny_but_strong"].prop_ready
    assert any("n_trades<100" in d for d in by_id["tiny_but_strong"].prop_ready_disqualified_for)
    # The normal (lower-ranked but eligible) bot DOES get PROP_READY
    assert by_id["normal"].prop_ready


def test_low_avg_r_disqualifies_from_prop_ready() -> None:
    """avg_r < MIN_PROP_READY_AVG_R=0.20 → DQ."""
    from eta_engine.scripts import diamond_leaderboard as lb

    entries = [
        _entry("noise_bot", n=2000, avg_r=0.05, composite=8.0),
        _entry("real_edge", n=200, avg_r=0.3, composite=4.0),
    ]
    lb._evaluate_prop_ready(entries)
    by_id = {e.bot_id: e for e in entries}
    assert not by_id["noise_bot"].prop_ready
    assert any("avg_r<" in d for d in by_id["noise_bot"].prop_ready_disqualified_for)
    assert by_id["real_edge"].prop_ready


def test_watchdog_critical_disqualifies_either_basis() -> None:
    from eta_engine.scripts import diamond_leaderboard as lb

    entries = [
        _entry("usd_critical", n=200, avg_r=0.5, composite=10.0, usd_cls="CRITICAL"),
        _entry("r_critical", n=200, avg_r=0.5, composite=9.0, r_cls="CRITICAL"),
        _entry("clean", n=200, avg_r=0.3, composite=5.0),
    ]
    lb._evaluate_prop_ready(entries)
    by_id = {e.bot_id: e for e in entries}
    assert not by_id["usd_critical"].prop_ready
    assert not by_id["r_critical"].prop_ready
    assert by_id["clean"].prop_ready


def test_sizing_breached_disqualifies() -> None:
    from eta_engine.scripts import diamond_leaderboard as lb

    entries = [
        _entry("breached", n=200, avg_r=0.5, composite=10.0, sizing_verdict="SIZING_BREACHED"),
        _entry("clean_eur_range", n=200, avg_r=0.3, composite=5.0),
    ]
    lb._evaluate_prop_ready(entries)
    by_id = {e.bot_id: e for e in entries}
    assert not by_id["breached"].prop_ready
    assert any("sizing BREACHED" in d for d in by_id["breached"].prop_ready_disqualified_for)
    assert by_id["clean_eur_range"].prop_ready


def test_spot_bot_disqualified_from_prop_ready_wave16() -> None:
    """Wave-16 mandate: PROP_READY is IBKR-futures-only.  A high-scoring
    spot bot (BTC/ETH/SOL via Alpaca) must NOT earn the badge —
    Alpaca spot is cellared (POOL_SPLIT["spot"]=0.0) and the prop-fund
    routing layer should never auto-route real capital to a broker the
    operator has cellared.

    Regression case: pre-wave-16 the leaderboard would award PROP_READY
    to volume_profile_btc (ranked #4 with composite +6.06 — would
    have been #3 if the operator had filtered eur_sweep_reclaim). The
    fix uses is_ibkr_futures_eligible() to gate at the eligibility
    layer."""
    from eta_engine.scripts import diamond_leaderboard as lb

    entries = [
        _entry("volume_profile_btc", n=339, avg_r=0.36, composite=10.0),  # spot
        _entry("met_sweep_reclaim", n=200, avg_r=0.6, composite=8.0),  # CME crypto futures
        _entry("eur_sweep_reclaim", n=200, avg_r=0.4, composite=6.0),  # CME currency futures
    ]
    lb._evaluate_prop_ready(entries)
    by_id = {e.bot_id: e for e in entries}
    # volume_profile_btc has highest composite but is DQ'd as Alpaca spot
    assert not by_id["volume_profile_btc"].prop_ready
    assert any("not IBKR-futures eligible" in d for d in by_id["volume_profile_btc"].prop_ready_disqualified_for)
    # The two IBKR-futures bots get PROP_READY
    assert by_id["met_sweep_reclaim"].prop_ready
    assert by_id["eur_sweep_reclaim"].prop_ready


def test_ibkr_futures_eligible_helper() -> None:
    """Sanity check on the upstream helper: futures bots pass, spot bots fail."""
    from eta_engine.feeds.capital_allocator import is_ibkr_futures_eligible

    # IBKR futures
    assert is_ibkr_futures_eligible("met_sweep_reclaim")  # CME micro ether
    assert is_ibkr_futures_eligible("mbt_sweep_reclaim")  # CME micro bitcoin
    assert is_ibkr_futures_eligible("m2k_sweep_reclaim")  # CME micro russell
    assert is_ibkr_futures_eligible("eur_sweep_reclaim")  # CME 6E
    assert is_ibkr_futures_eligible("ng_sweep_reclaim")  # CME NG
    assert is_ibkr_futures_eligible("mnq_futures_sage")  # CME micro NQ
    # Alpaca spot — NOT eligible
    assert not is_ibkr_futures_eligible("volume_profile_btc")
    assert not is_ibkr_futures_eligible("vwap_mr_btc")
    assert not is_ibkr_futures_eligible("funding_rate_btc")
    assert not is_ibkr_futures_eligible("btc_compression")
    assert not is_ibkr_futures_eligible("eth_sage_daily")


def test_no_eligible_means_no_prop_ready() -> None:
    """If 0 bots qualify, the badge is awarded to nobody (no floor-fill)."""
    from eta_engine.scripts import diamond_leaderboard as lb

    entries = [
        _entry("low_n_1", n=10, avg_r=0.5, composite=10.0),
        _entry("low_n_2", n=10, avg_r=0.4, composite=8.0),
        _entry("noise", n=2000, avg_r=0.01, composite=4.0),
    ]
    lb._evaluate_prop_ready(entries)
    assert not any(e.prop_ready for e in entries)


def test_fewer_than_three_eligible_awards_to_those_that_qualify() -> None:
    """If only 2 of N pass eligibility, both get PROP_READY (≤ TOP_N)."""
    from eta_engine.scripts import diamond_leaderboard as lb

    entries = [
        _entry("strong_1", n=200, avg_r=0.5, composite=10.0),
        _entry("strong_2", n=200, avg_r=0.4, composite=8.0),
        _entry("low_n", n=10, avg_r=2.0, composite=14.0),  # composite high but n DQ
    ]
    lb._evaluate_prop_ready(entries)
    by_id = {e.bot_id: e for e in entries}
    assert by_id["strong_1"].prop_ready
    assert by_id["strong_2"].prop_ready
    assert not by_id["low_n"].prop_ready


# ────────────────────────────────────────────────────────────────────
# Composite scoring sanity
# ────────────────────────────────────────────────────────────────────


def test_negative_edge_produces_negative_composite() -> None:
    """A losing strategy must rank below break-even (negative composite)."""
    from eta_engine.scripts import diamond_leaderboard as lb

    e = lb._build_entry(
        bot_id="loser",
        sizing={"verdict": "SIZING_OK", "n_trades_with_pnl": 100, "cum_r": -5.0},
        watchdog={"classification_usd": "HEALTHY", "classification_r": "HEALTHY"},
        direction={
            "n_long": 50,
            "n_short": 50,
            "long": {"avg_r": -0.05, "win_rate_pct": 30.0},
            "short": {"avg_r": -0.05, "win_rate_pct": 30.0},
            "verdict": "BIDIRECTIONAL_LOSS",
        },
    )
    # _build_entry doesn't apply symmetry; the runner does. So just
    # check the edge_score sign.
    assert e.edge_score < 0


# ────────────────────────────────────────────────────────────────────
# Snapshot file
# ────────────────────────────────────────────────────────────────────


def test_run_writes_json_receipt(tmp_path: Path, monkeypatch: object) -> None:
    """run() invokes all 4 sub-audits, scores, and persists."""
    from eta_engine.scripts import diamond_leaderboard as lb

    monkeypatch.setattr(
        lb,
        "_gather_signals",
        lambda: ({}, {}, {}, {}),  # no signal data → all bots score 0
    )
    out_path = tmp_path / "out.json"
    monkeypatch.setattr(lb, "OUT_LATEST", out_path)  # type: ignore[attr-defined]
    summary = lb.run()

    assert out_path.exists()
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert "ts" in on_disk
    assert "leaderboard" in on_disk
    assert "prop_ready_bots" in on_disk
    assert on_disk["n_diamonds"] == summary["n_diamonds"]
    assert on_disk["top_prop_ready_n"] == lb.TOP_PROP_READY_N
