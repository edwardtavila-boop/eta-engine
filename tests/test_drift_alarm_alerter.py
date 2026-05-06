"""Tests for ``eta_engine.scripts.drift_alarm_alerter``.

Covers:
  * state file round-trip (load → save → load) with atomic .tmp+replace
  * select_bots_to_alert dedup window (within 1h: skipped; after 1h: re-alerts)
  * run_once: heartbeat (`updated_at`) and per-bot `last_alert_ts` are written
  * run_once: dashboard fetch failure ⇒ no alerts, but heartbeat still updated
  * run_once: dispatcher failure ⇒ bot is NOT marked as alerted in state
  * select_bots_to_alert ignores bots without drift_alarm=true
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eta_engine.scripts import drift_alarm_alerter as mod


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "drift_alert_state.json"


def _snap(*alarms: tuple[str, bool, float]) -> dict:
    """Build a snapshot dict from (bot_id, drift_alarm, drift_gap_pp) tuples."""
    return {
        "ready": True,
        "per_bot": {
            bot_id: {
                "drift_alarm": alarm,
                "drift_gap_pp": gap,
                "live_wr_today": 0.30,
                "backtest_wr_target": 0.55,
                "wins": 3,
                "losses": 7,
            }
            for bot_id, alarm, gap in alarms
        },
    }


# --------------------------------------------------------------------------- #
# load_state / save_state round-trip
# --------------------------------------------------------------------------- #
def test_load_state_returns_empty_when_missing(state_path: Path) -> None:
    assert mod.load_state(state_path) == {}


def test_save_then_load_round_trip(state_path: Path) -> None:
    payload = {"updated_at": "2026-05-06T00:00:00+00:00", "bots": {"btc_alpha": {"last_alert_ts": 12345.0}}}
    mod.save_state(payload, state_path)
    assert state_path.exists()
    assert mod.load_state(state_path) == payload


def test_save_state_uses_atomic_tmp_replace(state_path: Path) -> None:
    # Pre-create the file with a sentinel; ensure save_state replaces it
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text('{"old": true}', encoding="utf-8")
    mod.save_state({"new": True}, state_path)
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"new": True}
    # The .tmp file must be cleaned up after the replace
    assert not state_path.with_suffix(state_path.suffix + ".tmp").exists()


def test_load_state_handles_corrupt_json(state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("not-json{{{", encoding="utf-8")
    assert mod.load_state(state_path) == {}


# --------------------------------------------------------------------------- #
# select_bots_to_alert — dedup window
# --------------------------------------------------------------------------- #
def test_select_emits_for_fresh_drift_alarm() -> None:
    snap = _snap(("btc_alpha", True, 18.0), ("eth_quiet", False, 0.0))
    state: dict = {}
    out = mod.select_bots_to_alert(snap, state, dedup_s=3600, now_ts=10_000.0)
    assert [b for b, _ in out] == ["btc_alpha"]


def test_select_skips_within_dedup_window() -> None:
    snap = _snap(("btc_alpha", True, 18.0))
    state = {"bots": {"btc_alpha": {"last_alert_ts": 10_000.0}}}
    # 30 minutes after last alert, with a 1h dedup ⇒ skip
    out = mod.select_bots_to_alert(snap, state, dedup_s=3600, now_ts=10_000.0 + 1800.0)
    assert out == []


def test_select_re_alerts_after_dedup_window() -> None:
    snap = _snap(("btc_alpha", True, 18.0))
    state = {"bots": {"btc_alpha": {"last_alert_ts": 10_000.0}}}
    # 1h + 1s after last alert ⇒ re-alert
    out = mod.select_bots_to_alert(snap, state, dedup_s=3600, now_ts=10_000.0 + 3601.0)
    assert [b for b, _ in out] == ["btc_alpha"]


def test_select_ignores_bots_without_drift_alarm() -> None:
    snap = _snap(("btc_alpha", False, 2.0), ("eth_alpha", False, 0.5))
    out = mod.select_bots_to_alert(snap, {}, dedup_s=3600, now_ts=10_000.0)
    assert out == []


def test_select_handles_empty_per_bot() -> None:
    out = mod.select_bots_to_alert({"ready": True, "per_bot": {}}, {}, dedup_s=3600, now_ts=10_000.0)
    assert out == []


def test_select_handles_missing_per_bot_key() -> None:
    out = mod.select_bots_to_alert({"ready": False, "error": "boom"}, {}, dedup_s=3600, now_ts=10_000.0)
    assert out == []


# --------------------------------------------------------------------------- #
# run_once — heartbeat + state writes
# --------------------------------------------------------------------------- #
def test_run_once_writes_heartbeat_and_alert_state(state_path: Path) -> None:
    snap = _snap(("btc_alpha", True, 22.0))
    sent: list[tuple[str, dict]] = []

    def fake_fetcher(_url: str | None = None) -> dict:
        return snap

    def fake_dispatcher(bot_id: str, info: dict) -> bool:
        sent.append((bot_id, info))
        return True

    summary = mod.run_once(
        state_path=state_path,
        dedup_s=3600,
        now_ts=20_000.0,
        fetcher=fake_fetcher,
        dispatcher=fake_dispatcher,
    )
    assert sent and sent[0][0] == "btc_alpha"
    assert summary["alerted"] == ["btc_alpha"]
    state = mod.load_state(state_path)
    assert "updated_at" in state
    assert state["bots"]["btc_alpha"]["last_alert_ts"] == 20_000.0
    assert state["last_poll_ok"] is True


def test_run_once_no_alerts_still_writes_heartbeat(state_path: Path) -> None:
    def fake_fetcher(_url: str | None = None) -> dict:
        return _snap()  # empty per_bot

    def fake_dispatcher(*_args, **_kwargs) -> bool:  # pragma: no cover — never called
        raise AssertionError("dispatcher should not be called when no drift alarms")

    summary = mod.run_once(
        state_path=state_path,
        dedup_s=3600,
        now_ts=21_000.0,
        fetcher=fake_fetcher,
        dispatcher=fake_dispatcher,
    )
    assert summary["alerted"] == []
    state = mod.load_state(state_path)
    assert "updated_at" in state
    assert state["last_poll_ts"] == 21_000.0


def test_run_once_dashboard_failure_keeps_loop_alive(state_path: Path) -> None:
    def fake_fetcher(_url: str | None = None) -> dict:
        return {}  # simulates fail-soft from fetch_per_bot_snapshot

    summary = mod.run_once(
        state_path=state_path,
        dedup_s=3600,
        now_ts=22_000.0,
        fetcher=fake_fetcher,
        dispatcher=lambda *a, **k: True,  # noqa: ARG005
    )
    assert summary["alerted"] == []
    state = mod.load_state(state_path)
    # heartbeat is updated even on dashboard failure
    assert state["last_poll_ts"] == 22_000.0
    assert state["last_poll_ok"] is False


def test_run_once_does_not_dedup_when_dispatch_fails(state_path: Path) -> None:
    """If the dispatcher returns False, the bot must NOT be marked alerted."""
    snap = _snap(("btc_alpha", True, 30.0))
    calls: list[str] = []

    def fake_fetcher(_url: str | None = None) -> dict:
        return snap

    def failing_dispatcher(bot_id: str, _info: dict) -> bool:
        calls.append(bot_id)
        return False

    mod.run_once(
        state_path=state_path,
        dedup_s=3600,
        now_ts=30_000.0,
        fetcher=fake_fetcher,
        dispatcher=failing_dispatcher,
    )
    state = mod.load_state(state_path)
    assert calls == ["btc_alpha"]
    # Dispatch failed ⇒ no last_alert_ts written ⇒ next run_once will retry
    assert "btc_alpha" not in (state.get("bots") or {})


def test_run_once_dispatcher_exception_is_swallowed(state_path: Path) -> None:
    snap = _snap(("btc_alpha", True, 30.0))

    def fake_fetcher(_url: str | None = None) -> dict:
        return snap

    def crashing_dispatcher(*_args, **_kwargs) -> bool:
        raise RuntimeError("telegram api 500")

    summary = mod.run_once(
        state_path=state_path,
        dedup_s=3600,
        now_ts=31_000.0,
        fetcher=fake_fetcher,
        dispatcher=crashing_dispatcher,
    )
    assert summary["alerted"] == []
    state = mod.load_state(state_path)
    assert state["last_poll_ts"] == 31_000.0
    # Crashing dispatch ⇒ bot not marked alerted
    assert "btc_alpha" not in (state.get("bots") or {})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def test_format_message_includes_drift_pp() -> None:
    title, body = mod._format_message(
        "btc_alpha",
        {
            "drift_gap_pp": 17.5,
            "live_wr_today": 0.30,
            "backtest_wr_target": 0.55,
            "wins": 3,
            "losses": 7,
        },
    )
    assert "btc_alpha" in title
    assert "17.5pp" in body
    assert "30.0%" in body or "30%" in body
