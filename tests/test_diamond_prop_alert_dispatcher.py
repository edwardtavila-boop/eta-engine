"""Tests for the wave-24 prop-fund alert dispatcher."""

# ruff: noqa: PLR2004, SLF001
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _alert(
    severity: str = "RED",
    source: str = "diamond_prop_drawdown_guard",
    ts: str = "2026-05-13T01:00:00+00:00",
    headline: str = "PROP GUARD HALT",
) -> dict:
    return {
        "timestamp_utc": ts,
        "ts": ts,
        "severity": severity,
        "source": source,
        "alert_id": f"prop_guard_{severity.lower()}",
        "headline": headline,
        "details": {"signal": "HALT"},
    }


def _write_log(path: Path, alerts: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for a in alerts:
            fh.write(json.dumps(a) + "\n")


@pytest.fixture(autouse=True)
def _isolated_workspace_root(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("ETA_WORKSPACE_ROOT", str(tmp_path))


# ────────────────────────────────────────────────────────────────────
# Channel detection
# ────────────────────────────────────────────────────────────────────


def test_no_channels_when_no_env(monkeypatch: object) -> None:
    """No env vars set = empty channel list (graceful degradation)."""
    from eta_engine.scripts import diamond_prop_alert_dispatcher as ad

    monkeypatch.delenv("ETA_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ETA_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("ETA_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ETA_GENERIC_WEBHOOK_URL", raising=False)
    assert ad.configured_channels() == []


def test_telegram_detected_when_both_env_set(monkeypatch: object) -> None:
    from eta_engine.scripts import diamond_prop_alert_dispatcher as ad

    monkeypatch.setenv("ETA_TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ETA_TELEGRAM_CHAT_ID", "999")
    monkeypatch.delenv("ETA_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ETA_GENERIC_WEBHOOK_URL", raising=False)
    assert "telegram" in ad.configured_channels()


def test_telegram_NOT_detected_when_only_token_set(monkeypatch: object) -> None:
    """Telegram needs BOTH token + chat_id; either alone = no channel."""
    from eta_engine.scripts import diamond_prop_alert_dispatcher as ad

    monkeypatch.setenv("ETA_TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("ETA_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("ETA_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ETA_GENERIC_WEBHOOK_URL", raising=False)
    assert "telegram" not in ad.configured_channels()


def test_telegram_detected_from_canonical_secret_files(tmp_path: Path, monkeypatch: object) -> None:
    from eta_engine.scripts import diamond_prop_alert_dispatcher as ad

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "telegram_bot_token.txt").write_text(
        "123456789:ABCDEFGHIJKLMNOPQRSTUV_abcdefghi",
        encoding="utf-8",
    )
    (secrets_dir / "telegram_chat_id.txt").write_text("-1001234567890", encoding="utf-8")
    monkeypatch.delenv("ETA_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ETA_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("ETA_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ETA_GENERIC_WEBHOOK_URL", raising=False)

    assert "telegram" in ad.configured_channels()


# ────────────────────────────────────────────────────────────────────
# Cursor-based dedup
# ────────────────────────────────────────────────────────────────────


def test_cursor_advances_after_dispatch(tmp_path: Path, monkeypatch: object) -> None:
    """After successful dispatch, cursor moves to last-dispatched timestamp."""
    from eta_engine.scripts import diamond_prop_alert_dispatcher as ad

    log = tmp_path / "alerts.jsonl"
    cursor = tmp_path / "cursor.json"
    out = tmp_path / "out.json"
    _write_log(log, [_alert(ts="2026-05-13T01:00:00+00:00")])
    monkeypatch.setattr(ad, "ALERTS_LOG", log)
    monkeypatch.setattr(ad, "CURSOR_PATH", cursor)
    monkeypatch.setattr(ad, "OUT_LATEST", out)
    # Pretend telegram is configured + always succeeds
    monkeypatch.setenv("ETA_TELEGRAM_BOT_TOKEN", "fake")
    monkeypatch.setenv("ETA_TELEGRAM_CHAT_ID", "fake")
    monkeypatch.setattr(ad, "_send_telegram", lambda _t: None)
    monkeypatch.delenv("ETA_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ETA_GENERIC_WEBHOOK_URL", raising=False)

    summary = ad.run()
    assert summary["n_alerts_dispatched"] == 1
    cursor_data = json.loads(cursor.read_text(encoding="utf-8"))
    assert cursor_data["last_dispatched_ts"] == "2026-05-13T01:00:00+00:00"


def test_cursor_blocks_replay(tmp_path: Path, monkeypatch: object) -> None:
    """Already-dispatched alerts (older than cursor) are not re-sent."""
    from eta_engine.scripts import diamond_prop_alert_dispatcher as ad

    log = tmp_path / "alerts.jsonl"
    cursor = tmp_path / "cursor.json"
    out = tmp_path / "out.json"
    _write_log(log, [_alert(ts="2026-05-13T01:00:00+00:00")])
    cursor.write_text(
        json.dumps(
            {
                "last_dispatched_ts": "2026-05-13T02:00:00+00:00",  # newer
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ad, "ALERTS_LOG", log)
    monkeypatch.setattr(ad, "CURSOR_PATH", cursor)
    monkeypatch.setattr(ad, "OUT_LATEST", out)
    monkeypatch.setenv("ETA_TELEGRAM_BOT_TOKEN", "fake")
    monkeypatch.setenv("ETA_TELEGRAM_CHAT_ID", "fake")
    monkeypatch.setattr(ad, "_send_telegram", lambda _t: None)
    monkeypatch.delenv("ETA_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ETA_GENERIC_WEBHOOK_URL", raising=False)

    summary = ad.run()
    assert summary["n_alerts_seen"] == 0  # all alerts before cursor
    assert summary["n_alerts_dispatched"] == 0


# ────────────────────────────────────────────────────────────────────
# Source filter
# ────────────────────────────────────────────────────────────────────


def test_irrelevant_sources_ignored(tmp_path: Path, monkeypatch: object) -> None:
    """Alerts from non-RELEVANT_SOURCES are skipped."""
    from eta_engine.scripts import diamond_prop_alert_dispatcher as ad

    log = tmp_path / "alerts.jsonl"
    cursor = tmp_path / "cursor.json"
    out = tmp_path / "out.json"
    _write_log(
        log,
        [
            _alert(source="some_other_audit", ts="2026-05-13T01:00:00+00:00"),
            _alert(source="diamond_prop_drawdown_guard", ts="2026-05-13T01:01:00+00:00"),
        ],
    )
    monkeypatch.setattr(ad, "ALERTS_LOG", log)
    monkeypatch.setattr(ad, "CURSOR_PATH", cursor)
    monkeypatch.setattr(ad, "OUT_LATEST", out)
    monkeypatch.setenv("ETA_TELEGRAM_BOT_TOKEN", "fake")
    monkeypatch.setenv("ETA_TELEGRAM_CHAT_ID", "fake")
    monkeypatch.setattr(ad, "_send_telegram", lambda _t: None)
    monkeypatch.delenv("ETA_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ETA_GENERIC_WEBHOOK_URL", raising=False)

    summary = ad.run()
    assert summary["n_alerts_seen"] == 1  # only the drawdown_guard one


# ────────────────────────────────────────────────────────────────────
# Graceful degradation: no channels = advance cursor, don't crash
# ────────────────────────────────────────────────────────────────────


def test_no_channels_still_advances_cursor(tmp_path: Path, monkeypatch: object) -> None:
    """If no push channels are configured, the dispatcher still
    advances the cursor so old alerts won't replay once channels
    are added."""
    from eta_engine.scripts import diamond_prop_alert_dispatcher as ad

    log = tmp_path / "alerts.jsonl"
    cursor = tmp_path / "cursor.json"
    out = tmp_path / "out.json"
    _write_log(log, [_alert(ts="2026-05-13T01:00:00+00:00")])
    monkeypatch.setattr(ad, "ALERTS_LOG", log)
    monkeypatch.setattr(ad, "CURSOR_PATH", cursor)
    monkeypatch.setattr(ad, "OUT_LATEST", out)
    monkeypatch.delenv("ETA_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ETA_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("ETA_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ETA_GENERIC_WEBHOOK_URL", raising=False)

    summary = ad.run()
    assert summary["n_alerts_seen"] == 1
    assert summary["n_alerts_skipped_no_channels"] == 1
    assert summary["n_alerts_dispatched"] == 0
    # cursor advanced anyway so once channels added, old alert won't replay
    assert summary["cursor_after"] == "2026-05-13T01:00:00+00:00"


# ────────────────────────────────────────────────────────────────────
# Dry-run mode
# ────────────────────────────────────────────────────────────────────


def test_dry_run_does_not_send_or_advance_cursor(tmp_path: Path, monkeypatch: object) -> None:
    """--dry-run computes everything but never POSTs and never moves cursor."""
    from eta_engine.scripts import diamond_prop_alert_dispatcher as ad

    log = tmp_path / "alerts.jsonl"
    cursor = tmp_path / "cursor.json"
    out = tmp_path / "out.json"
    _write_log(log, [_alert(ts="2026-05-13T01:00:00+00:00")])
    monkeypatch.setattr(ad, "ALERTS_LOG", log)
    monkeypatch.setattr(ad, "CURSOR_PATH", cursor)
    monkeypatch.setattr(ad, "OUT_LATEST", out)
    monkeypatch.setenv("ETA_TELEGRAM_BOT_TOKEN", "fake")
    monkeypatch.setenv("ETA_TELEGRAM_CHAT_ID", "fake")

    # If the sender is called, this fixture would raise — proving dry_run
    # actually blocks the network call.
    def _no_send(_t: str) -> None:
        msg = "should not be called in dry_run"
        raise AssertionError(msg)

    monkeypatch.setattr(ad, "_send_telegram", _no_send)
    monkeypatch.delenv("ETA_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ETA_GENERIC_WEBHOOK_URL", raising=False)

    summary = ad.run(dry_run=True)
    assert summary["n_alerts_seen"] == 1
    assert not cursor.exists()  # cursor not advanced
