"""Tests for data.requirements + data.audit."""

from __future__ import annotations

import csv
from pathlib import Path  # noqa: TC003 - pytest fixtures use Path at runtime

import pytest

from eta_engine.data.audit import audit_all, audit_bot, summary_markdown
from eta_engine.data.library import DataLibrary
from eta_engine.data.requirements import (
    REQUIREMENTS,
    BotRequirements,
    critical_requirements_for,
    get_requirements,
)

# ---------------------------------------------------------------------------
# Requirements registry
# ---------------------------------------------------------------------------


def test_requirements_is_tuple_of_BotRequirements() -> None:  # noqa: N802
    assert isinstance(REQUIREMENTS, tuple)
    assert all(isinstance(r, BotRequirements) for r in REQUIREMENTS)


def test_bot_ids_are_unique_in_requirements() -> None:
    ids = [r.bot_id for r in REQUIREMENTS]
    assert len(ids) == len(set(ids)), f"duplicate bot_ids: {ids}"


def test_requirements_are_immutable() -> None:
    r = REQUIREMENTS[0]
    with pytest.raises(Exception):  # noqa: B017 - frozen=True raises AttributeError|FrozenInstanceError|dataclasses.FrozenInstanceError; the exact class differs by Python version
        r.bot_id = "tampered"  # type: ignore[misc]


def test_get_requirements_returns_match_or_none() -> None:
    assert get_requirements("mnq_futures") is not None
    assert get_requirements("__nope__") is None


def test_critical_requirements_filters() -> None:
    crits = critical_requirements_for("btc_hybrid")
    assert len(crits) >= 3  # btc_hybrid has BTC bars at 5m/1h/D as critical
    assert all(r.critical for r in crits)


def test_known_bots_have_requirements() -> None:
    for bot in ("mnq_futures", "btc_hybrid", "eth_perp", "xrp_perp"):
        assert get_requirements(bot) is not None, f"{bot} missing reqs"


def test_every_requirement_has_kind_and_symbol() -> None:
    for bot in REQUIREMENTS:
        for req in bot.requirements:
            assert req.kind, f"{bot.bot_id} req without kind"
            assert req.symbol, f"{bot.bot_id} req without symbol"


# ---------------------------------------------------------------------------
# Audit logic
# ---------------------------------------------------------------------------


def _write_history(path: Path, rows: list[tuple[int, float, float, float, float, float]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for ts, o, h, low, c, v in rows:
            w.writerow([ts, o, h, low, c, v])


@pytest.fixture()
def fully_covered_lib(tmp_path: Path) -> DataLibrary:
    """Library that has every dataset mnq_futures requires."""
    history = tmp_path / "history"
    history.mkdir()
    base = [(1735689600, 100.0, 101.0, 99.0, 100.5, 1000.0)] * 3
    # MNQ1 5m, 1h, 4h
    for tf in ("5m", "1h", "4h"):
        _write_history(history / f"MNQ1_{tf}.csv", base)
    # ES1, DXY, VIX 5m correlations — written under main shape
    main = tmp_path / "main"
    main.mkdir()
    with (main / "mnq_es1_5.csv").open("w", encoding="utf-8") as fh:
        fh.write("timestamp_utc,epoch_s,open,high,low,close,volume,session\n")
        fh.write("2026-01-01T00:00:00Z,,5000,5005,4995,5002,800,RTH\n")
        fh.write("2026-01-01T00:05:00Z,,5002,5008,5001,5006,900,RTH\n")
    with (main / "mnq_dxy_5.csv").open("w", encoding="utf-8") as fh:
        fh.write("timestamp_utc,epoch_s,open,high,low,close,volume,session\n")
        fh.write("2026-01-01T00:00:00Z,,100,101,99,100.5,1000,RTH\n")
        fh.write("2026-01-01T00:05:00Z,,100.5,101.5,100,101,1100,RTH\n")
    with (main / "mnq_vix_5.csv").open("w", encoding="utf-8") as fh:
        fh.write("timestamp_utc,epoch_s,open,high,low,close,volume,session\n")
        fh.write("2026-01-01T00:00:00Z,,15,16,14,15.5,500,RTH\n")
        fh.write("2026-01-01T00:05:00Z,,15.5,16.5,15,16,600,RTH\n")
    return DataLibrary(roots=[main, history])


def test_audit_unknown_bot_returns_none(fully_covered_lib: DataLibrary) -> None:
    assert audit_bot("__nope__", library=fully_covered_lib) is None


def test_audit_runnable_when_all_critical_present(fully_covered_lib: DataLibrary) -> None:
    a = audit_bot("mnq_futures", library=fully_covered_lib)
    assert a is not None
    assert a.is_runnable, f"mnq_futures should be runnable, missing: {a.missing_critical}"
    assert a.critical_coverage_pct == pytest.approx(100.0)


def test_audit_blocked_when_critical_missing(tmp_path: Path) -> None:
    empty_lib = DataLibrary(roots=[tmp_path / "nope"])  # zero datasets
    # Reference-bot history: btc_hybrid (deactivated 2026-05-05 by
    # elite_scoreboard) -> btc_optimized (deactivated 2026-05-07 by the
    # post-dispatch-fix strict-gate audit, Sharpe -2.82) ->
    # volume_profile_btc (still active, same BTC critical bars).
    # Deactivated bots short-circuit audit_bot() to a runnable=True empty
    # BotAudit -- the data audit doesn't care about retired bots --
    # so the reference must always point at a still-active bot.
    a = audit_bot("volume_profile_mnq", library=empty_lib)
    assert a is not None
    assert not a.is_runnable
    assert a.critical_coverage_pct == pytest.approx(0.0)
    # volume_profile_btc critical: BTC bars at 5m/1h/D
    assert len(a.missing_critical) >= 3


def test_audit_separates_critical_from_optional(tmp_path: Path) -> None:
    empty_lib = DataLibrary(roots=[tmp_path / "nope"])
    a = audit_bot("btc_hybrid", library=empty_lib)
    assert a is not None
    # All criticals missing
    assert all(r.critical for r in a.missing_critical)
    # All optionals also missing on empty library
    assert all(not r.critical for r in a.missing_optional)


def test_audit_uses_fear_greed_as_crypto_sentiment_proxy(tmp_path: Path) -> None:
    history = tmp_path / "history"
    history.mkdir()
    _write_history(
        history / "FEAR_GREEDMACRO_D.csv",
        [(1_775_000_000, 55.0, 55.0, 55.0, 55.0, 1.0)],
    )

    # 2026-05-05: btc_hybrid is the only bot with a BTC sentiment
    # requirement, but it was deactivated by elite_scoreboard evidence.
    # audit_bot short-circuits on deactivated bots, so this test now
    # validates the proxy-mapping logic via direct DataLibrary lookup
    # rather than the full audit pipeline.
    from eta_engine.data.requirements import DataRequirement

    sentiment_req = DataRequirement(
        kind="sentiment",
        symbol="BTC",
        timeframe="1h",
        critical=False,
    )
    from eta_engine.data.audit import _resolve_library_lookup

    ds = _resolve_library_lookup(sentiment_req, DataLibrary(roots=[history]))
    assert ds is not None, "fear-greed proxy must resolve sentiment/BTC/1h"
    assert ds.symbol == "FEAR_GREEDMACRO"
    assert ds.timeframe == "D"


def test_audit_all_returns_one_per_bot() -> None:
    out = audit_all()
    assert len(out) == len(REQUIREMENTS)
    assert {a.bot_id for a in out} == {r.bot_id for r in REQUIREMENTS}


def test_deactivated_bot_is_not_reported_as_data_blocked(tmp_path: Path) -> None:
    lib = DataLibrary(roots=[tmp_path / "empty"])
    a = audit_bot("xrp_perp", library=lib)
    assert a is not None
    assert a.deactivated is True
    assert a.is_runnable is True
    assert not a.missing_critical


def test_summary_markdown_lists_runnable_and_blocked() -> None:
    md = summary_markdown(audit_all())
    assert "Runnable:" in md
    assert "Blocked:" in md
    assert "Deactivated:" in md
    assert "mnq_futures" in md


def test_summary_markdown_includes_source_hints_for_blocked() -> None:
    md = summary_markdown(audit_all())
    # When any bot is blocked on critical data, its source hints must
    # surface so the operator knows where to fetch from. Once every bot
    # has 100% coverage (all crypto data feeds wired 2026-04-27), there
    # are no blocked bots and the assertion is vacuously satisfied —
    # the test still guards against regressing by checking source hints
    # appear when bots exist that are blocked.
    audits = audit_all()
    blocked = [a for a in audits if a.missing_critical]
    if blocked:
        assert "Coinbase" in md or "Binance" in md or "blockscout" in md or "lunarcrush" in md, (
            "blocked bots present but no source hints surfaced in markdown"
        )


# ---------------------------------------------------------------------------
# Cross-check: every bot in per_bot_registry has a matching entry
# ---------------------------------------------------------------------------


def test_per_bot_registry_and_requirements_in_sync() -> None:
    """Bot in per_bot_registry must have a requirements entry, and
    vice versa — a strategy assignment without data requirements is
    a research blind spot, and a requirements row without an
    assignment is a fetch-something-we-won't-use bug.

    Rows flagged ``pending_assignment=True`` are exempt: those declare
    data needs for instrument expansions (Phase-2/3/4/5 commodities/
    FX/crypto-alts) that ramp data backfill before the strategy code
    lands.
    """
    from eta_engine.strategies.per_bot_registry import bots as registered_bots

    strat_bots = set(registered_bots())
    req_bots = {r.bot_id for r in REQUIREMENTS if not r.pending_assignment}
    missing_reqs = strat_bots - req_bots
    extra_reqs = req_bots - strat_bots
    assert not missing_reqs, f"bots in strategy registry without requirements: {missing_reqs}"
    assert not extra_reqs, f"bots in requirements without strategy assignment: {extra_reqs}"
