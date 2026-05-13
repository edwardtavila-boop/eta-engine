"""Tests for the JARVIS Telegram inbound bot (long-poll command dispatcher)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Command parsing & dispatch
# ---------------------------------------------------------------------------


def test_dispatch_help_returns_command_list() -> None:
    from eta_engine.scripts import telegram_inbound_bot

    reply = telegram_inbound_bot.dispatch_command("/help")
    assert "/pnl" in reply
    assert "/anomalies" in reply
    assert "/preflight" in reply


def test_dispatch_unknown_command_returns_hint() -> None:
    from eta_engine.scripts import telegram_inbound_bot

    reply = telegram_inbound_bot.dispatch_command("/nonsense")
    assert "unknown" in reply.lower()
    assert "/help" in reply


def test_dispatch_strips_botname_suffix() -> None:
    """Telegram appends @botname to commands in group chats — strip it."""
    from eta_engine.scripts import telegram_inbound_bot

    reply = telegram_inbound_bot.dispatch_command("/help@JarvisBot")
    assert "/pnl" in reply


def test_dispatch_empty_message_returns_hint() -> None:
    from eta_engine.scripts import telegram_inbound_bot

    reply = telegram_inbound_bot.dispatch_command("   ")
    assert "empty" in reply.lower()


def test_dispatch_handler_crash_caught_and_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a command handler raises, dispatcher returns a polite error not a stacktrace."""
    from eta_engine.scripts import telegram_inbound_bot

    def boom() -> str:
        raise RuntimeError("simulated crash")

    monkeypatch.setitem(telegram_inbound_bot.COMMANDS, "/help", (boom, False))
    reply = telegram_inbound_bot.dispatch_command("/help")
    assert "crashed" in reply
    assert "simulated crash" in reply


# ---------------------------------------------------------------------------
# Whitelist enforcement
# ---------------------------------------------------------------------------


def test_process_update_rejects_non_allowlisted_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.setattr(telegram_inbound_bot, "_LOG_PATH", tmp_path / "audit.jsonl")

    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(
        telegram_inbound_bot,
        "send_reply",
        lambda chat_id, text, **kw: sent.append((chat_id, text)) or {"ok": True},
    )

    update = {
        "update_id": 100,
        "message": {
            "message_id": 1,
            "chat": {"id": 99999},  # not in allowlist
            "from": {"username": "stranger"},
            "text": "/pnl",
        },
    }
    record = telegram_inbound_bot.process_update(update, allowed={123})
    assert record is not None
    assert record["allowed"] is False
    assert len(sent) == 1
    assert "private" in sent[0][1].lower()


def test_process_update_accepts_allowlisted_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.setattr(telegram_inbound_bot, "_LOG_PATH", tmp_path / "audit.jsonl")

    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(
        telegram_inbound_bot,
        "send_reply",
        lambda chat_id, text, **kw: sent.append((chat_id, text)) or {"ok": True},
    )

    update = {
        "update_id": 100,
        "message": {
            "message_id": 1,
            "chat": {"id": 123},
            "from": {"username": "edward"},
            "text": "/help",
        },
    }
    record = telegram_inbound_bot.process_update(update, allowed={123})
    assert record is not None
    assert record["allowed"] is True
    assert record["command"] == "/help"
    assert len(sent) == 1
    assert "/pnl" in sent[0][1]


def test_process_update_blocks_free_text_unless_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Free-text stays fail-closed unless the Hermes bridge is opted in."""
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.delenv("ETA_TELEGRAM_HERMES_FREE_TEXT", raising=False)
    monkeypatch.setattr(telegram_inbound_bot, "_LOG_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(
        telegram_inbound_bot,
        "_ask_hermes",
        lambda prompt: pytest.fail("_ask_hermes should not run while disabled"),
    )

    sent: list[str] = []
    monkeypatch.setattr(
        telegram_inbound_bot,
        "send_reply",
        lambda chat_id, text, **kw: sent.append(text) or {"ok": True},
    )

    update = {
        "update_id": 100,
        "message": {
            "message_id": 1,
            "chat": {"id": 1},
            "from": {"username": "edward"},
            "text": "how is the fleet doing?",
        },
    }
    record = telegram_inbound_bot.process_update(update, allowed={1})
    assert len(sent) == 1
    assert "/help" in sent[0]
    assert "ETA_TELEGRAM_HERMES_FREE_TEXT" in sent[0]
    assert record["command"] == "<free_text_blocked>"


def test_process_update_routes_free_text_to_hermes_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Free-text messages route to Hermes via _ask_hermes for a natural reply."""
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.setenv("ETA_TELEGRAM_HERMES_FREE_TEXT", "1")
    monkeypatch.setattr(telegram_inbound_bot, "_LOG_PATH", tmp_path / "audit.jsonl")

    captured: list[str] = []

    def fake_ask_hermes(prompt: str, *, chat_id: int | str | None = None) -> str:
        captured.append(prompt)
        return "Fleet up +2.5R today, all bots within prop limits."

    monkeypatch.setattr(telegram_inbound_bot, "_ask_hermes", fake_ask_hermes)

    sent: list[str] = []
    monkeypatch.setattr(
        telegram_inbound_bot,
        "send_reply",
        lambda chat_id, text, **kw: sent.append(text) or {"ok": True},
    )

    update = {
        "update_id": 100,
        "message": {
            "message_id": 1,
            "chat": {"id": 1},
            "from": {"username": "edward"},
            "text": "how is the fleet doing?",
        },
    }
    record = telegram_inbound_bot.process_update(update, allowed={1})
    assert len(sent) == 1
    assert "Fleet up +2.5R" in sent[0]
    assert captured == ["how is the fleet doing?"]
    assert record["command"] == "<free_text>"


def test_ask_hermes_handles_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the hermes exe is missing, return a polite error not a crash."""
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.setattr(telegram_inbound_bot, "_HERMES_EXE", "Z:/no/such/hermes.exe")
    reply = telegram_inbound_bot._ask_hermes("hi")
    assert "not found" in reply.lower()


def test_ask_hermes_handles_empty_input() -> None:
    from eta_engine.scripts import telegram_inbound_bot

    reply = telegram_inbound_bot._ask_hermes("   ")
    assert "empty" in reply.lower()


def test_session_name_for_chat_is_stable_within_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same chat_id back-to-back should reuse the same session name."""
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.setattr(
        telegram_inbound_bot,
        "_HERMES_LAST_CHAT_PATH",
        tmp_path / "last.json",
    )
    n1 = telegram_inbound_bot._session_name_for_chat(123)
    n2 = telegram_inbound_bot._session_name_for_chat(123)
    assert n1 == n2
    assert n1.startswith("telegram-123-")


def test_session_name_for_chat_rolls_on_long_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gap >TTL forces a fresh cycle number so old context doesn't bleed in."""
    import json

    from eta_engine.scripts import telegram_inbound_bot

    last_path = tmp_path / "last.json"
    stale_ts = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    last_path.write_text(
        json.dumps({"123": {"ts": stale_ts, "cycle": 0, "name": "telegram-123-0"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(telegram_inbound_bot, "_HERMES_LAST_CHAT_PATH", last_path)
    name = telegram_inbound_bot._session_name_for_chat(123)
    assert name == "telegram-123-1"


def test_session_name_for_chat_isolates_by_chat_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.setattr(
        telegram_inbound_bot,
        "_HERMES_LAST_CHAT_PATH",
        tmp_path / "last.json",
    )
    name_a = telegram_inbound_bot._session_name_for_chat(111)
    name_b = telegram_inbound_bot._session_name_for_chat(222)
    assert name_a != name_b
    assert "111" in name_a
    assert "222" in name_b


def test_ask_hermes_passes_continue_flag_with_session_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The subprocess invocation must include --continue <session> by default."""
    from types import SimpleNamespace

    from eta_engine.scripts import telegram_inbound_bot

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("ETA_HERMES_CLI", str(fake_exe))
    monkeypatch.setattr(
        telegram_inbound_bot,
        "_HERMES_LAST_CHAT_PATH",
        tmp_path / "last.json",
    )
    monkeypatch.delenv("ETA_TELEGRAM_HERMES_NO_CONTINUE", raising=False)

    captured: dict[str, list[str]] = {}

    def fake_run(args, **_kwargs):
        captured["cmd"] = list(args)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    telegram_inbound_bot._ask_hermes("hello", chat_id=555)
    cmd = captured["cmd"]
    assert "--continue" in cmd
    idx = cmd.index("--continue")
    assert cmd[idx + 1].startswith("telegram-555-")


def test_ask_hermes_omits_continue_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ETA_TELEGRAM_HERMES_NO_CONTINUE=1 disables the resume flag."""
    from types import SimpleNamespace

    from eta_engine.scripts import telegram_inbound_bot

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("ETA_HERMES_CLI", str(fake_exe))
    monkeypatch.setattr(
        telegram_inbound_bot,
        "_HERMES_LAST_CHAT_PATH",
        tmp_path / "last.json",
    )
    monkeypatch.setenv("ETA_TELEGRAM_HERMES_NO_CONTINUE", "1")

    captured: dict[str, list[str]] = {}

    def fake_run(args, **_kwargs):
        captured["cmd"] = list(args)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    telegram_inbound_bot._ask_hermes("hello", chat_id=555)
    cmd = captured["cmd"]
    assert "--continue" not in cmd


def test_ask_hermes_wraps_prompt_with_safety_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from eta_engine.scripts import telegram_inbound_bot

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("ETA_HERMES_CLI", str(fake_exe))
    monkeypatch.delenv("ETA_TELEGRAM_HERMES_ACCEPT_HOOKS", raising=False)

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="safe reply", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    reply = telegram_inbound_bot._ask_hermes("can you flatten everything?")

    assert reply == "safe reply"
    cmd = captured["cmd"]
    assert "--accept-hooks" not in cmd
    prompt = cmd[cmd.index("-q") + 1]
    assert "Do not place orders" in prompt
    assert "flatten positions" in prompt
    assert "Operator message: can you flatten everything?" in prompt


def test_ask_hermes_accept_hooks_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from eta_engine.scripts import telegram_inbound_bot

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("ETA_HERMES_CLI", str(fake_exe))
    monkeypatch.setenv("ETA_TELEGRAM_HERMES_ACCEPT_HOOKS", "1")

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert telegram_inbound_bot._ask_hermes("status") == "ok"
    assert "--accept-hooks" in captured["cmd"]


def test_ask_hermes_handles_subprocess_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Subprocess timeout returns a polite message, never raises."""
    import subprocess

    from eta_engine.scripts import telegram_inbound_bot

    # Point _HERMES_EXE at an existing file so the missing-binary check passes
    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(telegram_inbound_bot, "_HERMES_EXE", str(fake_exe))

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="hermes", timeout=90)

    monkeypatch.setattr("subprocess.run", fake_run)
    reply = telegram_inbound_bot._ask_hermes("anything")
    assert "took too long" in reply.lower() or "timeout" in reply.lower()


def test_ask_hermes_handles_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Non-zero exit code surfaces stderr instead of crashing."""
    from types import SimpleNamespace

    from eta_engine.scripts import telegram_inbound_bot

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(telegram_inbound_bot, "_HERMES_EXE", str(fake_exe))

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=3, stdout="", stderr="oh no")

    monkeypatch.setattr("subprocess.run", fake_run)
    reply = telegram_inbound_bot._ask_hermes("anything")
    assert "exit 3" in reply
    assert "oh no" in reply


def test_ask_hermes_truncates_oversize_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Output > 3500 chars gets truncated with a tag."""
    from types import SimpleNamespace

    from eta_engine.scripts import telegram_inbound_bot

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(telegram_inbound_bot, "_HERMES_EXE", str(fake_exe))

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="x" * 5000, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    reply = telegram_inbound_bot._ask_hermes("anything")
    assert len(reply) < 4000
    assert "truncated" in reply.lower()


# ---------------------------------------------------------------------------
# Silence
# ---------------------------------------------------------------------------


def test_silence_for_persists_until_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.setattr(telegram_inbound_bot, "_SILENCE_PATH", tmp_path / "silence.json")
    until = telegram_inbound_bot.silence_for(30)
    assert (tmp_path / "silence.json").exists()
    rec = json.loads((tmp_path / "silence.json").read_text(encoding="utf-8"))
    assert rec["minutes"] == 30
    assert rec["silence_until"] == until


def test_is_silenced_true_within_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import telegram_inbound_bot

    silence_path = tmp_path / "silence.json"
    until = datetime.now(UTC) + timedelta(minutes=15)
    silence_path.write_text(json.dumps({"silence_until": until.isoformat()}), encoding="utf-8")
    monkeypatch.setattr(telegram_inbound_bot, "_SILENCE_PATH", silence_path)
    assert telegram_inbound_bot.is_silenced() is True


def test_is_silenced_false_after_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import telegram_inbound_bot

    silence_path = tmp_path / "silence.json"
    until = datetime.now(UTC) - timedelta(minutes=1)  # already expired
    silence_path.write_text(json.dumps({"silence_until": until.isoformat()}), encoding="utf-8")
    monkeypatch.setattr(telegram_inbound_bot, "_SILENCE_PATH", silence_path)
    assert telegram_inbound_bot.is_silenced() is False


def test_is_silenced_false_when_no_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.setattr(telegram_inbound_bot, "_SILENCE_PATH", tmp_path / "no.json")
    assert telegram_inbound_bot.is_silenced() is False


def test_cmd_silence_parses_minutes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.setattr(telegram_inbound_bot, "_SILENCE_PATH", tmp_path / "silence.json")
    reply = telegram_inbound_bot.dispatch_command("/silence 45m")
    assert "45" in reply
    assert (tmp_path / "silence.json").exists()


def test_cmd_silence_parses_hours(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.setattr(telegram_inbound_bot, "_SILENCE_PATH", tmp_path / "silence.json")
    reply = telegram_inbound_bot.dispatch_command("/silence 2h")
    assert "120" in reply or "2h" in reply
    rec = json.loads((tmp_path / "silence.json").read_text(encoding="utf-8"))
    assert rec["minutes"] == 120


def test_cmd_silence_rejects_garbage() -> None:
    from eta_engine.scripts import telegram_inbound_bot

    reply = telegram_inbound_bot.dispatch_command("/silence ten minutes")
    assert "usage" in reply.lower()


def test_cmd_silence_rejects_zero() -> None:
    from eta_engine.scripts import telegram_inbound_bot

    reply = telegram_inbound_bot.dispatch_command("/silence 0m")
    assert "1m" in reply or "must be" in reply.lower()


def test_cmd_silence_rejects_too_long() -> None:
    from eta_engine.scripts import telegram_inbound_bot

    reply = telegram_inbound_bot.dispatch_command("/silence 9999m")
    assert "1m..24h" in reply or "must be" in reply.lower()


def test_cmd_unsilence_clears_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import telegram_inbound_bot

    silence_path = tmp_path / "silence.json"
    silence_path.write_text(json.dumps({"silence_until": "x"}), encoding="utf-8")
    monkeypatch.setattr(telegram_inbound_bot, "_SILENCE_PATH", silence_path)
    reply = telegram_inbound_bot.dispatch_command("/unsilence")
    assert "cleared" in reply.lower()
    assert not silence_path.exists()


# ---------------------------------------------------------------------------
# /ack — remove dedup key
# ---------------------------------------------------------------------------


def test_cmd_ack_removes_matching_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import telegram_inbound_bot

    hits_log = tmp_path / "anomaly_watcher.jsonl"
    hits_log.write_text(
        json.dumps({"key": "loss_streak:bot_a:3", "pattern": "loss_streak"})
        + "\n"
        + json.dumps({"key": "loss_streak:bot_b:4", "pattern": "loss_streak"})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(telegram_inbound_bot, "_WORKSPACE", tmp_path.parent)
    # The handler hard-codes _WORKSPACE / "var" / "anomaly_watcher.jsonl"
    # so monkeypatch the lookup directly via _WORKSPACE; recreate the
    # expected path under tmp_path.parent.
    var_dir = tmp_path.parent / "var"
    var_dir.mkdir(parents=True, exist_ok=True)
    target = var_dir / "anomaly_watcher.jsonl"
    target.write_text(hits_log.read_text(encoding="utf-8"), encoding="utf-8")

    reply = telegram_inbound_bot.dispatch_command("/ack loss_streak:bot_a:3")
    assert "Cleared" in reply or "cleared" in reply
    lines = target.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    assert "bot_b" in lines[0]


def test_cmd_ack_no_arg_returns_usage() -> None:
    from eta_engine.scripts import telegram_inbound_bot

    reply = telegram_inbound_bot.dispatch_command("/ack")
    assert "usage" in reply.lower()


# ---------------------------------------------------------------------------
# Offset persistence
# ---------------------------------------------------------------------------


def test_save_and_load_offset_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.setattr(telegram_inbound_bot, "_OFFSET_PATH", tmp_path / "offset.json")
    assert telegram_inbound_bot._load_offset() == 0
    telegram_inbound_bot._save_offset(42)
    assert telegram_inbound_bot._load_offset() == 42


def test_save_offset_atomic_via_tmp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_save_offset writes via .tmp then renames — partial writes can't corrupt."""
    from eta_engine.scripts import telegram_inbound_bot

    target = tmp_path / "offset.json"
    monkeypatch.setattr(telegram_inbound_bot, "_OFFSET_PATH", target)
    telegram_inbound_bot._save_offset(7)
    assert target.exists()
    # The .tmp helper should have been cleaned up by os.replace
    assert not (tmp_path / "offset.json.tmp").exists()


# ---------------------------------------------------------------------------
# Loop hardening
# ---------------------------------------------------------------------------


def test_run_loop_returns_zero_when_no_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fail-closed: no TELEGRAM_CHAT_ID means the loop refuses to run."""
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    n = telegram_inbound_bot.run_loop(once=True)
    assert n == 0


def test_run_loop_once_processes_returned_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(telegram_inbound_bot, "_OFFSET_PATH", tmp_path / "offset.json")
    monkeypatch.setattr(telegram_inbound_bot, "_LOG_PATH", tmp_path / "audit.jsonl")
    fake_updates = [
        {
            "update_id": 50,
            "message": {
                "message_id": 1,
                "chat": {"id": 1},
                "from": {"username": "edward"},
                "text": "/help",
            },
        },
    ]
    monkeypatch.setattr(
        telegram_inbound_bot,
        "get_updates",
        lambda offset, timeout_s=25: fake_updates,
    )
    sent: list[str] = []
    monkeypatch.setattr(
        telegram_inbound_bot,
        "send_reply",
        lambda chat_id, text, **kw: sent.append(text) or {"ok": True},
    )

    n = telegram_inbound_bot.run_loop(once=True)
    assert n == 1
    # offset advanced past the seen update
    assert telegram_inbound_bot._load_offset() == 51
    assert len(sent) == 1
    assert "/pnl" in sent[0]


# ---------------------------------------------------------------------------
# Pulse honors silence
# ---------------------------------------------------------------------------


def test_pulse_suppresses_send_when_silenced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: /silence makes the outbound pulse skip the Telegram send."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher
    from eta_engine.scripts import anomaly_telegram_pulse, telegram_inbound_bot

    monkeypatch.setattr(anomaly_telegram_pulse, "_PULSE_LOG", tmp_path / "pulse.jsonl")
    monkeypatch.setattr(telegram_inbound_bot, "_SILENCE_PATH", tmp_path / "silence.json")
    telegram_inbound_bot.silence_for(15)

    fake_hit = anomaly_watcher.AnomalyHit(
        asof="2026-05-12T13:00:00+00:00",
        pattern="loss_streak",
        key="loss_streak:silenced:3",
        bot_id="silenced",
        severity="warn",
        detail="3 losses",
        suggested_skill="jarvis-anomaly-investigator",
        extras={},
    )
    monkeypatch.setattr(anomaly_watcher, "scan", lambda **kw: [fake_hit])

    sent: list[str] = []
    monkeypatch.setattr(
        "eta_engine.deploy.scripts.telegram_alerts.send_from_env",
        lambda text, priority="INFO": sent.append(text) or {"ok": True},
    )

    result = anomaly_telegram_pulse.run_pulse(dry_run=False)
    assert result["ok"] is True
    assert result["n_new"] == 1
    assert result["sent"] is False
    assert result["reason"] == "silenced_by_operator"
    assert sent == []  # NOTHING sent during silence


# ---------------------------------------------------------------------------
# Command surface coverage
# ---------------------------------------------------------------------------


def test_cmd_pnl_returns_summary_when_module_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import telegram_inbound_bot

    fake = {
        "today": {"total_r": 1.5, "n_trades": 8, "n_wins": 5, "n_losses": 3, "win_rate": 0.625},
        "week": {"total_r": 7.2, "n_trades": 40, "n_wins": 22, "n_losses": 18, "win_rate": 0.55},
        "month": {"total_r": 15.5, "n_trades": 120, "n_wins": 60, "n_losses": 60, "win_rate": 0.5},
    }
    import eta_engine.brain.jarvis_v3.pnl_summary as pnl_mod

    monkeypatch.setattr(pnl_mod, "multi_window_summary", lambda: fake)

    reply = telegram_inbound_bot.dispatch_command("/pnl")
    assert "+1.50R" in reply
    assert "+7.20R" in reply
    assert "+15.50R" in reply


def test_cmd_pnl_handles_module_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eta_engine.brain.jarvis_v3.pnl_summary as pnl_mod
    from eta_engine.scripts import telegram_inbound_bot

    def boom() -> Any:
        raise RuntimeError("disk full")

    monkeypatch.setattr(pnl_mod, "multi_window_summary", boom)

    reply = telegram_inbound_bot.dispatch_command("/pnl")
    assert "unavailable" in reply.lower()
    assert "disk full" in reply


def test_cmd_anomalies_handles_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import anomaly_watcher
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.setattr(anomaly_watcher, "recent_hits", lambda since_hours=24: [])
    reply = telegram_inbound_bot.dispatch_command("/anomalies")
    assert "Clean" in reply or "no anomalies" in reply.lower()


def test_cmd_anomalies_lists_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import anomaly_watcher
    from eta_engine.scripts import telegram_inbound_bot

    fake_hits = [
        {
            "asof": "2026-05-12T13:00:00+00:00",
            "pattern": "loss_streak",
            "key": "x",
            "bot_id": "mnq_floor",
            "severity": "warn",
            "detail": "mnq_floor has 4 consecutive losses",
        }
    ]
    monkeypatch.setattr(anomaly_watcher, "recent_hits", lambda since_hours=24: fake_hits)
    reply = telegram_inbound_bot.dispatch_command("/anomalies")
    assert "mnq_floor" in reply
    assert "loss_streak" in reply
    assert "WARN" in reply.upper()


# ---------------------------------------------------------------------------
# /accounts and /killall
# ---------------------------------------------------------------------------


def test_cmd_accounts_lists_every_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g
    from eta_engine.scripts import telegram_inbound_bot

    fake_snap = g.AccountSnapshot(
        rules=g.PropFirmRules(
            firm="blusky",
            size="50K",
            account_id="blusky-50K-launch",
            starting_balance=50_000.0,
            daily_loss_limit=1_500.0,
            trailing_drawdown=2_000.0,
            profit_target=3_000.0,
            consistency_rule_pct=None,
            max_contracts=10,
            rth_only=False,
            automation_allowed=True,
        ),
        state=g.AccountState(
            account_id="blusky-50K-launch",
            starting_balance=50_000.0,
            current_balance=50_500.0,
            peak_balance=50_500.0,
            day_pnl_usd=500.0,
            today_date="2026-05-12",
            n_trades_today=3,
            open_contracts=0,
        ),
        daily_loss_remaining=1_500.0,
        daily_loss_pct_used=0.0,
        trailing_dd_remaining=2_000.0,
        profit_to_target=2_500.0,
        pct_to_target=0.1667,
        severity="ok",
        blockers=[],
    )
    monkeypatch.setattr(g, "aggregate_status", lambda **kw: [fake_snap])
    reply = telegram_inbound_bot.dispatch_command("/accounts")
    assert "blusky-50K-launch" in reply
    assert "[ok]" in reply
    assert "$50,500" in reply
    assert "DLR" in reply  # daily-loss-remaining label


def test_cmd_accounts_marks_automation_disallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g
    from eta_engine.scripts import telegram_inbound_bot

    blocked_snap = g.AccountSnapshot(
        rules=g.PropFirmRules(
            firm="topstep",
            size="50K",
            account_id="topstep-50K",
            starting_balance=50_000.0,
            daily_loss_limit=1_100.0,
            trailing_drawdown=2_000.0,
            profit_target=3_000.0,
            consistency_rule_pct=None,
            max_contracts=5,
            rth_only=True,
            automation_allowed=False,  # TOS lock
        ),
        state=g.AccountState(
            account_id="topstep-50K",
            starting_balance=50_000.0,
            current_balance=50_000.0,
            peak_balance=50_000.0,
            day_pnl_usd=0.0,
            today_date="2026-05-12",
            n_trades_today=0,
            open_contracts=0,
        ),
        daily_loss_remaining=1_100.0,
        daily_loss_pct_used=0.0,
        trailing_dd_remaining=2_000.0,
        profit_to_target=3_000.0,
        pct_to_target=0.0,
        severity="ok",
        blockers=[],
    )
    monkeypatch.setattr(g, "aggregate_status", lambda **kw: [blocked_snap])
    reply = telegram_inbound_bot.dispatch_command("/accounts")
    assert "automation disallowed" in reply.lower()


def test_cmd_killall_requires_reason() -> None:
    from eta_engine.scripts import telegram_inbound_bot

    reply = telegram_inbound_bot.dispatch_command("/killall")
    assert "reason" in reply.lower()
    assert "usage" in reply.lower()


def test_cmd_killall_writes_state_with_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.setattr(telegram_inbound_bot, "_WORKSPACE", tmp_path)
    reply = telegram_inbound_bot.dispatch_command("/killall approaching daily loss limit")
    assert "KILL SWITCH ENGAGED" in reply
    state_path = tmp_path / "var" / "eta_engine" / "state" / "jarvis_intel" / "hermes_state.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["kill_all"] is True
    assert "approaching daily loss" in state["reason"]
    assert state["source"] == "telegram_inbound_bot"


# ---------------------------------------------------------------------------
# /bots /pause /resume /size /debrief /route
# ---------------------------------------------------------------------------


def test_cmd_bots_returns_active_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import hermes_overrides
    from eta_engine.scripts import telegram_inbound_bot

    fake_summary = {
        "size_modifiers": {
            "mnq_floor": {
                "modifier": 0.5,
                "ttl_minutes_remaining": 45,
                "reason": "operator paused via telegram",
            }
        },
        "school_weights": {},
    }
    monkeypatch.setattr(hermes_overrides, "active_overrides_summary", lambda: fake_summary)
    reply = telegram_inbound_bot.dispatch_command("/bots")
    assert "mnq_floor" in reply
    assert "0.5" in reply
    assert "Size modifiers" in reply


def test_cmd_bots_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import hermes_overrides
    from eta_engine.scripts import telegram_inbound_bot

    monkeypatch.setattr(
        hermes_overrides,
        "active_overrides_summary",
        lambda: {"size_modifiers": {}, "school_weights": {}},
    )
    reply = telegram_inbound_bot.dispatch_command("/bots")
    assert "no active" in reply.lower()


def test_cmd_pause_applies_zero_modifier(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import hermes_overrides
    from eta_engine.scripts import telegram_inbound_bot

    captured: list[dict[str, Any]] = []

    def fake_apply(**kw: Any) -> dict[str, Any]:
        captured.append(kw)
        return {"status": "APPLIED"}

    monkeypatch.setattr(hermes_overrides, "apply_size_modifier", fake_apply)
    reply = telegram_inbound_bot.dispatch_command("/pause mnq_floor")
    assert "Paused" in reply
    assert "mnq_floor" in reply
    assert captured[0]["bot_id"] == "mnq_floor"
    assert captured[0]["modifier"] == 0.0


def test_cmd_pause_requires_botname() -> None:
    from eta_engine.scripts import telegram_inbound_bot

    reply = telegram_inbound_bot.dispatch_command("/pause")
    assert "usage" in reply.lower()


def test_cmd_resume_clears_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import hermes_overrides
    from eta_engine.scripts import telegram_inbound_bot

    captured: list[dict[str, Any]] = []

    def fake_clear(**kw: Any) -> dict[str, Any]:
        captured.append(kw)
        return {"status": "REMOVED"}

    monkeypatch.setattr(hermes_overrides, "clear_override", fake_clear)
    reply = telegram_inbound_bot.dispatch_command("/resume mnq_floor")
    assert "Resumed" in reply
    assert captured[0]["bot_id"] == "mnq_floor"


def test_cmd_size_parses_modifier(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import hermes_overrides
    from eta_engine.scripts import telegram_inbound_bot

    captured: list[dict[str, Any]] = []

    def fake_apply(**kw: Any) -> dict[str, Any]:
        captured.append(kw)
        return {"status": "APPLIED"}

    monkeypatch.setattr(hermes_overrides, "apply_size_modifier", fake_apply)
    reply = telegram_inbound_bot.dispatch_command("/size mnq_floor 0.5")
    assert "Resize" in reply
    assert captured[0]["modifier"] == 0.5


def test_cmd_size_rejects_out_of_range() -> None:
    from eta_engine.scripts import telegram_inbound_bot

    reply = telegram_inbound_bot.dispatch_command("/size mnq_floor 1.5")
    assert "0.0, 1.0" in reply or "must be" in reply.lower()


def test_cmd_size_rejects_garbage() -> None:
    from eta_engine.scripts import telegram_inbound_bot

    reply = telegram_inbound_bot.dispatch_command("/size mnq_floor abc")
    assert "must be a float" in reply.lower()


def test_cmd_size_requires_two_args() -> None:
    from eta_engine.scripts import telegram_inbound_bot

    reply = telegram_inbound_bot.dispatch_command("/size mnq_floor")
    assert "usage" in reply.lower()


def test_cmd_route_picks_account_with_most_headroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g
    from eta_engine.scripts import telegram_inbound_bot

    def fake_state(account_id: str, **kw: Any) -> g.AccountState:
        return g.AccountState(
            account_id=account_id,
            starting_balance=50_000.0,
            current_balance=50_000.0,
            peak_balance=50_000.0,
            day_pnl_usd=0.0,
            today_date="2026-05-12",
            n_trades_today=0,
            open_contracts=0,
        )

    monkeypatch.setattr(g, "account_state_from_trades", fake_state)
    reply = telegram_inbound_bot.dispatch_command("/route MNQ 1.0 2")
    # Should find at least one automation-allowed candidate
    assert "Best account" in reply
    # Apex-funded and topstep are blocked, so must pick from blusky/apex-eval/etf
    assert "blusky-50K-launch" in reply or "apex-50K-eval" in reply or "etf-50K" in reply


def test_cmd_route_rejects_garbage() -> None:
    from eta_engine.scripts import telegram_inbound_bot

    reply = telegram_inbound_bot.dispatch_command("/route MNQ")
    assert "usage" in reply.lower()


def test_cmd_debrief_returns_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.scripts import daily_debrief, telegram_inbound_bot

    monkeypatch.setattr(
        daily_debrief,
        "build_debrief",
        lambda: {"markdown": "*Daily Debrief* — content", "sections": [], "asof": "x"},
    )
    reply = telegram_inbound_bot.dispatch_command("/debrief")
    assert "Daily Debrief" in reply
