"""
EVOLUTIONARY TRADING ALGO  //  tests.test_alert_transports
==============================================
Exercise the concrete transport functions in obs.alert_dispatcher:
    _send_pushover, _send_email, _send_sms.

Uses monkeypatched urllib / smtplib so no real network.
"""

from __future__ import annotations

import base64
import io
import urllib.error
from typing import Any

import pytest  # noqa: TC002 - used for pytest.MonkeyPatch type hint under `from __future__ import annotations`

from eta_engine.obs import alert_dispatcher as mod


# --------------------------------------------------------------------------- #
# Pushover
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *a: Any) -> None:  # noqa: ANN401 - exit signature must accept arbitrary args
        return None


def test_send_pushover_returns_true_on_api_status_1(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(req, timeout=None) -> _FakeResp:  # noqa: ANN001 - mirrors urlopen signature
        captured["url"] = req.full_url
        captured["data"] = req.data
        return _FakeResp(200, b'{"status":1, "request":"abc"}')

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    ok = mod._send_pushover("USER", "TOKEN", "hello", "world", priority=1)
    assert ok is True
    assert "pushover.net" in captured["url"]
    # Body is URL-encoded form
    assert b"user=USER" in captured["data"]
    assert b"token=TOKEN" in captured["data"]
    assert b"priority=1" in captured["data"]


def test_send_pushover_returns_false_on_api_status_0(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req, timeout=None) -> _FakeResp:  # noqa: ANN001 - mirrors urlopen signature
        return _FakeResp(200, b'{"status":0, "errors":["invalid token"]}')

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    assert mod._send_pushover("u", "t", "title", "body") is False


def test_send_pushover_returns_false_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req, timeout=None) -> _FakeResp:  # noqa: ANN001 - mirrors urlopen signature
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    assert mod._send_pushover("u", "t", "title", "body") is False


def test_send_pushover_truncates_long_title_and_message(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(req, timeout=None) -> _FakeResp:  # noqa: ANN001 - mirrors urlopen signature
        captured["data"] = req.data
        return _FakeResp(200, b'{"status":1}')

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    long_title = "X" * 500
    long_msg = "Y" * 2000
    mod._send_pushover("u", "t", long_title, long_msg)
    # Title capped at 250, body capped at 1024.
    assert b"X" * 250 in captured["data"]
    assert b"X" * 251 not in captured["data"]
    assert b"Y" * 1024 in captured["data"]
    assert b"Y" * 1025 not in captured["data"]


# --------------------------------------------------------------------------- #
# SMTP email
# --------------------------------------------------------------------------- #
class _FakeSMTP:
    def __init__(self, host: str, port: int, timeout: float = 10) -> None:  # noqa: ARG002
        self.host = host
        self.port = port
        self.ehlo_called = 0
        self.starttls_called = False
        self.logged_in_with: tuple[str, str] | None = None
        self.sent: list[tuple[str, list[str], str]] = []
        self._has_starttls = True
        self.quit_called = False

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *a: Any) -> None:  # noqa: ANN401 - context-manager exit signature
        self.quit_called = True

    def ehlo(self) -> None:
        self.ehlo_called += 1

    def has_extn(self, name: str) -> bool:
        return name == "STARTTLS" and self._has_starttls

    def starttls(self) -> None:
        self.starttls_called = True

    def login(self, user: str, password: str) -> None:
        self.logged_in_with = (user, password)

    def sendmail(self, from_addr: str, to_addrs: list[str], msg: str) -> None:
        self.sent.append((from_addr, to_addrs, msg))


def test_send_email_login_and_sendmail(monkeypatch: pytest.MonkeyPatch) -> None:
    holder: dict[str, _FakeSMTP] = {}

    def factory(host: str, port: int, timeout: float = 10) -> _FakeSMTP:
        smtp = _FakeSMTP(host, port, timeout)
        holder["smtp"] = smtp
        return smtp

    monkeypatch.setattr(mod.smtplib, "SMTP", factory)
    ok = mod._send_email(
        "smtp.example.com",
        587,
        "user@x",
        "secret",
        "from@x",
        "to@x",
        "APEX KILL",
        "Body here",
    )
    assert ok is True
    smtp = holder["smtp"]
    assert smtp.host == "smtp.example.com"
    assert smtp.port == 587
    assert smtp.starttls_called is True
    assert smtp.logged_in_with == ("user@x", "secret")
    assert len(smtp.sent) == 1
    from_addr, to_addrs, msg = smtp.sent[0]
    assert from_addr == "from@x"
    assert to_addrs == ["to@x"]
    assert "APEX KILL" in msg
    # MIMEText base64-encodes utf-8 bodies by default.
    assert base64.b64encode(b"Body here").decode() in msg


def test_send_email_returns_false_on_smtp_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import smtplib

    class _Boom:
        def __init__(self, *a: Any, **kw: Any) -> None:  # noqa: ANN401 - fake factory accepts any SMTP ctor args
            raise smtplib.SMTPException("connection refused")

    monkeypatch.setattr(mod.smtplib, "SMTP", _Boom)
    ok = mod._send_email("h", 587, "u", "p", "f@x", "t@x", "s", "b")
    assert ok is False


def test_send_email_without_starttls_still_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain-text SMTP path (server doesn't advertise STARTTLS)."""

    def factory(host: str, port: int, timeout: float = 10) -> _FakeSMTP:
        smtp = _FakeSMTP(host, port, timeout)
        smtp._has_starttls = False
        return smtp

    monkeypatch.setattr(mod.smtplib, "SMTP", factory)
    ok = mod._send_email("h", 25, "u", "p", "f@x", "t@x", "s", "b")
    assert ok is True


# --------------------------------------------------------------------------- #
# Twilio SMS
# --------------------------------------------------------------------------- #
def test_send_sms_posts_with_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(req, timeout=None) -> _FakeResp:  # noqa: ANN001 - mirrors urlopen signature
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["headers"] = dict(req.header_items())
        return _FakeResp(201, b"")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    ok = mod._send_sms("SID123", "TOKEN456", "+10000000000", "+15555550123", "kill fired")
    assert ok is True
    # URL must contain the SID
    assert "SID123" in captured["url"]
    # Basic auth header with correct b64 payload
    expected = base64.b64encode(b"SID123:TOKEN456").decode()
    assert any(h.lower() == "authorization" and v == f"Basic {expected}" for h, v in captured["headers"].items())
    # Form body contains From/To/Body
    body = captured["data"].decode()
    assert "From=%2B10000000000" in body
    assert "To=%2B15555550123" in body
    assert "Body=kill+fired" in body


def test_send_sms_returns_false_on_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req, timeout=None) -> _FakeResp:  # noqa: ANN001 - mirrors urlopen signature
        raise urllib.error.HTTPError(req.full_url, 400, "bad request", {}, io.BytesIO(b"bad"))

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    assert mod._send_sms("s", "t", "+1", "+2", "x") is False


def test_send_sms_truncates_body_to_1600(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(req, timeout=None) -> _FakeResp:  # noqa: ANN001 - mirrors urlopen signature
        captured["data"] = req.data
        return _FakeResp(201, b"")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    mod._send_sms("s", "t", "+1", "+2", "Z" * 2000)
    # Inspect URL-decoded body length for Body= param
    import urllib.parse as up

    parsed = dict(up.parse_qsl(captured["data"].decode()))
    assert len(parsed["Body"]) == 1600
