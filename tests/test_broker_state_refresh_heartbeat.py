from __future__ import annotations

import json
from pathlib import Path

from eta_engine.scripts import broker_state_refresh_heartbeat as heartbeat


def test_refresh_heartbeat_uses_first_successful_dashboard_endpoint(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_fetch(url: str, *, timeout_s: float) -> dict:
        calls.append(url)
        if "8421" in url:
            raise TimeoutError("proxy not ready")
        assert timeout_s == 7.0
        return {
            "ready": True,
            "source": "live_broker_rest",
            "broker_snapshot_state": "fresh",
            "broker_snapshot_age_s": 0.0,
            "broker_mtd_pnl": 21302.0,
            "today_realized_pnl": -210.42,
            "total_unrealized_pnl": -598.58,
            "open_position_count": 3,
            "today_actual_fills": 121,
            "reporting_timezone": "America/New_York",
        }

    monkeypatch.setattr(heartbeat, "_fetch_json", fake_fetch)

    out = tmp_path / "broker_state_refresh_heartbeat.json"
    payload = heartbeat.refresh_broker_state(
        urls=[
            "http://127.0.0.1:8421/api/live/broker_state?refresh=1",
            "http://127.0.0.1:8000/api/live/broker_state?refresh=1",
        ],
        timeout_s=7.0,
        out_path=out,
    )

    assert calls == [
        "http://127.0.0.1:8421/api/live/broker_state?refresh=1",
        "http://127.0.0.1:8000/api/live/broker_state?refresh=1",
    ]
    assert payload["status"] == "fresh"
    assert payload["ok"] is True
    assert payload["endpoint"] == "http://127.0.0.1:8000/api/live/broker_state?refresh=1"
    assert payload["broker_mtd_pnl"] == 21302.0
    assert payload["today_realized_pnl"] == -210.42
    assert payload["total_unrealized_pnl"] == -598.58
    assert payload["open_position_count"] == 3
    assert payload["today_actual_fills"] == 121
    assert payload["reporting_timezone"] == "America/New_York"
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "fresh"


def test_refresh_heartbeat_records_failure_without_raising(monkeypatch, tmp_path: Path) -> None:
    def fake_fetch(url: str, *, timeout_s: float) -> dict:
        raise TimeoutError(f"failed {url} after {timeout_s}")

    monkeypatch.setattr(heartbeat, "_fetch_json", fake_fetch)

    out = tmp_path / "broker_state_refresh_heartbeat.json"
    payload = heartbeat.refresh_broker_state(
        urls=["http://127.0.0.1:8421/api/live/broker_state?refresh=1"],
        timeout_s=3.0,
        out_path=out,
    )

    assert payload["status"] == "failed"
    assert payload["ok"] is False
    assert "failed http://127.0.0.1:8421" in payload["error"]
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "failed"
