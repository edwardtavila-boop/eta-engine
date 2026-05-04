"""Tests for scripts.paper_soak_mnq_orb — the IBKR paper-soak prep script.

Coverage focuses on the deterministic pieces:
* session-date calendar (weekend / holiday filtering)
* registry-check failure paths
* expected-trade-band math
* SoakPlan serialization

The IBKR pre-flight is exercised only at the import-error / config-
missing branch — the real preflight needs a live gateway, which the
unit suite intentionally doesn't.
"""

from __future__ import annotations

from datetime import date

import pytest

from eta_engine.scripts.paper_soak_mnq_orb import (
    SoakPlan,
    _expected_trade_band,
    _ibkr_preflight,
    _redact_account,
    _registry_check,
    _session_dates,
    build_plan,
)

# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def test_session_dates_skip_weekends() -> None:
    # Mon 2026-04-27 .. Sun 2026-05-03
    out = _session_dates(date(2026, 4, 27), days=7)
    expected_weekdays = {0, 1, 2, 3, 4}
    for d in out:
        assert d.weekday() in expected_weekdays
    assert len(out) == 5  # Mon-Fri only


def test_session_dates_skip_holidays() -> None:
    # Memorial Day 2026 = Mon May 25 — should be excluded.
    out = _session_dates(date(2026, 5, 25), days=7)
    assert date(2026, 5, 25) not in out
    # The window should still produce 4 sessions (Tue-Fri).
    assert len(out) == 4


def test_session_dates_zero_when_window_all_weekend() -> None:
    # Sat 2026-05-09 + Sun 2026-05-10 — zero sessions
    out = _session_dates(date(2026, 5, 9), days=2)
    assert out == []


# ---------------------------------------------------------------------------
# Trade-band math
# ---------------------------------------------------------------------------


def test_expected_trade_band_midpoint_one_per_session() -> None:
    lo, hi = _expected_trade_band(10)
    assert lo == 5
    assert hi == 10


def test_expected_trade_band_minimum_one() -> None:
    """Even on a 1-session soak, lower bound is at least 1."""
    lo, _ = _expected_trade_band(1)
    assert lo == 1


# ---------------------------------------------------------------------------
# Account redaction
# ---------------------------------------------------------------------------


def test_redact_account_truncates_long_id() -> None:
    assert _redact_account("DUH1234567") == "DUH***4567"


def test_redact_account_handles_empty() -> None:
    assert _redact_account("") == ""


def test_redact_account_short_id() -> None:
    assert _redact_account("DUH12") == "***"


# ---------------------------------------------------------------------------
# Registry check
# ---------------------------------------------------------------------------


def test_registry_check_passes_for_active_mnq_orb_bot() -> None:
    """Live registry must keep an MNQ ORB-family bot wired in or this fails.

    Guards against a future PR silently dropping the MNQ ORB promotion.
    Originally this asserted on ``mnq_futures``, but DIAMOND CUT 2026-05-02
    deprecated that bot in favor of ``mnq_futures_sage``. The active MNQ
    ORB bot is now the sage-overlay variant. If THIS test fails, decide
    deliberately: either fix the registry, or update the test with a
    justification in the commit.
    """
    ok, msg, extras = _registry_check("mnq_futures_sage")
    assert ok, msg
    assert extras["bot_id"] == "mnq_futures_sage"
    assert extras["strategy_id"] == "mnq_orb_sage_v1"
    assert "baseline" in extras


def test_registry_check_rejects_unknown_bot() -> None:
    ok, msg, _ = _registry_check("does_not_exist")
    assert not ok
    assert "does_not_exist" in msg


def test_registry_check_rejects_non_orb_kind() -> None:
    """A bot wired to confluence/drb/etc. should fail the soak gate.

    nq_daily_drb is real and uses strategy_kind='drb'; the soak script
    only supports orb / orb_sage_gated strategies right now.
    """
    ok, msg, _ = _registry_check("nq_daily_drb")
    assert not ok
    assert "drb" in msg or "expected" in msg


# ---------------------------------------------------------------------------
# IBKR pre-flight (no-gateway path)
# ---------------------------------------------------------------------------


def test_ibkr_preflight_returns_well_formed_tuple() -> None:
    """The pre-flight always returns a (bool, str, dict) triple.

    Outcome (pass / fail) depends on the test environment: a CI box
    with no IBKR_* env vars sees fail-closed; a developer machine with
    a configured paper account sees pass. Both are valid — what we
    assert here is the contract: tuple shape + non-empty reason.
    """
    ok, msg, extras = _ibkr_preflight()
    assert isinstance(ok, bool)
    assert isinstance(msg, str) and msg
    assert isinstance(extras, dict)


def test_ibkr_preflight_fails_closed_without_account_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object,
) -> None:
    """When no IBKR creds are reachable, preflight must fail closed.

    The IBKR adapter falls back to disk-stored secrets at the runtime
    root, so to force a fail-closed run we (a) strip every IBKR_* env
    var and (b) repoint FIRM_RUNTIME_ROOT at an empty tmp dir so the
    fallback loader finds nothing on disk either.
    """
    import os

    for name in list(os.environ.keys()):
        if name.startswith("IBKR_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FIRM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("ETA_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))  # blocks Path.home() fallback
    ok, msg, _extras = _ibkr_preflight()
    assert not ok
    assert "IBKR" in msg or "account" in msg.lower()


# ---------------------------------------------------------------------------
# Plan exit codes
# ---------------------------------------------------------------------------


def test_build_plan_zero_sessions_exits_3() -> None:
    """A start date that lands in an all-weekend window → exit 3.

    Pinned to ``mnq_futures_sage`` (active sage-overlay bot) since the
    original ``mnq_futures`` is deprecated — passing the deprecated bot
    short-circuits at the registry-check step (exit 1) before ever
    reaching the zero-sessions check this test exercises.
    """
    with pytest.raises(SystemExit) as ei:
        build_plan(date(2026, 5, 9), days=2, bot_id="mnq_futures_sage")  # Sat + Sun
    assert ei.value.code == 3


# ---------------------------------------------------------------------------
# SoakPlan serialization
# ---------------------------------------------------------------------------


def test_soak_plan_to_dict_roundtrip_keys() -> None:
    p = SoakPlan(
        bot_id="mnq_futures", strategy_id="mnq_orb_v2",
        symbol="MNQ1", timeframe="5m",
        start_date=date(2026, 4, 27), end_date=date(2026, 5, 8),
        rth_session_dates=[date(2026, 4, 27)],
        venue="ibkr_paper", account_id_redacted="DUH***1234",
        expected_trades_lower=5, expected_trades_upper=10,
        pinned_baseline={"n_trades": 41, "win_rate": 0.488},
    )
    d = p.to_dict()
    for k in (
        "bot_id", "strategy_id", "symbol", "timeframe",
        "start_date", "end_date", "rth_session_dates",
        "n_sessions", "venue", "account_id_redacted",
        "expected_trades_lower", "expected_trades_upper",
        "pinned_baseline", "emitted_at_utc",
    ):
        assert k in d
    assert d["n_sessions"] == 1
    assert d["start_date"] == "2026-04-27"
