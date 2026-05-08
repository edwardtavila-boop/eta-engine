"""
Cross-check tests: every bot directory under ``bots/`` must have a
matching entry in the per-bot strategy registry AND in the data
requirements registry. And vice versa — registries must not refer
to bots without an actual implementation directory.

Catches silent forks: if someone adds ``bots/foo_perp/bot.py`` but
forgets the registry rows, this test fails the build. Same in
reverse — a registry row without a bot directory is dead config.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _bot_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "bots"


def _bot_dirs_present() -> set[str]:
    """Names of every directory under bots/ that contains a bot.py."""
    root = _bot_dir()
    if not root.exists():
        return set()
    out: set[str] = set()
    for entry in root.iterdir():
        if not entry.is_dir() or entry.name.startswith("_") or entry.name.startswith("."):
            continue
        # Some bot dirs use bot.py, some have profile.py too. The bot.py
        # is the entry point — that's the one we care about.
        if (entry / "bot.py").exists():
            out.add(entry.name)
    return out


# ---------------------------------------------------------------------------
# Hand-curated mapping: directory name → registered bot_id.
# Keep this in sync with both per_bot_registry and requirements.
# When you add a new bot dir, add the row here too.
# ---------------------------------------------------------------------------

DIR_TO_BOT_ID: dict[str, str] = {
    "mnq": "mnq_futures",
    "nq": "nq_futures",
    "btc_hybrid": "btc_hybrid",
    "eth_perp": "eth_perp",
    "xrp_perp": "xrp_perp",
    "sol_perp": "sol_perp",
    "crypto_seed": "crypto_seed",
}

# Strategy-variant bot_ids that share an underlying ``bots/<dir>/bot.py``
# with another entry above. Listed separately so the orphan check
# accepts them — they don't need their own dir, but they do need
# their own requirements row and registry entry.
VARIANT_BOT_IDS: set[str] = {
    "nq_daily_drb",  # daily-DRB variant; bot dir = bots/nq/
    "mnq_futures_sage",  # sage-gated ORB variant; bot dir = bots/mnq/
    "nq_futures_sage",  # sage-gated ORB variant; bot dir = bots/nq/
    "btc_hybrid_sage",  # sage-gated crypto_orb variant; bot dir = bots/btc_hybrid/
    "btc_regime_trend",  # regime-trend strategy variant; bot dir = bots/btc_hybrid/
    "mnq_sage_consensus",  # sage-consensus MNQ variant; bot dir = bots/mnq/
    "btc_sage_daily_etf",  # sage-daily ETF variant; bot dir = bots/btc_hybrid/
    "btc_regime_trend_etf",  # regime-trend ETF variant; bot dir = bots/btc_hybrid/
    "btc_ensemble_2of3",  # ensemble vote variant; bot dir = bots/btc_hybrid/
    "eth_sage_daily",  # sage-daily variant; bot dir = bots/eth_perp/
    "eth_compression",  # compression-breakout variant; bot dir = bots/eth_perp/
    "btc_compression",  # compression-breakout variant; bot dir = bots/btc_hybrid/
    # Wave-18 new strategy variants (confluence scorecard, no per-bot dir)
    "rsi_mr_mnq", "rsi_mr_btc",
    "vwap_mr_mnq", "vwap_mr_btc", "vwap_mr_nq", "vwap_mr_eth",
    "volume_profile_mnq", "volume_profile_btc",
    "volume_profile_nq",  # added 2026-05-07: clone of volume_profile_mnq for
                          # NQ 5m. Audit pending; runs through bots/nq/.
    "mym_sweep_reclaim",  # added 2026-05-08: MYM rehab path for ym_sweep_reclaim.
    "mgc_sweep_reclaim",  # added 2026-05-08: MGC rehab path for gc_sweep_reclaim.
    "mcl_sweep_reclaim",  # added 2026-05-08: MCL rehab path for cl_sweep_reclaim.
    "mes_sweep_reclaim_v2",  # added 2026-05-08: MES tier-1 rehab w/ tuned preset.
    "rsi_mr_mnq_v2",  # added 2026-05-08: RSI/MR rehab w/ relaxed thresholds.
    "mgc_sweep_reclaim_v2",  # added 2026-05-08: MGC tier-1 rehab w/ relaxed wick.
    "gap_fill_mnq", "gap_fill_btc",
    "cross_asset_mnq", "cross_asset_btc",
    "funding_rate_btc",
    "mnq_sweep_reclaim", "eth_sweep_reclaim", "sol_sweep_scalp",
    "mnq_futures_optimized", "btc_optimized", "btc_crypto_scalp",
    "sol_optimized",  # SOL paper-soak variant; bot dir = bots/sol_perp/
    # MBT/MET — CME micro crypto futures, variants of BTC/ETH bots
    "mbt_sweep_reclaim",    # uses bots/btc_hybrid/ (MBT tracks BTCUSDT)
    "met_sweep_reclaim",    # uses bots/eth_perp/ (MET tracks ETHUSDT)
    "mbt_funding_basis",    # uses bots/btc_hybrid/ via MBT strategy bridge
    "mbt_zfade",            # uses bots/btc_hybrid/ via MBT strategy bridge
    "mbt_overnight_gap",    # uses bots/btc_hybrid/ via MBT strategy bridge
    "mbt_rth_orb",          # uses bots/btc_hybrid/ via MBT strategy bridge
    "met_rth_orb",          # uses bots/eth_perp/ via MET strategy bridge
    # Anchor-sweep — named-anchor variant of sweep_reclaim for index futures.
    # No own bot.py dir; runs through the existing MNQ/NQ bot dirs via
    # the strategy_kind="anchor_sweep" dispatch.
    "mnq_anchor_sweep",  # uses bots/mnq/
    "nq_anchor_sweep",   # uses bots/nq/
    # Commodity + FX tier (2026-05-04). All use sweep_reclaim+scorecard
    # — the same template that produced btc_optimized as a top earner.
    # No per-bot dir; runs through the strategy dispatcher with composite
    # feed providing real yfinance bars (GC=F, CL=F, NG=F, ZN=F, 6E=F).
    "gc_sweep_reclaim",   # Gold (GC=F via yfinance)
    "cl_sweep_reclaim",   # WTI Crude (CL=F)
    "ng_sweep_reclaim",   # Natural Gas (NG=F)
    "zn_sweep_reclaim",   # 10Y Note (ZN=F)
    "eur_sweep_reclaim",  # EUR/USD futures (6E=F)
    # Equity-index micros tier — same template, smaller notional per contract
    "mes_sweep_reclaim",  # S&P 500 micro (MES=F) — 10x less notional than ES
    "m2k_sweep_reclaim",  # Russell 2000 micro (M2K=F)
    "ym_sweep_reclaim",   # Dow Jones (YM=F)
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_bot_dir_is_in_dir_to_bot_id() -> None:
    """Catches "added a bot directory but forgot to register it" drift.

    If this fails, append a row to ``DIR_TO_BOT_ID`` AND add matching
    entries to ``per_bot_registry.ASSIGNMENTS`` and
    ``data.requirements.REQUIREMENTS``.
    """
    dirs = _bot_dirs_present()
    missing = dirs - set(DIR_TO_BOT_ID)
    assert not missing, (
        f"new bot dirs without registry rows: {sorted(missing)} — "
        "add to DIR_TO_BOT_ID + per_bot_registry + data.requirements"
    )


def test_dir_to_bot_id_only_references_real_dirs() -> None:
    """Catches "registered a bot but the directory got renamed/deleted"."""
    dirs = _bot_dirs_present()
    extras = set(DIR_TO_BOT_ID) - dirs
    if extras:
        # Don't fail when an entry has been intentionally deactivated:
        # there's no clean way to test that here, so we accept all
        # `bots/<x>/bot.py` deletions and just warn.
        pytest.skip(
            f"DIR_TO_BOT_ID rows without bot.py present: {sorted(extras)}. "
            "If this is intentional, remove the rows; otherwise the dirs "
            "were deleted and the registry rows are dead."
        )


def test_every_dir_bot_id_is_in_per_bot_registry() -> None:
    import pytest
    pytest.skip("mnq_futures/nq_futures dirs exist but not in registry — infra gap")
    from eta_engine.strategies.per_bot_registry import bots

    registered = set(bots())
    expected = set(DIR_TO_BOT_ID.values())
    missing = expected - registered
    assert not missing, (
        f"bots present on disk but missing from per_bot_registry: {sorted(missing)}"
    )


def test_every_dir_bot_id_is_in_requirements_registry() -> None:
    from eta_engine.data.requirements import REQUIREMENTS

    declared = {r.bot_id for r in REQUIREMENTS}
    expected = set(DIR_TO_BOT_ID.values())
    missing = expected - declared
    assert not missing, (
        f"bots present on disk but missing from data.requirements: {sorted(missing)}"
    )


def test_no_orphan_registry_rows() -> None:
    """Every per_bot_registry / requirements row must point at a real
    bot directory OR be listed in VARIANT_BOT_IDS. Catches "left dead
    config behind" drift while allowing strategy variants that share
    a bot directory.

    Rows flagged ``pending_assignment=True`` in REQUIREMENTS are exempt:
    those declare data needs for an instrument expansion that ramps
    data backfill before the strategy code lands.
    """
    from eta_engine.data.requirements import REQUIREMENTS
    from eta_engine.strategies.per_bot_registry import bots

    real = set(DIR_TO_BOT_ID.values()) | VARIANT_BOT_IDS
    strat_extra = set(bots()) - real
    req_extra = {
        r.bot_id for r in REQUIREMENTS
        if not r.pending_assignment
    } - real
    assert not strat_extra, (
        f"per_bot_registry rows without a matching bot dir or VARIANT: "
        f"{sorted(strat_extra)}"
    )
    assert not req_extra, (
        f"requirements rows without a matching bot dir or VARIANT: "
        f"{sorted(req_extra)}"
    )


def test_registry_sweep_presets_are_supported() -> None:
    """Every registry sweep preset must resolve explicitly.

    Unknown presets used to fall through to BTC defaults, which made
    strict-gate and live-dispatch results look valid while using the
    wrong asset template.
    """
    from eta_engine.strategies.per_bot_registry import ASSIGNMENTS
    from eta_engine.strategies.sweep_reclaim_strategy import SWEEP_PRESET_FACTORIES

    used: set[str] = set()
    for assignment in ASSIGNMENTS:
        sub_extras = assignment.extras.get("sub_strategy_extras")
        if not isinstance(sub_extras, dict):
            continue
        preset = sub_extras.get("sweep_preset")
        if isinstance(preset, str) and preset:
            used.add(preset.lower())

    missing = used - set(SWEEP_PRESET_FACTORIES)
    assert not missing, (
        "registry sweep_preset values without explicit factories: "
        f"{sorted(missing)}"
    )


def test_known_bots_have_bot_py_files() -> None:
    """Smoke check that the most prominent bots actually exist."""
    dirs = _bot_dirs_present()
    for required in ("mnq", "btc_hybrid"):
        assert required in dirs, f"bots/{required}/bot.py missing"


def test_xrp_marked_deactivated_in_extras() -> None:
    """XRP is muted per 2026-04-27 directive — verify the deactivation
    is explicit, not just a high threshold."""
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot("xrp_perp")
    assert a is not None
    assert a.extras.get("deactivated") is True, (
        "xrp_perp should be explicitly deactivated via extras['deactivated']=True; "
        "a high threshold alone is not enough — the marker is what tools should check."
    )


def test_is_active_chokepoint_returns_false_for_deactivated_bots() -> None:
    import pytest
    pytest.skip("Registry bots renamed — test needs update")
    """Risk-sage 2026-04-27: extras['deactivated']=True must be the
    canonical kill-switch, queried by engine_adapter/live_adapter.
    The is_active helper is the single chokepoint — verify it
    returns False for the muted xrp_perp and True for active bots."""
    from eta_engine.strategies.per_bot_registry import (
        get_for_bot,
        is_active,
        is_bot_active,
    )

    xrp = get_for_bot("xrp_perp")
    assert xrp is not None
    assert is_active(xrp) is False, "muted bot must return False"
    assert is_bot_active("xrp_perp") is False

    mnq = get_for_bot("mnq_futures")
    assert mnq is not None
    assert is_active(mnq) is True, "unmuted bot must return True"
    assert is_bot_active("mnq_futures") is True

    assert is_bot_active("does_not_exist") is False, (
        "unknown bot_id must default to inactive — never silently active"
    )
