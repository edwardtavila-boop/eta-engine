"""Unit tests for ``brain.avengers.local_handlers``.

Each handler is exercised in isolation. The dispatch table is exercised
end-to-end via ``daemon._run_local_background_task`` in
``test_avengers_daemon.py::TestTick``.
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest

from apex_predator.brain.avengers import local_handlers as lh
from apex_predator.brain.avengers.dispatch import BackgroundTask

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# DASHBOARD_ASSEMBLE
# ---------------------------------------------------------------------------

class TestDashboardAssemble:
    def test_writes_atomic_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        out = tmp_path / "dashboard_latest.json"
        monkeypatch.setenv("APEX_DASHBOARD_PATH", str(out))
        result = lh._dashboard_assemble_handler(BackgroundTask.DASHBOARD_ASSEMBLE)
        assert result is not None
        assert result["written"] == str(out)
        assert out.exists()
        # No leftover .tmp file
        assert list(tmp_path.glob("*.tmp")) == []
        # Snapshot is parseable + carries all 9 panels
        data = json.loads(out.read_text(encoding="utf-8"))
        assert {"drift", "breaker", "deadman", "forecast", "daemons",
                "promotion", "calibration", "journal", "alerts"} <= set(data)
        assert set(result["panels"]) == set(data.keys())

    def test_handles_collect_state_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("APEX_DASHBOARD_PATH", str(tmp_path / "x.json"))
        # Patch the import target to raise.
        from apex_predator.scripts import jarvis_dashboard
        def _boom() -> dict:
            raise RuntimeError("synthetic failure")
        monkeypatch.setattr(jarvis_dashboard, "collect_state", _boom)
        result = lh._dashboard_assemble_handler(BackgroundTask.DASHBOARD_ASSEMBLE)
        assert result is not None
        assert result.get("written") is False
        assert "synthetic failure" in result.get("error", "")


# ---------------------------------------------------------------------------
# LOG_COMPACT
# ---------------------------------------------------------------------------

class TestLogCompact:
    def test_prunes_old_files_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        broker_dir = repo / "docs" / "broker_connections"
        broker_dir.mkdir(parents=True)
        old = broker_dir / "preflight_venue_connections_20260101T000000Z.json"
        new = broker_dir / "preflight_venue_connections_20260901T000000Z.json"
        latest = broker_dir / "preflight_venue_connections_latest.json"
        for p in (old, new, latest):
            p.write_text("{}")
        # Backdate `old` 30 days, leave `new` and `latest` fresh.
        old_ts = time.time() - 30 * 86_400.0
        import os
        os.utime(old, (old_ts, old_ts))
        # Redirect repo root.
        monkeypatch.setattr(lh, "_REPO_ROOT", repo)
        # Re-derive targets against the new root by patching the module
        # constant -- the handler resolves _REPO_ROOT / rel_dir at call time.
        result = lh._log_compact_handler(BackgroundTask.LOG_COMPACT)
        assert result is not None
        assert result["pruned"] == 1
        assert result["freed_bytes"] >= 2  # "{}" = 2 bytes
        assert not old.exists()
        assert new.exists()
        assert latest.exists()  # _latest.json is always preserved

    def test_no_targets_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(lh, "_REPO_ROOT", tmp_path)
        result = lh._log_compact_handler(BackgroundTask.LOG_COMPACT)
        assert result == {"pruned": 0, "freed_bytes": 0, "errors": []}


# ---------------------------------------------------------------------------
# PROMPT_WARMUP
# ---------------------------------------------------------------------------

class TestPromptWarmup:
    def test_returns_none_without_api_key(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert lh._prompt_warmup_handler(BackgroundTask.PROMPT_WARMUP) is None

    def test_skipped_when_warmup_flag_unset(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("APEX_PROMPT_WARMUP", raising=False)
        # Stub the SDK presence check so the handler can proceed past it.
        import sys
        import types
        monkeypatch.setitem(sys.modules, "anthropic", types.ModuleType("anthropic"))
        result = lh._prompt_warmup_handler(BackgroundTask.PROMPT_WARMUP)
        assert result is not None
        assert result["warmed"] == 0
        assert result["est_cost_usd"] == 0.0
        assert "warmup disabled" in result["skipped"]

    def test_real_sdk_call_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When APEX_PROMPT_WARMUP=1 + key + SDK present, the handler
        issues one real SDK call. Use a fake anthropic module that
        captures the request shape and returns a synthetic usage block."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("APEX_PROMPT_WARMUP", "1")

        captured: dict = {}

        class _FakeUsage:
            input_tokens = 250
            output_tokens = 4
            cache_creation_input_tokens = 250
            cache_read_input_tokens = 0

        class _FakeResponse:
            usage = _FakeUsage()

        class _FakeMessages:
            def create(self, **kwargs) -> _FakeResponse:
                captured.update(kwargs)
                return _FakeResponse()

        class _FakeAnthropic:
            def __init__(self, **_kwargs) -> None:
                self.messages = _FakeMessages()

        import sys
        import types
        fake_mod = types.ModuleType("anthropic")
        fake_mod.Anthropic = _FakeAnthropic  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

        result = lh._prompt_warmup_handler(BackgroundTask.PROMPT_WARMUP)
        assert result is not None
        assert result["warmed"] == 1
        assert result["failed"] == 0
        assert result["input_tokens"] == 250
        assert result["output_tokens"] == 4
        assert result["cache_creation"] == 250
        # 250 * 0.001/1000 + 250 * 0.00125/1000 + 4 * 0.005/1000
        # = 0.00025 + 0.0003125 + 0.00002 = 0.0005825
        assert result["est_cost_usd"] == pytest.approx(0.0005825, abs=1e-6)
        # Cache-control is on the system prefix.
        sys_block = captured["system"][0]
        assert sys_block["cache_control"]["type"] == "ephemeral"
        assert "JARVIS" in sys_block["text"]

    def test_sdk_failure_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A raising SDK call must surface as failed=1, not propagate."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("APEX_PROMPT_WARMUP", "1")

        class _Boom:
            def __init__(self, **_kwargs) -> None:
                self.messages = self

            def create(self, **_kwargs) -> None:
                raise RuntimeError("synthetic SDK failure")

        import sys
        import types
        fake_mod = types.ModuleType("anthropic")
        fake_mod.Anthropic = _Boom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

        result = lh._prompt_warmup_handler(BackgroundTask.PROMPT_WARMUP)
        assert result is not None
        assert result["warmed"] == 0
        assert result["failed"] == 1
        assert "synthetic" in result.get("error", "")


# ---------------------------------------------------------------------------
# SHADOW_TICK
# ---------------------------------------------------------------------------

class TestShadowTick:
    def test_none_when_no_journal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "APEX_SHADOW_JOURNAL_PATH",
            str(tmp_path / "missing.jsonl"),
        )
        assert lh._shadow_tick_handler(BackgroundTask.SHADOW_TICK) is None

    def test_tallies_buckets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        journal = tmp_path / "shadow.jsonl"
        rows = [
            {"strategy": "alpha", "regime": "TREND",  "is_win": True,  "pnl_r": 1.2},
            {"strategy": "alpha", "regime": "TREND",  "is_win": False, "pnl_r": -0.4},
            {"strategy": "alpha", "regime": "TREND",  "is_win": True,  "pnl_r": 0.8},
            {"strategy": "beta",  "regime": "RANGE",  "is_win": True,  "pnl_r": 0.3},
            "this is not json",
            {"strategy": "beta",  "regime": "RANGE",  "is_win": False, "pnl_r": -0.5},
        ]
        journal.write_text(
            "\n".join(
                json.dumps(r) if isinstance(r, dict) else r
                for r in rows
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("APEX_SHADOW_JOURNAL_PATH", str(journal))
        result = lh._shadow_tick_handler(BackgroundTask.SHADOW_TICK)
        assert result is not None
        assert result["parsed"] == 5
        assert result["skipped"] == 1
        assert result["buckets"] == 2
        assert result["by_bucket"]["alpha::TREND"]["n"] == 3
        assert result["by_bucket"]["alpha::TREND"]["win_rate"] == pytest.approx(2 / 3)
        assert result["by_bucket"]["beta::RANGE"]["cum_r"] == pytest.approx(-0.2)


# ---------------------------------------------------------------------------
# STRATEGY_MINE
# ---------------------------------------------------------------------------

class TestStrategyMine:
    def test_none_when_no_sources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(lh, "_REPO_ROOT", tmp_path)
        # Override the journal paths to nonexistent ones under tmp.
        monkeypatch.setattr(
            lh, "_DECISION_JOURNALS",
            (tmp_path / "x.jsonl",),
        )
        assert lh._strategy_mine_handler(BackgroundTask.STRATEGY_MINE) is None

    def test_tallies_top_strategies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        journal = tmp_path / "decisions.jsonl"
        rows = [
            {"strategy": "liquidity_sweep_displacement"},
            {"strategy": "liquidity_sweep_displacement"},
            {"strategy": "ob_breaker_retest"},
            {"setup":    "fvg_fill"},
            "garbage",
        ]
        journal.write_text(
            "\n".join(
                json.dumps(r) if isinstance(r, dict) else r
                for r in rows
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(lh, "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(lh, "_DECISION_JOURNALS", (journal,))
        result = lh._strategy_mine_handler(BackgroundTask.STRATEGY_MINE)
        assert result is not None
        assert result["total_records"] == 4
        assert result["unique_strategies"] == 3
        assert result["top_10"][0] == {
            "strategy": "liquidity_sweep_displacement", "count": 2,
        }


# ---------------------------------------------------------------------------
# Public dispatch + safety net
# ---------------------------------------------------------------------------

class TestPublicDispatch:
    def test_dispatch_table_covers_five_tasks(self) -> None:
        assert set(lh.LOCAL_HANDLERS.keys()) == {
            BackgroundTask.DASHBOARD_ASSEMBLE,
            BackgroundTask.LOG_COMPACT,
            BackgroundTask.PROMPT_WARMUP,
            BackgroundTask.SHADOW_TICK,
            BackgroundTask.STRATEGY_MINE,
        }

    def test_unregistered_task_returns_none(self) -> None:
        # KAIZEN_RETRO is not in the local-handler table -- must fall
        # through to fleet dispatch.
        assert lh.run_local_background_task(BackgroundTask.KAIZEN_RETRO) is None

    def test_handler_exception_returns_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _boom(_t: BackgroundTask) -> dict:
            raise RuntimeError("synthetic")
        monkeypatch.setitem(
            lh.LOCAL_HANDLERS, BackgroundTask.DASHBOARD_ASSEMBLE, _boom,
        )
        result = lh.run_local_background_task(BackgroundTask.DASHBOARD_ASSEMBLE)
        assert result is None
