from __future__ import annotations

import json
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from eta_engine.scripts import diamond_wave25_status as mod  # noqa: E402


def test_retune_advisory_summary_reads_public_caches(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)
    health_dir = tmp_path / "var" / "eta_engine" / "state" / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    (health_dir / "public_diamond_retune_truth_latest.json").write_text(
        json.dumps(
            {
                "surface": {
                    "normalized": {
                        "focus_bot": "mnq_futures_sage",
                        "focus_issue": "broker_pnl_negative",
                        "focus_state": "COLLECT_MORE_SAMPLE",
                        "focus_closed_trade_count": 141,
                        "focus_total_realized_pnl": -1939.75,
                        "focus_profit_factor": 0.3951,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (health_dir / "public_broker_close_truth_latest.json").write_text(
        json.dumps(
            {
                "surface": {
                    "normalized": {
                        "focus_closed_trade_count": 141,
                        "focus_total_realized_pnl": -1939.75,
                        "focus_profit_factor": 0.3951,
                        "broker_mtd_pnl": 20087.0,
                        "broker_snapshot_source": "ibkr_probe_cache",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (health_dir / "diamond_retune_truth_check_latest.json").write_text(
        json.dumps(
            {
                "diagnosis": "public_local_focus_mismatch",
                "warnings": ["Public retune focus and local canonical retune receipt disagree."],
                "action_items": ["Refresh the canonical trade_closes writer before trusting local-only counts."],
            }
        ),
        encoding="utf-8",
    )

    advisory = mod._retune_advisory_summary()

    assert advisory["available"] is True
    assert advisory["focus_bot"] == "mnq_futures_sage"
    assert advisory["focus_closed_trade_count"] == 141
    assert advisory["focus_total_realized_pnl"] == -1939.75
    assert advisory["broker_mtd_pnl"] == 20087.0
    assert advisory["diagnosis"] == "public_local_focus_mismatch"


def test_build_status_report_includes_retune_advisory(monkeypatch) -> None:
    monkeypatch.setattr(mod, "_bots_to_check", lambda: ["bot_a"])
    monkeypatch.setattr(
        mod,
        "_per_bot_status",
        lambda bot_id: {
            "bot_id": bot_id,
            "lifecycle": "EVAL_PAPER",
            "n_live_7d": 0,
            "n_live_24h": 0,
            "n_paper_7d": 2,
            "n_paper_24h": 1,
            "n_shadow_7d": 3,
            "last_live_ts": None,
            "seconds_since_last_live_trade": None,
        },
    )
    monkeypatch.setattr(
        mod,
        "_alert_channel_status",
        lambda: {
            "telegram_configured": True,
            "discord_configured": False,
            "generic_webhook_configured": False,
        },
    )
    monkeypatch.setattr(
        mod,
        "_ledger_truth_summary",
        lambda: {
            "unfiltered": {"live": 1},
            "production_strict_count": 1,
            "operator_inclusive_count": 1,
            "production_filter": ["live"],
            "operator_filter": ["live"],
        },
    )
    monkeypatch.setattr(
        mod,
        "_retune_advisory_summary",
        lambda: {
            "available": True,
            "focus_bot": "mnq_futures_sage",
            "focus_issue": "broker_pnl_negative",
            "focus_state": "COLLECT_MORE_SAMPLE",
            "focus_closed_trade_count": 141,
            "focus_total_realized_pnl": -1939.75,
            "focus_profit_factor": 0.3951,
            "broker_mtd_pnl": 20087.0,
            "diagnosis": "public_local_focus_mismatch",
        },
    )

    report = mod.build_status_report()

    assert report["n_bots_total"] == 1
    assert report["retune_advisory"]["focus_bot"] == "mnq_futures_sage"
    assert report["retune_advisory"]["broker_mtd_pnl"] == 20087.0


def test_print_table_renders_retune_advisory(capsys) -> None:
    mod._print_table(
        {
            "ts": "2026-05-15T20:10:00+00:00",
            "n_bots_total": 1,
            "lifecycle_breakdown": {
                "EVAL_LIVE": 0,
                "EVAL_PAPER": 1,
                "FUNDED_LIVE": 0,
                "RETIRED": 0,
            },
            "totals_24h": {"live_trades": 0, "paper_trades": 1},
            "totals_7d": {"shadow_signals": 3},
            "alert_channels": {
                "telegram_configured": True,
                "discord_configured": False,
                "generic_webhook_configured": False,
            },
            "ledger_pollution_snapshot": {
                "production_strict_count": 1,
                "operator_inclusive_count": 1,
                "production_filter": ["live"],
                "operator_filter": ["live"],
                "unfiltered": {"live": 1},
            },
            "retune_advisory": {
                "available": True,
                "focus_bot": "mnq_futures_sage",
                "focus_issue": "broker_pnl_negative",
                "focus_state": "COLLECT_MORE_SAMPLE",
                "focus_closed_trade_count": 141,
                "focus_total_realized_pnl": -1939.75,
                "focus_profit_factor": 0.3951,
                "broker_mtd_pnl": 20087.0,
                "diagnosis": "public_local_focus_mismatch",
            },
            "per_bot": [
                {
                    "bot_id": "bot_a",
                    "lifecycle": "EVAL_PAPER",
                    "n_live_24h": 0,
                    "n_paper_24h": 1,
                    "n_shadow_7d": 3,
                    "seconds_since_last_live_trade": None,
                }
            ],
        }
    )

    out = capsys.readouterr().out
    assert "retune advisory: mnq_futures_sage" in out
    assert "drift=public_local_focus_mismatch" in out


def test_print_table_renders_active_experiment(capsys) -> None:
    mod._print_table(
        {
            "ts": "2026-05-15T00:00:00+00:00",
            "n_bots_total": 1,
            "lifecycle_breakdown": {
                "EVAL_LIVE": 0,
                "EVAL_PAPER": 1,
                "FUNDED_LIVE": 0,
                "RETIRED": 0,
            },
            "totals_24h": {"live_trades": 0, "paper_trades": 1},
            "totals_7d": {"shadow_signals": 3},
            "alert_channels": {
                "telegram_configured": True,
                "discord_configured": False,
                "generic_webhook_configured": False,
            },
            "ledger_pollution_snapshot": {
                "production_strict_count": 1,
                "operator_inclusive_count": 1,
                "production_filter": ["live"],
                "operator_filter": ["live"],
                "unfiltered": {"live": 1},
            },
            "retune_advisory": {
                "available": True,
                "focus_bot": "mnq_futures_sage",
                "focus_issue": "broker_pnl_negative",
                "focus_state": "COLLECT_MORE_SAMPLE",
                "focus_closed_trade_count": 141,
                "focus_total_realized_pnl": -1939.75,
                "focus_profit_factor": 0.3951,
                "broker_mtd_pnl": 20087.0,
                "active_experiment": {
                    "experiment_id": "partial_profit_disabled",
                    "started_at": "2026-05-16T01:44:06+00:00",
                    "partial_profit_enabled": False,
                    "post_change_closed_trade_count": 2,
                    "post_change_total_realized_pnl": 40.0,
                    "post_change_profit_factor": 1.5,
                },
            },
            "per_bot": [
                {
                    "bot_id": "bot_a",
                    "lifecycle": "EVAL_PAPER",
                    "n_live_24h": 0,
                    "n_paper_24h": 1,
                    "n_shadow_7d": 3,
                    "seconds_since_last_live_trade": None,
                }
            ],
        }
    )

    out = capsys.readouterr().out
    assert "post-fix experiment: partial_profit_disabled since 2026-05-16T01:44:06+00:00" in out
    assert "partial_profit_enabled=False closes=2 pnl=$+40.00 pf=1.50" in out
