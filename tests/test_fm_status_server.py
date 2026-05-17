from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from eta_engine.deploy import fm_status_http_server, fm_status_payload, fm_status_server


def test_fm_status_server_exposes_cached_health_snapshot(tmp_path, monkeypatch):
    snapshot_path = tmp_path / "state" / "fm_health.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "all_ready": False,
                "pass_count": 2,
                "total_count": 3,
                "providers": [{"name": "codex", "ok": True}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ETA_FM_HEALTH_SNAPSHOT_PATH", str(snapshot_path))

    response = TestClient(fm_status_server.app).get("/api/fm/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "force_multiplier"
    assert payload["status"] == "ok"
    assert payload["health_snapshot"]["status"] == "present"
    assert payload["health_snapshot"]["path"] == str(snapshot_path)
    assert payload["health_snapshot"]["payload"]["pass_count"] == 2
    assert "no-store" in response.headers["Cache-Control"]


def test_fm_status_server_xml_keeps_canonical_launch_shape():
    xml = (Path(__file__).resolve().parents[1] / "deploy" / "FmStatusServer.xml").read_text(encoding="utf-8")

    assert r"C:\EvolutionaryTradingAlgo\eta_engine\.venv\Scripts\python.exe" in xml
    assert "-m eta_engine.deploy.fm_status_http_server" in xml
    assert "--host 127.0.0.1 --port 8422" in xml


def test_stdlib_fm_status_handler_uses_same_payload(monkeypatch):
    captured: dict[str, object] = {}

    class FakeHandler(fm_status_http_server.ForceMultiplierStatusHandler):
        path = "/api/fm/status"

        def _send_json(self, payload, status=fm_status_http_server.HTTPStatus.OK):  # type: ignore[override]
            captured["payload"] = payload
            captured["status"] = status

    monkeypatch.setattr(
        fm_status_http_server,
        "build_status_payload",
        lambda: {"mode": "force_multiplier", "status": "ok"},
    )

    handler = object.__new__(FakeHandler)
    handler.do_GET()

    assert captured == {
        "payload": {"mode": "force_multiplier", "status": "ok"},
        "status": fm_status_http_server.HTTPStatus.OK,
    }


def test_status_payload_module_has_no_fastapi_or_uvicorn_dependency():
    payload_source = Path(fm_status_payload.__file__).read_text(encoding="utf-8")

    assert "fastapi" not in payload_source
    assert "uvicorn" not in payload_source
