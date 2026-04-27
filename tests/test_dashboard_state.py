"""Tests for safe state-file reader (Wave-7 dashboard, 2026-04-27)."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def test_read_json_safe_returns_data_on_happy_path(tmp_path: Path) -> None:
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    f = tmp_path / "stuff.json"
    f.write_text(json.dumps({"ok": True}), encoding="utf-8")
    out = read_json_safe(f)
    assert out == {"ok": True}


def test_read_json_safe_returns_warning_when_missing(tmp_path: Path) -> None:
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    out = read_json_safe(tmp_path / "missing.json")
    assert out == {"_warning": "no_data", "_path": str(tmp_path / "missing.json")}


def test_read_json_safe_returns_error_on_corrupt(tmp_path: Path) -> None:
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    f = tmp_path / "bad.json"
    f.write_text("not json {{{", encoding="utf-8")
    out = read_json_safe(f)
    assert out["_error_code"] == "state_corrupt"
    assert "bad.json" in out["_path"]


def test_read_json_safe_returns_error_on_io_error(tmp_path: Path) -> None:
    from eta_engine.deploy.scripts.dashboard_state import read_json_safe

    # tmp_path itself IS a directory; reading it as a file raises IsADirectoryError (OSError subclass)
    out = read_json_safe(tmp_path)
    assert out["_error_code"] == "state_io_error"
    assert str(tmp_path) in out["_path"]
