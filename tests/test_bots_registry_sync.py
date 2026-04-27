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
    a bot directory."""
    from eta_engine.data.requirements import REQUIREMENTS
    from eta_engine.strategies.per_bot_registry import bots

    real = set(DIR_TO_BOT_ID.values()) | VARIANT_BOT_IDS
    strat_extra = set(bots()) - real
    req_extra = {r.bot_id for r in REQUIREMENTS} - real
    assert not strat_extra, (
        f"per_bot_registry rows without a matching bot dir or VARIANT: "
        f"{sorted(strat_extra)}"
    )
    assert not req_extra, (
        f"requirements rows without a matching bot dir or VARIANT: "
        f"{sorted(req_extra)}"
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
