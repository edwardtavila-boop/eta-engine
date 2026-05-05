from __future__ import annotations

import json
from pathlib import Path


def test_paper_live_uses_tws_surface_not_client_portal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import ibkr_surface_status as mod

    watchdog_path = tmp_path / "tws_watchdog.json"
    watchdog_path.write_text(
        json.dumps(
            {
                "checked_at": "2026-05-05T17:50:00+00:00",
                "healthy": True,
                "consecutive_failures": 0,
                "details": {
                    "handshake_ok": True,
                    "handshake_detail": "serverVersion=176; clientId=55",
                },
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_check_tcp", lambda *_args, **_kwargs: (True, "tcp_connect_ok"))
    monkeypatch.setattr(
        mod,
        "_http_get_json",
        lambda *_args, **_kwargs: (False, {}, "ConnectionRefusedError"),
    )

    status = mod.build_status(
        tws_watchdog_path=watchdog_path,
        client_portal_reauth_path=tmp_path / "ibkr_reauth.json",
    )

    assert status["paper_live_required_surface"] == "tws_api"
    assert status["summary"]["paper_live_ready"] is True
    assert status["summary"]["client_portal_ready"] is False
    assert "only for REST/data sidecars" in status["summary"]["operator_action"]


def test_client_portal_ready_does_not_make_paper_live_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import ibkr_surface_status as mod

    watchdog_path = tmp_path / "tws_watchdog.json"
    watchdog_path.write_text(
        json.dumps(
            {
                "healthy": False,
                "consecutive_failures": 2,
                "details": {
                    "handshake_ok": False,
                    "handshake_detail": "ConnectionRefusedError",
                },
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        mod,
        "_check_tcp",
        lambda *_args, **_kwargs: (False, "ConnectionRefusedError"),
    )
    monkeypatch.setattr(
        mod,
        "_http_get_json",
        lambda *_args, **_kwargs: (True, {"authenticated": True}, "http_get_ok"),
    )

    status = mod.build_status(
        tws_watchdog_path=watchdog_path,
        client_portal_reauth_path=tmp_path / "ibkr_reauth.json",
    )

    assert status["summary"]["paper_live_ready"] is False
    assert status["summary"]["client_portal_ready"] is True
    assert "paper_live direct orders still need TWS" in status["summary"]["operator_action"]


def test_tcp_open_without_watchdog_is_not_promotion_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import ibkr_surface_status as mod

    monkeypatch.setattr(mod, "_check_tcp", lambda *_args, **_kwargs: (True, "tcp_connect_ok"))
    monkeypatch.setattr(
        mod,
        "_http_get_json",
        lambda *_args, **_kwargs: (False, {}, "skipped"),
    )

    status = mod.build_status(
        check_client_portal=False,
        tws_watchdog_path=tmp_path / "missing_watchdog.json",
        client_portal_reauth_path=tmp_path / "ibkr_reauth.json",
    )

    tws = status["surfaces"]["tws_api"]
    assert status["summary"]["paper_live_ready"] is False
    assert tws["status"] == "tcp_open_handshake_unknown"
    assert "run tws_watchdog" in status["summary"]["operator_action"]


def test_main_writes_status_to_requested_output(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import ibkr_surface_status as mod

    output = tmp_path / "ibkr_surface_status.json"
    monkeypatch.setattr(mod, "_check_tcp", lambda *_args, **_kwargs: (False, "refused"))
    monkeypatch.setattr(
        mod,
        "_http_get_json",
        lambda *_args, **_kwargs: (False, {}, "skipped"),
    )

    rc = mod.main(
        [
            "--skip-client-portal",
            "--output",
            str(output),
            "--tws-host",
            "127.0.0.1",
            "--tws-port",
            "4002",
        ],
    )

    assert rc == 1
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["safe_default_mode"] == "paper_sim"
    assert data["summary"]["paper_live_ready"] is False
