from __future__ import annotations

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
                "ANTHROPIC_API_KEY=test-key",
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
                "ANTHROPIC_API_KEY=test-key",
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
