from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

from eta_engine.feeds._script_shim import build_script_shim

ETA_ENGINE_ROOT = Path(__file__).resolve().parents[1]


def _read_text(relative_path: str) -> str:
    return (ETA_ENGINE_ROOT / relative_path).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("feed_name", "script_name", "symbol"),
    [
        (
            "eta_engine.feeds.workspace_roots",
            "eta_engine.scripts.workspace_roots",
            "ETA_DATA_LAKE_ROOT",
        ),
        (
            "eta_engine.feeds.operator_action_queue",
            "eta_engine.scripts.operator_action_queue",
            "collect_items",
        ),
        (
            "eta_engine.feeds.run_eta_live",
            "eta_engine.scripts.run_eta_live",
            "RuntimeConfig",
        ),
        (
            "eta_engine.feeds.jarvis_strategy_supervisor",
            "eta_engine.scripts.jarvis_strategy_supervisor",
            "JarvisStrategySupervisor",
        ),
        (
            "eta_engine.feeds.weekly_sharpe_check",
            "eta_engine.scripts.weekly_sharpe_check",
            "ETA_RUNTIME_DECISION_JOURNAL_PATH",
        ),
        (
            "eta_engine.feeds.announce_data_library",
            "eta_engine.scripts.announce_data_library",
            "build_inventory_snapshot",
        ),
        (
            "eta_engine.feeds.paper_live_launch_check",
            "eta_engine.scripts.paper_live_launch_check",
            "write_snapshot",
        ),
        (
            "eta_engine.feeds.decision_journal_smoke",
            "eta_engine.scripts.decision_journal_smoke",
            "append_decision_journal_smoke",
        ),
        (
            "eta_engine.feeds.runtime_log_smoke",
            "eta_engine.scripts.runtime_log_smoke",
            "append_runtime_smoke",
        ),
        (
            "eta_engine.feeds.drift_check",
            "eta_engine.scripts.drift_check",
            "ETA_RUNTIME_DECISION_JOURNAL_PATH",
        ),
        (
            "eta_engine.feeds.drift_check_all",
            "eta_engine.scripts.drift_check_all",
            "ETA_RUNTIME_DECISION_JOURNAL_PATH",
        ),
        (
            "eta_engine.feeds.drift_watchdog_smoke",
            "eta_engine.scripts.drift_watchdog_smoke",
            "append_drift_watchdog_smoke",
        ),
        (
            "eta_engine.feeds.data_health_check",
            "eta_engine.scripts.data_health_check",
            "run_health_check",
        ),
        (
            "eta_engine.feeds.sage_health_check",
            "eta_engine.scripts.sage_health_check",
            "logger",
        ),
        (
            "eta_engine.feeds.venue_readiness_check",
            "eta_engine.scripts.venue_readiness_check",
            "check_venues",
        ),
        (
            "eta_engine.feeds.paper_trade_preflight",
            "eta_engine.scripts.paper_trade_preflight",
            "run_preflight",
        ),
        (
            "eta_engine.feeds.daily_premarket",
            "eta_engine.scripts.daily_premarket",
            "run",
        ),
        (
            "eta_engine.feeds.paper_run_harness",
            "eta_engine.scripts.paper_run_harness",
            "BOT_PLAN",
        ),
        (
            "eta_engine.feeds.btc_live",
            "eta_engine.scripts.btc_live",
            "evaluate_live_gate",
        ),
        (
            "eta_engine.feeds.btc_paper_lane",
            "eta_engine.scripts.btc_paper_lane",
            "PaperLaneRunner",
        ),
        (
            "eta_engine.feeds.btc_broker_fleet",
            "eta_engine.scripts.btc_broker_fleet",
            "fleet_workers",
        ),
        (
            "eta_engine.feeds.btc_paper_trade",
            "eta_engine.scripts.btc_paper_trade",
            "BtcPaperRunner",
        ),
        (
            "eta_engine.feeds._trade_journal_reconcile",
            "eta_engine.scripts._trade_journal_reconcile",
            "DEFAULT_BTC",
        ),
        (
            "eta_engine.feeds.chaos_drill",
            "eta_engine.scripts.chaos_drill",
            "run_drills",
        ),
        (
            "eta_engine.feeds.jarvis_status",
            "eta_engine.scripts.jarvis_status",
            "build_bot_strategy_readiness_summary",
        ),
        (
            "eta_engine.feeds.bot_strategy_readiness",
            "eta_engine.scripts.bot_strategy_readiness",
            "build_readiness_matrix",
        ),
        (
            "eta_engine.feeds.jarvis_live",
            "eta_engine.scripts.jarvis_live",
            "run_live",
        ),
        (
            "eta_engine.feeds.weekly_review",
            "eta_engine.scripts.weekly_review",
            "ReviewEntry",
        ),
        (
            "eta_engine.feeds.schedule_weekly_review",
            "eta_engine.scripts.schedule_weekly_review",
            "emit_cron",
        ),
        (
            "eta_engine.feeds.live_tiny_preflight_dryrun",
            "eta_engine.scripts.live_tiny_preflight_dryrun",
            "Gate",
        ),
        (
            "eta_engine.feeds.go_trigger",
            "eta_engine.scripts.go_trigger",
            "TriggerEvent",
        ),
        (
            "eta_engine.feeds._backup_state",
            "eta_engine.scripts._backup_state",
            "critical_files",
        ),
        (
            "eta_engine.feeds._sharpe_drift",
            "eta_engine.scripts._sharpe_drift",
            "DEFAULT_REPORT",
        ),
        (
            "eta_engine.feeds._repo_health",
            "eta_engine.scripts._repo_health",
            "log_files",
        ),
        (
            "eta_engine.feeds.live_supervisor",
            "eta_engine.scripts.live_supervisor",
            "JarvisAwareRouter",
        ),
        (
            "eta_engine.feeds.jarvis_dashboard",
            "eta_engine.scripts.jarvis_dashboard",
            "collect_state",
        ),
        (
            "eta_engine.feeds.jarvis_sage",
            "eta_engine.scripts.jarvis_sage",
            "ROOT",
        ),
        (
            "eta_engine.feeds.bandit_promotion_check",
            "eta_engine.scripts.bandit_promotion_check",
            "workspace_roots",
        ),
        (
            "eta_engine.feeds.export_to_notion",
            "eta_engine.scripts.export_to_notion",
            "workspace_roots",
        ),
        (
            "eta_engine.feeds.run_critique_nightly",
            "eta_engine.scripts.run_critique_nightly",
            "workspace_roots",
        ),
        (
            "eta_engine.feeds.jarvis_ask",
            "eta_engine.scripts.jarvis_ask",
            "workspace_roots",
        ),
        (
            "eta_engine.feeds.run_anomaly_scan",
            "eta_engine.scripts.run_anomaly_scan",
            "workspace_roots",
        ),
        (
            "eta_engine.feeds.run_calibration_fit",
            "eta_engine.scripts.run_calibration_fit",
            "workspace_roots",
        ),
        (
            "eta_engine.feeds.score_policy_candidate",
            "eta_engine.scripts.score_policy_candidate",
            "workspace_roots",
        ),
        (
            "eta_engine.feeds.refresh_correlation_matrix",
            "eta_engine.scripts.refresh_correlation_matrix",
            "workspace_roots",
        ),
        (
            "eta_engine.feeds.retrain_models",
            "eta_engine.scripts.retrain_models",
            "workspace_roots",
        ),
    ],
)
def test_feed_shims_reexport_script_symbols(feed_name: str, script_name: str, symbol: str) -> None:
    feed_module = importlib.import_module(feed_name)
    script_module = importlib.import_module(script_name)

    assert symbol in feed_module.__all__
    assert symbol in dir(feed_module)
    assert getattr(feed_module, symbol) is getattr(script_module, symbol)


def test_workspace_roots_shim_exposes_new_script_runtime_paths() -> None:
    feed_module = importlib.import_module("eta_engine.feeds.workspace_roots")
    script_module = importlib.import_module("eta_engine.scripts.workspace_roots")

    assert feed_module.ETA_DATA_LAKE_ROOT is script_module.ETA_DATA_LAKE_ROOT
    assert feed_module.ETA_FM_HEALTH_SNAPSHOT_PATH is script_module.ETA_FM_HEALTH_SNAPSHOT_PATH


def test_operator_action_queue_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.operator_action_queue")
    script_module = importlib.import_module("eta_engine.scripts.operator_action_queue")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 31

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--json"]) == 31
    assert seen["argv"] == ["--json"]


def test_run_eta_live_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.run_eta_live")
    script_module = importlib.import_module("eta_engine.scripts.run_eta_live")
    seen: dict[str, bool] = {"called": False}

    def _fake_main() -> int:
        seen["called"] = True
        return 17

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main() == 17
    assert seen["called"] is True


def test_jarvis_strategy_supervisor_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.jarvis_strategy_supervisor")
    script_module = importlib.import_module("eta_engine.scripts.jarvis_strategy_supervisor")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 41

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--once"]) == 41
    assert seen["argv"] == ["--once"]


def test_weekly_sharpe_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.weekly_sharpe_check")
    script_module = importlib.import_module("eta_engine.scripts.weekly_sharpe_check")
    seen: dict[str, bool] = {"called": False}

    def _fake_main() -> int:
        seen["called"] = True
        return 53

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main() == 53
    assert seen["called"] is True


def test_schedule_weekly_review_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.schedule_weekly_review")
    script_module = importlib.import_module("eta_engine.scripts.schedule_weekly_review")
    seen: dict[str, bool] = {"called": False}

    def _fake_main() -> int:
        seen["called"] = True
        return 57

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main() == 57
    assert seen["called"] is True


def test_live_tiny_preflight_dryrun_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.live_tiny_preflight_dryrun")
    script_module = importlib.import_module("eta_engine.scripts.live_tiny_preflight_dryrun")
    seen: dict[str, bool] = {"called": False}

    def _fake_main() -> int:
        seen["called"] = True
        return 67

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main() == 67
    assert seen["called"] is True


def test_go_trigger_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.go_trigger")
    script_module = importlib.import_module("eta_engine.scripts.go_trigger")
    seen: dict[str, bool] = {"called": False}

    def _fake_main() -> int:
        seen["called"] = True
        return 68

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main() == 68
    assert seen["called"] is True


def test_backup_state_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds._backup_state")
    script_module = importlib.import_module("eta_engine.scripts._backup_state")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 69

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--no-snapshot"]) == 69
    assert seen["argv"] == ["--no-snapshot"]


def test_sharpe_drift_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds._sharpe_drift")
    script_module = importlib.import_module("eta_engine.scripts._sharpe_drift")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 70

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--no-update"]) == 70
    assert seen["argv"] == ["--no-update"]


def test_repo_health_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds._repo_health")
    script_module = importlib.import_module("eta_engine.scripts._repo_health")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 72

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--skip-hook-check"]) == 72
    assert seen["argv"] == ["--skip-hook-check"]


def test_announce_data_library_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.announce_data_library")
    script_module = importlib.import_module("eta_engine.scripts.announce_data_library")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 59

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--dry-run"]) == 59
    assert seen["argv"] == ["--dry-run"]


def test_paper_live_launch_check_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.paper_live_launch_check")
    script_module = importlib.import_module("eta_engine.scripts.paper_live_launch_check")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 61

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--json"]) == 61
    assert seen["argv"] == ["--json"]


def test_decision_journal_smoke_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.decision_journal_smoke")
    script_module = importlib.import_module("eta_engine.scripts.decision_journal_smoke")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 71

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--json"]) == 71
    assert seen["argv"] == ["--json"]


def test_runtime_log_smoke_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.runtime_log_smoke")
    script_module = importlib.import_module("eta_engine.scripts.runtime_log_smoke")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 73

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--json"]) == 73
    assert seen["argv"] == ["--json"]


def test_drift_check_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.drift_check")
    script_module = importlib.import_module("eta_engine.scripts.drift_check")
    seen: dict[str, bool] = {"called": False}

    def _fake_main() -> int:
        seen["called"] = True
        return 79

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main() == 79
    assert seen["called"] is True


def test_drift_check_all_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.drift_check_all")
    script_module = importlib.import_module("eta_engine.scripts.drift_check_all")
    seen: dict[str, bool] = {"called": False}

    def _fake_main() -> int:
        seen["called"] = True
        return 81

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main() == 81
    assert seen["called"] is True


def test_drift_watchdog_smoke_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.drift_watchdog_smoke")
    script_module = importlib.import_module("eta_engine.scripts.drift_watchdog_smoke")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 83

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--json"]) == 83
    assert seen["argv"] == ["--json"]


def test_data_health_check_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.data_health_check")
    script_module = importlib.import_module("eta_engine.scripts.data_health_check")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 89

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--json"]) == 89
    assert seen["argv"] == ["--json"]


def test_sage_health_check_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.sage_health_check")
    script_module = importlib.import_module("eta_engine.scripts.sage_health_check")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 97

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--json-out", "state.json"]) == 97
    assert seen["argv"] == ["--json-out", "state.json"]


def test_venue_readiness_check_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.venue_readiness_check")
    script_module = importlib.import_module("eta_engine.scripts.venue_readiness_check")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 101

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--json"]) == 101
    assert seen["argv"] == ["--json"]


def test_paper_trade_preflight_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.paper_trade_preflight")
    script_module = importlib.import_module("eta_engine.scripts.paper_trade_preflight")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 103

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--json"]) == 103
    assert seen["argv"] == ["--json"]


def test_daily_premarket_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.daily_premarket")
    script_module = importlib.import_module("eta_engine.scripts.daily_premarket")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 107

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--inputs-path", "docs/premarket_inputs.json"]) == 107
    assert seen["argv"] == ["--inputs-path", "docs/premarket_inputs.json"]


def test_paper_run_harness_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.paper_run_harness")
    script_module = importlib.import_module("eta_engine.scripts.paper_run_harness")
    seen: dict[str, bool] = {"called": False}

    def _fake_main() -> int:
        seen["called"] = True
        return 109

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main() == 109
    assert seen["called"] is True


def test_btc_live_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.btc_live")
    script_module = importlib.import_module("eta_engine.scripts.btc_live")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 113

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--live", "--bars", "120"]) == 113
    assert seen["argv"] == ["--live", "--bars", "120"]


def test_btc_broker_fleet_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.btc_broker_fleet")
    script_module = importlib.import_module("eta_engine.scripts.btc_broker_fleet")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 127

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--once", "--out-dir", "docs/btc_live/broker_fleet"]) == 127
    assert seen["argv"] == ["--once", "--out-dir", "docs/btc_live/broker_fleet"]


def test_btc_paper_trade_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.btc_paper_trade")
    script_module = importlib.import_module("eta_engine.scripts.btc_paper_trade")
    seen: dict[str, bool] = {"called": False}

    def _fake_main() -> None:
        seen["called"] = True

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main() is None
    assert seen["called"] is True


def test_chaos_drill_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.chaos_drill")
    script_module = importlib.import_module("eta_engine.scripts.chaos_drill")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 131

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["all", "--json"]) == 131
    assert seen["argv"] == ["all", "--json"]


def test_jarvis_status_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.jarvis_status")
    script_module = importlib.import_module("eta_engine.scripts.jarvis_status")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 137

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--json"]) == 137
    assert seen["argv"] == ["--json"]


def test_bot_strategy_readiness_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.bot_strategy_readiness")
    script_module = importlib.import_module("eta_engine.scripts.bot_strategy_readiness")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 139

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--snapshot", "--no-write", "--json"]) == 139
    assert seen["argv"] == ["--snapshot", "--no-write", "--json"]


def test_jarvis_live_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.jarvis_live")
    script_module = importlib.import_module("eta_engine.scripts.jarvis_live")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 149

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--interval", "5", "--max-ticks", "1"]) == 149
    assert seen["argv"] == ["--interval", "5", "--max-ticks", "1"]


def test_weekly_review_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.weekly_review")
    script_module = importlib.import_module("eta_engine.scripts.weekly_review")
    seen: dict[str, bool] = {"called": False}

    def _fake_main() -> int:
        seen["called"] = True
        return 151

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main() == 151
    assert seen["called"] is True


def test_jarvis_dashboard_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.jarvis_dashboard")
    script_module = importlib.import_module("eta_engine.scripts.jarvis_dashboard")
    seen: dict[str, bool] = {"called": False}

    def _fake_main() -> int:
        seen["called"] = True
        return 157

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main() == 157
    assert seen["called"] is True


def test_jarvis_sage_main_delegates_to_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_module = importlib.import_module("eta_engine.feeds.jarvis_sage")
    script_module = importlib.import_module("eta_engine.scripts.jarvis_sage")
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 163

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(["--list-schools"]) == 163
    assert seen["argv"] == ["--list-schools"]


@pytest.mark.parametrize(
    ("feed_name", "script_name", "argv", "expected"),
    [
        (
            "eta_engine.feeds.bandit_promotion_check",
            "eta_engine.scripts.bandit_promotion_check",
            ["--dry-run"],
            59,
        ),
        (
            "eta_engine.feeds.export_to_notion",
            "eta_engine.scripts.export_to_notion",
            ["--dry-run"],
            61,
        ),
        (
            "eta_engine.feeds.run_critique_nightly",
            "eta_engine.scripts.run_critique_nightly",
            ["--dry-run"],
            67,
        ),
        (
            "eta_engine.feeds.jarvis_ask",
            "eta_engine.scripts.jarvis_ask",
            ["reasons", "--hours", "1"],
            71,
        ),
        (
            "eta_engine.feeds.run_anomaly_scan",
            "eta_engine.scripts.run_anomaly_scan",
            ["--dry-run"],
            73,
        ),
        (
            "eta_engine.feeds.run_calibration_fit",
            "eta_engine.scripts.run_calibration_fit",
            ["--dry-run"],
            79,
        ),
        (
            "eta_engine.feeds.score_policy_candidate",
            "eta_engine.scripts.score_policy_candidate",
            ["--json"],
            83,
        ),
        (
            "eta_engine.feeds.refresh_correlation_matrix",
            "eta_engine.scripts.refresh_correlation_matrix",
            ["--dry-run"],
            89,
        ),
        (
            "eta_engine.feeds.retrain_models",
            "eta_engine.scripts.retrain_models",
            ["--dry-run"],
            97,
        ),
        (
            "eta_engine.feeds._trade_journal_reconcile",
            "eta_engine.scripts._trade_journal_reconcile",
            ["--hours", "12"],
            101,
        ),
    ],
)
def test_daily_review_feed_mains_delegate_to_script_main(
    monkeypatch: pytest.MonkeyPatch,
    feed_name: str,
    script_name: str,
    argv: list[str],
    expected: int,
) -> None:
    feed_module = importlib.import_module(feed_name)
    script_module = importlib.import_module(script_name)
    seen: dict[str, object] = {}

    def _fake_main(argv_in: list[str] | None = None) -> int:
        seen["argv"] = argv_in
        return expected

    monkeypatch.setattr(script_module, "main", _fake_main)

    assert feed_module.main(argv) == expected
    assert seen["argv"] == argv


def test_runtime_readiness_feed_wrapper_points_to_canonical_script() -> None:
    feed_text = _read_text("feeds/runtime_readiness_check.ps1")
    script_text = _read_text("scripts/runtime_readiness_check.ps1")

    assert "Compatibility wrapper" in feed_text
    assert 'Join-Path $PSScriptRoot "..\\scripts\\runtime_readiness_check.ps1"' in feed_text
    assert "& $canonicalScript @forwardArgs" in feed_text
    assert "$critical_services = @(" not in feed_text
    assert "Get-ScheduledTask" not in feed_text
    assert "$critical_services = @(" in script_text


def test_reenable_eta_tasks_feed_wrapper_points_to_canonical_script() -> None:
    feed_text = _read_text("feeds/reenable_eta_tasks.ps1")
    script_text = _read_text("scripts/reenable_eta_tasks.ps1")

    assert "Compatibility wrapper" in feed_text
    assert 'Join-Path $PSScriptRoot "..\\scripts\\reenable_eta_tasks.ps1"' in feed_text
    assert "& $canonicalScript @forwardArgs" in feed_text
    assert "$enable_tasks = @(" not in feed_text
    assert "Enable-ScheduledTask" not in feed_text
    assert "$enable_tasks = @(" in script_text


def test_overlapping_feed_powershell_entrypoints_are_wrapper_only() -> None:
    feed_dir = ETA_ENGINE_ROOT / "feeds"
    script_dir = ETA_ENGINE_ROOT / "scripts"
    feed_names = {path.name for path in feed_dir.glob("*.ps1")}
    script_names = {path.name for path in script_dir.glob("*.ps1")}
    overlapping = sorted(feed_names & script_names)

    assert overlapping == [
        "reenable_eta_tasks.ps1",
        "runtime_readiness_check.ps1",
    ]
    for name in overlapping:
        text = _read_text(f"feeds/{name}")
        assert "Compatibility wrapper" in text
        assert "& $canonicalScript @forwardArgs" in text


def test_build_script_shim_deduplicates_explicit_all(monkeypatch: pytest.MonkeyPatch) -> None:
    script_module = types.ModuleType("eta_engine.tests._fake_script_shim_module")
    script_module.__all__ = ["alpha", "alpha", "beta"]
    script_module.alpha = object()
    script_module.beta = object()
    monkeypatch.setitem(sys.modules, script_module.__name__, script_module)

    _, public_names, __getattr__, __dir__ = build_script_shim(
        "eta_engine.feeds.fake_script_shim_module",
        script_module.__name__,
    )

    assert public_names == ["alpha", "beta"]
    assert __getattr__("alpha") is script_module.alpha
    assert "alpha" in __dir__()
