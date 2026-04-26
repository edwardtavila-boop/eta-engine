"""Tests for the dispatcher wires that turn drift AUTO_DEMOTE and stale
heartbeats into routable ``alert_dispatcher`` events.

Two wires:
* ``brain.avengers.drift_detector.DriftDetector`` -- emits ``drift_demote``
  when ``check()`` produces an ``AUTO_DEMOTE`` verdict.
* ``obs.heartbeat.HeartbeatMonitor`` -- emits ``deadman_timeout`` from
  ``_alert_stale()`` for each newly-stale bot.

Both injectors are optional ctor args -- when ``dispatcher=None`` (the
default) every existing caller of these classes keeps the legacy
behaviour unchanged. We pin both branches.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from apex_predator.brain.avengers.drift_detector import (
    DriftDetector,
    DriftVerdict,
)
from apex_predator.obs.heartbeat import HeartbeatMonitor

if TYPE_CHECKING:
    from pathlib import Path


class _FakeDispatcher:
    """Minimal stand-in for ``AlertDispatcher`` -- captures send() calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def send(self, event: str, payload: dict) -> None:
        self.calls.append((event, payload))


class _RaisingDispatcher:
    def send(self, event: str, payload: dict) -> None:
        raise RuntimeError("dispatcher is broken")


# ---------------------------------------------------------------------------
# DriftDetector wire
# ---------------------------------------------------------------------------


class TestDriftDispatchWire:
    def _bt_lv_for_demote(self) -> tuple[list[float], list[float]]:
        # Tight high-Sharpe backtest vs deep-loss live -- forces AUTO_DEMOTE
        # via both KL and Sharpe-delta thresholds.
        bt = [0.004] * 30 + [0.0035] * 30
        lv = [-0.006] * 30
        return bt, lv

    def _bt_lv_for_ok(self) -> tuple[list[float], list[float]]:
        # Same distribution -- verdict stays OK.
        bt = [0.001, 0.002, -0.001, 0.0015] * 8
        lv = [0.001, 0.002, -0.001, 0.0015] * 6
        return bt, lv

    def test_no_dispatcher_no_send(self, tmp_path: Path) -> None:
        det = DriftDetector(journal_path=tmp_path / "drift.jsonl")
        bt, lv = self._bt_lv_for_demote()
        report = det.check("strat_X", bt, lv, journal=False)
        # Sanity: this configuration produces AUTO_DEMOTE.
        assert report.verdict is DriftVerdict.AUTO_DEMOTE
        # No dispatcher injected -> nothing to assert besides "no crash".

    def test_auto_demote_emits_drift_demote_event(self, tmp_path: Path) -> None:
        d = _FakeDispatcher()
        det = DriftDetector(journal_path=tmp_path / "drift.jsonl", dispatcher=d)
        bt, lv = self._bt_lv_for_demote()
        report = det.check("strat_X", bt, lv, journal=False)
        assert report.verdict is DriftVerdict.AUTO_DEMOTE
        assert len(d.calls) == 1
        event, payload = d.calls[0]
        assert event == "drift_demote"
        assert payload["strategy_id"] == "strat_X"
        assert payload["verdict"] == "AUTO_DEMOTE"
        assert payload["live_sample_size"] == report.live_sample_size
        assert payload["bt_sample_size"] == report.bt_sample_size
        assert isinstance(payload["reasons"], list)
        assert len(payload["reasons"]) >= 1
        # JSON-serialisable so the dispatch journal can store it.
        json.dumps(payload)

    def test_ok_verdict_does_not_emit_event(self, tmp_path: Path) -> None:
        d = _FakeDispatcher()
        det = DriftDetector(journal_path=tmp_path / "drift.jsonl", dispatcher=d)
        bt, lv = self._bt_lv_for_ok()
        report = det.check("strat_Y", bt, lv, journal=False)
        assert report.verdict is DriftVerdict.OK
        assert d.calls == []

    def test_warn_verdict_does_not_emit_event(self, tmp_path: Path) -> None:
        # Threshold tuned so we land in WARN: live Sharpe noticeably below
        # backtest but not enough sigmas to hit demote.
        d = _FakeDispatcher()
        det = DriftDetector(
            journal_path=tmp_path / "drift.jsonl",
            warn_sharpe_delta_sigma=0.2,
            demote_sharpe_delta_sigma=20.0,
            warn_kl=0.0001,
            demote_kl=20.0,
            dispatcher=d,
        )
        bt = [0.002] * 30
        lv = [0.0015] * 30
        report = det.check("strat_Z", bt, lv, journal=False)
        # Sanity: this falls into WARN, not AUTO_DEMOTE.
        assert report.verdict is DriftVerdict.WARN
        # No drift_demote event for WARN.
        assert d.calls == []

    def test_dispatcher_failure_does_not_crash_check(self, tmp_path: Path) -> None:
        det = DriftDetector(
            journal_path=tmp_path / "drift.jsonl",
            dispatcher=_RaisingDispatcher(),
        )
        bt, lv = self._bt_lv_for_demote()
        # Must not raise -- the AUTO_DEMOTE path swallows dispatcher errors.
        report = det.check("strat_X", bt, lv, journal=False)
        assert report.verdict is DriftVerdict.AUTO_DEMOTE


# ---------------------------------------------------------------------------
# HeartbeatMonitor wire
# ---------------------------------------------------------------------------


class TestHeartbeatDispatchWire:
    def test_no_dispatcher_no_send_legacy_path_works(self) -> None:
        # No alerter, no dispatcher -- _alert_stale is a no-op.
        m = HeartbeatMonitor(default_timeout_s=1)
        m.register("mnq", timeout_s=1)
        # Don't call tick(), but force-age the last-seen entry so it goes
        # stale immediately without sleeping.
        from datetime import UTC, datetime, timedelta

        m._last["mnq"] = datetime.now(UTC) - timedelta(seconds=5)
        stale = m.check_stale()
        assert stale == ["mnq"]
        # No dispatcher / no alerter => no exception.
        asyncio.run(m._alert_stale(stale))

    def test_stale_bot_emits_deadman_timeout(self) -> None:
        from datetime import UTC, datetime, timedelta

        d = _FakeDispatcher()
        m = HeartbeatMonitor(default_timeout_s=1, dispatcher=d)
        m.register("mnq", timeout_s=1)
        m._last["mnq"] = datetime.now(UTC) - timedelta(seconds=5)
        stale = m.check_stale()
        assert stale == ["mnq"]
        asyncio.run(m._alert_stale(stale))
        assert len(d.calls) == 1
        event, payload = d.calls[0]
        assert event == "deadman_timeout"
        assert payload["bot"] == "mnq"
        assert payload["timeout_seconds"] == 1
        assert isinstance(payload["last_heartbeat"], str)
        assert payload["stale_seconds"] is not None
        assert payload["stale_seconds"] >= 1.0
        # JSON-serialisable.
        json.dumps(payload)

    def test_dedup_only_alerts_each_bot_once(self) -> None:
        from datetime import UTC, datetime, timedelta

        d = _FakeDispatcher()
        m = HeartbeatMonitor(default_timeout_s=1, dispatcher=d)
        m.register("mnq", timeout_s=1)
        m._last["mnq"] = datetime.now(UTC) - timedelta(seconds=5)

        asyncio.run(m._alert_stale(m.check_stale()))
        asyncio.run(m._alert_stale(m.check_stale()))
        asyncio.run(m._alert_stale(m.check_stale()))
        # First call dispatches; subsequent calls hit the _alerted-set
        # dedup and skip.
        assert len(d.calls) == 1

    def test_tick_clears_dedup_so_next_stale_realerts(self) -> None:
        from datetime import UTC, datetime, timedelta

        d = _FakeDispatcher()
        m = HeartbeatMonitor(default_timeout_s=1, dispatcher=d)
        m.register("mnq", timeout_s=1)
        m._last["mnq"] = datetime.now(UTC) - timedelta(seconds=5)
        asyncio.run(m._alert_stale(m.check_stale()))
        assert len(d.calls) == 1

        # Bot recovers -> goes stale again -> alert should re-fire.
        m.tick("mnq")
        m._last["mnq"] = datetime.now(UTC) - timedelta(seconds=5)
        asyncio.run(m._alert_stale(m.check_stale()))
        assert len(d.calls) == 2

    def test_dispatcher_failure_does_not_crash_alert_loop(self) -> None:
        from datetime import UTC, datetime, timedelta

        m = HeartbeatMonitor(default_timeout_s=1, dispatcher=_RaisingDispatcher())
        m.register("mnq", timeout_s=1)
        m._last["mnq"] = datetime.now(UTC) - timedelta(seconds=5)
        # Must not raise.
        asyncio.run(m._alert_stale(m.check_stale()))

    def test_multiple_stale_bots_each_get_their_own_event(self) -> None:
        from datetime import UTC, datetime, timedelta

        d = _FakeDispatcher()
        m = HeartbeatMonitor(default_timeout_s=1, dispatcher=d)
        for bot in ("mnq", "btc_hybrid", "eth_perp"):
            m.register(bot, timeout_s=1)
            m._last[bot] = datetime.now(UTC) - timedelta(seconds=5)
        stale = m.check_stale()
        assert sorted(stale) == ["btc_hybrid", "eth_perp", "mnq"]
        asyncio.run(m._alert_stale(stale))
        assert len(d.calls) == 3
        bots = sorted(payload["bot"] for _, payload in d.calls)
        assert bots == ["btc_hybrid", "eth_perp", "mnq"]


# ---------------------------------------------------------------------------
# alerts.yaml smoke -- new events are present and route through mcc_push
# ---------------------------------------------------------------------------


class TestAlertsYamlWiring:
    """Pin the YAML so future edits don't accidentally drop the routing."""

    @pytest.fixture
    def alerts_cfg(self) -> dict:
        from pathlib import Path

        import yaml

        repo_root = Path(__file__).resolve().parents[1]
        return yaml.safe_load((repo_root / "configs" / "alerts.yaml").read_text())

    def test_drift_demote_routes_through_mcc_push(self, alerts_cfg: dict) -> None:
        ev = alerts_cfg["routing"]["events"]["drift_demote"]
        assert ev["level"] == "critical"
        assert "mcc_push" in ev["channels"]

    def test_deadman_timeout_routes_through_mcc_push(self, alerts_cfg: dict) -> None:
        ev = alerts_cfg["routing"]["events"]["deadman_timeout"]
        assert ev["level"] == "critical"
        assert "mcc_push" in ev["channels"]
