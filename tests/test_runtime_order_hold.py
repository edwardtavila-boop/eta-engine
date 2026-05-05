"""Tests for the shared order-entry hold switch."""

from __future__ import annotations

from pathlib import Path

from eta_engine.scripts.runtime_order_hold import (
    load_order_entry_hold,
    main,
    write_order_entry_hold,
)


def test_missing_hold_file_is_inactive(tmp_path: Path, monkeypatch) -> None:
    hold_path = tmp_path / "missing.json"
    monkeypatch.delenv("ETA_ORDER_ENTRY_HOLD", raising=False)

    hold = load_order_entry_hold(hold_path)

    assert hold.active is False
    assert hold.path == hold_path


def test_active_hold_file_blocks_order_entry(tmp_path: Path, monkeypatch) -> None:
    hold_path = tmp_path / "order_entry_hold.json"
    monkeypatch.delenv("ETA_ORDER_ENTRY_HOLD", raising=False)
    write_order_entry_hold(
        active=True,
        reason="manual_flatten",
        path=hold_path,
    )

    hold = load_order_entry_hold(hold_path)

    assert hold.active is True
    assert hold.reason == "manual_flatten"
    assert hold.source == "file"


def test_malformed_hold_file_fails_closed(tmp_path: Path, monkeypatch) -> None:
    hold_path = tmp_path / "order_entry_hold.json"
    hold_path.write_text("{not-json", encoding="utf-8")
    monkeypatch.delenv("ETA_ORDER_ENTRY_HOLD", raising=False)

    hold = load_order_entry_hold(hold_path)

    assert hold.active is True
    assert hold.source == "file_error"
    assert hold.reason.startswith("malformed_hold_file")


def test_env_hold_overrides_inactive_file(tmp_path: Path, monkeypatch) -> None:
    hold_path = tmp_path / "order_entry_hold.json"
    write_order_entry_hold(active=False, reason="clear", path=hold_path)
    monkeypatch.setenv("ETA_ORDER_ENTRY_HOLD", "1")
    monkeypatch.setenv("ETA_ORDER_ENTRY_HOLD_REASON", "incident")

    hold = load_order_entry_hold(hold_path)

    assert hold.active is True
    assert hold.reason == "incident"
    assert hold.source == "ETA_ORDER_ENTRY_HOLD"


def test_status_accepts_json_flag_for_operator_scripts(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    hold_path = tmp_path / "order_entry_hold.json"
    monkeypatch.delenv("ETA_ORDER_ENTRY_HOLD", raising=False)
    write_order_entry_hold(active=True, reason="json_flag", path=hold_path)

    assert main(["status", "--json", "--path", str(hold_path)]) == 0

    out = capsys.readouterr().out
    assert '"active": true' in out
    assert '"reason": "json_flag"' in out
