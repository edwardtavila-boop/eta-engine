from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STARTER = ROOT / "deploy" / "scripts" / "start_ibgateway.ps1"
REPAIR = ROOT / "deploy" / "scripts" / "repair_ibgateway_vps.ps1"


def test_ibgateway_starter_uses_canonical_logs_and_hidden_start() -> None:
    text = STARTER.read_text(encoding="utf-8")

    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\logs\ibgateway" in text
    assert "Start-Process" in text
    assert 'Start-Process -FilePath "cmd.exe"' in text
    assert '/c start ""IBGateway""' in text
    assert "-WindowStyle Hidden" in text
    assert "ibgateway.exe" in text
    assert "-login=" in text
    assert '$_.Name -ieq "ibgateway.exe"' in text
    assert 'CommandLine -like "*ibgateway*"' not in text


def test_ibgateway_repair_profile_is_low_memory_and_backed_up() -> None:
    text = REPAIR.read_text(encoding="utf-8")

    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\backups\ibgateway" in text
    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibgateway_repair.json" in text
    assert '[string]$Heap = "512m"' in text
    assert "[int]$ParallelGCThreads = 2" in text
    assert "[int]$ConcGCThreads = 1" in text
    assert "ETA-IBGateway" in text
    assert "ETA-IBGateway-RunNow" in text
    assert "ETA-IBGateway-DailyRestart" in text
    assert "failed:" in text


def test_ibgateway_repair_scripts_do_not_reintroduce_legacy_workspace_paths() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (STARTER, REPAIR)
    )

    assert "OneDrive" not in combined
    assert "LOCALAPPDATA" not in combined
    assert "mnq_data" not in combined
    assert "crypto_data" not in combined
    assert "TheFirm" not in combined
    assert "The_Firm" not in combined
