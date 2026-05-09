from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRAR = ROOT / "deploy" / "scripts" / "register_tws_watchdog_task.ps1"
BOOTSTRAP = ROOT / "deploy" / "vps_bootstrap.ps1"
WATCHDOG = ROOT / "scripts" / "tws_watchdog.py"


def test_tws_watchdog_registrar_keeps_release_guard_fresh() -> None:
    text = REGISTRAR.read_text(encoding="utf-8")

    assert 'TaskName = "ETA-TWS-Watchdog"' in text
    assert 'Root = "C:\\EvolutionaryTradingAlgo"' in text
    assert "-m eta_engine.scripts.tws_watchdog --host 127.0.0.1 --port 4002" in text
    assert "--handshake-attempts 1" in text
    assert "--handshake-timeout 30" in text
    assert "-RepetitionInterval (New-TimeSpan -Seconds $IntervalSeconds)" in text
    assert "IntervalSeconds -lt 30 -or $IntervalSeconds -gt 120" in text
    assert "180-second paper-live release guard" in text
    assert "C:\\Users\\edwar\\OneDrive" not in text
    assert "%LOCALAPPDATA%" not in text
    assert "C:\\mnq_data" not in text


def test_vps_bootstrap_registers_tws_watchdog_before_reauth() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")

    tws_index = text.index("register_tws_watchdog_task.ps1")
    reauth_index = text.index("register_ibgateway_reauth_task.ps1")
    assert tws_index < reauth_index
    assert "ETA-TWS-Watchdog" in text
    assert "every 60s" in text


def test_tws_watchdog_doc_matches_release_guard_cadence() -> None:
    text = WATCHDOG.read_text(encoding="utf-8")

    assert "every 60 seconds" in text
    assert "every 5 min" not in text
