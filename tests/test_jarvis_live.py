"""Tests for scripts.jarvis_live -- the live supervised daemon."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from eta_engine.brain.jarvis_context import (
    ActionSuggestion,
    EquitySnapshot,
    JarvisContext,
    JarvisSuggestion,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
    StressComponent,
    StressScore,
)
from eta_engine.obs.jarvis_supervisor import JarvisSupervisor, SupervisorPolicy
from eta_engine.scripts import jarvis_live

if TYPE_CHECKING:
    from collections.abc import Callable


_T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Stubs reused from the supervisor test pattern
# --------------------------------------------------------------------------- #


def _clock_fixed(t: datetime) -> Callable[[], datetime]:
    def fn() -> datetime:
        return t

    return fn


def _mk_ctx(
    *,
    ts: datetime = _T0,
    composite: float = 0.3,
    binding: str = "drawdown",
) -> JarvisContext:
    stress = StressScore(
        composite=composite,
        components=[StressComponent(name=binding, value=composite, weight=1.0)],
        binding_constraint=binding,
    )
    return JarvisContext(
        ts=ts,
        macro=MacroSnapshot(),
        equity=EquitySnapshot(
            account_equity=100_000.0,
            daily_pnl=0.0,
            daily_drawdown_pct=0.0,
            open_positions=0,
            open_risk_r=0.0,
        ),
        regime=RegimeSnapshot(regime="neutral", confidence=0.8),
        journal=JournalSnapshot(),
        suggestion=JarvisSuggestion(
            action=next(iter(ActionSuggestion)),
            reason="stub",
            confidence=0.9,
        ),
        stress_score=stress,
    )


class _StubMemory:
    def __init__(self, buf: list[JarvisContext]) -> None:
        self._buf = buf

    def __len__(self) -> int:
        return len(self._buf)

    def snapshots(self) -> list[JarvisContext]:
        return list(self._buf)


class _StubEngine:
    def __init__(
        self,
        *,
        queue: list[JarvisContext] | None = None,
        raises: bool = False,
    ) -> None:
        self._q: list[JarvisContext] = list(queue or [])
        self._memory: list[JarvisContext] = []
        self.raises = raises
        self.tick_calls = 0
        self.memory = _StubMemory(self._memory)

    def tick(self, *, notes: list[str] | None = None) -> JarvisContext:  # noqa: ARG002
        self.tick_calls += 1
        if self.raises:
            raise RuntimeError("engine boom")
        ctx = self._q.pop(0) if self._q else _mk_ctx()
        self._memory.append(ctx)
        return ctx


class _RecordingAlerter:
    """Stand-in for MultiAlerter that records every Alert sent."""

    def __init__(self) -> None:
        self.sent: list[object] = []
        self.closed: bool = False

    async def send(self, alert: object) -> list[bool]:
        self.sent.append(alert)
        return [True]

    async def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# _neutral_inputs + _load_inputs_file
# --------------------------------------------------------------------------- #


def test_neutral_inputs_are_valid() -> None:
    inp = jarvis_live._neutral_inputs()
    assert isinstance(inp.macro, MacroSnapshot)
    assert isinstance(inp.equity, EquitySnapshot)
    assert isinstance(inp.regime, RegimeSnapshot)
    assert isinstance(inp.journal, JournalSnapshot)
    assert inp.equity.account_equity == 0.0
    assert inp.regime.regime == "UNKNOWN"


def test_load_inputs_missing_file_returns_neutral(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.json"
    inp = jarvis_live._load_inputs_file(missing)
    assert inp.regime.regime == "UNKNOWN"


def test_load_inputs_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "inputs.json"
    path.write_text(
        json.dumps(
            {
                "macro": {"vix_level": 18.5, "macro_bias": "risk_on"},
                "equity": {
                    "account_equity": 25_000.0,
                    "daily_pnl": 120.0,
                    "daily_drawdown_pct": 0.01,
                    "open_positions": 1,
                    "open_risk_r": 0.5,
                },
                "regime": {"regime": "bull_quiet", "confidence": 0.75},
                "journal": {},
            }
        ),
        encoding="utf-8",
    )
    inp = jarvis_live._load_inputs_file(path)
    assert inp.equity.account_equity == 25_000.0
    assert inp.regime.regime == "bull_quiet"
    assert inp.macro.vix_level == 18.5


def test_load_inputs_malformed_json_returns_neutral(tmp_path: Path) -> None:
    path = tmp_path / "inputs.json"
    path.write_text("{ not json", encoding="utf-8")
    inp = jarvis_live._load_inputs_file(path)
    assert inp.regime.regime == "UNKNOWN"


def test_load_inputs_invalid_schema_returns_neutral(tmp_path: Path) -> None:
    path = tmp_path / "inputs.json"
    # Missing required equity fields.
    path.write_text(json.dumps({"equity": {}, "regime": {}}), encoding="utf-8")
    inp = jarvis_live._load_inputs_file(path)
    assert inp.regime.regime == "UNKNOWN"


# --------------------------------------------------------------------------- #
# _FileBackedProviders hot-reload semantics
# --------------------------------------------------------------------------- #


def test_file_backed_providers_hot_reload(tmp_path: Path) -> None:
    path = tmp_path / "inputs.json"
    path.write_text(
        json.dumps(
            {
                "equity": {
                    "account_equity": 10_000.0,
                    "daily_pnl": 0.0,
                    "daily_drawdown_pct": 0.0,
                    "open_positions": 0,
                    "open_risk_r": 0.0,
                },
                "regime": {"regime": "neutral", "confidence": 0.6},
            }
        ),
        encoding="utf-8",
    )
    providers = jarvis_live._FileBackedProviders(path)
    assert providers.get_equity().account_equity == 10_000.0
    # Overwrite and re-read.
    path.write_text(
        json.dumps(
            {
                "equity": {
                    "account_equity": 20_000.0,
                    "daily_pnl": 100.0,
                    "daily_drawdown_pct": 0.0,
                    "open_positions": 2,
                    "open_risk_r": 1.0,
                },
                "regime": {"regime": "neutral", "confidence": 0.6},
            }
        ),
        encoding="utf-8",
    )
    assert providers.get_equity().account_equity == 20_000.0
    assert providers.get_equity().open_positions == 2


def test_file_backed_providers_missing_file_neutral(tmp_path: Path) -> None:
    providers = jarvis_live._FileBackedProviders(tmp_path / "ghost.json")
    assert providers.get_regime().regime == "UNKNOWN"
    assert providers.get_equity().account_equity == 0.0
    assert providers.get_macro().macro_bias is not None
    assert isinstance(providers.get_journal_snapshot(), JournalSnapshot)


# --------------------------------------------------------------------------- #
# _write_health sinks
# --------------------------------------------------------------------------- #


def test_write_health_writes_latest_and_appends_log(tmp_path: Path) -> None:
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    sup.tick()
    r1 = sup.snapshot_health()
    jarvis_live._write_health(r1, tmp_path)

    latest = tmp_path / "jarvis_live_health.json"
    log = tmp_path / "jarvis_live_log.jsonl"
    assert latest.exists()
    assert log.exists()
    data = json.loads(latest.read_text(encoding="utf-8"))
    assert data["health"] == "GREEN"

    # Second report appends.
    sup.tick()
    r2 = sup.snapshot_health()
    jarvis_live._write_health(r2, tmp_path)
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_write_health_creates_missing_out_dir(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep"
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    sup.tick()
    r = sup.snapshot_health()
    jarvis_live._write_health(r, target)
    assert (target / "jarvis_live_health.json").exists()


# --------------------------------------------------------------------------- #
# build_alerter_from_env
# --------------------------------------------------------------------------- #


def test_build_alerter_from_env_no_env_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for v in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "DISCORD_WEBHOOK_URL",
        "SLACK_WEBHOOK_URL",
    ):
        monkeypatch.delenv(v, raising=False)
    assert jarvis_live.build_alerter_from_env() is None


def test_build_alerter_from_env_telegram_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for v in ("DISCORD_WEBHOOK_URL", "SLACK_WEBHOOK_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    alerter = jarvis_live.build_alerter_from_env()
    assert alerter is not None
    assert len(alerter.alerters) == 1


def test_build_alerter_from_env_all_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example/d")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://example/s")
    alerter = jarvis_live.build_alerter_from_env()
    assert alerter is not None
    assert len(alerter.alerters) == 3


def test_build_alerter_from_env_partial_telegram_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only bot token, no chat id -> telegram not created.
    for v in (
        "TELEGRAM_CHAT_ID",
        "DISCORD_WEBHOOK_URL",
        "SLACK_WEBHOOK_URL",
    ):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
    assert jarvis_live.build_alerter_from_env() is None


# --------------------------------------------------------------------------- #
# run_live: bounded loop, stop event, degraded alerting
# --------------------------------------------------------------------------- #


def test_run_live_rejects_nonpositive_interval(tmp_path: Path) -> None:
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    with pytest.raises(ValueError):
        asyncio.run(
            jarvis_live.run_live(
                supervisor=sup,
                alerter=None,
                out_dir=tmp_path,
                interval_s=0.0,
                max_ticks=1,
            )
        )


def test_run_live_bounded_by_max_ticks(tmp_path: Path) -> None:
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    reports = asyncio.run(
        jarvis_live.run_live(
            supervisor=sup,
            alerter=None,
            out_dir=tmp_path,
            interval_s=0.01,  # small to keep test fast
            max_ticks=3,
        )
    )
    assert len(reports) == 3
    assert sup.tick_count == 3
    assert (tmp_path / "jarvis_live_health.json").exists()
    log_lines = (
        (tmp_path / "jarvis_live_log.jsonl")
        .read_text(
            encoding="utf-8",
        )
        .strip()
        .splitlines()
    )
    assert len(log_lines) == 3


def test_run_live_stop_event_exits(tmp_path: Path) -> None:
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    stop = asyncio.Event()

    async def _drive() -> list:
        # Start the loop, then set stop after first tick settles.
        task = asyncio.create_task(
            jarvis_live.run_live(
                supervisor=sup,
                alerter=None,
                out_dir=tmp_path,
                interval_s=60.0,  # would hang, but we stop first
                max_ticks=None,
                stop_event=stop,
            )
        )
        # Yield control so the loop can complete first tick + enter wait.
        await asyncio.sleep(0.05)
        stop.set()
        return await task

    reports = asyncio.run(_drive())
    # At least one tick should have completed; loop exits promptly.
    assert len(reports) >= 1


def test_run_live_tick_exception_does_not_crash(tmp_path: Path) -> None:
    engine = _StubEngine(raises=True)
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    reports = asyncio.run(
        jarvis_live.run_live(
            supervisor=sup,
            alerter=None,
            out_dir=tmp_path,
            interval_s=0.01,
            max_ticks=2,
        )
    )
    assert len(reports) == 2
    # Engine tick was attempted each loop iteration.
    assert engine.tick_calls == 2


def test_run_live_closes_alerter_on_exit(tmp_path: Path) -> None:
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    alerter = _RecordingAlerter()
    asyncio.run(
        jarvis_live.run_live(
            supervisor=sup,
            alerter=alerter,  # type: ignore[arg-type]
            out_dir=tmp_path,
            interval_s=0.01,
            max_ticks=1,
        )
    )
    assert alerter.closed is True


def test_run_live_no_alert_on_green(tmp_path: Path) -> None:
    engine = _StubEngine()
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    alerter = _RecordingAlerter()
    asyncio.run(
        jarvis_live.run_live(
            supervisor=sup,
            alerter=alerter,  # type: ignore[arg-type]
            out_dir=tmp_path,
            interval_s=0.01,
            max_ticks=2,
        )
    )
    assert alerter.sent == []


def test_run_live_alerts_on_red(tmp_path: Path) -> None:
    # Build a supervisor whose health will go RED: invalid composite.
    # Pydantic rejects NaN at construction, so we build a valid ctx and
    # corrupt the already-validated attribute in-place to simulate
    # what an engine bug could produce.
    bad_ctx = _mk_ctx(composite=0.3)
    object.__setattr__(bad_ctx.stress_score, "composite", float("nan"))
    engine = _StubEngine(queue=[bad_ctx])
    sup = JarvisSupervisor(engine=engine, clock=_clock_fixed(_T0))
    alerter = _RecordingAlerter()
    asyncio.run(
        jarvis_live.run_live(
            supervisor=sup,
            alerter=alerter,  # type: ignore[arg-type]
            out_dir=tmp_path,
            interval_s=0.01,
            max_ticks=1,
        )
    )
    assert len(alerter.sent) == 1


# --------------------------------------------------------------------------- #
# _build_default_supervisor end-to-end wiring
# --------------------------------------------------------------------------- #


def test_build_default_supervisor_ticks_against_missing_inputs(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "inputs.json"
    sup = jarvis_live._build_default_supervisor(missing)
    # Should tick against the neutral/stub inputs without raising.
    ctx = sup.tick()
    assert ctx is not None
    rpt = sup.snapshot_health()
    # First tick is always fresh => GREEN.
    assert rpt.health.value == "GREEN"
    assert rpt.memory_len == 1


def test_build_default_supervisor_policy_is_default() -> None:
    sup = jarvis_live._build_default_supervisor(Path("/nonexistent/inputs.json"))
    assert isinstance(sup.policy, SupervisorPolicy)
    assert sup.policy.stale_after_s == 300.0


# --------------------------------------------------------------------------- #
# CLI main() -- argparse path with --max-ticks=1
# --------------------------------------------------------------------------- #


def test_main_runs_one_tick_and_returns_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ensure no env alerters so we exercise dry-run.
    for v in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "DISCORD_WEBHOOK_URL",
        "SLACK_WEBHOOK_URL",
    ):
        monkeypatch.delenv(v, raising=False)
    rc = jarvis_live.main(
        [
            "--inputs",
            str(tmp_path / "no_inputs.json"),
            "--out-dir",
            str(tmp_path),
            "--interval",
            "0.01",
            "--max-ticks",
            "1",
        ]
    )
    assert rc == 0
    assert (tmp_path / "jarvis_live_health.json").exists()
