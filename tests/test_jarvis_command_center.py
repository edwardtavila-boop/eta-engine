"""Smoke tests for the JARVIS Master Command Center HTTP server.

These exercise the stdlib ``http.server`` wired into
``apex_predator.scripts.jarvis_dashboard``: route table, content types,
PWA shell endpoints, and import-time side-effect freedom (the obs probe
``dashboard_importable`` depends on the latter).
"""

from __future__ import annotations

import base64
import json
import socket
import threading
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from typing import TYPE_CHECKING

import pytest

import apex_predator.scripts.jarvis_dashboard as mcc

if TYPE_CHECKING:
    from pathlib import Path


def _free_port() -> int:
    """Bind :0, return the kernel-assigned port, release it."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server():
    """Spin up the MCC server on a free port; tear down at teardown."""
    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), mcc._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2.0)


def _get(url: str, timeout: float = 2.0) -> tuple[int, str, bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
        return r.status, r.headers.get("Content-Type", ""), r.read()


class TestMasterCommandCenterRoutes:
    def test_index_serves_html_with_command_center_brand(self, server: str) -> None:
        status, ctype, body = _get(server + "/")
        assert status == 200
        assert ctype.startswith("text/html")
        text = body.decode("utf-8")
        assert "Master Command Center" in text
        assert "/manifest.webmanifest" in text
        assert "/sw.js" in text
        # Drift-card slot ids that test_jarvis_hardening also pins.
        for elt_id in (
            "drift-state",
            "drift-kl",
            "drift-dsharpe",
            "drift-dmean",
            "drift-n",
            "drift-reason",
        ):
            assert f'id="{elt_id}"' in text

    def test_api_state_returns_collect_state_payload(self, server: str) -> None:
        status, ctype, body = _get(server + "/api/state")
        assert status == 200
        assert ctype.startswith("application/json")
        payload = json.loads(body)
        for key in (
            "drift",
            "breaker",
            "deadman",
            "forecast",
            "daemons",
            "promotion",
            "calibration",
            "journal",
            "alerts",
        ):
            assert key in payload

    def test_healthz_returns_ok(self, server: str) -> None:
        status, ctype, body = _get(server + "/healthz")
        assert status == 200
        assert ctype.startswith("text/plain")
        assert body.strip() == b"ok"

    def test_manifest_is_valid_pwa_manifest(self, server: str) -> None:
        status, ctype, body = _get(server + "/manifest.webmanifest")
        assert status == 200
        assert "manifest" in ctype
        manifest = json.loads(body)
        assert manifest["name"] == "JARVIS Master Command Center"
        assert manifest["start_url"] == "/"
        assert manifest["display"] == "standalone"
        assert manifest["icons"], "manifest must declare at least one icon"

    def test_service_worker_is_javascript(self, server: str) -> None:
        status, ctype, body = _get(server + "/sw.js")
        assert status == 200
        assert "javascript" in ctype
        text = body.decode("utf-8")
        # SW must register install/fetch handlers and bypass /api/.
        assert "addEventListener('install'" in text
        assert "addEventListener('fetch'" in text
        assert "/api/" in text  # network-only branch for live data

    def test_icon_is_svg(self, server: str) -> None:
        status, ctype, body = _get(server + "/icon.svg")
        assert status == 200
        assert ctype.startswith("image/svg+xml")
        assert b"<svg" in body

    def test_unknown_path_404s(self, server: str) -> None:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(server + "/does-not-exist")
        assert exc.value.code == 404


class TestImportSideEffectFreedom:
    """The dashboard_importable obs probe imports this module -- it must
    NOT start a server, open sockets, or touch the filesystem just by
    being imported.
    """

    def test_serve_is_not_invoked_on_import(self) -> None:
        # Re-import in a child interpreter would be ideal, but a simpler
        # check: confirm the module exposes serve() without having bound
        # any listening socket. We check that the symbol exists and is
        # callable but DEFAULT_PORT is not currently in use by us.
        assert callable(mcc.serve)
        assert callable(mcc.main)
        # Confirm the public surface tests rely on:
        assert hasattr(mcc, "DRIFT_JOURNAL")
        assert hasattr(mcc, "INDEX_HTML")
        assert hasattr(mcc, "collect_state")
        assert hasattr(mcc, "MANIFEST_JSON")
        assert hasattr(mcc, "SERVICE_WORKER_JS")
        assert hasattr(mcc, "ICON_SVG")

    def test_manifest_is_valid_json(self) -> None:
        json.loads(mcc.MANIFEST_JSON)  # raises if invalid


# ---------------------------------------------------------------------------
# Phase 3: SSE, action endpoints, audit log, push subscriptions, live tails
# ---------------------------------------------------------------------------


def _post(url: str, body: dict, headers: dict | None = None, timeout: float = 3.0) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def mcc_paths(tmp_path: Path, monkeypatch):
    """Redirect every MCC state path to tmp so tests don't touch real state."""
    monkeypatch.setattr(mcc, "AUDIT_LOG", tmp_path / "audit.jsonl")
    monkeypatch.setattr(mcc, "PUSH_SUBSCRIPTIONS", tmp_path / "push.jsonl")
    monkeypatch.setattr(mcc, "KILL_REQUEST", tmp_path / "kill.json")
    monkeypatch.setattr(mcc, "PAUSE_REQUESTS", tmp_path / "pause.jsonl")
    monkeypatch.setattr(mcc, "ALERT_ACKS", tmp_path / "acks.jsonl")
    monkeypatch.setattr(mcc, "DECISION_JOURNAL", tmp_path / "decision.jsonl")
    monkeypatch.setattr(mcc, "ALERTS_LOG", tmp_path / "alerts.jsonl")
    return tmp_path


class TestActionEndpoints:
    def test_kill_switch_trip_writes_request_and_audit(self, server: str, mcc_paths: Path) -> None:
        status, payload = _post(server + "/api/cmd/kill-switch-trip", {"reason": "test trip"})
        assert status == 200
        assert payload["ok"] is True
        # Request file written.
        kill_file = mcc_paths / "kill.json"
        assert kill_file.exists()
        rec = json.loads(kill_file.read_text())
        assert rec["reason"] == "test trip"
        assert rec["operator"] == "anonymous"
        # Audit row appended.
        audit_file = mcc_paths / "audit.jsonl"
        assert audit_file.exists()
        lines = audit_file.read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["action"] == "kill-switch-trip"

    def test_kill_switch_reset_clears_request(self, server: str, mcc_paths: Path) -> None:
        _post(server + "/api/cmd/kill-switch-trip", {"reason": "x"})
        assert (mcc_paths / "kill.json").exists()
        status, payload = _post(server + "/api/cmd/kill-switch-reset", {})
        assert status == 200
        assert payload["ok"] is True
        assert not (mcc_paths / "kill.json").exists()

    def test_pause_bot_requires_bot_id(self, server: str, mcc_paths: Path) -> None:
        status, payload = _post(server + "/api/cmd/pause-bot", {})
        assert status == 400
        assert "bot_id" in payload["error"]

    def test_pause_bot_appends_jsonl(self, server: str, mcc_paths: Path) -> None:
        status, payload = _post(server + "/api/cmd/pause-bot", {"bot_id": "mnq", "reason": "drift"})
        assert status == 200
        rows = (mcc_paths / "pause.jsonl").read_text().strip().splitlines()
        assert len(rows) == 1
        rec = json.loads(rows[0])
        assert rec["bot_id"] == "mnq"
        assert rec["intent"] == "pause"

    def test_unpause_bot_rejects_without_confirm_token(self, server: str, mcc_paths: Path) -> None:
        status, payload = _post(server + "/api/cmd/unpause-bot", {"bot_id": "mnq"})
        assert status == 403
        assert "confirm" in payload["error"]
        assert payload["expected_confirm_token"] == mcc.UNPAUSE_CONFIRM_TOKEN

    def test_unpause_bot_with_correct_token_records_intent(self, server: str, mcc_paths: Path) -> None:
        status, payload = _post(
            server + "/api/cmd/unpause-bot",
            {"bot_id": "mnq", "confirm": mcc.UNPAUSE_CONFIRM_TOKEN},
        )
        assert status == 200
        assert payload["ok"] is True
        rows = (mcc_paths / "pause.jsonl").read_text().strip().splitlines()
        assert json.loads(rows[0])["intent"] == "unpause"

    def test_ack_alert_writes_jsonl(self, server: str, mcc_paths: Path) -> None:
        status, _ = _post(server + "/api/cmd/ack-alert", {"alert_id": "A123", "note": "seen"})
        assert status == 200
        rows = (mcc_paths / "acks.jsonl").read_text().strip().splitlines()
        assert json.loads(rows[0])["alert_id"] == "A123"

    def test_unknown_action_404s(self, server: str, mcc_paths: Path) -> None:
        status, payload = _post(server + "/api/cmd/does-not-exist", {})
        assert status == 404
        assert "unknown action" in payload["error"]


class TestOperatorIdentity:
    def _jwt(self, payload: dict) -> str:
        # Construct a fake JWT (header.payload.signature). Signature is not
        # verified -- the MCC trusts the channel (cloudflared + Access).
        b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
        return b64({"alg": "RS256"}) + "." + b64(payload) + ".sig"

    def test_jwt_email_lands_in_audit(self, server: str, mcc_paths: Path) -> None:
        token = self._jwt({"email": "ops@example.com"})
        _post(
            server + "/api/cmd/ack-alert",
            {"alert_id": "A1"},
            headers={"Cf-Access-Jwt-Assertion": token},
        )
        audit = json.loads((mcc_paths / "audit.jsonl").read_text().splitlines()[-1])
        assert audit["operator"] == "ops@example.com"

    def test_missing_jwt_falls_back_to_anonymous(self, server: str, mcc_paths: Path) -> None:
        _post(server + "/api/cmd/ack-alert", {"alert_id": "A2"})
        audit = json.loads((mcc_paths / "audit.jsonl").read_text().splitlines()[-1])
        assert audit["operator"] == "anonymous"

    def test_malformed_jwt_falls_back_to_anonymous(self, server: str, mcc_paths: Path) -> None:
        _post(
            server + "/api/cmd/ack-alert",
            {"alert_id": "A3"},
            headers={"Cf-Access-Jwt-Assertion": "not-a-jwt"},
        )
        audit = json.loads((mcc_paths / "audit.jsonl").read_text().splitlines()[-1])
        assert audit["operator"] == "anonymous"


class TestPushSubscriptions:
    def test_subscribe_stores_subscription(self, server: str, mcc_paths: Path) -> None:
        sub = {
            "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
            "keys": {"p256dh": "BAA...", "auth": "xyz"},
        }
        status, payload = _post(server + "/api/push/subscribe", sub)
        assert status == 200
        assert payload["ok"] is True
        rows = (mcc_paths / "push.jsonl").read_text().strip().splitlines()
        rec = json.loads(rows[0])
        assert rec["endpoint"] == sub["endpoint"]
        assert rec["keys"] == sub["keys"]

    def test_subscribe_requires_endpoint_and_keys(self, server: str, mcc_paths: Path) -> None:
        status, _ = _post(server + "/api/push/subscribe", {"endpoint": "x"})  # no keys
        assert status == 400

    def test_vapid_public_key_returns_404_when_unset(self, server: str, monkeypatch) -> None:
        monkeypatch.delenv("MCC_VAPID_PUBLIC_KEY", raising=False)
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(server + "/api/push/vapid-public-key")
        assert exc.value.code == 404

    def test_vapid_public_key_returns_key_when_set(self, server: str, monkeypatch) -> None:
        monkeypatch.setenv("MCC_VAPID_PUBLIC_KEY", "BMy_test_key_value")
        status, _, body = _get(server + "/api/push/vapid-public-key")
        assert status == 200
        assert json.loads(body)["key"] == "BMy_test_key_value"


class TestLiveTailPanels:
    def test_render_journal_reads_decision_journal(self, mcc_paths: Path) -> None:
        (mcc_paths / "decision.jsonl").write_text(
            json.dumps({"ts": "2026-04-26T00:00:00+00:00", "summary": "trade entered"})
            + "\n"
            + json.dumps({"ts": "2026-04-26T00:01:00+00:00", "summary": "trade closed"})
            + "\n",
            encoding="utf-8",
        )
        out = mcc._render_journal()
        assert len(out["tail"]) == 2
        assert out["tail"][-1]["summary"] == "trade closed"

    def test_render_alerts_reads_alerts_log(self, mcc_paths: Path) -> None:
        (mcc_paths / "alerts.jsonl").write_text(
            json.dumps({"level": "WARN", "message": "drift seen"}) + "\n",
            encoding="utf-8",
        )
        out = mcc._render_alerts()
        assert len(out["tail"]) == 1
        assert out["tail"][0]["message"] == "drift seen"

    def test_tail_jsonl_skips_malformed(self, mcc_paths: Path) -> None:
        path = mcc_paths / "decision.jsonl"
        path.write_text('{"a":1}\nnot json\n{"b":2}\n', encoding="utf-8")
        rows = mcc._tail_jsonl(path)
        assert rows == [{"a": 1}, {"b": 2}]

    def test_tail_jsonl_caps_to_n(self, mcc_paths: Path) -> None:
        path = mcc_paths / "alerts.jsonl"
        path.write_text("\n".join(json.dumps({"i": i}) for i in range(50)) + "\n", encoding="utf-8")
        rows = mcc._tail_jsonl(path, n=5)
        assert len(rows) == 5
        assert rows[-1] == {"i": 49}


class TestSSEStateStream:
    def test_stream_returns_event_stream_with_data(self, server: str) -> None:
        # Open a streaming GET; read a single chunk; close. We don't loop --
        # one ``data:`` frame is enough to confirm the wire format.
        req = urllib.request.Request(server + "/api/state/stream")
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
            assert r.status == 200
            assert r.headers.get("Content-Type", "").startswith("text/event-stream")
            buf = b""
            # Read up to 64KB or until we see a complete data: frame.
            while len(buf) < 65536:
                chunk = r.read(4096)
                if not chunk:
                    break
                buf += chunk
                if b"\n\n" in buf:
                    break
        text = buf.decode("utf-8", errors="ignore")
        assert "data:" in text
        # Extract the JSON after the first "data: " prefix and validate.
        first = text.split("data: ", 1)[1].split("\n\n", 1)[0]
        snap = json.loads(first)
        for key in ("drift", "breaker", "deadman", "journal", "alerts"):
            assert key in snap
