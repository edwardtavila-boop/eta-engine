"""Tests for anomaly_telegram_pulse — the cron entrypoint that replaces
the noisy watchdog auto-heal spam with meaningful anomaly alerts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest


def _hit(
    *,
    pattern: str = "loss_streak",
    bot_id: str = "bot_a",
    severity: str = "warn",
    detail: str = "3 losses",
    suggested: str = "jarvis-anomaly-investigator",
) -> dict[str, Any]:
    return {
        "asof": "2026-05-12T13:00:00+00:00",
        "pattern": pattern,
        "key": f"{pattern}:{bot_id}:test",
        "bot_id": bot_id,
        "severity": severity,
        "detail": detail,
        "suggested_skill": suggested,
        "extras": {},
    }


def test_format_message_single_hit() -> None:
    from eta_engine.scripts import anomaly_telegram_pulse

    msg = anomaly_telegram_pulse._format_message([_hit(bot_id="bleeder", detail="4 in a row")])
    assert "1 new hit" in msg
    # pattern is bolded with markdown-escaped underscore (loss_streak -> loss\_streak)
    assert r"*loss\_streak*" in msg
    assert "`bleeder`" in msg
    assert "4 in a row" in msg


def test_format_message_pluralizes_multiple_hits() -> None:
    from eta_engine.scripts import anomaly_telegram_pulse

    msg = anomaly_telegram_pulse._format_message([_hit(bot_id="a"), _hit(bot_id="b")])
    assert "2 new hits" in msg


def test_format_message_critical_first() -> None:
    from eta_engine.scripts import anomaly_telegram_pulse

    msg = anomaly_telegram_pulse._format_message(
        [
            _hit(bot_id="warn_bot", severity="warn", detail="3 losses"),
            _hit(bot_id="bad_bot", severity="critical", detail="6 losses"),
        ]
    )
    # critical bot listed before warn bot in the body
    bad_idx = msg.index("bad_bot")
    warn_idx = msg.index("warn_bot")
    assert bad_idx < warn_idx


def test_format_message_caps_at_10_hits() -> None:
    from eta_engine.scripts import anomaly_telegram_pulse

    hits = [_hit(bot_id=f"bot_{i:02d}") for i in range(15)]
    msg = anomaly_telegram_pulse._format_message(hits)
    assert "15 new hits" in msg
    assert "and 5 more" in msg


def test_format_message_includes_suggested_skill() -> None:
    from eta_engine.scripts import anomaly_telegram_pulse

    msg = anomaly_telegram_pulse._format_message([_hit(suggested="jarvis-anomaly-investigator")])
    assert "jarvis-anomaly-investigator" in msg


def test_format_message_escapes_underscores_in_detail() -> None:
    """REGRESSION: Telegram Markdown parser crashes on underscores in
    free-form fields. Real bot IDs have underscores
    (``mnq_futures_sage``, ``rsi_mr_mnq_v2``), and the watcher's auto-
    generated detail string embeds them directly. Without escaping the
    underscores, Telegram returns 400 "Can't find end of the entity" and
    the alert silently drops.
    """
    from eta_engine.scripts import anomaly_telegram_pulse

    msg = anomaly_telegram_pulse._format_message(
        [
            _hit(
                bot_id="mnq_futures_sage",
                detail="mnq_futures_sage has 3 consecutive losses",
                pattern="loss_streak",
            )
        ]
    )
    # underscores in the pattern + detail must be backslash-escaped
    assert r"loss\_streak" in msg
    assert r"mnq\_futures\_sage has 3 consecutive losses" in msg
    # bot_id in the backtick code span is NOT escaped (literal)
    assert "`mnq_futures_sage`" in msg


def test_md_escape_handles_all_specials() -> None:
    """Backslash-escape every char Telegram MarkdownV1 treats as a delimiter."""
    from eta_engine.scripts import anomaly_telegram_pulse

    raw = "foo_bar *baz* [link] `code`"
    escaped = anomaly_telegram_pulse._md_escape(raw)
    assert "\\_" in escaped
    assert "\\*" in escaped
    assert "\\[" in escaped
    assert "\\]" in escaped
    assert "\\`" in escaped


# ---------------------------------------------------------------------------
# Positive event treatment (added 2026-05-12)
# ---------------------------------------------------------------------------


def test_format_message_all_positive_uses_celebration_header() -> None:
    """When every hit is a positive pattern, the header says 'Fleet celebration'."""
    from eta_engine.scripts import anomaly_telegram_pulse

    hits = [
        _hit(pattern="win_streak", bot_id="hotbot", severity="info", detail="6 wins"),
        _hit(
            pattern="fleet_hot_day",
            bot_id="__fleet__",
            severity="info",
            detail="Fleet up +5.5R",
        ),
    ]
    msg = anomaly_telegram_pulse._format_message(hits)
    assert "Fleet celebration" in msg
    assert "2 new events" in msg
    # Suggested-skill footer suppressed for pure-celebration
    assert "Suggested:" not in msg


def test_format_message_all_negative_uses_anomaly_header() -> None:
    """All-negative hits keep the legacy 'Anomaly pulse' header."""
    from eta_engine.scripts import anomaly_telegram_pulse

    hits = [_hit(pattern="loss_streak", bot_id="a"), _hit(pattern="loss_rate", bot_id="b")]
    msg = anomaly_telegram_pulse._format_message(hits)
    assert "Anomaly pulse" in msg
    assert "2 new hits" in msg
    assert "Suggested:" in msg


def test_format_message_mixed_uses_fleet_pulse_header() -> None:
    """Mixed positive+negative gets a 'Fleet pulse' header with both counts."""
    from eta_engine.scripts import anomaly_telegram_pulse

    hits = [
        _hit(pattern="loss_streak", bot_id="bad"),
        _hit(pattern="win_streak", bot_id="good", severity="info"),
        _hit(pattern="win_streak", bot_id="alsogood", severity="info"),
    ]
    msg = anomaly_telegram_pulse._format_message(hits)
    assert "Fleet pulse" in msg
    assert "1 issue" in msg
    assert "2 wins" in msg


def test_format_message_fleet_sentinel_renders_without_backticks() -> None:
    """`__fleet__` bot_id renders as bare 'fleet'-level event (no backticks)."""
    from eta_engine.scripts import anomaly_telegram_pulse

    hits = [
        _hit(
            pattern="fleet_drawdown",
            bot_id="__fleet__",
            severity="critical",
            detail="Fleet down -4.2R today",
        )
    ]
    msg = anomaly_telegram_pulse._format_message(hits)
    # No backtick code span around __fleet__
    assert "`__fleet__`" not in msg
    # Detail still rendered
    assert "Fleet down" in msg


def test_format_message_win_streak_uses_celebration_emoji() -> None:
    """win_streak (severity=info) should use the 🎉 (or info) emoji."""
    from eta_engine.scripts import anomaly_telegram_pulse

    msg = anomaly_telegram_pulse._format_message(
        [_hit(pattern="win_streak", severity="info", detail="6 wins in a row")]
    )
    # Body should contain the info-severity emoji (🎉 in the new mapping)
    assert "\U0001f389" in msg or "🎉" in msg


def test_sev_priority_escalates_to_critical() -> None:
    from eta_engine.scripts import anomaly_telegram_pulse

    assert anomaly_telegram_pulse._sev_priority([_hit(severity="warn")]) == "WARN"
    assert anomaly_telegram_pulse._sev_priority([_hit(severity="warn"), _hit(severity="critical")]) == "CRITICAL"
    assert anomaly_telegram_pulse._sev_priority([_hit(severity="info")]) == "INFO"


def test_sev_priority_empty_defaults_to_info() -> None:
    from eta_engine.scripts import anomaly_telegram_pulse

    assert anomaly_telegram_pulse._sev_priority([]) == "INFO"


def test_run_pulse_quiet_when_no_hits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No hits → no Telegram send, ok=True, n_new=0."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher
    from eta_engine.scripts import anomaly_telegram_pulse

    monkeypatch.setattr(anomaly_telegram_pulse, "_PULSE_LOG", tmp_path / "pulse.jsonl")
    monkeypatch.setattr(anomaly_watcher, "scan", lambda **kw: [])

    sent: list[str] = []

    def fake_send(text: str, priority: str = "INFO") -> dict[str, Any]:
        sent.append(text)
        return {"ok": True}

    from eta_engine.deploy.scripts import telegram_alerts

    monkeypatch.setattr(telegram_alerts, "send_from_env", fake_send)

    result = anomaly_telegram_pulse.run_pulse(dry_run=False)
    assert result["ok"] is True
    assert result["n_new"] == 0
    assert result["sent"] is False
    assert sent == []


def test_run_pulse_dry_run_skips_send_but_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run path scans + formats but never calls Telegram."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher
    from eta_engine.scripts import anomaly_telegram_pulse

    monkeypatch.setattr(anomaly_telegram_pulse, "_PULSE_LOG", tmp_path / "pulse.jsonl")

    fake_hit = anomaly_watcher.AnomalyHit(
        asof="2026-05-12T13:00:00+00:00",
        pattern="loss_streak",
        key="loss_streak:dry:3",
        bot_id="dry",
        severity="warn",
        detail="dry-run hit",
        suggested_skill="jarvis-anomaly-investigator",
        extras={},
    )
    monkeypatch.setattr(anomaly_watcher, "scan", lambda **kw: [fake_hit])

    sent: list[str] = []
    monkeypatch.setattr(
        "eta_engine.deploy.scripts.telegram_alerts.send_from_env",
        lambda text, priority="INFO": sent.append(text) or {"ok": True},
    )

    result = anomaly_telegram_pulse.run_pulse(dry_run=True)
    assert result["ok"] is True
    assert result["n_new"] == 1
    assert result["sent"] is False
    assert result["reason"] == "dry_run"
    assert "preview" in result
    assert "dry-run hit" in result["preview"]
    assert sent == []  # NOT sent

    # Log file written
    log = tmp_path / "pulse.jsonl"
    assert log.exists()
    lines = log.read_text(encoding="utf-8").strip().split("\n")
    rec = json.loads(lines[0])
    assert rec["n_new"] == 1


def test_run_pulse_real_send_when_hits_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When hits exist and not dry-run: one Telegram message, priority escalates."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher
    from eta_engine.scripts import anomaly_telegram_pulse

    monkeypatch.setattr(anomaly_telegram_pulse, "_PULSE_LOG", tmp_path / "pulse.jsonl")

    crit_hit = anomaly_watcher.AnomalyHit(
        asof="2026-05-12T13:00:00+00:00",
        pattern="loss_streak",
        key="loss_streak:crit:6",
        bot_id="crit",
        severity="critical",
        detail="6 losses in a row",
        suggested_skill="jarvis-anomaly-investigator",
        extras={},
    )
    monkeypatch.setattr(anomaly_watcher, "scan", lambda **kw: [crit_hit])

    sent: list[tuple[str, str]] = []

    def fake_send(text: str, priority: str = "INFO") -> dict[str, Any]:
        sent.append((text, priority))
        return {"ok": True, "result": {"message_id": 42}}

    import eta_engine.deploy.scripts.telegram_alerts as telegram_alerts_mod

    monkeypatch.setattr(telegram_alerts_mod, "send_from_env", fake_send)

    result = anomaly_telegram_pulse.run_pulse(dry_run=False)
    assert result["ok"] is True
    assert result["n_new"] == 1
    assert result["sent"] is True
    assert result["priority"] == "CRITICAL"
    assert len(sent) == 1
    body, prio = sent[0]
    assert prio == "CRITICAL"
    # underscore in pattern is escaped for Markdown safety
    assert r"loss\_streak" in body
    assert "crit" in body


def test_run_pulse_never_raises_on_scan_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken scan() must NOT crash the cron entry point."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher
    from eta_engine.scripts import anomaly_telegram_pulse

    monkeypatch.setattr(anomaly_telegram_pulse, "_PULSE_LOG", tmp_path / "pulse.jsonl")

    def boom(**kw: Any) -> Any:
        raise RuntimeError("simulated scan crash")

    monkeypatch.setattr(anomaly_watcher, "scan", boom)

    result = anomaly_telegram_pulse.run_pulse(dry_run=False)
    assert result["ok"] is False
    assert "simulated scan crash" in result["error"]
    assert result["n_new"] == 0


def test_main_exits_zero_when_quiet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cron-friendly: main() returns 0 even when nothing to report."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher
    from eta_engine.scripts import anomaly_telegram_pulse

    monkeypatch.setattr(anomaly_telegram_pulse, "_PULSE_LOG", tmp_path / "pulse.jsonl")
    monkeypatch.setattr(anomaly_watcher, "scan", lambda **kw: [])

    rc = anomaly_telegram_pulse.main(["--dry-run", "--json"])
    assert rc == 0
