"""Tests for the eta_engine.scripts.clear_kill_switch operator CLI.

Covers:
  * CLI arg validation (--confirm, --operator both mandatory => exit 4)
  * tripped-latch happy path (clears + audits + exit 0)
  * already-armed latch (refused, exit 1)
  * missing latch file (refused, exit 2)
  * malformed JSON (refused, exit 3)
  * --dry-run (exit 0, no writes)
  * audit-log append semantics (multiple clears each append a row)
  * workspace hard-rule guard (path outside workspace => refused)
  * legacy-path read fallback (read legacy, write canonical)

The CLI must be import-side-effect-free; we exercise it by calling
``main(argv)`` directly with a list, instead of patching ``sys.argv``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eta_engine.scripts.clear_kill_switch import (
    AUDIT_LOG_FILENAME,
    EXIT_BAD_ARGS,
    EXIT_CLEARED,
    EXIT_FILE_MISSING,
    EXIT_MALFORMED,
    EXIT_NOT_TRIPPED,
    main,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _write_tripped_latch(path: Path, *, reason: str = "test trip") -> dict:
    """Write a TRIPPED latch JSON to ``path`` and return the dict."""
    payload = {
        "state": "TRIPPED",
        "tripped_at_utc": "2026-05-04T12:00:00+00:00",
        "reason": reason,
        "scope": "global",
        "action": "FLATTEN_ALL",
        "severity": "CRITICAL",
        "evidence": {"daily_loss_pct": 6.02, "cap_pct": 6.0},
        "cleared_at_utc": None,
        "cleared_by": None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def _write_armed_latch(path: Path) -> dict:
    """Write an ARMED latch JSON to ``path`` and return the dict."""
    payload = {
        "state": "ARMED",
        "tripped_at_utc": None,
        "reason": None,
        "scope": None,
        "action": None,
        "severity": None,
        "evidence": {},
        "cleared_at_utc": None,
        "cleared_by": None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


@pytest.fixture
def workspace_latch_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a tmp latch path that the CLI's workspace guard will accept.

    The CLI's workspace-root check uses
    ``Path(__file__).resolve().parents[2]`` from the CLI module; that
    points at the real ``C:\\EvolutionaryTradingAlgo`` workspace root.
    Pytest's ``tmp_path`` lives on the same drive (``C:\\Users\\...``) so
    is *outside* that root, which would trip the hard-rule guard. To
    test happy paths we monkeypatch ``WORKSPACE_ROOT`` for the
    duration of the test. The hard-rule guard itself is exercised in
    a dedicated negative test below.
    """
    monkeypatch.setattr(
        "eta_engine.scripts.clear_kill_switch.WORKSPACE_ROOT",
        tmp_path,
    )
    return tmp_path / "var" / "eta_engine" / "state" / "kill_switch_latch.json"


# --------------------------------------------------------------------------- #
# CLI arg validation (exit 4)
# --------------------------------------------------------------------------- #
def test_refuses_without_confirm(workspace_latch_path: Path) -> None:
    _write_tripped_latch(workspace_latch_path)
    rc = main(
        [
            "--operator",
            "edward",
            "--latch-path",
            str(workspace_latch_path),
        ]
    )
    assert rc == EXIT_BAD_ARGS
    # Latch must be untouched.
    raw = json.loads(workspace_latch_path.read_text(encoding="utf-8"))
    assert raw["state"] == "TRIPPED"


def test_refuses_without_operator(workspace_latch_path: Path) -> None:
    _write_tripped_latch(workspace_latch_path)
    rc = main(
        [
            "--confirm",
            "--latch-path",
            str(workspace_latch_path),
        ]
    )
    assert rc == EXIT_BAD_ARGS
    raw = json.loads(workspace_latch_path.read_text(encoding="utf-8"))
    assert raw["state"] == "TRIPPED"


def test_refuses_when_operator_is_blank(workspace_latch_path: Path) -> None:
    _write_tripped_latch(workspace_latch_path)
    rc = main(
        [
            "--confirm",
            "--operator",
            "   ",
            "--latch-path",
            str(workspace_latch_path),
        ]
    )
    assert rc == EXIT_BAD_ARGS


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_clears_tripped_latch(workspace_latch_path: Path) -> None:
    _write_tripped_latch(workspace_latch_path)
    rc = main(
        [
            "--confirm",
            "--operator",
            "edward",
            "--reason",
            "post-mortem reviewed",
            "--latch-path",
            str(workspace_latch_path),
        ]
    )
    assert rc == EXIT_CLEARED

    raw = json.loads(workspace_latch_path.read_text(encoding="utf-8"))
    assert raw["state"] == "ARMED"
    assert raw["cleared_by"] == "edward"
    assert raw["cleared_at_utc"] is not None
    # Audit trail of prior trip must survive.
    assert raw["action"] == "FLATTEN_ALL"
    assert raw["scope"] == "global"

    audit = workspace_latch_path.parent / AUDIT_LOG_FILENAME
    assert audit.exists()
    lines = audit.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["operator"] == "edward"
    assert entry["reason"] == "post-mortem reviewed"
    assert entry["prior_state"]["state"] == "TRIPPED"
    assert entry["new_state"]["state"] == "ARMED"


def test_clears_latch_without_reason(workspace_latch_path: Path) -> None:
    """--reason is optional; clear should still succeed and audit-log."""
    _write_tripped_latch(workspace_latch_path)
    rc = main(
        [
            "--confirm",
            "--operator",
            "edward",
            "--latch-path",
            str(workspace_latch_path),
        ]
    )
    assert rc == EXIT_CLEARED

    audit = workspace_latch_path.parent / AUDIT_LOG_FILENAME
    entry = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert entry["reason"] is None


# --------------------------------------------------------------------------- #
# Refusal cases
# --------------------------------------------------------------------------- #
def test_refuses_when_not_tripped(workspace_latch_path: Path) -> None:
    _write_armed_latch(workspace_latch_path)
    rc = main(
        [
            "--confirm",
            "--operator",
            "edward",
            "--latch-path",
            str(workspace_latch_path),
        ]
    )
    assert rc == EXIT_NOT_TRIPPED
    audit = workspace_latch_path.parent / AUDIT_LOG_FILENAME
    assert not audit.exists()


def test_refuses_when_missing_file(workspace_latch_path: Path) -> None:
    # Note: do NOT create the file; just point at a path that doesn't exist.
    assert not workspace_latch_path.exists()
    rc = main(
        [
            "--confirm",
            "--operator",
            "edward",
            "--latch-path",
            str(workspace_latch_path),
        ]
    )
    assert rc == EXIT_FILE_MISSING


def test_refuses_when_malformed(workspace_latch_path: Path) -> None:
    workspace_latch_path.parent.mkdir(parents=True, exist_ok=True)
    workspace_latch_path.write_text("{not-valid-json", encoding="utf-8")
    rc = main(
        [
            "--confirm",
            "--operator",
            "edward",
            "--latch-path",
            str(workspace_latch_path),
        ]
    )
    assert rc == EXIT_MALFORMED


def test_refuses_when_root_is_not_object(workspace_latch_path: Path) -> None:
    """A JSON-but-not-an-object root should be classified malformed too."""
    workspace_latch_path.parent.mkdir(parents=True, exist_ok=True)
    workspace_latch_path.write_text('["array","root"]', encoding="utf-8")
    rc = main(
        [
            "--confirm",
            "--operator",
            "edward",
            "--latch-path",
            str(workspace_latch_path),
        ]
    )
    assert rc == EXIT_MALFORMED


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #
def test_dry_run_does_not_write(workspace_latch_path: Path) -> None:
    original = _write_tripped_latch(workspace_latch_path)
    rc = main(
        [
            "--confirm",
            "--operator",
            "edward",
            "--reason",
            "rehearsal",
            "--dry-run",
            "--latch-path",
            str(workspace_latch_path),
        ]
    )
    assert rc == EXIT_CLEARED

    raw = json.loads(workspace_latch_path.read_text(encoding="utf-8"))
    # Original latch payload must be byte-equivalent (still TRIPPED).
    assert raw == original

    audit = workspace_latch_path.parent / AUDIT_LOG_FILENAME
    assert not audit.exists()


# --------------------------------------------------------------------------- #
# Audit log append behavior
# --------------------------------------------------------------------------- #
def test_audit_log_appends_entry(workspace_latch_path: Path) -> None:
    """Multiple consecutive clears each append; never overwrite."""
    audit = workspace_latch_path.parent / AUDIT_LOG_FILENAME

    # First clear.
    _write_tripped_latch(workspace_latch_path, reason="trip A")
    rc = main(
        [
            "--confirm",
            "--operator",
            "alice",
            "--reason",
            "first clear",
            "--latch-path",
            str(workspace_latch_path),
        ]
    )
    assert rc == EXIT_CLEARED
    assert len(audit.read_text(encoding="utf-8").splitlines()) == 1

    # Re-trip and clear again.
    _write_tripped_latch(workspace_latch_path, reason="trip B")
    rc = main(
        [
            "--confirm",
            "--operator",
            "bob",
            "--reason",
            "second clear",
            "--latch-path",
            str(workspace_latch_path),
        ]
    )
    assert rc == EXIT_CLEARED
    lines = audit.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    operators = [json.loads(line)["operator"] for line in lines]
    reasons = [json.loads(line)["reason"] for line in lines]
    assert operators == ["alice", "bob"]
    assert reasons == ["first clear", "second clear"]


# --------------------------------------------------------------------------- #
# Hard-rule: latch path must live under workspace root
# --------------------------------------------------------------------------- #
def test_refuses_path_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A latch path outside WORKSPACE_ROOT must be refused."""
    # Pin the workspace root to a fake location that the tmp_path is
    # NOT under, so the guard fires.
    fake_workspace = tmp_path / "fake_workspace_root"
    fake_workspace.mkdir()
    monkeypatch.setattr(
        "eta_engine.scripts.clear_kill_switch.WORKSPACE_ROOT",
        fake_workspace,
    )

    # Put a tripped latch outside the fake workspace.
    outside = tmp_path / "outside_latch.json"
    _write_tripped_latch(outside)

    rc = main(
        [
            "--confirm",
            "--operator",
            "edward",
            "--latch-path",
            str(outside),
        ]
    )
    assert rc == EXIT_BAD_ARGS
    # Latch untouched.
    raw = json.loads(outside.read_text(encoding="utf-8"))
    assert raw["state"] == "TRIPPED"


# --------------------------------------------------------------------------- #
# Legacy path read fallback + canonical write
# --------------------------------------------------------------------------- #
def test_resolves_legacy_path_if_canonical_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When --latch-path is omitted and only the legacy file exists,
    the CLI must still find it and clear it (via resolve_existing_path).
    The canonical write path receives the cleared record."""
    monkeypatch.setattr(
        "eta_engine.scripts.clear_kill_switch.WORKSPACE_ROOT",
        tmp_path,
    )
    legacy = tmp_path / "legacy" / "kill_switch_latch.json"
    canonical = tmp_path / "canonical" / "kill_switch_latch.json"
    _write_tripped_latch(legacy)
    assert not canonical.exists()

    monkeypatch.setattr(
        "eta_engine.scripts.clear_kill_switch.default_path",
        lambda: canonical,
    )
    monkeypatch.setattr(
        "eta_engine.scripts.clear_kill_switch.default_legacy_path",
        lambda: legacy,
    )

    def _resolve_existing() -> Path:
        # Mirror the real helper: prefer canonical, fall back to legacy.
        if canonical.exists():
            return canonical
        if legacy.exists():
            return legacy
        return canonical

    monkeypatch.setattr(
        "eta_engine.scripts.clear_kill_switch.resolve_existing_path",
        _resolve_existing,
    )

    rc = main(
        [
            "--confirm",
            "--operator",
            "edward",
        ]
    )
    assert rc == EXIT_CLEARED

    # Canonical file MUST now exist with cleared record.
    assert canonical.exists()
    raw_canonical = json.loads(canonical.read_text(encoding="utf-8"))
    assert raw_canonical["state"] == "ARMED"
    assert raw_canonical["cleared_by"] == "edward"
    # Prior-trip audit trail must be preserved.
    assert raw_canonical["action"] == "FLATTEN_ALL"


def test_clear_writes_canonical_path_even_when_only_legacy_existed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migration story: read from legacy if that's where it lives,
    but write the cleared record to the canonical path."""
    monkeypatch.setattr(
        "eta_engine.scripts.clear_kill_switch.WORKSPACE_ROOT",
        tmp_path,
    )
    legacy = tmp_path / "legacy" / "state" / "kill_switch_latch.json"
    canonical = tmp_path / "var" / "eta_engine" / "state" / "kill_switch_latch.json"
    _write_tripped_latch(legacy, reason="legacy-located trip")

    monkeypatch.setattr(
        "eta_engine.scripts.clear_kill_switch.default_path",
        lambda: canonical,
    )
    monkeypatch.setattr(
        "eta_engine.scripts.clear_kill_switch.default_legacy_path",
        lambda: legacy,
    )
    monkeypatch.setattr(
        "eta_engine.scripts.clear_kill_switch.resolve_existing_path",
        lambda: legacy if legacy.exists() and not canonical.exists() else canonical,
    )

    rc = main(
        [
            "--confirm",
            "--operator",
            "edward",
        ]
    )
    assert rc == EXIT_CLEARED

    assert canonical.exists()
    raw_canonical = json.loads(canonical.read_text(encoding="utf-8"))
    assert raw_canonical["state"] == "ARMED"
    assert raw_canonical["reason"] == "legacy-located trip"

    # Audit log should be next to the canonical write target.
    audit = canonical.parent / AUDIT_LOG_FILENAME
    assert audit.exists()
