from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from eta_engine.scripts import vps_failover_drill


def test_check_secrets_present_accepts_direct_ibkr_without_client_portal_fields(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "ETA_MODE=PAPER",
                "DEEPSEEK_API_KEY=test-key",
                "JARVIS_HOURLY_USD_BUDGET=0.5",
                "JARVIS_DAILY_USD_BUDGET=5.0",
                "IBKR_VENUE_TYPE=paper",
                "ETA_PAPER_LIVE_ORDER_ROUTE=direct_ibkr",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(vps_failover_drill, "ROOT", tmp_path)

    result = vps_failover_drill._check_secrets_present()

    assert result.severity == "green"
    assert result.details["paper_live_route"] == "direct_ibkr"
    assert result.details["required_missing"] == {}
    assert "ibkr_client_portal_sidecars" in result.details["recommended_groups"]


def test_check_secrets_present_requires_client_portal_fields_for_non_direct_route(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "ETA_MODE=PAPER",
                "DEEPSEEK_API_KEY=test-key",
                "JARVIS_HOURLY_USD_BUDGET=0.5",
                "JARVIS_DAILY_USD_BUDGET=5.0",
                "IBKR_VENUE_TYPE=paper",
                "ETA_PAPER_LIVE_ORDER_ROUTE=broker_router",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(vps_failover_drill, "ROOT", tmp_path)

    result = vps_failover_drill._check_secrets_present()

    assert result.severity == "amber"
    assert result.details["paper_live_route"] == "broker_router"
    assert result.details["required_missing"]["ibkr_primary"] == [
        "IBKR_CP_BASE_URL",
        "IBKR_ACCOUNT_ID",
        "IBKR_SYMBOL_CONID_MAP or IBKR_CONID_",
    ]


def test_state_freshness_exempts_static_strategy_baseline(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    baselines = tmp_path / "docs" / "strategy_baselines.json"
    journal = tmp_path / "var" / "eta_engine" / "state" / "decision_journal.jsonl"
    runtime = tmp_path / "logs" / "eta_engine" / "runtime_log.jsonl"
    drift = tmp_path / "var" / "eta_engine" / "state" / "drift_watchdog.jsonl"
    alerts = tmp_path / "logs" / "eta_engine" / "alerts_log.jsonl"
    for path in (baselines, journal, runtime, drift, alerts):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")

    old = datetime.now(UTC) - timedelta(days=30)
    os.utime(baselines, (old.timestamp(), old.timestamp()))

    monkeypatch.setattr(
        vps_failover_drill,
        "_state_file_paths",
        lambda: (
            [
                ("docs/strategy_baselines.json", baselines),
                ("var/eta_engine/state/decision_journal.jsonl", journal),
            ],
            [
                ("var/eta_engine/state/drift_watchdog.jsonl", drift),
                ("logs/eta_engine/alerts_log.jsonl", alerts),
                ("logs/eta_engine/runtime_log.jsonl", runtime),
            ],
        ),
    )

    result = vps_failover_drill._check_state_files_fresh()

    assert result.severity == "green"
    assert "within 24h" in result.summary


def test_state_freshness_still_warns_for_dynamic_state_files(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    journal = tmp_path / "var" / "eta_engine" / "state" / "decision_journal.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{}\n", encoding="utf-8")

    old = datetime.now(UTC) - timedelta(hours=30)
    os.utime(journal, (old.timestamp(), old.timestamp()))

    monkeypatch.setattr(
        vps_failover_drill,
        "_state_file_paths",
        lambda: ([("var/eta_engine/state/decision_journal.jsonl", journal)], []),
    )

    result = vps_failover_drill._check_state_files_fresh()

    assert result.severity == "amber"
    assert result.details["stale"][0]["file"] == "var/eta_engine/state/decision_journal.jsonl"
