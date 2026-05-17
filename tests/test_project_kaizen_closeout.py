from __future__ import annotations

import json
import sys
from pathlib import Path


def test_secret_gate_warns_unless_strict() -> None:
    from eta_engine.scripts.project_kaizen_closeout import _classify_command_exit

    assert _classify_command_exit("secrets_validator", 1, strict_secrets=False) == "pass"
    assert _classify_command_exit("secrets_validator", 1, strict_secrets=True) == "fail"
    assert _classify_command_exit("secrets_validator", 2, strict_secrets=False) == "warn"
    assert _classify_command_exit("secrets_validator", 2, strict_secrets=True) == "fail"


def test_closeout_writes_latest_report(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    def fake_run_command(args, *, cwd, timeout_s):
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout="ok",
            stderr="",
            duration_s=0.01,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)
    output_dir = tmp_path / "var" / "eta_engine" / "state"

    report = closeout.run_closeout(
        root=tmp_path,
        output_dir=output_dir,
        python_exe=sys.executable,
        include_live=False,
        run_tests=False,
        strict_secrets=False,
    )

    latest = output_dir / "kaizen_closeout_latest.json"
    assert latest.exists()
    assert json.loads(latest.read_text(encoding="utf-8"))["status"] == report["status"]
    assert report["root"] == str(tmp_path)
    assert {gate["name"] for gate in report["gates"]} >= {
        "canonical_root",
        "eta_engine_diff_check",
        "secrets_validator",
        "health_check",
    }


def test_closeout_uses_wiring_preflight_for_submodule_gate(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    def fake_run_command(args, *, cwd, timeout_s):
        stdout = ""
        if "eta_engine.scripts.submodule_wiring_preflight" in args:
            stdout = json.dumps({"ready": True, "action": "safe_to_wire_gitlinks"})
        elif "submodule" in args:
            stdout = (
                " 15e701e12bdd09995847d279861b3c12b0ba06f2 eta_engine (main)\n"
                "-1c3a2ef93a2d25561a4ec3e022cdbe1176ce590a eta/legacy_child\n"
            )
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout=stdout,
            stderr="",
            duration_s=0.01,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)

    report = closeout.run_closeout(
        root=tmp_path,
        output_dir=tmp_path / "state",
        python_exe=sys.executable,
        include_live=False,
        run_tests=False,
        strict_secrets=False,
    )

    submodule_gate = next(gate for gate in report["gates"] if gate["name"] == "submodule_status")
    assert submodule_gate["status"] == "pass"
    assert "eta_engine.scripts.submodule_wiring_preflight" in submodule_gate["extra"]["args"]


def test_closeout_includes_jarvis_memory_migration_gate_when_script_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    script = tmp_path / "eta_engine" / "scripts" / "jarvis_memory_migration.py"
    script.parent.mkdir(parents=True)
    script.write_text("# test marker\n", encoding="utf-8")
    memory_report = json.dumps(
        {
            "status": "current",
            "copy_count": 0,
            "missing_source_count": 1,
            "canonical_present_count": 0,
            "dry_run": True,
        }
    )

    def fake_run_command(args, *, cwd, timeout_s):
        stdout = memory_report if "eta_engine.scripts.jarvis_memory_migration" in args else ""
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout=stdout,
            stderr="",
            duration_s=0.01,
            stdout_raw=stdout,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)
    monkeypatch.setattr(
        closeout,
        "_canonical_root_gate",
        lambda root: closeout._gate("canonical_root", "pass", str(root)),
    )

    report = closeout.run_closeout(
        root=tmp_path,
        output_dir=tmp_path / "state",
        python_exe=sys.executable,
        include_live=False,
        run_tests=False,
        strict_secrets=False,
    )

    gate = next(gate for gate in report["gates"] if gate["name"] == "jarvis_memory_migration")
    assert gate["status"] == "pass"
    assert gate["detail"] == "status=current copy_count=0 missing_source_count=1 canonical_present_count=0"
    assert "eta_engine.scripts.jarvis_memory_migration" in gate["extra"]["args"]
    assert gate["extra"]["summary"]["status"] == "current"


def test_closeout_warns_when_jarvis_memory_needs_migration(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    script = tmp_path / "eta_engine" / "scripts" / "jarvis_memory_migration.py"
    script.parent.mkdir(parents=True)
    script.write_text("# test marker\n", encoding="utf-8")
    memory_report = json.dumps(
        {
            "status": "needs_migration",
            "copy_count": 2,
            "missing_source_count": 0,
            "canonical_present_count": 0,
            "dry_run": True,
        }
    )

    def fake_run_command(args, *, cwd, timeout_s):
        stdout = memory_report if "eta_engine.scripts.jarvis_memory_migration" in args else ""
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout=stdout,
            stderr="",
            duration_s=0.01,
            stdout_raw=stdout,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)
    monkeypatch.setattr(
        closeout,
        "_canonical_root_gate",
        lambda root: closeout._gate("canonical_root", "pass", str(root)),
    )

    report = closeout.run_closeout(
        root=tmp_path,
        output_dir=tmp_path / "state",
        python_exe=sys.executable,
        include_live=False,
        run_tests=False,
        strict_secrets=False,
    )

    gate = next(gate for gate in report["gates"] if gate["name"] == "jarvis_memory_migration")
    assert gate["status"] == "warn"
    assert "status=needs_migration copy_count=2" in gate["detail"]
    assert report["status"] == "warn"
    assert report["exit_code"] == 1


def test_closeout_includes_dirty_worktree_reconciliation_gate_when_script_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    script = tmp_path / "eta_engine" / "scripts" / "dirty_worktree_reconciliation.py"
    script.parent.mkdir(parents=True)
    script.write_text("# test marker\n", encoding="utf-8")
    reconciliation_report = json.dumps(
        {
            "ready": False,
            "action": "review_child_dirty_groups_before_gitlink_wiring",
            "dirty_modules": ["eta_engine", "mnq_backtest"],
            "blocking_modules": ["eta_engine", "mnq_backtest"],
            "output_path": str(tmp_path / "state" / "dirty_worktree_reconciliation_latest.json"),
        }
    )

    def fake_run_command(args, *, cwd, timeout_s):
        stdout = reconciliation_report if "eta_engine.scripts.dirty_worktree_reconciliation" in args else ""
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=1 if "eta_engine.scripts.dirty_worktree_reconciliation" in args else 0,
            stdout=stdout,
            stderr="",
            duration_s=0.01,
            stdout_raw=stdout,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)
    monkeypatch.setattr(
        closeout,
        "_canonical_root_gate",
        lambda root: closeout._gate("canonical_root", "pass", str(root)),
    )

    report = closeout.run_closeout(
        root=tmp_path,
        output_dir=tmp_path / "state",
        python_exe=sys.executable,
        include_live=False,
        run_tests=False,
        strict_secrets=False,
    )

    gate = next(gate for gate in report["gates"] if gate["name"] == "dirty_worktree_reconciliation")
    assert gate["status"] == "warn"
    assert "action=review_child_dirty_groups_before_gitlink_wiring" in gate["detail"]
    assert "dirty_modules=eta_engine, mnq_backtest" in gate["detail"]
    assert gate["extra"]["summary"]["dirty_modules"] == ["eta_engine", "mnq_backtest"]
    assert "--summary-json" in gate["extra"]["args"]
    assert "--json" not in gate["extra"]["args"]


def test_live_closeout_allows_remote_supervisor_truth_for_local_health(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    commands: list[list[str]] = []

    def fake_run_command(args, *, cwd, timeout_s):
        commands.append(list(args))
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.01,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)
    monkeypatch.setattr(
        closeout,
        "_live_endpoint_gate",
        lambda name, url, *, timeout_s: closeout._gate(name, "pass", "ok"),
    )

    closeout.run_closeout(
        root=tmp_path,
        output_dir=tmp_path / "state",
        python_exe=sys.executable,
        include_live=True,
        run_tests=False,
    )

    health_args = next(args for args in commands if "eta_engine.scripts.health_check" in args)
    assert "--output-dir" in health_args
    output_dir_index = health_args.index("--output-dir")
    assert health_args[output_dir_index + 1] == str(tmp_path / "state" / "health")
    assert "--allow-remote-supervisor-truth" in health_args
    assert "--allow-remote-retune-truth" in health_args


def test_live_closeout_uses_shorter_timeout_for_bot_fleet_probe(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    def fake_run_command(args, *, cwd, timeout_s):
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.01,
        )

    live_calls: list[tuple[str, str, int]] = []

    def fake_live_endpoint_gate(name: str, url: str, *, timeout_s: int):
        live_calls.append((name, url, timeout_s))
        return closeout._gate(name, "pass", "ok")

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)
    monkeypatch.setattr(closeout, "_live_endpoint_gate", fake_live_endpoint_gate)

    closeout.run_closeout(
        root=tmp_path,
        output_dir=tmp_path / "state",
        python_exe=sys.executable,
        include_live=True,
        run_tests=False,
        timeout_s=120,
    )

    assert ("live_health", "https://ops.evolutionarytradingalgo.com/health", 30) in live_calls
    assert (
        "live_paper_transition",
        "https://ops.evolutionarytradingalgo.com/api/jarvis/paper_live_transition",
        30,
    ) in live_calls
    assert ("live_bot_fleet", "https://ops.evolutionarytradingalgo.com/api/bot-fleet", 10) in live_calls


def test_live_bot_fleet_gate_falls_back_to_dashboard_diagnostics(monkeypatch) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    def fake_fetch_json(url: str, *, timeout_s: int):
        if url.endswith("/api/bot-fleet"):
            raise TimeoutError("The read operation timed out")
        raise AssertionError(url)

    def fake_fetch_json_via_curl(url: str, *, timeout_s: int):
        assert url.endswith("/api/dashboard/diagnostics")
        return 200, {
            "bot_fleet": {
                "truth_status": "live",
                "truth_summary_line": "Live ETA truth is fresh.",
            }
        }

    monkeypatch.setattr(closeout, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(closeout, "_fetch_json_via_curl", fake_fetch_json_via_curl)

    gate = closeout._live_endpoint_gate(
        "live_bot_fleet",
        "https://ops.evolutionarytradingalgo.com/api/bot-fleet",
        timeout_s=30,
    )

    assert gate["status"] == "warn"
    assert "fallback http=200 via dashboard diagnostics" in gate["detail"]
    assert gate["extra"]["primary_error"] == "The read operation timed out"
    assert gate["extra"]["fallback"]["source"] == "dashboard_diagnostics.bot_fleet"
    assert gate["extra"]["summary"]["truth_status"] == "live"


def test_live_bot_fleet_gate_keeps_failure_when_diagnostics_fallback_is_unavailable(monkeypatch) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    def fake_fetch_json(url: str, *, timeout_s: int):
        raise TimeoutError("The read operation timed out")

    def fake_fetch_json_via_curl(url: str, *, timeout_s: int):
        raise OSError("curl timed out")

    monkeypatch.setattr(closeout, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(closeout, "_fetch_json_via_curl", fake_fetch_json_via_curl)

    gate = closeout._live_endpoint_gate(
        "live_bot_fleet",
        "https://ops.evolutionarytradingalgo.com/api/bot-fleet",
        timeout_s=30,
    )

    assert gate["status"] == "fail"
    assert gate["detail"] == "The read operation timed out"
    assert gate["extra"]["fallback_error"] == "The read operation timed out"


def test_closeout_warns_when_submodule_preflight_only_reports_dirty_worktree(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    def fake_run_command(args, *, cwd, timeout_s):
        if "eta_engine.scripts.submodule_wiring_preflight" in args:
            return closeout.CommandResult(
                args=list(args),
                cwd=str(cwd),
                returncode=1,
                stdout=json.dumps(
                    {
                        "action": "do_not_wire_gitlinks",
                        "modules": {
                            "eta_engine": {
                                "ready": False,
                                "blockers": ["dirty worktree"],
                            },
                            "firm": {
                                "ready": True,
                                "blockers": [],
                            },
                        },
                    }
                ),
                stderr="",
                duration_s=0.01,
            )
        if args[:3] == ["git", "-C", str(tmp_path / "eta_engine")] and args[-2:] == ["status", "--short"]:
            return closeout.CommandResult(
                args=list(args),
                cwd=str(cwd),
                returncode=0,
                stdout="M scripts/health_check.py",
                stderr="",
                duration_s=0.01,
            )
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.01,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)
    monkeypatch.setattr(
        closeout,
        "_canonical_root_gate",
        lambda root: closeout._gate("canonical_root", "pass", str(root)),
    )

    report = closeout.run_closeout(
        root=tmp_path,
        output_dir=tmp_path / "state",
        python_exe=sys.executable,
        include_live=False,
        run_tests=False,
        strict_secrets=False,
    )

    gate = next(gate for gate in report["gates"] if gate["name"] == "submodule_status")
    assert gate["status"] == "warn"
    assert "dirty child worktree blocks gitlink wiring" in gate["detail"]
    assert report["status"] == "warn"
    assert report["exit_code"] == 1


def test_closeout_keeps_submodule_gate_failed_for_real_wiring_blockers(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    def fake_run_command(args, *, cwd, timeout_s):
        if "eta_engine.scripts.submodule_wiring_preflight" in args:
            return closeout.CommandResult(
                args=list(args),
                cwd=str(cwd),
                returncode=1,
                stdout=json.dumps(
                    {
                        "action": "do_not_wire_gitlinks",
                        "modules": {
                            "eta_engine": {
                                "ready": False,
                                "blockers": ["gitlink mismatch", "dirty worktree"],
                            },
                        },
                    }
                ),
                stderr="",
                duration_s=0.01,
            )
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.01,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)
    monkeypatch.setattr(
        closeout,
        "_canonical_root_gate",
        lambda root: closeout._gate("canonical_root", "pass", str(root)),
    )

    report = closeout.run_closeout(
        root=tmp_path,
        output_dir=tmp_path / "state",
        python_exe=sys.executable,
        include_live=False,
        run_tests=False,
        strict_secrets=False,
    )

    gate = next(gate for gate in report["gates"] if gate["name"] == "submodule_status")
    assert gate["status"] == "fail"
    assert report["status"] == "fail"
    assert report["exit_code"] == 2


def test_closeout_warns_when_submodule_preflight_only_reports_dirty_diverged_integration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    def fake_run_command(args, *, cwd, timeout_s):
        if "eta_engine.scripts.submodule_wiring_preflight" in args:
            return closeout.CommandResult(
                args=list(args),
                cwd=str(cwd),
                returncode=1,
                stdout=json.dumps(
                    {
                        "action": "do_not_wire_gitlinks",
                        "modules": {
                            "eta_engine": {
                                "ready": False,
                                "blockers": ["gitlink diverged", "dirty worktree"],
                            },
                            "mnq_backtest": {
                                "ready": False,
                                "blockers": ["dirty worktree"],
                            },
                        },
                    }
                ),
                stderr="",
                duration_s=0.01,
            )
        if args[:3] == ["git", "-C", str(tmp_path / "eta_engine")] and args[-2:] == ["status", "--short"]:
            return closeout.CommandResult(
                args=list(args),
                cwd=str(cwd),
                returncode=0,
                stdout="M scripts/health_check.py",
                stderr="",
                duration_s=0.01,
            )
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.01,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)
    monkeypatch.setattr(
        closeout,
        "_canonical_root_gate",
        lambda root: closeout._gate("canonical_root", "pass", str(root)),
    )

    report = closeout.run_closeout(
        root=tmp_path,
        output_dir=tmp_path / "state",
        python_exe=sys.executable,
        include_live=False,
        run_tests=False,
        strict_secrets=False,
    )

    gate = next(gate for gate in report["gates"] if gate["name"] == "submodule_status")
    assert gate["status"] == "warn"
    assert "dirty/diverged child integration blocks gitlink wiring" in gate["detail"]
    assert report["status"] == "warn"
    assert report["exit_code"] == 1


def test_closeout_warns_when_optional_backtest_submodule_is_missing_but_runtime_children_only_need_integration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    def fake_run_command(args, *, cwd, timeout_s):
        if "eta_engine.scripts.submodule_wiring_preflight" in args:
            return closeout.CommandResult(
                args=list(args),
                cwd=str(cwd),
                returncode=1,
                stdout=json.dumps(
                    {
                        "action": "do_not_wire_gitlinks",
                        "modules": {
                            "eta_engine": {
                                "ready": False,
                                "blockers": ["dirty worktree"],
                            },
                            "mnq_backtest": {
                                "ready": False,
                                "blockers": ["missing submodule checkout", "gitlink uninitialized"],
                            },
                        },
                    }
                ),
                stderr="",
                duration_s=0.01,
            )
        if args[:3] == ["git", "-C", str(tmp_path / "eta_engine")] and args[-2:] == ["status", "--short"]:
            return closeout.CommandResult(
                args=list(args),
                cwd=str(cwd),
                returncode=0,
                stdout="M scripts/health_check.py",
                stderr="",
                duration_s=0.01,
            )
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.01,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)
    monkeypatch.setattr(
        closeout,
        "_canonical_root_gate",
        lambda root: closeout._gate("canonical_root", "pass", str(root)),
    )

    report = closeout.run_closeout(
        root=tmp_path,
        output_dir=tmp_path / "state",
        python_exe=sys.executable,
        include_live=False,
        run_tests=False,
        strict_secrets=False,
    )

    gate = next(gate for gate in report["gates"] if gate["name"] == "submodule_status")
    assert gate["status"] == "warn"
    assert "optional missing submodule checkout" in gate["detail"]
    assert report["status"] == "warn"
    assert report["exit_code"] == 1


def test_submodule_status_uses_raw_stdout_when_display_output_is_truncated() -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    dirty_entries = [f" M file_{idx}.py" for idx in range(800)]
    raw_stdout = json.dumps(
        {
            "action": "do_not_wire_gitlinks",
            "modules": {
                "eta_engine": {
                    "ready": False,
                    "blockers": ["dirty worktree"],
                    "dirty_entries": dirty_entries,
                }
            },
        }
    )
    result = closeout.CommandResult(
        args=["python", "-m", "eta_engine.scripts.submodule_wiring_preflight"],
        cwd="C:\\EvolutionaryTradingAlgo",
        returncode=1,
        stdout=closeout._clip(raw_stdout, 200),
        stderr="",
        duration_s=0.01,
        stdout_raw=raw_stdout,
    )

    status, detail = closeout._classify_submodule_status(result)

    assert status == "warn"
    assert "dirty child worktree blocks gitlink wiring" in detail


def test_submodule_status_surfaces_dirty_group_summary() -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    raw_stdout = json.dumps(
        {
            "action": "do_not_wire_gitlinks",
            "modules": {
                "eta_engine": {
                    "ready": False,
                    "blockers": ["gitlink diverged", "dirty worktree"],
                    "dirty_summary": {
                        "top_groups": [
                            {"group": "scripts", "count": 128},
                            {"group": "tests", "count": 91},
                            {"group": "feeds", "count": 73},
                        ],
                        "review_action": "split_dirty_worktree_by_group_before_gitlink_wiring",
                    },
                },
                "mnq_backtest": {
                    "ready": False,
                    "blockers": ["dirty worktree"],
                    "dirty_summary": {
                        "top_groups": [{"group": "docs", "count": 3}],
                        "review_action": "review_untracked_files_before_gitlink_wiring",
                    },
                },
            },
        }
    )
    result = closeout.CommandResult(
        args=["python", "-m", "eta_engine.scripts.submodule_wiring_preflight"],
        cwd="C:\\EvolutionaryTradingAlgo",
        returncode=1,
        stdout=raw_stdout,
        stderr="",
        duration_s=0.01,
        stdout_raw=raw_stdout,
    )

    status, detail = closeout._classify_submodule_status(result)

    assert status == "warn"
    assert "dirty/diverged child integration blocks gitlink wiring" in detail
    assert "eta_engine: scripts=128, tests=91, feeds=73" in detail
    assert "mnq_backtest: docs=3" in detail


def test_closeout_summarizes_eta_engine_status_dirty_entries(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    status_stdout = "\n".join(
        [
            " M scripts/health_check.py",
            " M scripts/project_kaizen_closeout.py",
            "?? deploy/scripts/sync_trade_closes_from_vps.ps1",
        ]
    )

    def fake_run_command(args, *, cwd, timeout_s):
        if args[:3] == ["git", "-C", str(tmp_path / "eta_engine")] and args[-2:] == ["status", "--short"]:
            return closeout.CommandResult(
                args=list(args),
                cwd=str(cwd),
                returncode=0,
                stdout=status_stdout,
                stderr="",
                duration_s=0.01,
                stdout_raw=status_stdout,
            )
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.01,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)
    monkeypatch.setattr(
        closeout,
        "_canonical_root_gate",
        lambda root: closeout._gate("canonical_root", "pass", str(root)),
    )

    report = closeout.run_closeout(
        root=tmp_path,
        output_dir=tmp_path / "state",
        python_exe=sys.executable,
        include_live=False,
        run_tests=False,
        strict_secrets=False,
    )

    gate = next(gate for gate in report["gates"] if gate["name"] == "eta_engine_status")
    assert gate["status"] == "warn"
    assert gate["detail"].startswith("3 dirty entries (modified=2, untracked=1)")
    assert gate["extra"]["summary"]["entry_count"] == 3
    assert gate["extra"]["summary"]["counts"]["modified"] == 2
    assert gate["extra"]["summary"]["counts"]["untracked"] == 1
    assert gate["extra"]["summary"]["preview"] == [
        "scripts/health_check.py",
        "scripts/project_kaizen_closeout.py",
        "deploy/scripts/sync_trade_closes_from_vps.ps1",
    ]
    assert gate["extra"]["summary"]["top_groups"] == [
        {"group": "scripts", "count": 2},
        {"group": "deploy", "count": 1},
    ]
    assert gate["extra"]["summary"]["change_type_preview"] == {
        "modified": ["scripts/health_check.py", "scripts/project_kaizen_closeout.py"],
        "untracked": ["deploy/scripts/sync_trade_closes_from_vps.ps1"],
    }
    assert gate["extra"]["summary"]["review_action"] == "review_untracked_files_before_gitlink_wiring"
    assert "top_groups: scripts=2, deploy=1" in gate["detail"]


def test_git_status_summary_points_large_dirty_trees_to_grouped_review() -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    raw_status = "\n".join(
        [f" M scripts/worker_{idx}.py" for idx in range(40)]
        + [f"?? tests/test_worker_{idx}.py" for idx in range(12)]
        + [" D docs/runbook.md", "R  old/path.py -> feeds/path.py"]
    )

    summary = closeout._summarize_git_status(raw_status)

    assert summary["entry_count"] == 54
    assert summary["counts"]["modified"] == 40
    assert summary["counts"]["untracked"] == 12
    assert summary["counts"]["deleted"] == 1
    assert summary["counts"]["renamed"] == 1
    assert summary["top_groups"][:4] == [
        {"group": "scripts", "count": 40},
        {"group": "tests", "count": 12},
        {"group": "docs", "count": 1},
        {"group": "feeds", "count": 1},
    ]
    assert summary["change_type_preview"]["modified"] == [
        "scripts/worker_0.py",
        "scripts/worker_1.py",
        "scripts/worker_2.py",
        "scripts/worker_3.py",
        "scripts/worker_4.py",
    ]
    assert summary["change_type_preview"]["renamed"] == ["old/path.py -> feeds/path.py"]
    assert summary["review_action"] == "split_dirty_worktree_by_group_before_gitlink_wiring"
    assert "top_groups: scripts=40, tests=12, docs=1, feeds=1" in summary["detail"]


def test_closeout_includes_public_retune_advisory_when_caches_exist(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    def fake_run_command(args, *, cwd, timeout_s):
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.01,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)
    monkeypatch.setattr(
        closeout,
        "_canonical_root_gate",
        lambda root: closeout._gate("canonical_root", "pass", str(root)),
    )

    output_dir = tmp_path / "state"
    health_dir = output_dir / "health"
    health_dir.mkdir(parents=True)
    (health_dir / "public_diamond_retune_truth_latest.json").write_text(
        json.dumps(
            {
                "focus_bot": "mnq_futures_sage",
                "focus_issue": "broker_pnl_negative",
                "focus_state": "COLLECT_MORE_SAMPLE",
                "surface": {
                    "normalized": {
                        "focus_bot": "mnq_futures_sage",
                        "focus_issue": "broker_pnl_negative",
                        "focus_state": "COLLECT_MORE_SAMPLE",
                        "focus_closed_trade_count": 141,
                        "focus_total_realized_pnl": -1939.75,
                        "focus_profit_factor": 0.3951,
                    },
                    "summary": {
                        "broker_truth_focus_closed_trade_count": 141,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (health_dir / "public_broker_close_truth_latest.json").write_text(
        json.dumps(
            {
                "surface": {
                    "normalized": {
                        "focus_bot": "mnq_futures_sage",
                        "focus_closed_trade_count": 141,
                        "focus_total_realized_pnl": -1939.75,
                        "focus_profit_factor": 0.3951,
                        "broker_mtd_pnl": 20752.0,
                        "today_realized_pnl": -1751.81,
                        "total_unrealized_pnl": 385.81,
                        "open_position_count": 4,
                        "reporting_timezone": "America/New_York",
                        "broker_snapshot_source": "ibkr_probe_cache",
                        "broker_snapshot_state": "warm",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (health_dir / "diamond_retune_truth_check_latest.json").write_text(
        json.dumps(
            {
                "diagnosis": "public_local_focus_mismatch",
                "warnings": ["Public retune focus and local canonical retune receipt disagree."],
                "action_items": [
                    "Refresh or repair the canonical trade_closes writer before trusting local broker-proof counts."
                ],
            }
        ),
        encoding="utf-8",
    )

    report = closeout.run_closeout(
        root=tmp_path,
        output_dir=output_dir,
        python_exe=sys.executable,
        include_live=False,
        run_tests=False,
        strict_secrets=False,
    )

    advisory = report["retune_advisory"]
    assert advisory["available"] is True
    assert advisory["focus_bot"] == "mnq_futures_sage"
    assert advisory["focus_closed_trade_count"] == 141
    assert advisory["focus_total_realized_pnl"] == -1939.75
    assert advisory["focus_profit_factor"] == 0.3951
    assert advisory["broker_mtd_pnl"] == 20752.0
    assert advisory["diagnosis"] == "public_local_focus_mismatch"
    assert advisory["preferred_warning"] == "Public retune focus and local canonical retune receipt disagree."
    assert advisory["preferred_action"] == (
        "Refresh or repair the canonical trade_closes writer before trusting local broker-proof counts."
    )


def test_closeout_marks_retune_advisory_unavailable_without_caches(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    def fake_run_command(args, *, cwd, timeout_s):
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.01,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)
    monkeypatch.setattr(
        closeout,
        "_canonical_root_gate",
        lambda root: closeout._gate("canonical_root", "pass", str(root)),
    )

    report = closeout.run_closeout(
        root=tmp_path,
        output_dir=tmp_path / "state",
        python_exe=sys.executable,
        include_live=False,
        run_tests=False,
        strict_secrets=False,
    )

    advisory = report["retune_advisory"]
    assert advisory["available"] is False
    assert advisory["focus_bot"] is None
    assert advisory["diagnosis"] is None


def test_main_prints_active_experiment_hint(monkeypatch, capsys) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    monkeypatch.setattr(
        closeout,
        "run_closeout",
        lambda **_: {
            "status": "warn",
            "outputs": {"latest": "C:/tmp/kaizen_closeout_latest.json"},
            "retune_advisory": {
                "available": True,
                "focus_bot": "mnq_futures_sage",
                "focus_closed_trade_count": 141,
                "focus_total_realized_pnl": -1939.75,
                "broker_mtd_pnl": 20752.0,
                "diagnosis": "public_local_focus_mismatch",
                "active_experiment": {
                    "experiment_id": "partial_profit_disabled",
                    "started_at": "2026-05-16T01:44:06+00:00",
                    "partial_profit_enabled": False,
                    "post_change_closed_trade_count": 2,
                    "post_change_total_realized_pnl": 40.0,
                    "post_change_profit_factor": 1.5,
                },
            },
            "gates": [],
            "exit_code": 1,
        },
    )

    rc = closeout.main(["--skip-tests"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "experiment: partial_profit_disabled since 2026-05-16T01:44:06+00:00" in out
    assert "outcome: partial_profit_disabled: 2 post-change closes | PnL $40.00 | PF 1.50" in out
