"""Tests for obs.alert_dispatcher — routing, rate-limit, logging."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
import yaml

from eta_engine.obs import alert_dispatcher as mod
from eta_engine.obs.alert_dispatcher import AlertDispatcher, _RateLimiter

if TYPE_CHECKING:
    from pathlib import Path


# --------------------------------------------------------------------------- #
# Autouse: stub the real transports so routing tests don't hit Pushover/SMTP/Twilio.
# New tests that want to exercise the real functions override the stub explicitly.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _stub_transports(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mod, "_send_pushover", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "_send_email", lambda *a, **kw: True)
    monkeypatch.setattr(mod, "_send_sms", lambda *a, **kw: True)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _cfg() -> dict:
    return {
        "rate_limit": {
            "info_per_minute": 10,
            "warn_per_minute": 5,
            "critical_per_minute": 0,  # unthrottled
        },
        "channels": {
            "pushover": {
                "enabled": True,
                "env_keys": {"user": "PUSHOVER_USER", "token": "PUSHOVER_TOKEN"},
            },
            "email": {
                "enabled": True,
                "env_keys": {
                    "smtp_host": "SMTP_HOST",
                    "smtp_port": "SMTP_PORT",
                    "smtp_user": "SMTP_USER",
                    "smtp_pass": "SMTP_PASS",
                },
                "to": "edward.t.avila@gmail.com",
                "from": "eta-engine@thefirm.local",
            },
            "sms": {
                "enabled": False,
                "env_keys": {
                    "twilio_sid": "TWILIO_SID",
                    "twilio_token": "TWILIO_TOKEN",
                    "from_number": "TWILIO_FROM",
                },
                "to_number": "+15555550123",
            },
        },
        "routing": {
            "events": {
                "bot_entry": {"level": "info", "channels": ["pushover"]},
                "kill_switch": {"level": "critical", "channels": ["pushover", "email", "sms"]},
                "weekly_review": {"level": "info", "channels": ["email"]},
            },
        },
    }


@pytest.fixture()
def dispatcher(tmp_path: Path):
    return AlertDispatcher(_cfg(), log_path=tmp_path / "alerts.jsonl")


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #
def test_rate_limiter_zero_means_unthrottled():
    rl = _RateLimiter(per_minute=0)
    now = time.time()
    for _ in range(1000):
        assert rl.allow(now) is True


def test_rate_limiter_enforces_cap_within_window():
    rl = _RateLimiter(per_minute=3)
    now = 1000.0
    assert rl.allow(now) is True
    assert rl.allow(now + 1) is True
    assert rl.allow(now + 2) is True
    assert rl.allow(now + 3) is False  # cap hit
    # Window slides — past 60s the oldest prunes out
    assert rl.allow(now + 61) is True


# --------------------------------------------------------------------------- #
# Unknown event
# --------------------------------------------------------------------------- #
def test_unknown_event_is_logged_but_not_dispatched(dispatcher, tmp_path: Path):
    r = dispatcher.send("not_a_real_event", {"foo": "bar"})
    assert r.delivered == []
    assert r.level == "unknown"
    assert r.blocked and "unknown event" in r.blocked[0]
    log = (tmp_path / "alerts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(log) == 1
    assert json.loads(log[0])["event"] == "not_a_real_event"


# --------------------------------------------------------------------------- #
# Routing: creds present
# --------------------------------------------------------------------------- #
def test_delivers_when_creds_and_enabled(dispatcher, monkeypatch):
    monkeypatch.setenv("PUSHOVER_USER", "u")
    monkeypatch.setenv("PUSHOVER_TOKEN", "t")
    r = dispatcher.send("bot_entry", {"bot": "mnq"})
    assert "pushover" in r.delivered
    assert r.blocked == []
    assert r.level == "info"


def test_blocks_disabled_channel(dispatcher, monkeypatch):
    monkeypatch.setenv("PUSHOVER_USER", "u")
    monkeypatch.setenv("PUSHOVER_TOKEN", "t")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASS", "p")
    r = dispatcher.send("kill_switch", {"reason": "test"})
    assert "pushover" in r.delivered
    assert "email" in r.delivered
    assert any("sms:disabled" in b for b in r.blocked)


def test_blocks_creds_missing(dispatcher, monkeypatch):
    # Ensure env keys are empty
    for k in ("PUSHOVER_USER", "PUSHOVER_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    r = dispatcher.send("bot_entry", {"bot": "mnq"})
    assert r.delivered == []
    assert any("pushover:creds_missing" in b for b in r.blocked)


# --------------------------------------------------------------------------- #
# Rate-limit behavior end-to-end
# --------------------------------------------------------------------------- #
def test_rate_limit_info_blocks_after_cap(tmp_path: Path, monkeypatch):
    # Tight cap so we can exercise it.
    cfg = _cfg()
    cfg["rate_limit"]["info_per_minute"] = 2
    monkeypatch.setenv("PUSHOVER_USER", "u")
    monkeypatch.setenv("PUSHOVER_TOKEN", "t")
    d = AlertDispatcher(cfg, log_path=tmp_path / "a.jsonl")
    assert d.send("bot_entry", {}).delivered == ["pushover"]
    assert d.send("bot_entry", {}).delivered == ["pushover"]
    r = d.send("bot_entry", {})
    assert r.delivered == []
    assert any("rate_limited" in b for b in r.blocked)


def test_critical_unthrottled(tmp_path: Path, monkeypatch):
    # critical_per_minute=0 → no limit, even at burst
    monkeypatch.setenv("PUSHOVER_USER", "u")
    monkeypatch.setenv("PUSHOVER_TOKEN", "t")
    monkeypatch.setenv("SMTP_HOST", "h")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASS", "p")
    d = AlertDispatcher(_cfg(), log_path=tmp_path / "a.jsonl")
    for _ in range(20):
        r = d.send("kill_switch", {"reason": "burst"})
        assert "pushover" in r.delivered


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def test_each_send_writes_jsonl_line(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PUSHOVER_USER", "u")
    monkeypatch.setenv("PUSHOVER_TOKEN", "t")
    d = AlertDispatcher(_cfg(), log_path=tmp_path / "a.jsonl")
    d.send("bot_entry", {"n": 1})
    d.send("bot_entry", {"n": 2})
    lines = (tmp_path / "a.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert [json.loads(l)["payload"]["n"] for l in lines] == [1, 2]


# --------------------------------------------------------------------------- #
# from_yaml
# --------------------------------------------------------------------------- #
def test_from_yaml_parses(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PUSHOVER_USER", "u")
    monkeypatch.setenv("PUSHOVER_TOKEN", "t")
    p = tmp_path / "alerts.yaml"
    p.write_text(yaml.safe_dump(_cfg()), encoding="utf-8")
    d = AlertDispatcher.from_yaml(p, log_path=tmp_path / "a.jsonl")
    r = d.send("bot_entry", {"bot": "mnq"})
    assert "pushover" in r.delivered
