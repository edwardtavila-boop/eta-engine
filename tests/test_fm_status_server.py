from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from eta_engine.deploy import fm_status_server


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
    monkeypatch.setattr(
        fm_status_server,
        "_force_multiplier_status",
        lambda: {"mode": "force_multiplier", "providers": {"codex": {"available": True}}},
    )

    response = TestClient(fm_status_server.app).get("/api/fm/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "force_multiplier"
    assert payload["status"] == "ok"
    assert payload["health_snapshot"]["status"] == "present"
    assert payload["health_snapshot"]["path"] == str(snapshot_path)
    assert payload["health_snapshot"]["payload"]["pass_count"] == 2
    assert "no-store" in response.headers["Cache-Control"]


def test_fm_status_server_xml_uses_existing_command_center_runtime():
    xml = (Path(__file__).resolve().parents[1] / "deploy" / "FmStatusServer.xml").read_text(encoding="utf-8")

    assert r"C:\Python314\python.exe" in xml
    assert "-m uvicorn eta_engine.deploy.fm_status_server:app" in xml
    assert "--host 127.0.0.1 --port 8422" in xml
