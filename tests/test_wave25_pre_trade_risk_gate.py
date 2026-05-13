"""Tests for the wave-25 pre-trade risk gate + lifecycle state.

The gate decides whether a signal goes to:
  - the live broker (target="live")
  - the paper-trading simulator (target="paper")
  - the bin (target="reject")

It composes three checks:
  1. Per-bot lifecycle state (EVAL_LIVE / EVAL_PAPER / FUNDED_LIVE / RETIRED)
  2. Wave-22 prop drawdown guard (HALT/WATCH/OK)
  3. Pre-trade risk vs the live drawdown buffers
"""

# ruff: noqa: PLR2004, SLF001
from __future__ import annotations

import json
from pathlib import Path


def _write_guard_state(
    path: Path,
    *,
    daily_buffer: float = 1500.0,
    daily_limit: float = 1500.0,
    static_buffer: float = 2500.0,
    signal: str = "OK",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ts": "2026-05-13T00:00:00+00:00",
                "account_size": 50_000.0,
                "daily_dd_check": {
                    "limit_usd": daily_limit,
                    "used_usd": daily_limit - daily_buffer,
                    "buffer_usd": daily_buffer,
                    "status": signal if daily_buffer == 0 else "OK",
                },
                "static_dd_check": {
                    "limit_usd": 2500.0,
                    "used_usd": 2500.0 - static_buffer,
                    "buffer_usd": static_buffer,
                    "status": signal if static_buffer == 0 else "OK",
                },
                "signal": signal,
            },
        ),
        encoding="utf-8",
    )


# ────────────────────────────────────────────────────────────────────
# evaluate_pre_trade_risk
# ────────────────────────────────────────────────────────────────────


def test_pre_trade_risk_allow_when_buffer_large(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """Small prospective loss vs a large buffer = allow."""
    from eta_engine.feeds import capital_allocator as ca

    p = tmp_path / "guard.json"
    _write_guard_state(p, daily_buffer=1500.0)
    monkeypatch.setattr(ca, "PROP_DRAWDOWN_GUARD_RECEIPT", p)

    verdict, reason = ca.evaluate_pre_trade_risk(
        "m2k_sweep_reclaim",
        prospective_loss_usd=100.0,
    )
    assert verdict == "allow_live", reason


def test_pre_trade_risk_routes_to_paper_at_soft_threshold(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """Loss >= 50% of daily limit triggers route_to_paper."""
    from eta_engine.feeds import capital_allocator as ca

    p = tmp_path / "guard.json"
    _write_guard_state(p, daily_buffer=1500.0, daily_limit=1500.0)
    monkeypatch.setattr(ca, "PROP_DRAWDOWN_GUARD_RECEIPT", p)

    # Soft threshold = 50% × $1500 = $750
    verdict, reason = ca.evaluate_pre_trade_risk(
        "m2k_sweep_reclaim",
        prospective_loss_usd=800.0,
    )
    assert verdict == "route_to_paper", reason
    assert "soft_dd" in reason


def test_pre_trade_risk_rejects_when_loss_exceeds_daily_buffer(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """Loss >= daily buffer = hard reject."""
    from eta_engine.feeds import capital_allocator as ca

    p = tmp_path / "guard.json"
    _write_guard_state(p, daily_buffer=400.0, daily_limit=1500.0)
    monkeypatch.setattr(ca, "PROP_DRAWDOWN_GUARD_RECEIPT", p)

    verdict, reason = ca.evaluate_pre_trade_risk(
        "m2k_sweep_reclaim",
        prospective_loss_usd=500.0,
    )
    assert verdict == "reject", reason
    assert "daily_dd" in reason


def test_pre_trade_risk_rejects_when_loss_exceeds_static_buffer(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """Loss >= static buffer = hard reject (account-blow risk)."""
    from eta_engine.feeds import capital_allocator as ca

    p = tmp_path / "guard.json"
    _write_guard_state(p, daily_buffer=500.0, static_buffer=200.0)
    monkeypatch.setattr(ca, "PROP_DRAWDOWN_GUARD_RECEIPT", p)

    verdict, reason = ca.evaluate_pre_trade_risk(
        "m2k_sweep_reclaim",
        prospective_loss_usd=300.0,
    )
    assert verdict == "reject", reason
    assert "static_dd" in reason


def test_pre_trade_risk_no_guard_state_fails_open(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """Missing guard receipt → fail open (allow). The wave-22 prop guard
    layer above this would have blocked already if anything was wrong."""
    from eta_engine.feeds import capital_allocator as ca

    monkeypatch.setattr(ca, "PROP_DRAWDOWN_GUARD_RECEIPT", tmp_path / "missing.json")
    verdict, _ = ca.evaluate_pre_trade_risk(
        "m2k_sweep_reclaim",
        prospective_loss_usd=999_999.0,
    )
    assert verdict == "allow_live"


# ────────────────────────────────────────────────────────────────────
# Lifecycle state
# ────────────────────────────────────────────────────────────────────


def test_lifecycle_defaults_to_eval_paper(tmp_path: Path, monkeypatch: object) -> None:
    """Bots without an explicit entry default to EVAL_PAPER."""
    from eta_engine.feeds import capital_allocator as ca

    monkeypatch.setattr(ca, "BOT_LIFECYCLE_STATE_PATH", tmp_path / "lifecycle.json")
    assert ca.get_bot_lifecycle("anything") == ca.LIFECYCLE_EVAL_PAPER


def test_lifecycle_set_persists_and_reads_back(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    from eta_engine.feeds import capital_allocator as ca

    p = tmp_path / "lifecycle.json"
    monkeypatch.setattr(ca, "BOT_LIFECYCLE_STATE_PATH", p)

    ca.set_bot_lifecycle("m2k", ca.LIFECYCLE_EVAL_LIVE)
    assert ca.get_bot_lifecycle("m2k") == ca.LIFECYCLE_EVAL_LIVE

    # Update
    ca.set_bot_lifecycle("m2k", ca.LIFECYCLE_RETIRED)
    assert ca.get_bot_lifecycle("m2k") == ca.LIFECYCLE_RETIRED

    # File on disk
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["bots"]["m2k"] == ca.LIFECYCLE_RETIRED


def test_lifecycle_invalid_state_raises(tmp_path: Path, monkeypatch: object) -> None:
    import pytest

    from eta_engine.feeds import capital_allocator as ca

    monkeypatch.setattr(ca, "BOT_LIFECYCLE_STATE_PATH", tmp_path / "lifecycle.json")
    with pytest.raises(ValueError, match="unknown lifecycle"):
        ca.set_bot_lifecycle("m2k", "INVALID_STATE")


def test_lifecycle_set_is_idempotent(tmp_path: Path, monkeypatch: object) -> None:
    """Repeated set_bot_lifecycle with the same value returns False and
    does not touch the file (no wasted write, no mtime bump)."""
    from eta_engine.feeds import capital_allocator as ca

    p = tmp_path / "lifecycle.json"
    monkeypatch.setattr(ca, "BOT_LIFECYCLE_STATE_PATH", p)

    # First call: file written, returns True.
    changed = ca.set_bot_lifecycle("m2k", ca.LIFECYCLE_EVAL_LIVE)
    assert changed is True
    mtime_first = p.stat().st_mtime_ns

    # Second call with same value: no-op, returns False, mtime unchanged.
    changed_again = ca.set_bot_lifecycle("m2k", ca.LIFECYCLE_EVAL_LIVE)
    assert changed_again is False
    assert p.stat().st_mtime_ns == mtime_first, "idempotent set should not rewrite the file"


def test_lifecycle_atomic_write_no_stale_tmp(tmp_path: Path, monkeypatch: object) -> None:
    """After set_bot_lifecycle, the temp file used for atomic replace
    must NOT linger on disk (would clutter the state dir and could
    cause confusion on subsequent reads)."""
    from eta_engine.feeds import capital_allocator as ca

    p = tmp_path / "lifecycle.json"
    monkeypatch.setattr(ca, "BOT_LIFECYCLE_STATE_PATH", p)

    ca.set_bot_lifecycle("m2k", ca.LIFECYCLE_EVAL_LIVE)
    tmp = p.with_suffix(p.suffix + ".tmp")
    assert not tmp.exists(), f"stale temp file lingered: {tmp}"
    assert p.exists()


def test_lifecycle_corrupt_file_does_not_crash_read(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """A corrupt lifecycle.json should NOT crash get_bot_lifecycle;
    defensive default = EVAL_PAPER."""
    from eta_engine.feeds import capital_allocator as ca

    p = tmp_path / "lifecycle.json"
    monkeypatch.setattr(ca, "BOT_LIFECYCLE_STATE_PATH", p)
    p.write_text("not-json-at-all", encoding="utf-8")
    assert ca.get_bot_lifecycle("m2k") == ca.LIFECYCLE_EVAL_PAPER


def test_lifecycle_corrupt_file_does_not_crash_write(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """A corrupt lifecycle.json should NOT crash set_bot_lifecycle;
    the writer rebuilds from scratch rather than silently merging."""
    from eta_engine.feeds import capital_allocator as ca

    p = tmp_path / "lifecycle.json"
    monkeypatch.setattr(ca, "BOT_LIFECYCLE_STATE_PATH", p)
    p.write_text("not-json-at-all", encoding="utf-8")
    # Should not raise
    ca.set_bot_lifecycle("m2k", ca.LIFECYCLE_EVAL_LIVE)
    assert ca.get_bot_lifecycle("m2k") == ca.LIFECYCLE_EVAL_LIVE
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["bots"]["m2k"] == ca.LIFECYCLE_EVAL_LIVE


# ────────────────────────────────────────────────────────────────────
# resolve_execution_target — composite gate
# ────────────────────────────────────────────────────────────────────


def test_target_retired_lifecycle_rejects(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    from eta_engine.feeds import capital_allocator as ca

    p = tmp_path / "lifecycle.json"
    monkeypatch.setattr(ca, "BOT_LIFECYCLE_STATE_PATH", p)
    monkeypatch.setattr(ca, "PROP_DRAWDOWN_GUARD_RECEIPT", tmp_path / "missing.json")
    ca.set_bot_lifecycle("m2k", ca.LIFECYCLE_RETIRED)

    target, reason = ca.resolve_execution_target("m2k", prospective_loss_usd=100.0)
    assert target == "reject"
    assert "retired" in reason


def test_target_eval_paper_lifecycle_routes_paper(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """EVAL_PAPER short-circuits to paper before any other check."""
    from eta_engine.feeds import capital_allocator as ca

    p = tmp_path / "lifecycle.json"
    monkeypatch.setattr(ca, "BOT_LIFECYCLE_STATE_PATH", p)
    monkeypatch.setattr(ca, "PROP_DRAWDOWN_GUARD_RECEIPT", tmp_path / "missing.json")
    ca.set_bot_lifecycle("m2k", ca.LIFECYCLE_EVAL_PAPER)

    target, reason = ca.resolve_execution_target("m2k", prospective_loss_usd=100.0)
    assert target == "paper"
    assert "eval_paper" in reason


def test_target_eval_live_with_safe_buffer_routes_live(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    from eta_engine.feeds import capital_allocator as ca

    lp = tmp_path / "lifecycle.json"
    gp = tmp_path / "guard.json"
    monkeypatch.setattr(ca, "BOT_LIFECYCLE_STATE_PATH", lp)
    monkeypatch.setattr(ca, "PROP_DRAWDOWN_GUARD_RECEIPT", gp)
    _write_guard_state(gp, daily_buffer=1500.0)
    ca.set_bot_lifecycle("m2k", ca.LIFECYCLE_EVAL_LIVE)

    target, _ = ca.resolve_execution_target("m2k", prospective_loss_usd=100.0)
    assert target == "live"


def test_target_eval_live_with_soft_breach_routes_paper(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """EVAL_LIVE bot whose prospective loss trips the soft threshold
    falls back to paper instead of live."""
    from eta_engine.feeds import capital_allocator as ca

    lp = tmp_path / "lifecycle.json"
    gp = tmp_path / "guard.json"
    monkeypatch.setattr(ca, "BOT_LIFECYCLE_STATE_PATH", lp)
    monkeypatch.setattr(ca, "PROP_DRAWDOWN_GUARD_RECEIPT", gp)
    _write_guard_state(gp, daily_buffer=1500.0, daily_limit=1500.0)
    ca.set_bot_lifecycle("m2k", ca.LIFECYCLE_EVAL_LIVE)

    target, reason = ca.resolve_execution_target("m2k", prospective_loss_usd=800.0)
    assert target == "paper"
    assert "soft_dd" in reason


def test_target_eval_live_with_hard_breach_rejects(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    from eta_engine.feeds import capital_allocator as ca

    lp = tmp_path / "lifecycle.json"
    gp = tmp_path / "guard.json"
    monkeypatch.setattr(ca, "BOT_LIFECYCLE_STATE_PATH", lp)
    monkeypatch.setattr(ca, "PROP_DRAWDOWN_GUARD_RECEIPT", gp)
    _write_guard_state(gp, daily_buffer=400.0)
    ca.set_bot_lifecycle("m2k", ca.LIFECYCLE_EVAL_LIVE)

    target, reason = ca.resolve_execution_target("m2k", prospective_loss_usd=600.0)
    assert target == "reject"
    assert "daily_dd" in reason
