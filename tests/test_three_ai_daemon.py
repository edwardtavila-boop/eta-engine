from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

from eta_engine.brain.model_policy import ForceProvider
from eta_engine.scripts import three_ai_daemon
from eta_engine.scripts.process_singleton import ProcessSingletonLock

ETA_ROOT = Path(__file__).resolve().parents[1]


def test_coordination_cycle_accepts_route_injection_and_marks_degraded() -> None:
    calls: list[tuple[str, int]] = []

    def fake_route(**kwargs):
        calls.append((kwargs["category"].value, kwargs["max_tokens"]))
        if kwargs["category"].value == "strategy_edit":
            raise RuntimeError("worker quota exhausted")
        return SimpleNamespace(
            provider=ForceProvider.CLAUDE,
            fallback_used=False,
            text="recommendation",
        )

    report = three_ai_daemon.run_coordination_cycle(
        route=fake_route,
        now=datetime(2026, 5, 5, 7, 0, tzinfo=UTC),
        max_tokens=123,
    )

    assert report["cycle_id"] == "CYC-20260505T070000"
    assert report["status"] == "degraded"
    assert calls == [
        ("architecture_decision", 123),
        ("strategy_edit", 123),
        ("test_execution", 123),
    ]
    assert report["results"]["implementation"]["error"] == "worker quota exhausted"
    assert report["results"]["verification"]["provider"] == ForceProvider.CLAUDE.value


def test_write_report_uses_canonical_state_root(tmp_path: Path) -> None:
    report = {
        "cycle_id": "CYC-1",
        "ts": "2026-05-05T07:00:00+00:00",
        "status": "complete",
        "results": {},
    }

    paths = three_ai_daemon.write_report(report, state_root=tmp_path)

    jsonl = tmp_path / "three_ai_autonomous.jsonl"
    latest = tmp_path / "three_ai_latest.json"
    assert paths == {"jsonl": jsonl, "latest": latest}
    assert json.loads(jsonl.read_text(encoding="utf-8"))["cycle_id"] == "CYC-1"
    assert json.loads(latest.read_text(encoding="utf-8"))["cycle_id"] == "CYC-1"


def test_process_singleton_lock_blocks_second_live_instance(tmp_path: Path) -> None:
    lock_path = tmp_path / "three_ai_daemon.lock"
    first = ProcessSingletonLock(lock_path, name="three_ai_daemon")
    second = ProcessSingletonLock(lock_path, name="three_ai_daemon")

    try:
        assert first.acquire() is True
        assert second.acquire() is False
    finally:
        first.release()

    assert not lock_path.exists()


def test_daemon_skips_when_singleton_lock_is_active(tmp_path: Path) -> None:
    lock_path = tmp_path / "three_ai_daemon.lock"
    active = ProcessSingletonLock(lock_path, name="three_ai_daemon")

    try:
        assert active.acquire() is True
        rc = three_ai_daemon.run_loop(
            max_cycles=1,
            state_root=tmp_path,
            lock_path=lock_path,
            sleep=lambda _seconds: None,
        )
    finally:
        active.release()

    assert rc == 0
    skip_report = json.loads((tmp_path / "three_ai_daemon_singleton_skip.json").read_text(encoding="utf-8"))
    assert skip_report["status"] == "skipped"
    assert skip_report["reason"] == "singleton_lock_active"


def test_dispatch_module_has_no_top_level_runtime_calls() -> None:
    tree = ast.parse((ETA_ROOT / "scripts" / "three_ai_dispatch.py").read_text(encoding="utf-8"))
    forbidden_nodes = (ast.For, ast.While)
    top_level_runtime_nodes = [node for node in tree.body if isinstance(node, forbidden_nodes)]

    assert top_level_runtime_nodes == []
    assert any(isinstance(node, ast.If) for node in tree.body), "dispatch script should guard runtime under __main__"


def test_three_ai_daemon_winsw_xml_uses_canonical_paths_and_10m_interval() -> None:
    xml_path = ETA_ROOT / "deploy" / "ThreeAIDaemon.xml"
    root = ElementTree.fromstring(xml_path.read_text(encoding="utf-8"))

    assert root.findtext("workingdirectory") == r"C:\EvolutionaryTradingAlgo\eta_engine"
    assert root.findtext("logpath") == r"C:\EvolutionaryTradingAlgo\logs\eta_engine"
    arguments = root.findtext("arguments") or ""
    assert r"C:\EvolutionaryTradingAlgo\eta_engine\scripts\three_ai_daemon.py" in arguments
    assert "--interval-sec 600" in arguments
    assert r"--lock-file C:\EvolutionaryTradingAlgo\var\eta_engine\state\three_ai_daemon.lock" in arguments
