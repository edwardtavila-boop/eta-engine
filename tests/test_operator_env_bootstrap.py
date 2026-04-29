from __future__ import annotations

import json

from eta_engine.scripts import operator_env_bootstrap, vps_failover_drill


def test_bootstrap_status_is_read_only_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(operator_env_bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(vps_failover_drill, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text("APEX_MODE=PAPER\n", encoding="utf-8")

    status = operator_env_bootstrap.build_status()

    assert status["exists"] is False
    assert status["created"] is False
    assert not (tmp_path / ".env").exists()
    assert status["values_emitted"] is False
    assert status["ready_to_launch"] is False
    assert set(status["required_pending"]) == {"runtime_mode", "jarvis_budget", "ibkr_primary"}
    assert status["required_pending"]["runtime_mode"] == ["APEX_MODE=PAPER"]
    assert status["next_actions"][0]["action"] == "create_env"
    assert status["next_actions"][0]["blocking"] is True


def test_bootstrap_create_copies_template_without_overwriting(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(operator_env_bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(vps_failover_drill, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text("APEX_MODE=PAPER\n", encoding="utf-8")

    first = operator_env_bootstrap.build_status(create=True)
    (tmp_path / ".env").write_text("APEX_MODE=LIVE\n", encoding="utf-8")
    second = operator_env_bootstrap.build_status(create=True)

    assert first["created"] is True
    assert second["created"] is False
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "APEX_MODE=LIVE\n"


def test_bootstrap_json_output_never_emits_values(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(operator_env_bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(vps_failover_drill, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text("APEX_MODE=PAPER\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "APEX_MODE=PAPER",
                "ANTHROPIC_API_KEY=secret-token",
                "JARVIS_HOURLY_USD_BUDGET=5",
                "JARVIS_DAILY_USD_BUDGET=25",
                "IBKR_VENUE_TYPE=paper",
                "IBKR_CP_BASE_URL=https://127.0.0.1:5000/v1/api",
                "IBKR_ACCOUNT_ID=DU123",
                "IBKR_CONID_MNQ1=123456",
            ]
        ),
        encoding="utf-8",
    )

    rc = operator_env_bootstrap.main(["--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["severity"] == "green"
    assert payload["ready_to_launch"] is True
    assert payload["required_pending"] == {}
    assert payload["values_emitted"] is False
    assert payload["redaction_contract"] == {
        "key_names_only": True,
        "paths_only": True,
        "values_emitted": False,
    }
    assert payload["next_actions"][-1]["action"] == "refresh_operator_queue"
    assert "secret-token" not in json.dumps(payload)
    assert "DU123" not in json.dumps(payload)
