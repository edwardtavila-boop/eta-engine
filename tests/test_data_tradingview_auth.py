"""Tests for ``eta_engine.data.tradingview.auth``.

Covers load+save round-trip, file-mode validation, malformed-JSON
rejection, and the ``has_session_cookie`` heuristic used by the
client + dashboard panel.
"""

from __future__ import annotations

import json
import os
from pathlib import Path  # noqa: TC003 -- runtime via tmp_path

import pytest

from eta_engine.data.tradingview.auth import (
    DEFAULT_AUTH_PATH,
    AuthState,
    AuthStateError,
    load_auth_state,
    save_auth_state,
)
from eta_engine.scripts import workspace_roots


def _ok_state() -> dict[str, object]:
    return {
        "cookies": [
            {"name": "sessionid", "value": "x", "domain": ".tradingview.com"},
            {"name": "device_t", "value": "y", "domain": ".tradingview.com"},
        ],
        "origins": [],
    }


def test_default_auth_path_uses_canonical_workspace_state() -> None:
    assert DEFAULT_AUTH_PATH == workspace_roots.ETA_TRADINGVIEW_AUTH_STATE_PATH


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    out = save_auth_state(_ok_state(), tmp_path / "auth.json")
    assert out.exists()
    state = load_auth_state(out)
    assert state.has_session_cookie is True
    assert state.source_path == out


def test_save_uses_0600_on_posix(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX-only mode check")
    out = save_auth_state(_ok_state(), tmp_path / "auth.json")
    mode = out.stat().st_mode & 0o777
    assert mode == 0o600


def test_load_rejects_group_readable(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX-only mode check")
    p = tmp_path / "auth.json"
    p.write_text(json.dumps(_ok_state()))
    p.chmod(0o644)
    with pytest.raises(AuthStateError, match="too open"):
        load_auth_state(p)


def test_load_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(AuthStateError, match="not found"):
        load_auth_state(tmp_path / "nope.json")


def test_load_malformed_json(tmp_path: Path) -> None:
    p = tmp_path / "auth.json"
    p.write_text("{not json")
    if os.name == "posix":
        p.chmod(0o600)
    with pytest.raises(AuthStateError, match="malformed"):
        load_auth_state(p)


def test_load_rejects_non_object_root(tmp_path: Path) -> None:
    p = tmp_path / "auth.json"
    p.write_text("[]")
    if os.name == "posix":
        p.chmod(0o600)
    with pytest.raises(AuthStateError, match="object"):
        load_auth_state(p)


def test_load_rejects_non_list_cookies(tmp_path: Path) -> None:
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"cookies": {}, "origins": []}))
    if os.name == "posix":
        p.chmod(0o600)
    with pytest.raises(AuthStateError, match="lists"):
        load_auth_state(p)


def test_save_rejects_non_dict_state(tmp_path: Path) -> None:
    with pytest.raises(AuthStateError):
        save_auth_state("not a dict", tmp_path / "auth.json")  # type: ignore[arg-type]


def test_has_session_cookie_false_when_missing() -> None:
    state = AuthState(cookies=[{"name": "device_t"}], origins=[])
    assert state.has_session_cookie is False


def test_has_session_cookie_false_when_wrong_domain() -> None:
    state = AuthState(
        cookies=[{"name": "sessionid", "value": "x", "domain": "other.com"}],
        origins=[],
    )
    assert state.has_session_cookie is False


def test_to_storage_state_returns_copy() -> None:
    state = AuthState(cookies=[{"name": "x"}], origins=[{"k": 1}])
    out = state.to_storage_state()
    assert out["cookies"] == [{"name": "x"}]
    out["cookies"].append({"name": "y"})
    # original list should not be mutated
    assert state.cookies == [{"name": "x"}]
