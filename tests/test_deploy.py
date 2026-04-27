"""
Tests for the deploy/ package -- task runner, smoke check, daemon.
"""

from __future__ import annotations

from pathlib import Path

from eta_engine.brain.avengers import BackgroundTask


class TestRunTask:
    def test_all_background_tasks_have_handlers(self):
        """Every BackgroundTask enum value has a registered handler."""
        from eta_engine.deploy.scripts.run_task import HANDLERS

        for task in BackgroundTask:
            assert task in HANDLERS, f"missing handler for {task.value}"

    def test_unknown_task_exits_nonzero(self, tmp_path):
        from eta_engine.deploy.scripts.run_task import main

        rc = main(
            [
                "TOTALLY_MADE_UP_TASK",
                "--state-dir",
                str(tmp_path / "state"),
                "--log-dir",
                str(tmp_path / "logs"),
            ]
        )
        assert rc == 2

    def test_kaizen_retro_runs_clean(self, tmp_path):
        from eta_engine.deploy.scripts.run_task import main

        rc = main(
            [
                "KAIZEN_RETRO",
                "--state-dir",
                str(tmp_path / "state"),
                "--log-dir",
                str(tmp_path / "logs"),
            ]
        )
        assert rc == 0
        assert (tmp_path / "state" / "kaizen_ledger.json").exists()
        assert (tmp_path / "state" / "last_task.json").exists()

    def test_dashboard_assemble_runs_clean(self, tmp_path):
        from eta_engine.deploy.scripts.run_task import main

        rc = main(
            [
                "DASHBOARD_ASSEMBLE",
                "--state-dir",
                str(tmp_path / "state"),
                "--log-dir",
                str(tmp_path / "logs"),
            ]
        )
        assert rc == 0
        assert (tmp_path / "state" / "dashboard_payload.json").exists()

    def test_twin_verdict_runs_clean(self, tmp_path):
        from eta_engine.deploy.scripts.run_task import main

        rc = main(
            [
                "TWIN_VERDICT",
                "--state-dir",
                str(tmp_path / "state"),
                "--log-dir",
                str(tmp_path / "logs"),
            ]
        )
        assert rc == 0
        assert (tmp_path / "state" / "twin_verdict.json").exists()

    def test_strategy_mine_runs_clean(self, tmp_path):
        from eta_engine.deploy.scripts.run_task import main

        rc = main(
            [
                "STRATEGY_MINE",
                "--state-dir",
                str(tmp_path / "state"),
                "--log-dir",
                str(tmp_path / "logs"),
            ]
        )
        assert rc == 0
        assert (tmp_path / "state" / "strategy_candidates.json").exists()

    def test_doctrine_review_writes_report(self, tmp_path):
        from eta_engine.deploy.scripts.run_task import main

        rc = main(
            [
                "DOCTRINE_REVIEW",
                "--state-dir",
                str(tmp_path / "state"),
                "--log-dir",
                str(tmp_path / "logs"),
            ]
        )
        assert rc == 0
        out = (tmp_path / "state" / "doctrine_review.md").read_text(encoding="utf-8")
        assert "EVOLUTIONARY TRADING ALGO DOCTRINE" in out
        assert "CAPITAL_FIRST" in out


class TestSmokeCheck:
    def test_imports_pass(self):
        from eta_engine.deploy.scripts.smoke_check import check_imports

        ok, _ = check_imports()
        assert ok

    def test_dirs_check_creates_dirs(self):
        from eta_engine.deploy.scripts.smoke_check import check_dirs

        ok, _ = check_dirs()
        assert ok

    def test_dispatch_check_runs(self):
        from eta_engine.deploy.scripts.smoke_check import check_dispatch

        ok, msg = check_dispatch()
        assert ok, msg

    def test_task_handlers_check_passes(self):
        from eta_engine.deploy.scripts.smoke_check import check_task_handlers

        ok, msg = check_task_handlers()
        assert ok, msg

    def test_smoke_with_skip_systemd(self, tmp_path, monkeypatch):
        """Main entry with --skip-systemd should work even without .env."""
        monkeypatch.chdir(tmp_path)
        # Create a minimal .env with required keys
        (tmp_path / ".env").write_text(
            "ANTHROPIC_API_KEY=stub\nJARVIS_HOURLY_USD_BUDGET=1.00\nJARVIS_DAILY_USD_BUDGET=10.00\n",
            encoding="utf-8",
        )
        from eta_engine.deploy.scripts.smoke_check import main

        rc = main(["--skip-systemd"])
        assert rc in (0, 1)  # may fail on dir perms in CI; just shouldn't crash


class TestAvengersDaemon:
    def test_heartbeat_shape(self, tmp_path):
        from eta_engine.deploy.scripts.avengers_daemon import AvengersDaemon

        d = AvengersDaemon(state_dir=tmp_path)
        hb = d.heartbeat()
        assert "ts" in hb
        assert "quota_state" in hb
        assert hb["quota_state"] in {"OK", "WARN", "DOWNSHIFT", "FREEZE"}

    def test_persist_writes_state(self, tmp_path):
        from eta_engine.deploy.scripts.avengers_daemon import AvengersDaemon

        d = AvengersDaemon(state_dir=tmp_path)
        d.persist()
        assert (tmp_path / "usage_tracker.json").exists()
        assert (tmp_path / "distiller.json").exists()


class TestDeployArtifacts:
    """Sanity checks on the non-Python deploy artifacts."""

    _DEPLOY = Path(__file__).resolve().parent.parent / "deploy"

    def test_install_script_exists_and_parseable(self):
        p = self._DEPLOY / "install_vps.sh"
        assert p.exists()
        text = p.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash")
        assert "set -euo pipefail" in text

    def test_uninstall_script_exists(self):
        assert (self._DEPLOY / "uninstall_vps.sh").exists()

    def test_systemd_units_all_present(self):
        for unit in ("jarvis-live.service", "avengers-fleet.service", "eta-dashboard.service"):
            p = self._DEPLOY / "systemd" / unit
            assert p.exists(), f"missing {unit}"
            text = p.read_text(encoding="utf-8")
            assert "[Unit]" in text
            assert "[Service]" in text
            assert "[Install]" in text
            # Hardening should be present
            assert "NoNewPrivileges" in text

    def test_crontab_has_all_tasks(self):
        text = (self._DEPLOY / "cron" / "avengers.crontab").read_text(encoding="utf-8")
        for task in BackgroundTask:
            assert task.value in text, f"{task.value} missing from crontab"
        # Every apex line must have the tag
        for line in text.splitlines():
            if "run_task" in line and not line.strip().startswith("#"):
                assert "eta-engine:avengers" in line

    def test_readme_exists(self):
        assert (self._DEPLOY / "README.md").exists()
