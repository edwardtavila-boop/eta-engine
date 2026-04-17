"""
EVOLUTIONARY TRADING ALGO  //  tests.test_btc_live
======================================
Unit tests for the live-gate decision function.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eta_engine.scripts.btc_live import (
    LiveGateDecision,
    evaluate_live_gate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


NOW = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)


def _write_verify(
    tmp_path: Path,
    *,
    verdict: str = "PASS",
    ended_utc: datetime | None = None,
) -> Path:
    ended_utc = ended_utc or (NOW - timedelta(hours=2))
    path = tmp_path / "btc_paper_run_latest.json"
    path.write_text(
        json.dumps({"verdict": verdict, "ended_utc": ended_utc.isoformat()}),
        encoding="utf-8",
    )
    return path


def _probe_ok() -> bool:
    return True


def _probe_missing() -> bool:
    return False


# ---------------------------------------------------------------------------
# Default-paper paths
# ---------------------------------------------------------------------------


def test_default_is_paper_without_live_flag(tmp_path: Path) -> None:
    verify = _write_verify(tmp_path)
    decision = evaluate_live_gate(
        want_live=False,
        env={"APEX_BTC_LIVE": "1"},
        verify_path=verify,
        now=NOW,
        adapter_probe=_probe_ok,
    )
    assert decision.mode == "PAPER"
    assert decision.allow_live is False
    assert any("paper requested" in r for r in decision.reasons)


def test_live_blocked_without_env_flag(tmp_path: Path) -> None:
    verify = _write_verify(tmp_path)
    decision = evaluate_live_gate(
        want_live=True,
        env={},  # missing APEX_BTC_LIVE
        verify_path=verify,
        now=NOW,
        adapter_probe=_probe_ok,
    )
    assert decision.mode == "PAPER"
    assert any("APEX_BTC_LIVE" in r for r in decision.reasons)


def test_live_blocked_when_env_flag_not_one(tmp_path: Path) -> None:
    verify = _write_verify(tmp_path)
    decision = evaluate_live_gate(
        want_live=True,
        env={"APEX_BTC_LIVE": "true"},
        verify_path=verify,
        now=NOW,
        adapter_probe=_probe_ok,
    )
    assert decision.mode == "PAPER"
    assert any("APEX_BTC_LIVE" in r for r in decision.reasons)


# ---------------------------------------------------------------------------
# Verification artifact gate
# ---------------------------------------------------------------------------


def test_live_blocked_when_verify_missing(tmp_path: Path) -> None:
    decision = evaluate_live_gate(
        want_live=True,
        env={"APEX_BTC_LIVE": "1"},
        verify_path=tmp_path / "no_such_file.json",
        now=NOW,
        adapter_probe=_probe_ok,
    )
    assert decision.mode == "PAPER"
    assert any("no paper verification artifact" in r for r in decision.reasons)


def test_live_blocked_when_verdict_fail(tmp_path: Path) -> None:
    verify = _write_verify(tmp_path, verdict="FAIL")
    decision = evaluate_live_gate(
        want_live=True,
        env={"APEX_BTC_LIVE": "1"},
        verify_path=verify,
        now=NOW,
        adapter_probe=_probe_ok,
    )
    assert decision.mode == "PAPER"
    assert any("need PASS" in r for r in decision.reasons)
    assert decision.verify_verdict == "FAIL"


def test_live_blocked_when_verify_too_old(tmp_path: Path) -> None:
    verify = _write_verify(
        tmp_path,
        verdict="PASS",
        ended_utc=NOW - timedelta(hours=72),  # > 48h
    )
    decision = evaluate_live_gate(
        want_live=True,
        env={"APEX_BTC_LIVE": "1"},
        verify_path=verify,
        now=NOW,
        adapter_probe=_probe_ok,
        max_age_h=48.0,
    )
    assert decision.mode == "PAPER"
    assert any("old" in r for r in decision.reasons)
    assert decision.verify_age_h is not None
    assert decision.verify_age_h > 48.0


def test_live_blocked_when_ended_utc_missing(tmp_path: Path) -> None:
    verify = tmp_path / "btc_paper_run_latest.json"
    verify.write_text(json.dumps({"verdict": "PASS"}), encoding="utf-8")
    decision = evaluate_live_gate(
        want_live=True,
        env={"APEX_BTC_LIVE": "1"},
        verify_path=verify,
        now=NOW,
        adapter_probe=_probe_ok,
    )
    assert decision.mode == "PAPER"
    assert any("ended_utc" in r for r in decision.reasons)


def test_verify_path_round_trips_through_decision(tmp_path: Path) -> None:
    verify = _write_verify(tmp_path)
    decision = evaluate_live_gate(
        want_live=True,
        env={"APEX_BTC_LIVE": "1"},
        verify_path=verify,
        now=NOW,
        adapter_probe=_probe_ok,
    )
    assert decision.verify_path == verify


def test_verify_corrupt_json_is_treated_as_missing(tmp_path: Path) -> None:
    verify = tmp_path / "btc_paper_run_latest.json"
    verify.write_text("not-json-at-all", encoding="utf-8")
    decision = evaluate_live_gate(
        want_live=True,
        env={"APEX_BTC_LIVE": "1"},
        verify_path=verify,
        now=NOW,
        adapter_probe=_probe_ok,
    )
    assert decision.mode == "PAPER"
    assert any("no paper verification artifact" in r for r in decision.reasons)


# ---------------------------------------------------------------------------
# Adapter probe
# ---------------------------------------------------------------------------


def test_live_blocked_when_adapter_missing(tmp_path: Path) -> None:
    verify = _write_verify(tmp_path)
    decision = evaluate_live_gate(
        want_live=True,
        env={"APEX_BTC_LIVE": "1"},
        verify_path=verify,
        now=NOW,
        adapter_probe=_probe_missing,
    )
    assert decision.mode == "PAPER"
    assert any("bybit venue adapter" in r for r in decision.reasons)


# ---------------------------------------------------------------------------
# All four gates pass
# ---------------------------------------------------------------------------


def test_live_allowed_when_all_gates_pass(tmp_path: Path) -> None:
    verify = _write_verify(tmp_path)
    decision = evaluate_live_gate(
        want_live=True,
        env={"APEX_BTC_LIVE": "1"},
        verify_path=verify,
        now=NOW,
        adapter_probe=_probe_ok,
    )
    assert decision.mode == "LIVE"
    assert decision.allow_live is True
    assert decision.reasons == ()
    assert decision.verify_verdict == "PASS"
    assert decision.verify_age_h == pytest.approx(2.0, rel=0.1)


# ---------------------------------------------------------------------------
# as_dict serialization
# ---------------------------------------------------------------------------


def test_decision_as_dict_is_json_safe(tmp_path: Path) -> None:
    verify = _write_verify(tmp_path)
    decision = evaluate_live_gate(
        want_live=True,
        env={"APEX_BTC_LIVE": "1"},
        verify_path=verify,
        now=NOW,
        adapter_probe=_probe_ok,
    )
    payload = decision.as_dict()
    # Must be json-serializable (verify_path is stringified)
    serialized = json.dumps(payload)
    round_trip = json.loads(serialized)
    assert round_trip["allow_live"] is True
    assert round_trip["mode"] == "LIVE"
    assert round_trip["verify_verdict"] == "PASS"


def test_live_gate_decision_is_frozen() -> None:
    decision = LiveGateDecision(
        allow_live=False,
        mode="PAPER",
        reasons=("x",),
        verify_path=Path("/tmp/x"),
        verify_verdict=None,
        verify_age_h=None,
    )
    with pytest.raises((AttributeError, TypeError)):
        decision.allow_live = True  # type: ignore[misc]
