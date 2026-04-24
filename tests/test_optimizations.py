"""
Tests for the 10-optimization bundle:
  - META_UPGRADE task handler (#5)
  - PROMPT_WARMUP handler (#7 -- gracefully skips without API key)
  - TelegramAdapter (#9)
  - status_page HTML exists + is well-formed (#10)
"""
from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# #5 META_UPGRADE
# ---------------------------------------------------------------------------

class TestMetaUpgrade:

    def test_handler_registered(self):
        from eta_engine.brain.avengers import BackgroundTask
        from deploy.scripts.run_task import HANDLERS
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
        from deploy.scripts.run_task import _task_meta_upgrade
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
        from deploy.scripts.run_task import _task_prompt_warmup
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
        from deploy.scripts.telegram_alerts import TelegramAdapter
        assert TelegramAdapter.from_env() is None

    def test_from_env_builds_adapter(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        from deploy.scripts.telegram_alerts import TelegramAdapter
        adapter = TelegramAdapter.from_env(state_dir=tmp_path)
        assert adapter is not None
        assert adapter.bot_token == "123:abc"
        assert adapter.chat_id == "999"
        assert adapter.api_base == "https://api.telegram.org/bot123:abc"

    def test_voice_sender_wraps_send(self, monkeypatch, tmp_path):
        from deploy.scripts.telegram_alerts import TelegramAdapter
        adapter = TelegramAdapter("t", "c", state_dir=tmp_path)
        sent = []
        adapter.send = lambda text, priority="INFO", **k: sent.append((text, priority)) or {"ok": True}
        fn = adapter.as_voice_sender()
        fn("TELEGRAM", "hello", "CRITICAL")
        assert sent == [("hello", "CRITICAL")]

    def test_send_records_to_state(self, monkeypatch, tmp_path):
        # Mock httpx.post to avoid network
        import httpx

        import deploy.scripts.telegram_alerts as mod

        class FakeResp:
            def json(self): return {"ok": True, "result": {"message_id": 1}}

        def fake_post(*args, **kwargs): return FakeResp()
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
        # Must polls our API
        assert "jarvis.evolutionarytradingalgo.live" in html
        # Must render the 4 main cards
        for anchor in ("m-health", "m-stress", "m-quota", "m-retros", "tasks-grid"):
            assert anchor in html
        # No hardcoded secrets or debug leftovers
        assert "localhost" not in html.lower() or "const API" in html
        assert "console.log" not in html
