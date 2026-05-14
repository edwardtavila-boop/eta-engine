"""Smoke test for Force Multiplier integration (Wave-19) — pytest format."""

import sys
from argparse import Namespace
from collections import Counter

sys.path.insert(0, r"C:\EvolutionaryTradingAlgo")


def test_cli_provider_imports():
    from eta_engine.brain.cli_provider import (
        call_codex,
        check_claude_available,
    )

    assert call_codex is not None
    assert check_claude_available() is False


def test_force_provider_enum():
    from eta_engine.brain.model_policy import ForceProvider

    assert ForceProvider.CLAUDE.value == "claude"
    assert ForceProvider.DEEPSEEK.value == "deepseek"
    assert ForceProvider.CODEX.value == "codex"


def test_multi_model_imports():
    from eta_engine.brain.multi_model import (
        route_and_execute,
    )

    assert route_and_execute is not None


def test_cli_health_check():
    from eta_engine.brain.cli_provider import cli_provider_status

    status = cli_provider_status()
    assert "claude_available" in status
    assert "codex_available" in status
    assert "claude_command" in status
    assert "codex_command" in status
    assert isinstance(status["claude_available"], bool)
    assert isinstance(status["codex_available"], bool)
    assert status["claude_available"] is False
    assert status["claude_disabled_by_policy"] is True


def test_cli_health_check_can_skip_version_probe(monkeypatch):
    from eta_engine.brain import cli_provider

    def fail_version_probe(*_args, **_kwargs):
        raise AssertionError("version probe should not run for path-only status")

    monkeypatch.setattr(cli_provider, "_check_cli_available", fail_version_probe)
    monkeypatch.setattr(cli_provider, "_codex_command", lambda: [sys.executable])
    monkeypatch.setattr(cli_provider, "_claude_command", lambda: [sys.executable])

    status = cli_provider.cli_provider_status(probe=False)

    assert status["codex_available"] is True
    assert status["claude_available"] is False
    assert status["availability_probe"] == "path"


def test_force_multiplier_health_non_live_codex_probe_is_path_only(monkeypatch):
    from eta_engine.scripts import force_multiplier_health

    def fail_version_probe():
        raise AssertionError("non-live FM health should not run codex --version")

    monkeypatch.setattr(force_multiplier_health, "check_codex_available", fail_version_probe)
    monkeypatch.setattr(
        force_multiplier_health,
        "cli_provider_status",
        lambda *, probe=True: {
            "codex_available": True,
            "codex_command": "codex",
            "availability_probe": "path" if not probe else "version",
        },
    )

    ok, message = force_multiplier_health.probe_codex(live=False)

    assert ok is True
    assert "path discovered" in message
    assert "skipped live call" in message


def test_force_multiplier_status():
    from eta_engine.brain.multi_model import force_multiplier_status

    fm = force_multiplier_status()
    assert fm["mode"] == "force_multiplier"
    assert "claude" in fm["providers"]
    assert "codex" in fm["providers"]
    assert "deepseek" in fm["providers"]
    assert "routing_table" in fm
    assert fm["providers"]["claude"]["disabled_by_policy"] is True


def test_routing_counts():
    from eta_engine.brain.model_policy import ForceProvider, TaskCategory, force_provider_for

    counts = Counter(force_provider_for(c) for c in TaskCategory)
    assert counts.get(ForceProvider.CLAUDE, 0) == 0
    assert counts.get(ForceProvider.CODEX, 0) == 11
    assert counts.get(ForceProvider.DEEPSEEK, 0) == 13
    assert sum(counts.values()) == 24


def test_specific_routes():
    from eta_engine.brain.model_policy import ForceProvider, TaskCategory, force_provider_for

    assert force_provider_for(TaskCategory.ARCHITECTURE_DECISION) == ForceProvider.CODEX
    assert force_provider_for(TaskCategory.CODE_REVIEW) == ForceProvider.CODEX
    assert force_provider_for(TaskCategory.DEBUG) == ForceProvider.CODEX
    assert force_provider_for(TaskCategory.SECURITY_AUDIT) == ForceProvider.CODEX
    assert force_provider_for(TaskCategory.BOILERPLATE) == ForceProvider.DEEPSEEK
    assert force_provider_for(TaskCategory.STRATEGY_EDIT) == ForceProvider.DEEPSEEK


def test_fallback_routing():
    from eta_engine.brain.model_policy import ForceProvider, TaskCategory, force_provider_for

    all_cats = set(TaskCategory)
    routed = {c: force_provider_for(c) for c in all_cats}
    for cat, provider in routed.items():
        assert provider in ForceProvider, f"{cat} routed to unknown provider {provider}"


def test_force_multiplier_health_tracks_only_allowed_lanes(monkeypatch):
    from eta_engine.scripts import force_multiplier_health

    monkeypatch.setattr(force_multiplier_health, "probe_codex", lambda *, live: (True, "codex ok"))
    monkeypatch.setattr(force_multiplier_health, "probe_deepseek", lambda *, live: (True, "deepseek ok"))

    results = force_multiplier_health._probe_results(live=False)

    labels = [name for name, _, _ in results]
    assert labels == ["CODEX    (Lead Architect / Systems Expert)", "DEEPSEEK (Worker Bee)"]
    assert all("CLAUDE" not in label for label in labels)


def test_fm_status_marks_policy_disabled_claude_as_skip(monkeypatch, capsys):
    from eta_engine.scripts import fm

    monkeypatch.setattr(
        fm,
        "force_multiplier_status",
        lambda: {
            "providers": {
                "claude": {"available": False, "disabled_by_policy": True, "role": "disabled"},
                "codex": {"available": True, "role": "architect"},
                "deepseek": {"available": True, "role": "worker"},
            }
        },
    )
    monkeypatch.setattr(
        fm,
        "summarize",
        lambda *, limit: {"calls": 0, "total_cost_usd": 0.0, "fallback_rate": 0.0, "by_provider": {}},
    )

    assert fm._cmd_status(Namespace(json=False, limit=100)) == 0

    out = capsys.readouterr().out
    assert "[SKIP]" in out
    assert "[FAIL] claude" not in out


def test_live_codex_smoke_dry_run_writes_canonical_artifact(monkeypatch, tmp_path):
    from eta_engine.deploy.scripts import live_codex_smoke

    monkeypatch.setattr(live_codex_smoke, "check_codex_available", lambda: True)
    monkeypatch.setattr(
        live_codex_smoke,
        "cli_provider_status",
        lambda *, probe=True: {
            "codex_available": True,
            "codex_command": "codex",
            "claude_disabled_by_policy": True,
        },
    )

    rc, payload, out = live_codex_smoke.run_smoke(live=False, state_dir=tmp_path)

    assert rc == 0
    assert payload["lane"] == "codex"
    assert payload["legacy_claude_policy"] == "disabled"
    assert payload["ok"] is True
    assert out == tmp_path / "live_codex_smoke.json"
    assert out.exists()


def test_live_codex_smoke_dry_run_is_path_only(monkeypatch, tmp_path):
    from eta_engine.deploy.scripts import live_codex_smoke

    probes: list[bool] = []

    def fake_status(*, probe=True):
        probes.append(probe)
        return {
            "codex_available": True,
            "codex_command": "codex",
            "claude_disabled_by_policy": True,
        }

    def fail_version_probe():
        raise AssertionError("dry-run smoke should not run codex --version")

    monkeypatch.setattr(live_codex_smoke, "cli_provider_status", fake_status)
    monkeypatch.setattr(live_codex_smoke, "check_codex_available", fail_version_probe)

    rc, payload, _out = live_codex_smoke.run_smoke(live=False, state_dir=tmp_path)

    assert rc == 0
    assert payload["ok"] is True
    assert probes == [False]


def test_legacy_claude_smoke_delegates_to_codex(monkeypatch):
    from eta_engine.deploy.scripts import live_claude_smoke

    called = {}

    def fake_codex_main(argv=None):
        called["argv"] = argv
        return 0

    monkeypatch.setattr(live_claude_smoke, "codex_main", fake_codex_main)

    assert live_claude_smoke.main(["--json"]) == 0
    assert called["argv"] == ["--json"]
