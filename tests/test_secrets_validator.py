from __future__ import annotations

import json
from pathlib import Path

from eta_engine.scripts import secrets_validator


def test_default_root_points_to_canonical_workspace_root() -> None:
    assert Path(__file__).resolve().parents[2] == secrets_validator.ROOT


def _write_required_baseline(root) -> None:
    secrets_dir = root / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "telegram_bot_token.txt").write_text("123456789:ABCDEFGHIJKLMNOPQRSTUV_abcdefghi", encoding="utf-8")
    (secrets_dir / "telegram_chat_id.txt").write_text("-1001234567890", encoding="utf-8")
    (secrets_dir / "ibkr_account_id.txt").write_text("DUQ319869", encoding="utf-8")


def _result_by_path(results: list[secrets_validator.SecretCheck]) -> dict[str, secrets_validator.SecretCheck]:
    return {result.path: result for result in results}


def test_check_secrets_detects_placeholder_text_values(tmp_path) -> None:
    _write_required_baseline(tmp_path)
    (tmp_path / "secrets" / "telegram_bot_token.txt").write_text(
        "# Place your telegram_bot_token.txt here",
        encoding="utf-8",
    )
    (tmp_path / "secrets" / "ibkr_credentials.json").write_text(
        json.dumps({"username": "apexpredator"}),
        encoding="utf-8",
    )

    results = _result_by_path(secrets_validator.check_secrets(root=tmp_path))

    token_result = results["secrets/telegram_bot_token.txt"]
    assert token_result.status == secrets_validator.SecretStatus.PLACEHOLDER
    assert "placeholder" in token_result.detail.lower()


def test_check_secrets_rejects_invalid_ibkr_credentials_json(tmp_path) -> None:
    _write_required_baseline(tmp_path)
    (tmp_path / "secrets" / "ibkr_credentials.json").write_text(
        json.dumps({"host": "127.0.0.1", "port": 4002}),
        encoding="utf-8",
    )

    results = _result_by_path(secrets_validator.check_secrets(root=tmp_path))

    creds_result = results["secrets/ibkr_credentials.json"]
    assert creds_result.status == secrets_validator.SecretStatus.INVALID
    assert "username" in creds_result.detail.lower() or "login" in creds_result.detail.lower()


def test_check_secrets_reports_binary_json_as_invalid(tmp_path) -> None:
    _write_required_baseline(tmp_path)
    (tmp_path / "secrets" / "ibkr_credentials.json").write_text(
        json.dumps({"username": "apexpredator"}),
        encoding="utf-8",
    )
    (tmp_path / "secrets" / "quantum_creds.json").write_bytes(b"\xff\xfe\x00\x00")

    results = _result_by_path(secrets_validator.check_secrets(root=tmp_path))

    quantum_result = results["secrets/quantum_creds.json"]
    assert quantum_result.status == secrets_validator.SecretStatus.INVALID
    assert "json" in quantum_result.detail.lower()


def test_check_secrets_accepts_windows_encoded_json_files(tmp_path) -> None:
    _write_required_baseline(tmp_path)
    (tmp_path / "secrets" / "ibkr_credentials.json").write_text(
        json.dumps({"username": "apexpredator"}),
        encoding="utf-8-sig",
    )
    (tmp_path / "secrets" / "quantum_creds.json").write_text(
        json.dumps({"dwave": {"token": "live-token"}, "budget": {"enable_cloud": False}}),
        encoding="utf-16",
    )
    (tmp_path / "secrets" / "tastytrade_credentials.json").write_text(
        json.dumps({"login": "operator@example.com", "password_file": "secrets/tastytrade_password.txt"}),
        encoding="utf-8-sig",
    )

    results = _result_by_path(secrets_validator.check_secrets(root=tmp_path))

    assert results["secrets/ibkr_credentials.json"].status == secrets_validator.SecretStatus.PRESENT
    assert results["secrets/quantum_creds.json"].status == secrets_validator.SecretStatus.PRESENT
    assert results["secrets/tastytrade_credentials.json"].status == secrets_validator.SecretStatus.PRESENT


def test_main_supports_json_output(tmp_path, monkeypatch, capsys) -> None:
    _write_required_baseline(tmp_path)
    (tmp_path / "secrets" / "ibkr_credentials.json").write_text(
        json.dumps({"username": "apexpredator"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(secrets_validator, "ROOT", tmp_path)

    exit_code = secrets_validator.main(["--json"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 1
    assert payload["summary"]["required_invalid_count"] == 0
    statuses = {item["path"]: item["status"] for item in payload["results"]}
    assert statuses["secrets/quantum_creds.json"] == secrets_validator.SecretStatus.OPTIONAL_MISSING
