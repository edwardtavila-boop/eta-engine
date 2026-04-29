from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_roadmap_matches_current_broker_policy() -> None:
    text = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")

    assert "IBKR primary" in text
    assert "Tastytrade secondary" in text
    assert "Tradovate is DORMANT" in text or "Tradovate dormant" in text
    assert "US-legal venue routing only" in text
    assert "Tradovate primary" not in text
    assert "Bybit primary" not in text
    assert "OKX/Bitget backup" not in text
