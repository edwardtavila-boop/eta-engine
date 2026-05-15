from __future__ import annotations

import urllib.error


def test_build_audit_falls_back_to_reachable_endpoint(monkeypatch, tmp_path) -> None:
    from eta_engine.deploy.scripts import audit_firm_command_center_surface as mod

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ETA_FIRM_COMMAND_CENTER_SURFACE_AUDIT_URL", raising=False)

    direct_payload = {
        "summary": {
            "active_bots": 4,
            "runtime_active_bots": 4,
            "running_bots": 4,
            "live_attached_bots": 9,
            "live_in_trade_bots": 4,
            "idle_live_bots": 5,
            "inactive_runtime_bots": 0,
            "staged_bots": 0,
            "truth_status": "healthy",
        },
        "truth_summary_line": "Live ETA truth: 9/9 bot heartbeat(s) are fresh; 9 attached, 4 in trade, 5 flat/idle.",
    }

    monkeypatch.setattr(mod.dashboard_api, "bot_fleet_roster", lambda *args, **kwargs: direct_payload)

    def fake_fetch(url: str) -> dict:
        if url.endswith(":8421/api/bot-fleet"):
            raise urllib.error.URLError("bridge unavailable")
        if url.endswith(":8000/api/bot-fleet"):
            return direct_payload
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(mod, "_fetch_payload", fake_fetch)

    audit = mod.build_audit()

    assert audit["status"] == "ok"
    assert audit["endpoint"] == "http://127.0.0.1:8000/api/bot-fleet"
    assert audit["candidate_endpoints"][0] == "http://127.0.0.1:8421/api/bot-fleet"
    assert audit["served_errors"]["http://127.0.0.1:8421/api/bot-fleet"].startswith("URLError:")
    assert audit["mismatched_summary_fields"] == {}
    assert audit["truth_line_matches"] is True


def test_build_audit_reports_endpoint_errors_when_all_candidates_fail(monkeypatch, tmp_path) -> None:
    from eta_engine.deploy.scripts import audit_firm_command_center_surface as mod

    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ETA_FIRM_COMMAND_CENTER_SURFACE_AUDIT_URL", "http://127.0.0.1:9999/api/bot-fleet")
    monkeypatch.setattr(
        mod.dashboard_api,
        "bot_fleet_roster",
        lambda *args, **kwargs: {"summary": {}, "truth_summary_line": ""},
    )
    monkeypatch.setattr(mod, "_fetch_payload", lambda url: (_ for _ in ()).throw(urllib.error.URLError("down")))

    audit = mod.build_audit()

    assert audit["status"] == "mismatch"
    assert audit["endpoint"] is None
    assert audit["served_summary"] == {field: None for field in mod.EXPECTED_SUMMARY_FIELDS}
    assert "http://127.0.0.1:9999/api/bot-fleet -> URLError: <urlopen error down>" in audit["served_error"]
