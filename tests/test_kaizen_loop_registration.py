from __future__ import annotations

from pathlib import Path

ETA_ROOT = Path(__file__).resolve().parents[1]


def test_register_tasks_registers_kaizen_loop() -> None:
    text = (ETA_ROOT / "deploy" / "scripts" / "register_tasks.ps1").read_text(encoding="utf-8")

    assert "ETA-Kaizen-Loop" in text
    assert "scripts\\kaizen_loop.py" in text
    assert "--apply" in text
    assert "06:00" in text


def test_kaizen_daily_batch_uses_canonical_workspace() -> None:
    text = (ETA_ROOT / "deploy" / "kaizen_daily.bat").read_text(encoding="utf-8")

    assert "C:\\EvolutionaryTradingAlgo" in text
    assert "python -m eta_engine.scripts.kaizen_loop --apply" in text
    assert "var\\eta_engine\\state" in text
