"""
Tests for the 10-optimization bundle:
  - META_UPGRADE task handler (#5)
  - PROMPT_WARMUP handler (#7 -- gracefully skips without API key)
  - TelegramAdapter (#9)
  - status_page HTML exists + is well-formed (#10)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# #5 META_UPGRADE
# ---------------------------------------------------------------------------


class TestMetaUpgrade:
    def test_handler_registered(self):
        from eta_engine.brain.avengers import BackgroundTask
        from eta_engine.deploy.scripts.run_task import HANDLERS

        assert BackgroundTask.META_UPGRADE in HANDLERS

    def test_task_has_owner_and_cadence(self):
        from eta_engine.brain.avengers import (
            TASK_CADENCE,
            TASK_OWNERS,
            BackgroundTask,
        )

        assert TASK_OWNERS[BackgroundTask.META_UPGRADE] == "ALFRED"
        assert TASK_CADENCE[BackgroundTask.META_UPGRADE].startswith("30 4")

    def test_handler_skips_when_not_a_repo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APEX_REPO_DIR", str(tmp_path))
        from eta_engine.deploy.scripts.run_task import _task_meta_upgrade

        result = _task_meta_upgrade(tmp_path / "state")
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        # Re-run after state dir exists
        result = _task_meta_upgrade(tmp_path / "state")
        assert result.get("skipped") is True


# ---------------------------------------------------------------------------
# #7 PROMPT_WARMUP
# ---------------------------------------------------------------------------


class TestPromptWarmup:
    def test_skips_without_api_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from eta_engine.deploy.scripts.run_task import _task_prompt_warmup

        out = _task_prompt_warmup(tmp_path)
        assert out.get("skipped") is True
        assert "no API key" in out.get("reason", "")


# ---------------------------------------------------------------------------
# #9 Telegram adapter
# ---------------------------------------------------------------------------


class TestTelegramAdapter:
    def test_from_env_returns_none_if_unconfigured(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        from eta_engine.deploy.scripts.telegram_alerts import TelegramAdapter

        assert TelegramAdapter.from_env() is None

    def test_from_env_builds_adapter(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        from eta_engine.deploy.scripts.telegram_alerts import TelegramAdapter

        adapter = TelegramAdapter.from_env(state_dir=tmp_path)
        assert adapter is not None
        assert adapter.bot_token == "123:abc"
        assert adapter.chat_id == "999"
        assert adapter.api_base == "https://api.telegram.org/bot123:abc"

    def test_voice_sender_wraps_send(self, monkeypatch, tmp_path):
        from eta_engine.deploy.scripts.telegram_alerts import TelegramAdapter

        adapter = TelegramAdapter("t", "c", state_dir=tmp_path)
        sent = []
        adapter.send = lambda text, priority="INFO", **k: sent.append((text, priority)) or {"ok": True}
        fn = adapter.as_voice_sender()
        fn("TELEGRAM", "hello", "CRITICAL")
        assert sent == [("hello", "CRITICAL")]

    def test_send_records_to_state(self, monkeypatch, tmp_path):
        # Mock httpx.post to avoid network
        import httpx

        import eta_engine.deploy.scripts.telegram_alerts as mod

        class FakeResp:
            def json(self):
                return {"ok": True, "result": {"message_id": 1}}

        def fake_post(*args, **kwargs):
            return FakeResp()

        monkeypatch.setattr(httpx, "post", fake_post)

        adapter = mod.TelegramAdapter("t", "c", state_dir=tmp_path)
        result = adapter.send("test msg", priority="WARN")
        assert result.get("ok")
        log_path = tmp_path / "telegram_alerts.jsonl"
        assert log_path.exists()
        line = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        assert line["priority"] == "WARN"
        assert line["ok"] is True


# ---------------------------------------------------------------------------
# #10 status page
# ---------------------------------------------------------------------------


class TestStatusPage:
    def test_index_exists(self):
        path = Path(__file__).resolve().parent.parent / "deploy" / "status_page" / "index.html"
        assert path.exists()

    def test_index_has_expected_anchors(self):
        path = Path(__file__).resolve().parent.parent / "deploy" / "status_page" / "index.html"
        html = path.read_text(encoding="utf-8")
        # Wave-7 vanilla shell: top bar, login modal, tab nav, panel containers
        for anchor in (
            "login-modal",
            "step-up-modal",
            "top-bar",
            "top-operator-queue",
            "top-sse-status",
            "top-card-health",
            "view-jarvis",
            "view-fleet",
            "cc-operator-queue",
            "fl-fill-tape",
            "toast-container",
        ):
            assert anchor in html, f"missing shell anchor: {anchor}"
        # JS modules wired
        for module in ("/js/panels.js", "/js/auth.js", "/js/live.js",
                       "/js/command_center.js", "/js/bot_fleet.js"):
            assert module in html, f"missing JS module: {module}"
        # Theme css linked
        assert "/theme.css" in html
        # Batches 1-4: phone-safe shell, login fit, adaptive nav, operator route marker
        assert "viewport-fit=cover" in html
        assert 'data-command-center-shell="eta-live-status-page"' in html
        assert 'data-mobile-dashboard="adaptive"' in html
        assert 'data-dashboard-version="v1"' in html
        assert 'data-release-stage="pre_beta"' in html
        assert "ETA // V1 Command Center" in html
        assert "Pre-Beta V1" in html
        assert "Live data contract: bot fleet, equity, auth, freshness" in html
        assert "/api/dashboard/card-health" in html
        assert 'aria-label="Primary dashboard tabs"' in html
        assert 'class="skip-link"' in html
        assert 'class="modal-card' in html
        assert 'id="command-center-main"' in html
        assert "ops.evolutionarytradingalgo.com" in html
        # No hardcoded secrets or debug leftovers
        assert "localhost" not in html.lower() or "const API" in html
        assert "console.log" not in html

    def test_command_center_renders_operator_queue_panel(self):
        path = Path(__file__).resolve().parent.parent / "deploy" / "status_page" / "js" / "command_center.js"
        js = path.read_text(encoding="utf-8")
        assert "OperatorQueuePanel" in js
        assert "/api/jarvis/operator_queue" in js
        assert "top-operator-queue" in js
        assert "next_actions" in js

    def test_status_page_mobile_fleet_and_equity_contracts(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        css = (root / "theme.css").read_text(encoding="utf-8")
        bot_fleet = (root / "js" / "bot_fleet.js").read_text(encoding="utf-8")
        panels = (root / "js" / "panels.js").read_text(encoding="utf-8")
        auth = (root / "js" / "auth.js").read_text(encoding="utf-8")

        # Batches 5-7: phone roster cards, equity sizing, live freshness cues.
        assert "@media (max-width: 760px)" in css
        assert "@media (max-width: 520px)" in css
        assert ".mobile-card-table" in css
        assert "content: attr(data-label)" in css
        assert ".mobile-chart-shell" in css
        assert "data-label=\"Bot\"" in bot_fleet
        assert "data-label=\"Day PnL\"" in bot_fleet
        assert "data-label=\"Last Trade\"" in bot_fleet
        assert "mobile-card-table" in bot_fleet
        assert "mobile-chart-shell" in bot_fleet
        assert "data-quality" in bot_fleet
        assert "server_ts" in bot_fleet
        assert "source_age_s" in bot_fleet
        assert "source_updated_at" in bot_fleet
        assert "dashboard_version" in bot_fleet
        assert "release_stage" in bot_fleet
        assert "ensureLiveBotSelection" in bot_fleet
        assert "selectBot(firstLiveBot.name" in bot_fleet
        assert "document.hidden" in panels
        assert "cache: 'no-store'" in auth

    def test_status_page_card_health_contract_is_wired(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")
        css = (root / "theme.css").read_text(encoding="utf-8")
        supercharge = (root / "js" / "supercharge.js").read_text(encoding="utf-8")

        assert 'id="top-card-health"' in html
        assert "card-health-chip" in css
        assert "initCardHealthContract" in supercharge
        assert "/api/dashboard/card-health" in supercharge
        assert "dead_cards" in supercharge
        assert "stale_cards" in supercharge
        assert "LIVE_CARD_WATCHDOG_GRACE_MS" in supercharge
        assert "eta-card-health" in supercharge
        assert "never_refreshed" in supercharge
        assert "panel_error" in supercharge
        assert "refresh_age_exceeded" in supercharge
        assert "card-health-inspector" in supercharge
        assert "Card Health Inspector" in supercharge
        assert "toggleCardHealthInspector" in supercharge
        assert "focusCardHealthPanel" in supercharge
        assert "data-focus-card" in supercharge
        assert "card-health-focus" in supercharge
        assert "card-health-dead" in supercharge
        assert "card-health-stale" in supercharge
        assert "retryUnhealthyCards" in supercharge
        assert "data-retry-card-health" in supercharge
        assert "eta-card-retry" in supercharge
        assert "Retry unhealthy" in supercharge
        assert ".card-health-inspector" in css
        assert ".card-health-retry" in css
        assert ".panel.card-health-focus" in css
        assert ".panel.card-health-dead" in css
        assert ".panel.card-health-stale" in css

    def test_card_health_registry_covers_every_rendered_panel(self):
        from eta_engine.deploy.scripts.dashboard_api import DASHBOARD_CARD_REGISTRY

        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")
        rendered = set(re.findall(r'data-panel-id="([^"]+)"', html))
        registered = {str(card["id"]) for card in DASHBOARD_CARD_REGISTRY}

        assert rendered
        assert rendered == registered
        assert len(registered) == len(DASHBOARD_CARD_REGISTRY)

    def test_status_page_has_no_visible_mojibake_tokens(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        files = [
            root / "index.html",
            root / "theme.css",
            root / "js" / "panels.js",
            root / "js" / "command_center.js",
            root / "js" / "bot_fleet.js",
            root / "js" / "auth.js",
            root / "js" / "live.js",
        ]
        bad_tokens = ("â", "Â", "Ï", "�")
        for file in files:
            text = file.read_text(encoding="utf-8")
            for token in bad_tokens:
                assert token not in text, f"{file.name} contains mojibake token {token!r}"

    def test_theme_css_exists(self):
        path = Path(__file__).resolve().parent.parent / "deploy" / "status_page" / "theme.css"
        assert path.exists()
        css = path.read_text(encoding="utf-8")
        # Must define core panel + dark-mode tokens
        assert "--panel-bg" in css
        assert ".panel" in css
        assert ".panel.loading" in css
        assert ".panel.error" in css
        assert ".panel.stale" in css
        assert ".sse-connected" in css
        assert ".toast" in css
        # Batches 8-10: safe-area, touch targets, and readable phone density.
        assert "env(safe-area-inset-top)" in css
        assert "min-height: 44px" in css
        assert "overflow-wrap: anywhere" in css
        assert "prefers-reduced-motion" in css


# ---------------------------------------------------------------------------
# Supercharge tasks (round 2)
# ---------------------------------------------------------------------------


class TestSuperchargeTasks:
    """All 6 new supercharge tasks must be registered + have handlers."""

    def test_all_new_tasks_registered(self):
        from eta_engine.brain.avengers import (
            TASK_CADENCE,
            TASK_OWNERS,
            BackgroundTask,
        )
        from eta_engine.deploy.scripts.run_task import HANDLERS

        new_tasks = (
            BackgroundTask.HEALTH_WATCHDOG,
            BackgroundTask.SELF_TEST,
            BackgroundTask.LOG_ROTATE,
            BackgroundTask.DISK_CLEANUP,
            BackgroundTask.BACKUP,
            BackgroundTask.PROMETHEUS_EXPORT,
        )
        for task in new_tasks:
            assert task in TASK_OWNERS, f"{task.value} missing from TASK_OWNERS"
            assert task in TASK_CADENCE, f"{task.value} missing from TASK_CADENCE"
            assert task in HANDLERS, f"{task.value} missing from HANDLERS"

    def test_log_rotate_handler_writes_report(self, tmp_path):
        """LOG_ROTATE should run without error even on empty log dir."""
        state = tmp_path / "state"
        state.mkdir()
        logdir = tmp_path / "logs"
        logdir.mkdir()
        # Create a fresh .log file (too new to archive)
        (logdir / "active.log").write_text("hello\n")
        from eta_engine.deploy.scripts.run_task import _task_log_rotate

        out = _task_log_rotate(state, logdir)
        assert "archived" in out
        assert (state / "log_rotate.json").exists()

    def test_backup_handler_creates_archive(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        state.mkdir()
        (state / "foo.json").write_text("{}")
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".env").write_text("DUMMY=1")
        monkeypatch.setenv("APEX_REPO_DIR", str(repo))
        from eta_engine.deploy.scripts.run_task import _task_backup

        out = _task_backup(state)
        assert "archive" in out
        assert out["size_bytes"] > 0
        backups = list((state / "backups").glob("apex-backup-*.tar.gz"))
        assert len(backups) == 1

    def test_prometheus_export_handler_writes_metrics(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        # Seed minimal heartbeat
        hb = {
            "ts": "2026-04-24T00:00:00+00:00",
            "quota_state": "OK",
            "hourly_pct": 0.05,
            "daily_pct": 0.12,
            "cache_hit_rate": 0.88,
            "distiller_version": 3,
            "distiller_trained": True,
        }
        (state / "avengers_heartbeat.json").write_text(json.dumps(hb))
        from eta_engine.deploy.scripts.run_task import _task_prometheus_export

        out = _task_prometheus_export(state)
        prom_file = state / "prometheus" / "avengers.prom"
        assert prom_file.exists()
        text = prom_file.read_text(encoding="utf-8")
        assert "apex_up 1" in text
        assert "apex_quota_hourly_pct 0.05" in text
        assert "apex_cache_hit_rate 0.88" in text
        assert out["metrics"] > 0

    def test_self_test_report_written(self, tmp_path, monkeypatch):
        """SELF_TEST writes a structured report even when probes fail."""
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from eta_engine.deploy.scripts.run_task import _task_self_test

        out = _task_self_test(state)
        assert "overall" in out
        assert (state / "self_test.json").exists()

    def test_health_watchdog_non_windows_skip(self, tmp_path, monkeypatch):
        """On non-Windows, watchdog reports skipped without error."""
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr("os.name", "posix", raising=False)
        from eta_engine.deploy.scripts.run_task import _task_health_watchdog

        out = _task_health_watchdog(state)
        assert out.get("skipped") is True

    def test_disk_cleanup_runs_without_error(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setenv("APEX_REPO_DIR", str(tmp_path / "nonexistent"))
        from eta_engine.deploy.scripts.run_task import _task_disk_cleanup

        out = _task_disk_cleanup(state)
        assert "bytes_freed" in out
        assert "files_deleted" in out


class TestPrometheusEndpoint:
    """Dashboard API should expose /metrics."""

    def test_metrics_endpoint_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APEX_STATE_DIR", str(tmp_path))
        import importlib

        import eta_engine.deploy.scripts.dashboard_api as mod

        importlib.reload(mod)
        from fastapi.testclient import TestClient

        client = TestClient(mod.app)

        # Empty -- no metrics file yet
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "apex_up" in r.text

        # Seed metrics file
        prom_dir = tmp_path / "prometheus"
        prom_dir.mkdir()
        (prom_dir / "avengers.prom").write_text(
            "# HELP apex_up daemon alive\n# TYPE apex_up gauge\napex_up 1\n",
        )
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "apex_up 1" in r.text
        assert "text/plain" in r.headers["content-type"]
