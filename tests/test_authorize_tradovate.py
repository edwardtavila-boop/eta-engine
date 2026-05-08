"""Tradovate authorization script tests -- exercises the end-to-end script
path used by the operator to run ``eta_engine.scripts.authorize_tradovate``.

Tests monkeypatch SECRETS.get and swap in a fake aiohttp session so no real
Tradovate endpoint is hit and no real creds are read. Exit-code contract:

    0 = AUTHORIZED  (real OAuth2 success)
    1 = FAILED      (creds present but HTTP rejected)
    2 = STUBBED     (creds missing, fell to stub path)
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from eta_engine.scripts import authorize_tradovate as azt
from eta_engine.venues.tradovate import TradovateVenue

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

# --------------------------------------------------------------------------- #
# Fake aiohttp session (mirrors test_venues_tradovate_http.py)
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, status: int, body: Any) -> None:  # noqa: ANN401 - generic body
        self.status = status
        self._body = body

    async def text(self) -> str:
        return json.dumps(self._body) if isinstance(self._body, (dict, list)) else str(self._body)

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed: bool = False
        self._queue: list[_FakeResponse] = []

    def enqueue(self, status: int, body: Any) -> None:  # noqa: ANN401
        self._queue.append(_FakeResponse(status, body))

    def post(self, url: str, data: str = "", headers: dict[str, str] | None = None) -> _FakeResponse:
        self.calls.append({"url": url, "data": data, "headers": headers or {}})
        return self._queue.pop(0) if self._queue else _FakeResponse(200, {})

    async def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _patch_secrets(
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, str | None],
) -> None:
    """Make SECRETS.get return ``values`` for the 5 Tradovate keys."""

    def fake_get(key: str, required: bool = False) -> str | None:  # noqa: ARG001
        return values.get(key)

    monkeypatch.setattr(azt.SECRETS, "get", fake_get)


def _patch_status_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect STATUS_PATH so tests don't clobber the real artifact."""
    status = tmp_path / "tradovate_auth_status.json"
    monkeypatch.setattr(azt, "STATUS_PATH", status)
    return status


# CLAUDE.md hard rule: Tradovate is dormant unless ETA_TRADOVATE_ENABLED=1.
# Tests in this module exercise the auth flow itself, not the dormancy gate
# — set the activation flag for every test via autouse fixture so the gate
# doesn't short-circuit. Defined here at module level (TYPE_CHECKING already
# imported pytest above for the type hint, and pytest is used as
# pytest.MonkeyPatch in the test functions, so the runtime import below
# uses the existing module reference.)
from collections.abc import Iterator  # noqa: E402  -- intentionally late

import pytest as _pytest_for_fixture  # noqa: E402  -- intentionally late


@_pytest_for_fixture.fixture(autouse=True)
def _enable_tradovate_for_tests(
    monkeypatch: _pytest_for_fixture.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("ETA_TRADOVATE_ENABLED", "1")
    yield


# --------------------------------------------------------------------------- #
# STUBBED path -- creds missing
# --------------------------------------------------------------------------- #


def test_default_status_path_uses_runtime_state() -> None:
    parts = set(azt.STATUS_PATH.parts)

    assert {"var", "eta_engine", "state"}.issubset(parts)
    assert "docs" not in parts
    assert azt.STATUS_PATH.name == "tradovate_auth_status.json"


def test_stubbed_when_all_creds_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_secrets(monkeypatch, {})
    status = _patch_status_dir(monkeypatch, tmp_path)

    rc, report = asyncio.run(azt._run(demo=True))

    assert rc == 2
    assert report.result == "STUBBED"
    assert report.auth_path == "stub"
    assert report.has_all_creds is False
    assert report.endpoint.startswith("https://demo.")
    assert all(v is False for v in report.creds_present.values())
    # Exit-code side effect: the main() writer should land on disk when run
    # Here we mimic what main() does so we exercise the writer.
    azt._write(report)
    assert status.exists()
    on_disk = json.loads(status.read_text())
    assert on_disk["result"] == "STUBBED"


def test_stubbed_when_only_some_creds_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # 3/5 present -> still stub, not real auth
    _patch_secrets(
        monkeypatch,
        {
            "TRADOVATE_USERNAME": "u",
            "TRADOVATE_PASSWORD": "p",
            "TRADOVATE_APP_ID": "aid",
        },
    )
    _patch_status_dir(monkeypatch, tmp_path)

    rc, report = asyncio.run(azt._run(demo=True))

    assert rc == 2
    assert report.result == "STUBBED"
    # Per-key presence reflects input
    assert report.creds_present["TRADOVATE_USERNAME"] is True
    assert report.creds_present["TRADOVATE_APP_SECRET"] is False
    assert report.creds_present["TRADOVATE_CID"] is False


# --------------------------------------------------------------------------- #
# AUTHORIZED path -- all creds present, venue returns 200
# --------------------------------------------------------------------------- #


def test_authorized_when_all_creds_present_and_http_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_secrets(
        monkeypatch,
        {
            "TRADOVATE_USERNAME": "trader@example.com",
            "TRADOVATE_PASSWORD": "account-pw",
            "TRADOVATE_APP_ID": "EtaEngine",
            "TRADOVATE_APP_SECRET": "app-sec-xyz",
            "TRADOVATE_CID": "12345",
        },
    )
    _patch_status_dir(monkeypatch, tmp_path)

    # Patch TradovateVenue to inject our fake session + canned 200 response.
    # Capture the session externally so we can inspect it after venue.close().
    real_init = TradovateVenue.__init__
    captured_session: dict[str, _FakeSession] = {}

    def wrapped_init(self: TradovateVenue, *args: object, **kwargs: object) -> None:
        real_init(self, *args, **kwargs)
        sess = _FakeSession()
        sess.enqueue(
            200,
            {
                "accessToken": "LIVE-TOKEN-ABCDEF9999",
                "mdAccessToken": "MD-TOKEN",
                "expirationTime": "2099-01-01T00:00:00Z",
            },
        )
        self._session = sess
        captured_session["s"] = sess

    monkeypatch.setattr(TradovateVenue, "__init__", wrapped_init)

    rc, report = asyncio.run(azt._run(demo=True))

    assert rc == 0
    assert report.result == "AUTHORIZED"
    assert report.auth_path == "real"
    assert report.has_all_creds is True
    assert report.token_last4 == "9999"
    assert "2099" in report.token_expires_at

    # Verify the OAuth payload was what we expected: distinct sec != password
    sess = captured_session["s"]
    assert sess.closed is True
    payload = json.loads(sess.calls[0]["data"])
    assert payload["name"] == "trader@example.com"
    assert payload["password"] == "account-pw"
    assert payload["sec"] == "app-sec-xyz"
    assert payload["cid"] == "12345"
    assert payload["appId"] == "EtaEngine"


def test_authorize_prop_account_reads_prefixed_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_secrets(
        monkeypatch,
        {
            "TRADOVATE_USERNAME": "generic-should-not-be-used@example.com",
            "TRADOVATE_PASSWORD": "generic-pw",
            "TRADOVATE_APP_ID": "GenericApp",
            "TRADOVATE_APP_SECRET": "generic-sec",
            "TRADOVATE_CID": "111",
            "BLUSKY_TRADOVATE_USERNAME": "blusky@example.com",
            "BLUSKY_TRADOVATE_PASSWORD": "prop-pw",
            "BLUSKY_TRADOVATE_APP_ID": "EtaEngine",
            "BLUSKY_TRADOVATE_APP_SECRET": "prop-sec",
            "BLUSKY_TRADOVATE_CID": "222",
        },
    )
    _patch_status_dir(monkeypatch, tmp_path)

    real_init = TradovateVenue.__init__
    captured_session: dict[str, _FakeSession] = {}

    def wrapped_init(self: TradovateVenue, *args: object, **kwargs: object) -> None:
        real_init(self, *args, **kwargs)
        sess = _FakeSession()
        sess.enqueue(
            200,
            {
                "accessToken": "PROP-TOKEN-ABCDEF7777",
                "mdAccessToken": "MD-TOKEN",
                "expirationTime": "2099-01-01T00:00:00Z",
            },
        )
        self._session = sess
        captured_session["s"] = sess

    monkeypatch.setattr(TradovateVenue, "__init__", wrapped_init)

    rc, report = asyncio.run(azt._run(demo=True, prop_account="blusky_50k"))

    assert rc == 0
    assert report.result == "AUTHORIZED"
    assert report.credential_scope == "blusky_50k"
    assert report.creds_present["BLUSKY_TRADOVATE_USERNAME"] is True
    assert "TRADOVATE_USERNAME" not in report.creds_present

    payload = json.loads(captured_session["s"].calls[0]["data"])
    assert payload["name"] == "blusky@example.com"
    assert payload["password"] == "prop-pw"
    assert payload["sec"] == "prop-sec"
    assert payload["cid"] == "222"


# --------------------------------------------------------------------------- #
# FAILED path -- creds present, HTTP 401
# --------------------------------------------------------------------------- #


def test_failed_when_http_rejects_creds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_secrets(
        monkeypatch,
        {
            "TRADOVATE_USERNAME": "u",
            "TRADOVATE_PASSWORD": "p",
            "TRADOVATE_APP_ID": "a",
            "TRADOVATE_APP_SECRET": "s",
            "TRADOVATE_CID": "c",
        },
    )
    _patch_status_dir(monkeypatch, tmp_path)

    real_init = TradovateVenue.__init__

    def wrapped_init(self: TradovateVenue, *args: object, **kwargs: object) -> None:
        real_init(self, *args, **kwargs)
        sess = _FakeSession()
        sess.enqueue(401, {"errorText": "bad creds"})
        self._session = sess

    monkeypatch.setattr(TradovateVenue, "__init__", wrapped_init)

    rc, report = asyncio.run(azt._run(demo=True))

    assert rc == 1
    assert report.result == "FAILED"
    assert report.auth_path == "real"
    assert "tradovate authenticate failed" in report.reason
    assert report.token_last4 == ""


# --------------------------------------------------------------------------- #
# Endpoint selection
# --------------------------------------------------------------------------- #


def test_live_flag_selects_live_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_secrets(monkeypatch, {})
    _patch_status_dir(monkeypatch, tmp_path)
    _, report = asyncio.run(azt._run(demo=False))
    assert report.demo is False
    assert report.endpoint.startswith("https://live.")


def test_demo_flag_selects_demo_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_secrets(monkeypatch, {})
    _patch_status_dir(monkeypatch, tmp_path)
    _, report = asyncio.run(azt._run(demo=True))
    assert report.demo is True
    assert report.endpoint.startswith("https://demo.")


# --------------------------------------------------------------------------- #
# Writer / last4 helper
# --------------------------------------------------------------------------- #


def test_write_emits_valid_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    status = _patch_status_dir(monkeypatch, tmp_path)
    report = azt.AuthReport(
        generated_at_utc="2026-04-17T00:00:00Z",
        endpoint="https://demo.x",
        result="STUBBED",
    )
    path = azt._write(report)
    assert path == status
    raw = json.loads(status.read_text())
    assert raw["kind"] == "eta_tradovate_auth_status"
    assert raw["result"] == "STUBBED"


def test_last4_handles_short_and_none() -> None:
    assert azt._last4(None) == ""
    assert azt._last4("") == ""
    assert azt._last4("ab") == "****"
    assert azt._last4("stub-access-token") == "oken"
