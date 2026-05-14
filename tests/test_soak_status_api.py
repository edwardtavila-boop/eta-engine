from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SOAK_STATUS_API = ROOT / "deploy" / "status_page" / "soak_status_api.py"


def _load_soak_status_api():
    spec = importlib.util.spec_from_file_location("soak_status_api_under_test", SOAK_STATUS_API)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_soak_status_prefers_elite_dashboard(tmp_path, monkeypatch) -> None:
    mod = _load_soak_status_api()
    elite = tmp_path / "elite_dashboard.html"
    legacy = tmp_path / "soak_dashboard.html"
    elite.write_text("<main>elite dashboard</main>", encoding="utf-8")
    legacy.write_text("<main>legacy soak</main>", encoding="utf-8")
    monkeypatch.setattr(mod, "HTML_PATHS", (elite, legacy))

    response = TestClient(mod.app).get("/")

    assert response.status_code == 200
    assert "elite dashboard" in response.text
    assert "legacy soak" not in response.text
    assert "Paper Soak / Diamond Factory" in response.text
    assert "8421 operator route" in response.text
    assert "source: elite_dashboard.html" in response.text


def test_soak_status_falls_back_to_legacy_dashboard(tmp_path, monkeypatch) -> None:
    mod = _load_soak_status_api()
    elite = tmp_path / "missing_elite.html"
    legacy = tmp_path / "soak_dashboard.html"
    legacy.write_text("<main>legacy soak</main>", encoding="utf-8")
    monkeypatch.setattr(mod, "HTML_PATHS", (elite, legacy))

    response = TestClient(mod.app).get("/")

    assert response.status_code == 200
    assert "legacy soak" in response.text
    assert "Paper Soak / Diamond Factory" in response.text
    assert "source: soak_dashboard.html" in response.text


def test_soak_status_404_when_no_dashboard_exists(tmp_path, monkeypatch) -> None:
    mod = _load_soak_status_api()
    monkeypatch.setattr(
        mod,
        "HTML_PATHS",
        (tmp_path / "missing_elite.html", tmp_path / "missing_soak.html"),
    )

    response = TestClient(mod.app).get("/")

    assert response.status_code == 404
    assert "Dashboard not found" in response.text
    assert "8421 operator route" in response.text
