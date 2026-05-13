"""Tests for the evening journal cron entrypoint."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def test_run_journal_dry_run_audits_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run writes audit record + previews prompt, does NOT spawn subprocess."""
    from eta_engine.scripts import hermes_evening_journal as m

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("ETA_HERMES_CLI", str(fake_exe))
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "journal_audit.jsonl")

    spawned = []

    def fake_run(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("subprocess.run should not be called in dry-run")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = m.run_journal(dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert "EVENING JOURNAL" in result["prompt_preview"]
    assert spawned == []
    assert (tmp_path / "journal_audit.jsonl").exists()


def test_run_journal_returns_error_on_missing_exe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import hermes_evening_journal as m

    monkeypatch.setenv("ETA_HERMES_CLI", "Z:/no/hermes.exe")
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    result = m.run_journal(dry_run=False)
    assert result["ok"] is False
    assert "not found" in result["error"].lower()


def test_run_journal_parses_json_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hermes's reply (JSON envelope) is parsed into n_facts_saved + summary."""
    from eta_engine.scripts import hermes_evening_journal as m

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("ETA_HERMES_CLI", str(fake_exe))
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "audit.jsonl")

    fake_output = """\
Here is today's journal:

{
  "facts_saved": [
    {"category": "regime", "fact": "fact one"},
    {"category": "anomaly_pattern", "fact": "fact two"}
  ],
  "summary": "Quiet day. mnq_futures_sage dominant."
}
"""

    def fake_run(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=fake_output, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    result = m.run_journal(dry_run=False)
    assert result["ok"] is True
    assert result["n_facts_saved"] == 2
    assert "mnq_futures_sage" in result["summary"]


def test_run_journal_handles_subprocess_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from eta_engine.scripts import hermes_evening_journal as m

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("ETA_HERMES_CLI", str(fake_exe))
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "audit.jsonl")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="hermes", timeout=180)

    monkeypatch.setattr("subprocess.run", fake_run)
    result = m.run_journal(dry_run=False)
    assert result["ok"] is False
    assert "timeout" in result["error"].lower()


def test_run_journal_handles_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import hermes_evening_journal as m

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("ETA_HERMES_CLI", str(fake_exe))
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "audit.jsonl")

    def fake_run(args, **kwargs):
        return SimpleNamespace(returncode=2, stdout="", stderr="oh no")

    monkeypatch.setattr("subprocess.run", fake_run)
    result = m.run_journal(dry_run=False)
    assert result["ok"] is False
    assert result["returncode"] == 2
    assert "oh no" in result["stderr_preview"]


def test_run_journal_tolerates_unparseable_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Hermes replies without a JSON envelope, return ok=True n_facts_saved=0."""
    from eta_engine.scripts import hermes_evening_journal as m

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("ETA_HERMES_CLI", str(fake_exe))
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "audit.jsonl")

    def fake_run(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="just some prose, no json", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    result = m.run_journal(dry_run=False)
    assert result["ok"] is True
    assert result["n_facts_saved"] == 0
    assert result["facts_saved"] == []


def test_run_journal_audit_log_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every run appends a JSONL line to the audit log."""
    from eta_engine.scripts import hermes_evening_journal as m

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("ETA_HERMES_CLI", str(fake_exe))
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(m, "_AUDIT_PATH", audit)

    def fake_run(args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout='{"facts_saved": [{"category":"x","fact":"y"}], "summary":"ok"}',
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    m.run_journal(dry_run=False)
    m.run_journal(dry_run=False)
    lines = audit.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        rec = json.loads(line)
        assert rec["n_facts_saved"] == 1


def test_run_journal_omits_accept_hooks_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memory-writing journal does not auto-accept hooks unless opted in."""
    from eta_engine.scripts import hermes_evening_journal as m

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("ETA_HERMES_CLI", str(fake_exe))
    monkeypatch.delenv("ETA_HERMES_JOURNAL_ACCEPT_HOOKS", raising=False)
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "audit.jsonl")

    captured: dict[str, list[str]] = {}

    def fake_run(args, **kwargs):
        captured["cmd"] = list(args)
        return SimpleNamespace(returncode=0, stdout='{"facts_saved": [], "summary": "ok"}', stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    result = m.run_journal(dry_run=False)

    assert result["ok"] is True
    assert "--accept-hooks" not in captured["cmd"]
    assert "--yolo" not in captured["cmd"]


def test_run_journal_accept_hooks_requires_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import hermes_evening_journal as m

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("ETA_HERMES_CLI", str(fake_exe))
    monkeypatch.setenv("ETA_HERMES_JOURNAL_ACCEPT_HOOKS", "1")
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "audit.jsonl")

    captured: dict[str, list[str]] = {}

    def fake_run(args, **kwargs):
        captured["cmd"] = list(args)
        return SimpleNamespace(returncode=0, stdout='{"facts_saved": [], "summary": "ok"}', stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    result = m.run_journal(dry_run=False)

    assert result["ok"] is True
    assert "--accept-hooks" in captured["cmd"]
