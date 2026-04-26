"""Tests for ``apex_predator.obs.mcc_push_sender``.

The sender is **safe-by-default**: missing pywebpush, missing VAPID
env, or missing subscriptions all yield a :class:`PushResult` with
``attempted=0`` and a ``skipped`` reason -- never raises. These
tests pin that contract so an alert dispatcher running on a phone-less
dev box never blows up.

The actual webpush call is exercised via a stubbed ``pywebpush`` module
inserted into ``sys.modules`` -- the real network send is never made.
"""

from __future__ import annotations

import json
import sys
import types
from typing import TYPE_CHECKING

import pytest

from apex_predator.obs import mcc_push_sender as ps

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def push_state(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ps, "PUSH_SUBSCRIPTIONS", tmp_path / "push.jsonl")
    return tmp_path


def _write_subs(path: Path, subs: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(s) for s in subs) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Subscription IO
# ---------------------------------------------------------------------------


class TestReadSubscriptions:
    def test_no_file_returns_empty(self, push_state: Path) -> None:
        assert ps.read_subscriptions() == []

    def test_round_trip(self, push_state: Path) -> None:
        subs = [
            {"endpoint": "https://fcm.googleapis.com/fcm/send/A", "keys": {"p256dh": "BAA...", "auth": "x"}},
            {
                "endpoint": "https://updates.push.services.mozilla.com/wpush/v1/B",
                "keys": {"p256dh": "BBB...", "auth": "y"},
            },
        ]
        _write_subs(push_state / "push.jsonl", subs)
        out = ps.read_subscriptions()
        assert out == subs

    def test_skips_records_missing_endpoint_or_keys(self, push_state: Path) -> None:
        _write_subs(
            push_state / "push.jsonl",
            [
                {"endpoint": "https://x", "keys": {"p256dh": "k", "auth": "a"}},  # ok
                {"endpoint": "https://y"},  # no keys
                {"keys": {"p256dh": "k", "auth": "a"}},  # no endpoint
                {"endpoint": "https://z", "keys": "not-a-dict"},  # wrong type
            ],
        )
        out = ps.read_subscriptions()
        assert len(out) == 1
        assert out[0]["endpoint"] == "https://x"

    def test_malformed_lines_skipped(self, push_state: Path) -> None:
        (push_state / "push.jsonl").write_text(
            'not json\n{"endpoint":"https://x","keys":{"p256dh":"k","auth":"a"}}\n',
            encoding="utf-8",
        )
        out = ps.read_subscriptions()
        assert len(out) == 1


# ---------------------------------------------------------------------------
# send_to_all -- the safe-by-default + happy paths
# ---------------------------------------------------------------------------


class TestSendToAllSafeByDefault:
    """No deps / no env / no subs => structured no-op result, never raises."""

    def test_no_pywebpush_returns_skip(self, push_state: Path, monkeypatch) -> None:
        # Force pywebpush to look uninstalled by removing both from sys.modules
        # AND blocking the import.
        monkeypatch.delitem(sys.modules, "pywebpush", raising=False)
        original_find = sys.meta_path

        class _Block:
            def find_spec(self, name, path=None, target=None):
                if name == "pywebpush":
                    raise ImportError("pywebpush blocked by test")
                return None

        monkeypatch.setattr(sys, "meta_path", [_Block(), *original_find])
        # Even with VAPID env set, missing dep takes precedence.
        monkeypatch.setenv("MCC_VAPID_PUBLIC_KEY", "x")
        monkeypatch.setenv("MCC_VAPID_PRIVATE_KEY", "y")
        monkeypatch.setenv("MCC_VAPID_SUBJECT", "mailto:o@example.com")

        result = ps.send_to_all("info", "title", "body")
        assert result.attempted == 0
        assert result.delivered == 0
        assert result.failed == 0
        assert "pywebpush-not-installed" in result.skipped

    def test_no_vapid_env_returns_skip(self, push_state: Path, monkeypatch) -> None:
        # Insert a stub pywebpush so the dep check passes.
        _install_stub_pywebpush(monkeypatch)
        for key in ("MCC_VAPID_PUBLIC_KEY", "MCC_VAPID_PRIVATE_KEY", "MCC_VAPID_SUBJECT"):
            monkeypatch.delenv(key, raising=False)
        result = ps.send_to_all("info", "title", "body")
        assert result.attempted == 0
        assert "vapid-env-missing" in result.skipped

    def test_no_subscriptions_returns_skip(self, push_state: Path, monkeypatch) -> None:
        _install_stub_pywebpush(monkeypatch)
        monkeypatch.setenv("MCC_VAPID_PUBLIC_KEY", "pub")
        monkeypatch.setenv("MCC_VAPID_PRIVATE_KEY", "priv")
        monkeypatch.setenv("MCC_VAPID_SUBJECT", "mailto:o@example.com")
        # No subscriptions file written.
        result = ps.send_to_all("info", "title", "body")
        assert result.attempted == 0
        assert "no-subscriptions" in result.skipped


# ---------------------------------------------------------------------------
# send_to_all -- happy + failure paths via stubbed pywebpush
# ---------------------------------------------------------------------------


def _install_stub_pywebpush(monkeypatch, *, fail_endpoints: set[str] | None = None) -> list[dict]:
    """Install a stub pywebpush module. Returns the list of received calls."""
    received: list[dict] = []
    fail_endpoints = fail_endpoints or set()

    class WebPushException(Exception):  # noqa: N818 -- mirror real name
        def __init__(self, message: str, response: object = None) -> None:
            super().__init__(message)
            self.response = response

    class _Resp:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    def webpush(**kwargs):
        endpoint = kwargs["subscription_info"]["endpoint"]
        received.append(kwargs)
        if endpoint in fail_endpoints:
            raise WebPushException("simulated failure", response=_Resp(410))
        return None

    stub = types.ModuleType("pywebpush")
    stub.webpush = webpush  # type: ignore[attr-defined]
    stub.WebPushException = WebPushException  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pywebpush", stub)
    return received


class TestSendToAllHappyPath:
    def test_delivers_to_all_subscriptions(self, push_state: Path, monkeypatch) -> None:
        received = _install_stub_pywebpush(monkeypatch)
        monkeypatch.setenv("MCC_VAPID_PUBLIC_KEY", "pub")
        monkeypatch.setenv("MCC_VAPID_PRIVATE_KEY", "priv-key-bytes")
        monkeypatch.setenv("MCC_VAPID_SUBJECT", "mailto:o@example.com")

        _write_subs(
            push_state / "push.jsonl",
            [
                {"endpoint": "https://A", "keys": {"p256dh": "K1", "auth": "x"}},
                {"endpoint": "https://B", "keys": {"p256dh": "K2", "auth": "y"}},
            ],
        )

        result = ps.send_to_all("warn", "Drift WARN", "kl=0.18")
        assert result.attempted == 2
        assert result.delivered == 2
        assert result.failed == 0
        assert result.ok is True

        # Two webpush calls landed; payload was JSON with our fields.
        assert len(received) == 2
        payloads = [json.loads(call["data"]) for call in received]
        for p in payloads:
            assert p["title"] == "Drift WARN"
            assert p["body"] == "kl=0.18"
            assert p["severity"] == "warn"
        # VAPID config was forwarded.
        assert received[0]["vapid_private_key"] == "priv-key-bytes"
        assert received[0]["vapid_claims"]["sub"] == "mailto:o@example.com"

    def test_per_severity_urgency_and_ttl(self, push_state: Path, monkeypatch) -> None:
        received = _install_stub_pywebpush(monkeypatch)
        monkeypatch.setenv("MCC_VAPID_PUBLIC_KEY", "p")
        monkeypatch.setenv("MCC_VAPID_PRIVATE_KEY", "k")
        monkeypatch.setenv("MCC_VAPID_SUBJECT", "mailto:o@example.com")
        _write_subs(push_state / "push.jsonl", [{"endpoint": "https://A", "keys": {"p256dh": "k", "auth": "a"}}])

        ps.send_to_all("critical", "Breaker TRIPPED", "scope=global")
        assert received[-1]["headers"]["Urgency"] == "high"
        assert received[-1]["ttl"] == 120

        ps.send_to_all("info", "ok", "ok")
        assert received[-1]["headers"]["Urgency"] == "low"

    def test_partial_failure_counts_correctly(self, push_state: Path, monkeypatch) -> None:
        _install_stub_pywebpush(monkeypatch, fail_endpoints={"https://B"})
        monkeypatch.setenv("MCC_VAPID_PUBLIC_KEY", "p")
        monkeypatch.setenv("MCC_VAPID_PRIVATE_KEY", "k")
        monkeypatch.setenv("MCC_VAPID_SUBJECT", "mailto:o@example.com")

        _write_subs(
            push_state / "push.jsonl",
            [
                {"endpoint": "https://A", "keys": {"p256dh": "k", "auth": "a"}},
                {"endpoint": "https://B", "keys": {"p256dh": "k", "auth": "a"}},
                {"endpoint": "https://C", "keys": {"p256dh": "k", "auth": "a"}},
            ],
        )

        result = ps.send_to_all("critical", "kill", "manual trip")
        assert result.attempted == 3
        assert result.delivered == 2
        assert result.failed == 1
        assert result.ok is False  # any failure flips ok to False


# ---------------------------------------------------------------------------
# alert_dispatcher integration: 'mcc_push' channel routes through send_to_all
# ---------------------------------------------------------------------------


class TestAlertDispatcherIntegration:
    def test_mcc_push_channel_routes_via_send_to_all(self, push_state: Path, monkeypatch) -> None:
        received = _install_stub_pywebpush(monkeypatch)
        monkeypatch.setenv("MCC_VAPID_PUBLIC_KEY", "p")
        monkeypatch.setenv("MCC_VAPID_PRIVATE_KEY", "k")
        monkeypatch.setenv("MCC_VAPID_SUBJECT", "mailto:o@example.com")
        _write_subs(push_state / "push.jsonl", [{"endpoint": "https://A", "keys": {"p256dh": "k", "auth": "a"}}])

        from apex_predator.obs.alert_dispatcher import AlertDispatcher

        cfg = {
            "channels": {"mcc_push": {"enabled": True}},
            "rate_limit": {"info": {"window_sec": 1, "max": 10}},
            "routing": {
                "events": {"breaker_trip": {"level": "critical", "channels": ["mcc_push"]}},
            },
        }
        d = AlertDispatcher(cfg, log_path=push_state / "dispatch.jsonl")
        result = d.send("breaker_trip", {"reason": "test"})
        assert "mcc_push" in result.delivered
        assert len(received) == 1
        payload = json.loads(received[0]["data"])
        assert payload["severity"] == "critical"
        assert payload["title"]
