from __future__ import annotations

import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from eta_engine.scripts import monday_first_light_check as mod  # noqa: E402


def test_format_telegram_body_includes_retune_advisory() -> None:
    body = mod._format_telegram_body(
        "NO_GO",
        "NO_GO: lifecycle",
        [mod.Check("lifecycle", "NO_GO", "paper only")],
        retune_advisory={
            "available": True,
            "focus_bot": "mnq_futures_sage",
            "focus_issue": "broker_pnl_negative",
            "focus_state": "COLLECT_MORE_SAMPLE",
            "focus_closed_trade_count": 141,
            "focus_total_realized_pnl": -1939.75,
            "focus_profit_factor": 0.3951,
            "diagnosis": "public_local_focus_mismatch",
            "preferred_warning": "Public retune focus and local canonical retune receipt disagree.",
        },
    )

    assert "Retune truth: mnq_futures_sage COLLECT_MORE_SAMPLE issue=broker_pnl_negative" in body
    assert "Broker proof: closes=141 pnl=$-1939.75 pf=0.40" in body
    assert "Local drift: public_local_focus_mismatch" in body


def test_format_telegram_body_includes_active_experiment() -> None:
    body = mod._format_telegram_body(
        "NO_GO",
        "NO_GO: lifecycle",
        [mod.Check("lifecycle", "NO_GO", "paper only")],
        retune_advisory={
            "available": True,
            "focus_bot": "mnq_futures_sage",
            "focus_issue": "broker_pnl_negative",
            "focus_state": "COLLECT_MORE_SAMPLE",
            "focus_closed_trade_count": 141,
            "focus_total_realized_pnl": -1939.75,
            "focus_profit_factor": 0.3951,
            "active_experiment": {
                "experiment_id": "partial_profit_disabled",
                "started_at": "2026-05-16T01:44:06+00:00",
                "partial_profit_enabled": False,
                "post_change_closed_trade_count": 2,
                "post_change_total_realized_pnl": 40.0,
                "post_change_profit_factor": 1.5,
            },
        },
    )

    assert "Post-fix experiment: partial_profit_disabled since 2026-05-16T01:44:06+00:00" in body
    assert "partial_profit_enabled=False closes=2 pnl=$+40.00 pf=1.50" in body


def test_retune_advisory_reads_public_caches(tmp_path, monkeypatch) -> None:
    health_dir = tmp_path / "var" / "eta_engine" / "state" / "health"
    monkeypatch.setattr(mod, "HEALTH_DIR", health_dir)
    health_dir.mkdir(parents=True, exist_ok=True)
    (health_dir / "public_diamond_retune_truth_latest.json").write_text(
        """
        {
          "surface": {
            "normalized": {
              "focus_bot": "mnq_futures_sage",
              "focus_issue": "broker_pnl_negative",
              "focus_state": "COLLECT_MORE_SAMPLE",
              "focus_closed_trade_count": 141,
              "focus_total_realized_pnl": -1939.75,
              "focus_profit_factor": 0.3951
            }
          }
        }
        """.strip(),
        encoding="utf-8",
    )
    (health_dir / "public_broker_close_truth_latest.json").write_text(
        """
        {
          "surface": {
            "normalized": {
              "focus_closed_trade_count": 141,
              "focus_total_realized_pnl": -1939.75,
              "focus_profit_factor": 0.3951,
              "broker_mtd_pnl": 18131.0,
              "broker_snapshot_source": "ibkr_probe_cache"
            }
          }
        }
        """.strip(),
        encoding="utf-8",
    )
    (health_dir / "diamond_retune_truth_check_latest.json").write_text(
        """
        {
          "diagnosis": "public_local_focus_mismatch",
          "warnings": ["Public retune focus and local canonical retune receipt disagree."],
          "action_items": ["Refresh the canonical trade_closes writer before trusting local-only counts."]
        }
        """.strip(),
        encoding="utf-8",
    )

    advisory = mod._retune_advisory()

    assert advisory["available"] is True
    assert advisory["focus_bot"] == "mnq_futures_sage"
    assert advisory["focus_closed_trade_count"] == 141
    assert advisory["broker_mtd_pnl"] == 18131.0
    assert advisory["diagnosis"] == "public_local_focus_mismatch"


def test_main_json_report_includes_retune_advisory(monkeypatch, capsys) -> None:
    monkeypatch.setattr(mod, "_check_supervisor_alive", lambda: mod.Check("supervisor", "GO", "ok"))
    monkeypatch.setattr(mod, "_check_drawdown_clear", lambda: mod.Check("drawdown_guard", "GO", "ok"))
    monkeypatch.setattr(mod, "_check_lifecycle_opt_in", lambda: mod.Check("lifecycle", "NO_GO", "paper only"))
    monkeypatch.setattr(mod, "_check_alert_channel", lambda: mod.Check("alert_channel", "GO", "ok"))
    monkeypatch.setattr(mod, "_check_recent_shadow_activity", lambda: mod.Check("gate_activity", "GO", "ok"))
    monkeypatch.setattr(
        mod,
        "_retune_advisory",
        lambda: {
            "available": True,
            "focus_bot": "mnq_futures_sage",
            "focus_issue": "broker_pnl_negative",
            "focus_state": "COLLECT_MORE_SAMPLE",
        },
    )

    rc = mod.main(["--json", "--no-push"])
    out = capsys.readouterr().out

    assert rc == 2
    assert '"focus_bot": "mnq_futures_sage"' in out
    assert '"retune_advisory"' in out
