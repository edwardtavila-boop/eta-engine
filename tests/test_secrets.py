"""Tests for core.secrets.SecretsManager."""

from __future__ import annotations

import pytest

from eta_engine.core.secrets import (
    REQUIRED_KEYS,
    TELEGRAM_BOT_TOKEN,
    SecretsManager,
)


def test_env_lookup_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_TEST_KEY", "hello-value")
    sm = SecretsManager(env_file="does_not_exist.env")
    assert sm.get("APEX_TEST_KEY", required=False) == "hello-value"
    assert any("source=env" in line for line in sm.audit_log)


def test_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APEX_ABSENT_KEY", raising=False)
    sm = SecretsManager(env_file="does_not_exist.env")
    with pytest.raises(KeyError):
        sm.get("APEX_ABSENT_KEY", required=True)


def test_missing_optional_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APEX_ABSENT_KEY", raising=False)
    sm = SecretsManager(env_file="does_not_exist.env")
    assert sm.get("APEX_ABSENT_KEY", required=False) is None
    assert sm.audit_log[-1].endswith("source=missing")


def test_audit_log_grows_per_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_TEST_KEY", "x")
    sm = SecretsManager(env_file="does_not_exist.env")
    initial = len(sm.audit_log)
    sm.get("APEX_TEST_KEY", required=False)
    sm.get("APEX_TEST_KEY", required=False)
    sm.get("APEX_TEST_KEY", required=False)
    assert len(sm.audit_log) == initial + 3


def test_audit_log_never_contains_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_SECRET_VAL", "super-sekret-123")
    sm = SecretsManager(env_file="does_not_exist.env")
    sm.get("APEX_SECRET_VAL", required=False)
    joined = "\n".join(sm.audit_log)
    assert "super-sekret-123" not in joined
    assert "APEX_SECRET_VAL" in joined


def test_validate_required_keys_all_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in REQUIRED_KEYS:
        monkeypatch.delenv(k, raising=False)
    sm = SecretsManager(env_file="does_not_exist.env")
    missing = sm.validate_required_keys()
    assert set(missing) == set(REQUIRED_KEYS)


def test_validate_required_keys_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in REQUIRED_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv(TELEGRAM_BOT_TOKEN, "tg-token")
    sm = SecretsManager(env_file="does_not_exist.env")
    missing = sm.validate_required_keys()
    assert TELEGRAM_BOT_TOKEN not in missing
    assert len(missing) == len(REQUIRED_KEYS) - 1


def test_env_file_lookup(tmp_path) -> None:
    ef = tmp_path / ".env"
    ef.write_text('APEX_FILE_KEY="file-value"\n# comment\nAPEX_OTHER=x\n', encoding="utf-8")
    sm = SecretsManager(env_file=ef)
    assert sm.get("APEX_FILE_KEY", required=False) == "file-value"
    assert sm.get("APEX_OTHER", required=False) == "x"
