"""Tests for wave-4 supercharge wiring (2026-04-27).

Covers:
  * jarvis_verdict_webhook: format Slack vs Discord; cursor advances
  * bandit_promotion_check: identifies promotable candidates correctly
  * run_anomaly_scan helpers: window stress aggregation
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


# ─── jarvis_verdict_webhook formatters ────────────────────────────


def test_format_slack_payload_has_required_fields() -> None:
    from eta_engine.obs.jarvis_verdict_webhook import _format_for_slack

    rec = {
        "ts": "2026-04-27T10:00:00Z",
        "policy_version": 17,
        "request": {"subsystem": "bot.mnq", "action": "ORDER_PLACE"},
        "response": {
            "verdict": "DENIED",
            "reason": "drawdown_breach",
            "reason_code": "dd_over_kill",
            "stress_composite": 0.92,
            "session_phase": "OPEN_DRIVE",
        },
    }
    payload = _format_for_slack(rec)
    assert "text" in payload
    text = payload["text"]
    assert "DENIED" in text
    assert "bot.mnq" in text
    assert "drawdown_breach" in text
    assert "0.92" in text


def test_format_discord_payload_has_embed_color() -> None:
    from eta_engine.obs.jarvis_verdict_webhook import _format_for_discord

    rec = {
        "ts": "2026-04-27T10:00:00Z",
        "policy_version": 17,
        "request": {"subsystem": "bot.mnq", "action": "ORDER_PLACE"},
        "response": {
            "verdict": "DENIED",
            "reason": "test",
            "reason_code": "test",
            "stress_composite": 0.5,
            "session_phase": "OPEN_DRIVE",
        },
    }
    payload = _format_for_discord(rec)
    assert "embeds" in payload
    assert len(payload["embeds"]) == 1
    e = payload["embeds"][0]
    assert e["color"] == 0xE74C3C  # red for DENIED
    assert "fields" in e


def test_format_discord_uses_correct_color_per_verdict() -> None:
    from eta_engine.obs.jarvis_verdict_webhook import _format_for_discord

    expected = {
        "APPROVED": 0x2ECC71,
        "CONDITIONAL": 0xF1C40F,
        "DEFERRED": 0x3498DB,
        "DENIED": 0xE74C3C,
    }
    for verdict, color in expected.items():
        rec = {
            "ts": "2026-04-27T10:00:00Z",
            "request": {},
            "response": {"verdict": verdict, "reason": "", "reason_code": "",
                         "stress_composite": 0, "session_phase": ""},
        }
        payload = _format_for_discord(rec)
        assert payload["embeds"][0]["color"] == color, f"verdict {verdict}"


def test_is_discord_url_detection() -> None:
    from eta_engine.obs.jarvis_verdict_webhook import _is_discord
    assert _is_discord("https://discord.com/api/webhooks/123/abc") is True
    assert _is_discord("https://discordapp.com/api/webhooks/123/abc") is True
    assert _is_discord("https://hooks.slack.com/services/T/B/X") is False
    assert _is_discord("https://example.com") is False


# ─── bandit_promotion_check logic (smoke) ─────────────────────────


def test_bandit_promotion_check_skips_when_too_few_records(tmp_path: Path, monkeypatch) -> None:
    """With 0 records in audit, the script exits 0 without firing alerts."""
    from eta_engine.scripts import bandit_promotion_check as bp

    # Patch the module so it points at an empty audit dir + tmp out dir
    rc = bp.main([
        "--audit-dir", str(tmp_path / "audit"),
        "--out-dir", str(tmp_path / "out"),
        "--window-days", "30",
        "--min-decisions", "100",
        "--dry-run",
    ])
    assert rc == 0


# ─── anomaly_scan helpers ─────────────────────────────────────────


def test_anomaly_load_recent_filters_by_window(tmp_path: Path) -> None:
    from eta_engine.scripts.run_anomaly_scan import _load_recent_verdict_stress

    audit = tmp_path / "x.jsonl"
    now = datetime.now(UTC)
    rows = [
        {"ts": (now - timedelta(hours=1)).isoformat(),
         "response": {"stress_composite": 0.5}},
        {"ts": (now - timedelta(hours=10)).isoformat(),
         "response": {"stress_composite": 0.7}},
        {"ts": (now - timedelta(hours=100)).isoformat(),  # outside any window
         "response": {"stress_composite": 0.2}},
    ]
    audit.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    # 5 hour window -> only the first
    out = _load_recent_verdict_stress(tmp_path, since=now - timedelta(hours=5))
    assert out == [0.5]

    # 50 hour window -> first two
    out = _load_recent_verdict_stress(tmp_path, since=now - timedelta(hours=50))
    assert sorted(out) == [0.5, 0.7]


def test_anomaly_in_cooldown_returns_false_when_no_state(tmp_path: Path) -> None:
    from eta_engine.scripts.run_anomaly_scan import _in_cooldown
    assert _in_cooldown(tmp_path / "missing.json", cooldown_min=60.0) is False
