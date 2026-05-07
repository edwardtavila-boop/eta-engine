from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "deploy" / "scripts" / "ceiling_audit.py"


def test_ceiling_audit_uses_ib_gateway_truth_not_client_portal() -> None:
    text = AUDIT.read_text(encoding="utf-8")

    assert "https://127.0.0.1:5000" not in text
    assert '(4002, "IBKR Gateway API")' in text
    assert "tws_watchdog.json" in text
    assert "ibgateway_reauth.json" in text
    assert "IB Gateway API" in text


def test_ceiling_audit_accepts_ok_quota_state() -> None:
    text = AUDIT.read_text(encoding="utf-8")

    assert 'quota_state in ("NORMAL", "OK")' in text
