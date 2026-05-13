"""Tests for the live-cutover preflight reporter.

Distinct from ``test_preflight.py`` (which covers the legacy
``eta_engine.scripts.preflight`` trading-engine boot gate). This module
covers ``eta_engine.brain.jarvis_v3.preflight`` — the brain-OS Go/No-Go
checker built for the 2026-05-15 capital cutover.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

# ---------------------------------------------------------------------------
# Fundamentals
# ---------------------------------------------------------------------------


def test_run_preflight_returns_report() -> None:
    """run_preflight() always returns a PreflightReport with the four fields."""
    from eta_engine.brain.jarvis_v3 import preflight

    report = preflight.run_preflight()
    assert isinstance(report, preflight.PreflightReport)
    assert report.verdict in ("READY", "NOT READY")
    assert report.n_pass + report.n_warn + report.n_fail == len(report.checks)


def test_verdict_is_ready_only_when_zero_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single FAIL flips verdict to NOT READY, no matter how many PASS."""
    from eta_engine.brain.jarvis_v3 import preflight

    def fake_fail() -> preflight.PreflightCheck:
        return preflight.PreflightCheck(name="workspace_writable", status="FAIL", detail="simulated")

    def fake_pass() -> preflight.PreflightCheck:
        return preflight.PreflightCheck(name="state_dir_writable", status="PASS", detail="ok")

    monkeypatch.setattr(preflight, "_ALL_CHECKS", (fake_fail, fake_pass))

    report = preflight.run_preflight()
    assert report.verdict == "NOT READY"
    assert report.n_fail == 1
    assert report.n_pass == 1


def test_check_does_not_propagate_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken check becomes WARN, doesn't crash the report."""
    from eta_engine.brain.jarvis_v3 import preflight

    def boom() -> preflight.PreflightCheck:
        raise RuntimeError("simulated check crash")

    monkeypatch.setattr(preflight, "_ALL_CHECKS", (boom,))
    report = preflight.run_preflight()
    assert len(report.checks) == 1
    assert report.checks[0].status == "WARN"
    assert "simulated check crash" in report.checks[0].detail


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def test_check_workspace_writable_passes_for_writable_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    monkeypatch.setattr(preflight, "_WORKSPACE", tmp_path)
    check = preflight.check_workspace_writable()
    assert check.status == "PASS"


def test_check_state_dir_writable_creates_missing_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    state_dir = tmp_path / "state"
    monkeypatch.setattr(preflight, "_STATE_ROOT", state_dir)
    check = preflight.check_state_dir_writable()
    assert check.status == "PASS"
    assert state_dir.exists()


def test_check_hermes_port_listening_fails_when_no_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    monkeypatch.setattr(preflight, "_port_listening", lambda *a, **kw: False)
    check = preflight.check_hermes_port_listening()
    assert check.status == "FAIL"


def test_check_hermes_port_listening_passes_when_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    monkeypatch.setattr(preflight, "_port_listening", lambda *a, **kw: True)
    check = preflight.check_hermes_port_listening()
    assert check.status == "PASS"


def test_check_status_server_health_passes_on_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    monkeypatch.setattr(preflight, "_port_listening", lambda *a, **kw: True)
    monkeypatch.setattr(preflight, "_http_health", lambda *a, **kw: (True, "HTTP 200"))
    check = preflight.check_status_server()
    assert check.status == "PASS"


def test_check_status_server_health_fails_on_bad_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    monkeypatch.setattr(preflight, "_port_listening", lambda *a, **kw: True)
    monkeypatch.setattr(preflight, "_http_health", lambda *a, **kw: (False, "HTTP 500"))
    check = preflight.check_status_server()
    assert check.status == "FAIL"


def test_check_trade_close_stream_fails_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    monkeypatch.setattr(preflight, "_TRADE_CLOSES_PATH", tmp_path / "missing1.jsonl")
    monkeypatch.setattr(preflight, "_LEGACY_TRADE_CLOSES_PATH", tmp_path / "missing2.jsonl")
    check = preflight.check_trade_close_stream_fresh()
    assert check.status == "FAIL"


def test_check_trade_close_stream_passes_when_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    f = tmp_path / "trade_closes.jsonl"
    f.write_text('{"ok": true}\n', encoding="utf-8")
    monkeypatch.setattr(preflight, "_TRADE_CLOSES_PATH", f)
    monkeypatch.setattr(preflight, "_LEGACY_TRADE_CLOSES_PATH", tmp_path / "missing.jsonl")
    check = preflight.check_trade_close_stream_fresh()
    assert check.status == "PASS"
    assert check.extras["freshest_age_hours"] < preflight.TRADE_FRESHNESS_MAX_HOURS


def test_check_memory_backup_warns_when_missing_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    monkeypatch.setattr(preflight, "_MEMORY_BACKUP_DIR", tmp_path / "no_backups")
    check = preflight.check_memory_backup_fresh()
    assert check.status == "WARN"


def test_check_memory_backup_passes_with_fresh_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    backup = tmp_path / "memory_2026.sqlite"
    backup.write_text("x", encoding="utf-8")
    monkeypatch.setattr(preflight, "_MEMORY_BACKUP_DIR", tmp_path)
    check = preflight.check_memory_backup_fresh()
    assert check.status == "PASS"


def test_check_kaizen_latest_warns_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    monkeypatch.setattr(preflight, "_KAIZEN_LATEST", tmp_path / "no_kaizen.json")
    check = preflight.check_kaizen_latest_fresh()
    assert check.status == "WARN"


def test_check_kill_switch_fails_when_engaged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    state = tmp_path / "hermes_state.json"
    state.write_text(json.dumps({"kill_all": True}), encoding="utf-8")
    monkeypatch.setattr(preflight, "_HERMES_STATE", state)
    check = preflight.check_kill_switch_disengaged()
    assert check.status == "FAIL"


def test_check_kill_switch_passes_when_disengaged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    state = tmp_path / "hermes_state.json"
    state.write_text(json.dumps({"kill_all": False}), encoding="utf-8")
    monkeypatch.setattr(preflight, "_HERMES_STATE", state)
    check = preflight.check_kill_switch_disengaged()
    assert check.status == "PASS"


def test_check_kill_switch_passes_when_state_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No hermes_state.json means kill switch was never engaged → PASS."""
    from eta_engine.brain.jarvis_v3 import preflight

    monkeypatch.setattr(preflight, "_HERMES_STATE", tmp_path / "missing.json")
    check = preflight.check_kill_switch_disengaged()
    assert check.status == "PASS"


def test_check_active_overrides_passes_when_few(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    f = tmp_path / "overrides.json"
    f.write_text(
        json.dumps({"size_modifiers": {"bot_a": 0.5, "bot_b": 0.7}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(preflight, "_OVERRIDES_PATH", f)
    check = preflight.check_active_overrides_reasonable()
    assert check.status == "PASS"
    assert check.extras["n_overrides"] == 2


def test_check_active_overrides_warns_when_over_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    big = {f"bot_{i}": 0.5 for i in range(20)}
    f = tmp_path / "overrides.json"
    f.write_text(json.dumps({"size_modifiers": big}), encoding="utf-8")
    monkeypatch.setattr(preflight, "_OVERRIDES_PATH", f)
    check = preflight.check_active_overrides_reasonable()
    assert check.status == "WARN"


def test_check_open_critical_anomalies_passes_when_no_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    monkeypatch.setattr(preflight, "_ANOMALY_HITS_LOG", tmp_path / "no_log.jsonl")
    check = preflight.check_no_open_critical_anomalies()
    assert check.status == "PASS"


def test_check_open_critical_anomalies_fails_on_recent_critical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight

    now = datetime.now(UTC)
    log = tmp_path / "hits.jsonl"
    with log.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "asof": (now - timedelta(hours=2)).isoformat(),
                    "pattern": "fleet_drawdown",
                    "severity": "critical",
                    "detail": "fleet down 4R",
                }
            )
            + "\n"
        )
    monkeypatch.setattr(preflight, "_ANOMALY_HITS_LOG", log)
    check = preflight.check_no_open_critical_anomalies()
    assert check.status == "FAIL"
    assert check.extras["n_critical"] == 1


def test_check_open_critical_anomalies_ignores_old_hits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critical hits older than 24h don't count — they're presumed resolved."""
    from eta_engine.brain.jarvis_v3 import preflight

    now = datetime.now(UTC)
    log = tmp_path / "hits.jsonl"
    with log.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "asof": (now - timedelta(hours=48)).isoformat(),
                    "pattern": "fleet_drawdown",
                    "severity": "critical",
                }
            )
            + "\n"
        )
    monkeypatch.setattr(preflight, "_ANOMALY_HITS_LOG", log)
    check = preflight.check_no_open_critical_anomalies()
    assert check.status == "PASS"


def test_check_open_critical_anomalies_ignores_warn_severity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WARN-severity hits are informational, not preflight blockers."""
    from eta_engine.brain.jarvis_v3 import preflight

    now = datetime.now(UTC)
    log = tmp_path / "hits.jsonl"
    with log.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "asof": (now - timedelta(hours=1)).isoformat(),
                    "pattern": "loss_streak",
                    "severity": "warn",
                }
            )
            + "\n"
        )
    monkeypatch.setattr(preflight, "_ANOMALY_HITS_LOG", log)
    check = preflight.check_no_open_critical_anomalies()
    assert check.status == "PASS"


def test_check_telegram_inbound_warns_when_no_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No offset file AND no log → bot has not started yet."""
    from eta_engine.brain.jarvis_v3 import preflight

    monkeypatch.setattr(preflight, "_VAR_ROOT", tmp_path)
    check = preflight.check_telegram_inbound_running()
    assert check.status == "WARN"
    assert "may not have started" in check.detail or "no offset" in check.detail


def test_check_telegram_inbound_passes_with_recent_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recent inbound log → bot is alive."""
    from eta_engine.brain.jarvis_v3 import preflight

    log = tmp_path / "telegram_inbound.log"
    log.write_text("starting...\n", encoding="utf-8")
    monkeypatch.setattr(preflight, "_VAR_ROOT", tmp_path)
    check = preflight.check_telegram_inbound_running()
    assert check.status == "PASS"


def test_check_telegram_inbound_warns_when_log_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Log older than 12h → WARN even with offset file present."""
    from eta_engine.brain.jarvis_v3 import preflight

    log = tmp_path / "telegram_inbound.log"
    log.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(preflight, "_VAR_ROOT", tmp_path)
    monkeypatch.setattr(preflight, "_file_age_hours", lambda p: 24.0)
    check = preflight.check_telegram_inbound_running()
    assert check.status == "WARN"


def test_check_prop_firm_accounts_passes_when_all_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All accounts at severity=ok → PASS."""
    from eta_engine.brain.jarvis_v3 import preflight
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    fake_snap = g.AccountSnapshot(
        rules=g.REGISTRY["blusky-50K-launch"],
        state=g.account_state_from_trades("blusky-50K-launch", trade_closes_path=Path("/no_such_file.jsonl")),
        daily_loss_remaining=1_500.0,
        daily_loss_pct_used=0.0,
        trailing_dd_remaining=2_000.0,
        profit_to_target=3_000.0,
        pct_to_target=0.0,
        severity="ok",
        blockers=[],
    )
    monkeypatch.setattr(g, "aggregate_status", lambda **kw: [fake_snap])
    check = preflight.check_prop_firm_accounts_healthy()
    assert check.status == "PASS"


def test_check_prop_firm_accounts_warns_when_one_at_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account at severity=warn → WARN (not FAIL, but heads-up)."""
    from eta_engine.brain.jarvis_v3 import preflight
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    warn_snap = g.AccountSnapshot(
        rules=g.REGISTRY["blusky-50K-launch"],
        state=g.account_state_from_trades("blusky-50K-launch", trade_closes_path=Path("/no_such_file.jsonl")),
        daily_loss_remaining=300.0,
        daily_loss_pct_used=0.80,
        trailing_dd_remaining=2_000.0,
        profit_to_target=3_000.0,
        pct_to_target=0.0,
        severity="warn",
        blockers=[],
    )
    monkeypatch.setattr(g, "aggregate_status", lambda **kw: [warn_snap])
    check = preflight.check_prop_firm_accounts_healthy()
    assert check.status == "WARN"


def test_check_prop_firm_accounts_fails_on_blown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blown account is a hard FAIL — operator must investigate."""
    from eta_engine.brain.jarvis_v3 import preflight
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    blown_snap = g.AccountSnapshot(
        rules=g.REGISTRY["blusky-50K-launch"],
        state=g.account_state_from_trades("blusky-50K-launch", trade_closes_path=Path("/no_such_file.jsonl")),
        daily_loss_remaining=0.0,
        daily_loss_pct_used=1.10,
        trailing_dd_remaining=0.0,
        profit_to_target=3_000.0,
        pct_to_target=0.0,
        severity="blown",
        blockers=["daily_loss_blown"],
    )
    monkeypatch.setattr(g, "aggregate_status", lambda **kw: [blown_snap])
    check = preflight.check_prop_firm_accounts_healthy()
    assert check.status == "FAIL"
    assert "blusky-50K-launch" in check.detail


def test_check_prop_firm_accounts_fails_on_critical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Severity=critical also flips preflight to FAIL — too risky for live."""
    from eta_engine.brain.jarvis_v3 import preflight
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    crit_snap = g.AccountSnapshot(
        rules=g.REGISTRY["blusky-50K-launch"],
        state=g.account_state_from_trades("blusky-50K-launch", trade_closes_path=Path("/no_such_file.jsonl")),
        daily_loss_remaining=100.0,
        daily_loss_pct_used=0.93,
        trailing_dd_remaining=200.0,
        profit_to_target=3_000.0,
        pct_to_target=0.0,
        severity="critical",
        blockers=["daily_loss_93%"],
    )
    monkeypatch.setattr(g, "aggregate_status", lambda **kw: [crit_snap])
    check = preflight.check_prop_firm_accounts_healthy()
    assert check.status == "FAIL"


def test_schtasks_parser_converts_local_time_to_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION: schtasks emits LOCAL time; parser must convert to UTC.

    Earlier bug: parser called .replace(tzinfo=UTC) which assumed the
    timestamp was already UTC. On an EDT machine that's a 4-hour error,
    making fresh crons appear ~247min stale.
    """
    from eta_engine.brain.jarvis_v3 import preflight

    # Fake schtasks /Query LIST output with a local 7:26:01 PM timestamp
    fake_stdout = (
        "TaskName:                             \\ETA-Anomaly-Pulse\n"
        "Status:                               Ready\n"
        "Last Result:                          0\n"
        "Last Run Time:                        5/12/2026 7:26:01 PM\n"
        "Next Run Time:                        5/12/2026 7:41:00 PM\n"
    )

    class FakeRun:
        returncode = 0
        stdout = fake_stdout

    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *a, **kw: FakeRun(),
    )

    last_result, last_run = preflight._schtasks_last_run("ETA-Anomaly-Pulse")
    assert last_result == 0
    assert last_run is not None
    # The parsed datetime must be tz-aware in UTC, regardless of the
    # local system's timezone. The naive 7:26:01 PM is local; once
    # converted to UTC, the difference between local and UTC tells us
    # the parser handled tz correctly.
    assert last_run.tzinfo is not None
    # Sanity: it must be a real datetime in 2026
    assert last_run.year == 2026
    assert last_run.month == 5
    assert last_run.day == 12


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_exits_zero_when_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import preflight as preflight_mod
    from eta_engine.scripts import preflight_check

    fake = preflight_mod.PreflightReport(
        asof="2026-05-12T23:00:00+00:00",
        verdict="READY",
        n_pass=12,
        n_warn=0,
        n_fail=0,
        checks=[],
    )
    monkeypatch.setattr(preflight_check.preflight, "run_preflight", lambda: fake)
    rc = preflight_check.main(["--silent"])
    assert rc == 0


def test_cli_exits_one_when_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import preflight as preflight_mod
    from eta_engine.scripts import preflight_check

    fake = preflight_mod.PreflightReport(
        asof="2026-05-12T23:00:00+00:00",
        verdict="NOT READY",
        n_pass=10,
        n_warn=1,
        n_fail=1,
        checks=[],
    )
    monkeypatch.setattr(preflight_check.preflight, "run_preflight", lambda: fake)
    rc = preflight_check.main(["--silent"])
    assert rc == 1


def test_cli_exits_two_when_preflight_crashes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from eta_engine.scripts import preflight_check

    def boom() -> Any:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(preflight_check.preflight, "run_preflight", boom)
    rc = preflight_check.main([])
    assert rc == 2


def test_cli_json_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight as preflight_mod
    from eta_engine.scripts import preflight_check

    fake = preflight_mod.PreflightReport(
        asof="2026-05-12T23:00:00+00:00",
        verdict="READY",
        n_pass=1,
        n_warn=0,
        n_fail=0,
        checks=[
            preflight_mod.PreflightCheck(name="x", status="PASS", detail="ok"),
        ],
    )
    monkeypatch.setattr(preflight_check.preflight, "run_preflight", lambda: fake)
    rc = preflight_check.main(["--json"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["verdict"] == "READY"
    assert payload["checks"][0]["name"] == "x"
