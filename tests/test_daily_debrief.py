"""Tests for the end-of-day debrief module."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

# ---------------------------------------------------------------------------
# build_debrief — always returns a dict
# ---------------------------------------------------------------------------


def test_build_debrief_returns_envelope_shape() -> None:
    """Every run yields {asof, markdown, sections[6]} — never raises."""
    from eta_engine.scripts import daily_debrief

    envelope = daily_debrief.build_debrief()
    assert "asof" in envelope
    assert "markdown" in envelope
    assert "sections" in envelope
    assert len(envelope["sections"]) == 6
    # All sections must have title + body_md
    for s in envelope["sections"]:
        assert "title" in s
        assert "body_md" in s


def test_build_debrief_uses_markdown_header() -> None:
    """Body opens with the 'Daily Debrief' header."""
    from eta_engine.scripts import daily_debrief

    envelope = daily_debrief.build_debrief()
    assert "Daily Debrief" in envelope["markdown"]


def test_build_debrief_isolates_section_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken section becomes '(section crashed: ...)' rather than killing the build."""
    from eta_engine.scripts import daily_debrief

    def boom() -> Any:
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(daily_debrief, "section_pnl_today", boom)
    envelope = daily_debrief.build_debrief()
    assert len(envelope["sections"]) == 6
    crashed_section = envelope["sections"][0]
    assert "simulated crash" in crashed_section["body_md"]


# ---------------------------------------------------------------------------
# section_pnl_today
# ---------------------------------------------------------------------------


def test_section_pnl_today_with_data(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import pnl_summary
    from eta_engine.scripts import daily_debrief

    monkeypatch.setattr(
        pnl_summary,
        "multi_window_summary",
        lambda: {
            "today": {
                "total_r": 3.5,
                "n_trades": 12,
                "n_wins": 8,
                "n_losses": 4,
                "win_rate": 0.6667,
            }
        },
    )
    title, body, raw = daily_debrief.section_pnl_today()
    assert "PnL today" in title
    assert "+3.50R" in body
    assert "12 trades" in body
    assert "66.7%" in body
    assert "W/L 8/4" in body


def test_section_pnl_today_handles_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import pnl_summary
    from eta_engine.scripts import daily_debrief

    monkeypatch.setattr(pnl_summary, "multi_window_summary", lambda: {})
    title, body, raw = daily_debrief.section_pnl_today()
    assert "+0.00R" in body
    assert "0 trades" in body


def test_section_pnl_today_uses_loss_emoji(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import pnl_summary
    from eta_engine.scripts import daily_debrief

    monkeypatch.setattr(
        pnl_summary,
        "multi_window_summary",
        lambda: {"today": {"total_r": -1.5}},
    )
    title, _body, _raw = daily_debrief.section_pnl_today()
    assert "📉" in title


# ---------------------------------------------------------------------------
# section_top_performers
# ---------------------------------------------------------------------------


def test_section_top_performers_lists_winners_and_losers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import pnl_summary
    from eta_engine.scripts import daily_debrief

    monkeypatch.setattr(
        pnl_summary,
        "multi_window_summary",
        lambda: {
            "today": {
                "top_performers": [
                    {"bot_id": "alpha", "total_r": 2.5, "n_trades": 5},
                    {"bot_id": "beta", "total_r": 1.2, "n_trades": 3},
                ],
                "worst_performers": [{"bot_id": "loser", "total_r": -1.8, "n_trades": 4}],
            }
        },
    )
    title, body, raw = daily_debrief.section_top_performers()
    assert "Top performers" in title
    assert "alpha" in body
    assert "+2.50R" in body
    assert "loser" in body
    assert "-1.80R" in body


def test_section_top_performers_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import pnl_summary
    from eta_engine.scripts import daily_debrief

    monkeypatch.setattr(pnl_summary, "multi_window_summary", lambda: {})
    title, body, _raw = daily_debrief.section_top_performers()
    assert "no winners" in body or "no losers" in body or "_" in body


# ---------------------------------------------------------------------------
# section_prop_firm_scorecard
# ---------------------------------------------------------------------------


def test_section_prop_firm_scorecard_lists_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g
    from eta_engine.scripts import daily_debrief

    fake_snap = g.AccountSnapshot(
        rules=g.REGISTRY["blusky-50K-launch"],
        state=g.AccountState(
            account_id="blusky-50K-launch",
            starting_balance=50_000.0,
            current_balance=50_500.0,
            peak_balance=50_500.0,
            day_pnl_usd=500.0,
            today_date="2026-05-12",
            n_trades_today=3,
            open_contracts=0,
        ),
        daily_loss_remaining=1_500.0,
        daily_loss_pct_used=0.0,
        trailing_dd_remaining=2_000.0,
        profit_to_target=2_500.0,
        pct_to_target=0.1667,
        severity="ok",
        blockers=[],
    )
    monkeypatch.setattr(g, "aggregate_status", lambda **kw: [fake_snap])
    title, body, raw = daily_debrief.section_prop_firm_scorecard()
    assert "blusky-50K-launch" in body
    assert "✅" in body
    assert "+500" in body  # day_pnl


def test_section_prop_firm_scorecard_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g
    from eta_engine.scripts import daily_debrief

    monkeypatch.setattr(g, "aggregate_status", lambda **kw: [])
    title, body, _raw = daily_debrief.section_prop_firm_scorecard()
    assert "none registered" in body


# ---------------------------------------------------------------------------
# section_notable_events
# ---------------------------------------------------------------------------


def test_section_notable_events_groups_by_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import anomaly_watcher
    from eta_engine.scripts import daily_debrief

    fake_hits = [
        {"pattern": "loss_streak", "bot_id": "a", "severity": "warn", "detail": "x"},
        {"pattern": "loss_streak", "bot_id": "b", "severity": "warn", "detail": "y"},
        {"pattern": "win_streak", "bot_id": "c", "severity": "info", "detail": "z"},
    ]
    monkeypatch.setattr(anomaly_watcher, "recent_hits", lambda since_hours=24: fake_hits)
    title, body, raw = daily_debrief.section_notable_events()
    assert "loss_streak" in body
    assert "×2" in body
    assert "×1" in body


def test_section_notable_events_surfaces_criticals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import anomaly_watcher
    from eta_engine.scripts import daily_debrief

    fake_hits = [
        {
            "pattern": "fleet_drawdown",
            "bot_id": "__fleet__",
            "severity": "critical",
            "detail": "Fleet down -5.2R",
        }
    ]
    monkeypatch.setattr(anomaly_watcher, "recent_hits", lambda since_hours=24: fake_hits)
    title, body, raw = daily_debrief.section_notable_events()
    assert "CRITICAL hits" in body
    assert "Fleet down" in body
    assert raw["n_critical"] == 1


def test_section_notable_events_quiet_day(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.brain.jarvis_v3 import anomaly_watcher
    from eta_engine.scripts import daily_debrief

    monkeypatch.setattr(anomaly_watcher, "recent_hits", lambda since_hours=24: [])
    title, body, _raw = daily_debrief.section_notable_events()
    assert "quiet" in body.lower()


# ---------------------------------------------------------------------------
# section_override_activity
# ---------------------------------------------------------------------------


def test_section_override_activity_counts_overrides_today(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import daily_debrief

    audit_log = tmp_path / "hermes_actions.jsonl"
    recent_ts = datetime.now(UTC).isoformat()
    audit_log.write_text(
        json.dumps({"ts": recent_ts, "tool": "jarvis_set_size_modifier"})
        + "\n"
        + json.dumps({"ts": recent_ts, "tool": "jarvis_set_size_modifier"})
        + "\n"
        + json.dumps({"ts": recent_ts, "tool": "jarvis_pin_school_weight"})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(daily_debrief, "_HERMES_ACTIONS_LOG_PATH", audit_log)
    title, body, raw = daily_debrief.section_override_activity()
    assert "3 overrides today" in body
    assert raw["n"] == 3


def test_section_override_activity_ignores_old_audit_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import daily_debrief

    audit_log = tmp_path / "hermes_actions.jsonl"
    old_ts = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    audit_log.write_text(
        json.dumps({"ts": old_ts, "tool": "jarvis_set_size_modifier"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(daily_debrief, "_HERMES_ACTIONS_LOG_PATH", audit_log)
    title, body, _raw = daily_debrief.section_override_activity()
    assert "no overrides applied" in body


def test_section_override_activity_missing_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import daily_debrief

    monkeypatch.setattr(daily_debrief, "_HERMES_ACTIONS_LOG_PATH", tmp_path / "missing.jsonl")
    title, body, _raw = daily_debrief.section_override_activity()
    assert "no audit log" in body


# ---------------------------------------------------------------------------
# section_tomorrow_outlook
# ---------------------------------------------------------------------------


def test_section_tomorrow_outlook_includes_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight
    from eta_engine.scripts import daily_debrief

    fake_report = preflight.PreflightReport(
        asof="2026-05-12T21:30:00+00:00",
        verdict="READY",
        n_pass=14,
        n_warn=0,
        n_fail=0,
        checks=[],
    )
    monkeypatch.setattr(preflight, "run_preflight", lambda: fake_report)
    title, body, raw = daily_debrief.section_tomorrow_outlook()
    assert "READY" in body
    assert raw.get("preflight_verdict") == "READY"


def test_section_tomorrow_outlook_lists_fail_blockers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.brain.jarvis_v3 import preflight
    from eta_engine.scripts import daily_debrief

    fail_report = preflight.PreflightReport(
        asof="2026-05-12T21:30:00+00:00",
        verdict="NOT READY",
        n_pass=12,
        n_warn=1,
        n_fail=1,
        checks=[
            preflight.PreflightCheck(
                name="prop_firm_accounts_healthy",
                status="FAIL",
                detail="blusky-50K-launch is critical",
            ),
            preflight.PreflightCheck(
                name="x",
                status="PASS",
                detail="ok",
            ),
        ],
    )
    monkeypatch.setattr(preflight, "run_preflight", lambda: fail_report)
    title, body, _raw = daily_debrief.section_tomorrow_outlook()
    assert "NOT READY" in body
    assert "prop_firm_accounts_healthy" in body


# ---------------------------------------------------------------------------
# send_debrief
# ---------------------------------------------------------------------------


def test_send_debrief_dry_run_does_not_call_telegram(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import daily_debrief

    monkeypatch.setattr(daily_debrief, "_DEBRIEF_LOG", tmp_path / "debrief.jsonl")

    sent: list[str] = []
    monkeypatch.setattr(
        "eta_engine.deploy.scripts.telegram_alerts.send_from_env",
        lambda text, priority="INFO": sent.append(text) or {"ok": True},
    )

    envelope = daily_debrief.send_debrief(dry_run=True)
    assert envelope["sent"] is False
    assert envelope["reason"] == "dry_run"
    assert sent == []
    assert (tmp_path / "debrief.jsonl").exists()


def test_send_debrief_real_send_calls_telegram(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import daily_debrief, telegram_inbound_bot

    monkeypatch.setattr(daily_debrief, "_DEBRIEF_LOG", tmp_path / "debrief.jsonl")
    monkeypatch.setattr(telegram_inbound_bot, "_SILENCE_PATH", tmp_path / "silence.json")

    sent: list[str] = []
    monkeypatch.setattr(
        "eta_engine.deploy.scripts.telegram_alerts.send_from_env",
        lambda text, priority="INFO": sent.append(text) or {"ok": True, "result": {"message_id": 42}},
    )

    envelope = daily_debrief.send_debrief(dry_run=False)
    assert envelope["sent"] is True
    assert len(sent) == 1
    assert "Daily Debrief" in sent[0]


def test_send_debrief_honors_silence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When operator has /silence active, the cron debrief still records but doesn't send."""
    from eta_engine.scripts import daily_debrief, telegram_inbound_bot

    monkeypatch.setattr(daily_debrief, "_DEBRIEF_LOG", tmp_path / "debrief.jsonl")
    monkeypatch.setattr(telegram_inbound_bot, "_SILENCE_PATH", tmp_path / "silence.json")
    telegram_inbound_bot.silence_for(60)

    sent: list[str] = []
    monkeypatch.setattr(
        "eta_engine.deploy.scripts.telegram_alerts.send_from_env",
        lambda text, priority="INFO": sent.append(text) or {"ok": True},
    )

    envelope = daily_debrief.send_debrief(dry_run=False)
    assert envelope["sent"] is False
    assert envelope["reason"] == "silenced_by_operator"
    assert sent == []
