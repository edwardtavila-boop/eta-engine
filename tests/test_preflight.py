"""Preflight gate tests -- P12_POLISH.go_live_checklist."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from eta_engine.scripts import preflight

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from eta_engine.obs.alerts import Alert


# ---------------------------------------------------------------------------
# check_secrets
# ---------------------------------------------------------------------------


def test_check_secrets_passes_when_all_required_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight.SECRETS,
        "validate_required_keys",
        lambda keys: [],
    )
    name, ok, msg = preflight.check_secrets()
    assert name == "secrets"
    assert ok is True
    assert "all" in msg


def test_check_secrets_fails_when_keys_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight.SECRETS,
        "validate_required_keys",
        lambda keys: ["BYBIT_API_KEY", "TRADOVATE_USERNAME"],
    )
    name, ok, msg = preflight.check_secrets()
    assert ok is False
    assert "missing" in msg
    assert "BYBIT_API_KEY" in msg


def test_check_secrets_caps_missing_list_at_five_for_display(monkeypatch: pytest.MonkeyPatch) -> None:
    many = [f"KEY_{i}" for i in range(10)]
    monkeypatch.setattr(
        preflight.SECRETS,
        "validate_required_keys",
        lambda keys: many,
    )
    name, ok, msg = preflight.check_secrets()
    assert ok is False
    # Exactly 5 comma-separated names shown
    assert msg.count(",") == 4


# ---------------------------------------------------------------------------
# check_venues
# ---------------------------------------------------------------------------


def test_check_venues_passes_when_config_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(preflight, "CONFIG_PATH", tmp_path / "nope.json")
    name, ok, msg = preflight.check_venues()
    assert ok is True
    # Baked-in venues include tradovate/ibkr/tastytrade once the
    # broker-dormancy mandate (2026-04-24) added IBKR + Tastytrade to
    # the canonical list. Check that at least the baked-in set is
    # reported (previously 2, now 3).
    assert "venues configured" in msg
    assert "ready=" in msg


def test_check_venues_reads_venues_from_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"venues": ["tradovate", "bybit", "okx", "binance"]}))
    monkeypatch.setattr(preflight, "CONFIG_PATH", cfg)
    name, ok, msg = preflight.check_venues()
    assert ok is True
    assert "4 venues" in msg


def test_check_venues_fails_on_unreadable_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text("{this is not valid json")
    monkeypatch.setattr(preflight, "CONFIG_PATH", cfg)
    name, ok, msg = preflight.check_venues()
    assert ok is False
    assert "unreadable" in msg


# ---------------------------------------------------------------------------
# check_blackout_window
# ---------------------------------------------------------------------------


def test_check_blackout_window_passes_on_empty_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "is_news_blackout", lambda now, events: False)
    name, ok, msg = preflight.check_blackout_window()
    assert name == "blackout"
    assert ok is True
    assert "clear" in msg


def test_check_blackout_window_fails_when_in_blackout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "is_news_blackout", lambda now, events: True)
    name, ok, msg = preflight.check_blackout_window()
    assert ok is False
    assert "blackout" in msg


# ---------------------------------------------------------------------------
# check_firm_verdict
# ---------------------------------------------------------------------------


def test_check_firm_verdict_passes_when_file_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(preflight, "FIRM_VERDICT_PATH", tmp_path / "missing.json")
    name, ok, msg = preflight.check_firm_verdict()
    assert ok is True
    assert "first run" in msg


def test_check_firm_verdict_passes_on_go_verdict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "verdict.json"
    f.write_text(json.dumps({"verdict": "GO"}))
    monkeypatch.setattr(preflight, "FIRM_VERDICT_PATH", f)
    name, ok, msg = preflight.check_firm_verdict()
    assert ok is True
    assert "GO" in msg


def test_check_firm_verdict_fails_on_kill(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "verdict.json"
    f.write_text(json.dumps({"verdict": "KILL"}))
    monkeypatch.setattr(preflight, "FIRM_VERDICT_PATH", f)
    name, ok, msg = preflight.check_firm_verdict()
    assert ok is False
    assert "KILL" in msg


def test_check_firm_verdict_fails_on_no_go_verdict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "verdict.json"
    f.write_text(json.dumps({"verdict": "NO_GO"}))
    monkeypatch.setattr(preflight, "FIRM_VERDICT_PATH", f)
    name, ok, msg = preflight.check_firm_verdict()
    assert ok is False


def test_check_firm_verdict_is_case_insensitive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "verdict.json"
    f.write_text(json.dumps({"verdict": "kill"}))  # lowercase
    monkeypatch.setattr(preflight, "FIRM_VERDICT_PATH", f)
    name, ok, msg = preflight.check_firm_verdict()
    assert ok is False


def test_check_firm_verdict_fails_on_unreadable_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "verdict.json"
    f.write_text("not json{{{")
    monkeypatch.setattr(preflight, "FIRM_VERDICT_PATH", f)
    name, ok, msg = preflight.check_firm_verdict()
    assert ok is False
    assert "unreadable" in msg


def test_check_firm_verdict_handles_missing_verdict_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    f = tmp_path / "verdict.json"
    f.write_text(json.dumps({"other_field": "value"}))
    monkeypatch.setattr(preflight, "FIRM_VERDICT_PATH", f)
    name, ok, msg = preflight.check_firm_verdict()
    assert ok is True  # empty verdict is NOT kill/no-go
    assert "UNKNOWN" in msg


# ---------------------------------------------------------------------------
# check_tick_cadence  (R2 operator-surfaced validator)
# ---------------------------------------------------------------------------


def test_check_tick_cadence_fails_when_yaml_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        preflight,
        "KILL_SWITCH_YAML_PATH",
        tmp_path / "nope.yaml",
    )
    name, ok, msg = preflight.check_tick_cadence()
    assert name == "tick_cadence"
    assert ok is False
    assert "not found" in msg


def test_check_tick_cadence_fails_on_unreadable_yaml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    y = tmp_path / "kill_switch.yaml"
    y.write_text(":\n  - not: [valid: yaml")  # broken flow
    monkeypatch.setattr(preflight, "KILL_SWITCH_YAML_PATH", y)
    name, ok, msg = preflight.check_tick_cadence()
    assert ok is False
    assert "unreadable" in msg


def test_check_tick_cadence_passes_when_cushion_is_sufficient(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # At default tick_interval_s=1.0 with default max_usd_move_per_sec=300
    # and safety=2.0, required cushion = $600. Use $700 to clear comfortably.
    y = tmp_path / "kill_switch.yaml"
    y.write_text(
        "tier_a:\n  apex_eval_preemptive:\n    cushion_usd: 700.0\n",
    )
    monkeypatch.setattr(preflight, "KILL_SWITCH_YAML_PATH", y)
    name, ok, msg = preflight.check_tick_cadence()
    assert ok is True
    assert "tick=1.0s" in msg
    assert "cushion=$700" in msg


def test_check_tick_cadence_fails_when_cushion_too_tight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    y = tmp_path / "kill_switch.yaml"
    y.write_text(
        "tier_a:\n  apex_eval_preemptive:\n    cushion_usd: 400.0\n",
    )
    monkeypatch.setattr(preflight, "KILL_SWITCH_YAML_PATH", y)
    name, ok, msg = preflight.check_tick_cadence()
    assert ok is False
    # ApexTickCadenceError's str is propagated verbatim as the msg.
    assert "cushion_usd" in msg
    assert "required" in msg


def test_check_tick_cadence_uses_default_cushion_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Empty YAML -> should fall back to the 500.0 default which (at the
    # canonical 1.0s tick) is too tight, so the check should fail red.
    y = tmp_path / "kill_switch.yaml"
    y.write_text("")
    monkeypatch.setattr(preflight, "KILL_SWITCH_YAML_PATH", y)
    name, ok, msg = preflight.check_tick_cadence()
    assert ok is False
    assert "cushion_usd" in msg


def test_check_tick_cadence_fails_on_negative_cushion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    y = tmp_path / "kill_switch.yaml"
    y.write_text(
        "tier_a:\n  apex_eval_preemptive:\n    cushion_usd: -1.0\n",
    )
    monkeypatch.setattr(preflight, "KILL_SWITCH_YAML_PATH", y)
    name, ok, msg = preflight.check_tick_cadence()
    assert ok is False
    assert "invalid input" in msg


# ---------------------------------------------------------------------------
# check_audit_log_readiness  (R3 writable+fsyncable audit dir)
# ---------------------------------------------------------------------------


def test_check_audit_log_readiness_passes_on_writable_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audit = tmp_path / "state"
    monkeypatch.setattr(preflight, "DEFAULT_AUDIT_LOG_DIR", audit)
    name, ok, msg = preflight.check_audit_log_readiness()
    assert name == "audit_log"
    assert ok is True
    assert "writable" in msg
    assert "fsyncable" in msg
    # tempfile should have been cleaned up
    assert list(audit.glob("preflight_audit_*.tmp")) == []


def test_check_audit_log_readiness_creates_missing_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audit = tmp_path / "nested" / "deeper" / "state"
    assert not audit.exists()
    monkeypatch.setattr(preflight, "DEFAULT_AUDIT_LOG_DIR", audit)
    name, ok, msg = preflight.check_audit_log_readiness()
    assert ok is True
    assert audit.exists()


def test_check_audit_log_readiness_fails_when_dir_is_a_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A regular file where the audit dir should be -> mkdir blows up.
    collision = tmp_path / "state"
    collision.write_text("i am a file, not a dir")
    monkeypatch.setattr(preflight, "DEFAULT_AUDIT_LOG_DIR", collision)
    name, ok, msg = preflight.check_audit_log_readiness()
    assert ok is False
    assert "cannot create" in msg


def test_check_audit_log_readiness_fails_when_fsync_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audit = tmp_path / "state"
    monkeypatch.setattr(preflight, "DEFAULT_AUDIT_LOG_DIR", audit)

    def _boom(_fd: int) -> None:  # simulates read-only mount / fsync refusal
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(preflight.os, "fsync", _boom)
    name, ok, msg = preflight.check_audit_log_readiness()
    assert ok is False
    assert "fsync failed" in msg
    assert "OSError" in msg
    # Tempfile cleanup should still have run in the finally block.
    assert list(audit.glob("preflight_audit_*.tmp")) == []


# ---------------------------------------------------------------------------
# check_telegram
# ---------------------------------------------------------------------------


def test_check_telegram_fails_when_creds_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight.SECRETS,
        "get",
        lambda key, required=True: None,
    )
    name, ok, msg = asyncio.run(preflight.check_telegram())
    assert name == "telegram"
    assert ok is False
    assert "missing" in msg


def test_check_telegram_dispatches_when_creds_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight.SECRETS,
        "get",
        lambda key, required=True: "stub_value",
    )

    class FakeAlerter:
        def __init__(self, bot_token: str, chat_id: str) -> None:
            self.bot_token = bot_token
            self.chat_id = chat_id

        async def send(self, alert: Alert) -> bool:
            return True

    monkeypatch.setattr(preflight, "TelegramAlerter", FakeAlerter)
    name, ok, msg = asyncio.run(preflight.check_telegram())
    assert ok is True
    assert "dispatched" in msg


def test_check_telegram_reports_send_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight.SECRETS,
        "get",
        lambda key, required=True: "stub_value",
    )

    class FailingAlerter:
        def __init__(self, bot_token: str, chat_id: str) -> None:
            pass

        async def send(self, alert: Alert) -> bool:
            return False

    monkeypatch.setattr(preflight, "TelegramAlerter", FailingAlerter)
    name, ok, msg = asyncio.run(preflight.check_telegram())
    assert ok is False


# ---------------------------------------------------------------------------
# _run_async (full sweep)
# ---------------------------------------------------------------------------


def _stub_all_checks_green(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch every preflight check to a GREEN stub.

    Individual tests then selectively re-patch checks to RED to exercise
    failure branches. Keeps _run_async tests decoupled from the exact
    check set, so future additions don't require N test edits.
    """
    monkeypatch.setattr(preflight, "check_secrets", lambda: ("secrets", True, "ok"))
    monkeypatch.setattr(preflight, "check_venues", lambda: ("venues", True, "ok"))
    monkeypatch.setattr(preflight, "check_blackout_window", lambda: ("blackout", True, "ok"))
    monkeypatch.setattr(preflight, "check_firm_verdict", lambda: ("firm_verdict", True, "ok"))
    monkeypatch.setattr(preflight, "check_tick_cadence", lambda: ("tick_cadence", True, "ok"))
    monkeypatch.setattr(preflight, "check_audit_log_readiness", lambda: ("audit_log", True, "ok"))

    async def _ok() -> tuple:
        return ("telegram", True, "ok")

    monkeypatch.setattr(preflight, "check_telegram", _ok)


def test_run_async_returns_zero_when_all_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_all_checks_green(monkeypatch)
    rc = asyncio.run(preflight._run_async())
    assert rc == 0


def test_run_async_returns_one_when_any_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_all_checks_green(monkeypatch)
    monkeypatch.setattr(preflight, "check_secrets", lambda: ("secrets", False, "missing"))
    rc = asyncio.run(preflight._run_async())
    assert rc == 1


def test_run_async_returns_one_when_tick_cadence_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_all_checks_green(monkeypatch)
    monkeypatch.setattr(preflight, "check_tick_cadence", lambda: ("tick_cadence", False, "cushion too tight"))
    rc = asyncio.run(preflight._run_async())
    assert rc == 1


def test_run_async_returns_one_when_audit_log_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_all_checks_green(monkeypatch)
    monkeypatch.setattr(preflight, "check_audit_log_readiness", lambda: ("audit_log", False, "fsync failed"))
    rc = asyncio.run(preflight._run_async())
    assert rc == 1


def test_run_async_returns_one_when_multiple_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_all_checks_green(monkeypatch)
    monkeypatch.setattr(preflight, "check_secrets", lambda: ("secrets", False, "missing"))
    monkeypatch.setattr(preflight, "check_venues", lambda: ("venues", False, "unreachable"))
    monkeypatch.setattr(preflight, "check_blackout_window", lambda: ("blackout", False, "in blackout"))
    monkeypatch.setattr(preflight, "check_firm_verdict", lambda: ("firm_verdict", False, "KILL"))
    monkeypatch.setattr(preflight, "check_tick_cadence", lambda: ("tick_cadence", False, "cushion too tight"))
    monkeypatch.setattr(preflight, "check_audit_log_readiness", lambda: ("audit_log", False, "fsync failed"))

    async def _fail() -> tuple:
        return ("telegram", False, "missing")

    monkeypatch.setattr(preflight, "check_telegram", _fail)

    rc = asyncio.run(preflight._run_async())
    assert rc == 1


def test_run_async_prints_all_seven_check_rows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_all_checks_green(monkeypatch)
    asyncio.run(preflight._run_async())
    out = capsys.readouterr().out
    for name in (
        "secrets",
        "venues",
        "blackout",
        "firm_verdict",
        "tick_cadence",
        "audit_log",
        "telegram",
    ):
        assert name in out
    assert "GO" in out
    assert "passed 7/7" in out


# ---------------------------------------------------------------------------
# run (sync wrapper)
# ---------------------------------------------------------------------------


def test_run_propagates_run_async_return_code(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _return(code: int) -> int:
        return code

    monkeypatch.setattr(preflight, "_run_async", lambda: _return(0))
    assert preflight.run() == 0

    monkeypatch.setattr(preflight, "_run_async", lambda: _return(1))
    assert preflight.run() == 1
