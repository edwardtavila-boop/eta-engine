"""Tests for the proactive auto-investigator cron entrypoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest


def _write_hits(path: Path, hits: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for h in hits:
            fh.write(json.dumps(h) + "\n")


def test_run_once_returns_no_new_when_empty_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import hermes_proactive_investigator as m

    monkeypatch.setattr(m, "_HITS_LOG", tmp_path / "no_hits.jsonl")
    monkeypatch.setattr(m, "_CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "audit.jsonl")

    result = m.run_once(dry_run=True)
    assert result["n_new"] == 0
    assert result["reason"] == "no_new_hits"


def test_run_once_skips_already_seen_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.scripts import hermes_proactive_investigator as m

    hits_path = tmp_path / "hits.jsonl"
    _write_hits(
        hits_path,
        [{"key": "loss_streak:bot_a:3", "pattern": "loss_streak", "severity": "warn", "bot_id": "bot_a"}],
    )
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"seen_keys": ["loss_streak:bot_a:3"]}), encoding="utf-8")
    monkeypatch.setattr(m, "_HITS_LOG", hits_path)
    monkeypatch.setattr(m, "_CURSOR_PATH", cursor)
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "audit.jsonl")

    result = m.run_once(dry_run=True)
    assert result["n_new"] == 0


def test_run_once_filters_to_warn_critical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """info-severity hits like win_streak don't auto-investigate."""
    from eta_engine.scripts import hermes_proactive_investigator as m

    hits_path = tmp_path / "hits.jsonl"
    _write_hits(
        hits_path,
        [
            {"key": "win_streak:happy:6", "pattern": "win_streak", "severity": "info", "bot_id": "happy"},
            {"key": "fleet_hot_day:2026-05-12", "pattern": "fleet_hot_day", "severity": "info", "bot_id": "__fleet__"},
        ],
    )
    monkeypatch.setattr(m, "_HITS_LOG", hits_path)
    monkeypatch.setattr(m, "_CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "audit.jsonl")

    result = m.run_once(dry_run=True)
    assert result["n_new"] == 2
    assert result["n_investigated"] == 0
    assert result["reason"] == "no_investigate_worthy_hits"


def test_run_once_skips_fleet_drawdown_pattern(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fleet_drawdown is handled by the dedicated pulse + skill."""
    from eta_engine.scripts import hermes_proactive_investigator as m

    hits_path = tmp_path / "hits.jsonl"
    _write_hits(
        hits_path,
        [
            {
                "key": "fleet_drawdown:2026-05-12",
                "pattern": "fleet_drawdown",
                "severity": "critical",
                "bot_id": "__fleet__",
            }
        ],
    )
    monkeypatch.setattr(m, "_HITS_LOG", hits_path)
    monkeypatch.setattr(m, "_CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    result = m.run_once(dry_run=True)
    assert result["n_investigated"] == 0


def test_run_once_investigates_warn_hits_in_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """warn-severity hits trigger _ask_hermes_to_investigate (mocked)."""
    from eta_engine.scripts import hermes_proactive_investigator as m

    hits_path = tmp_path / "hits.jsonl"
    _write_hits(
        hits_path,
        [
            {
                "key": "loss_streak:bot_a:4",
                "pattern": "loss_streak",
                "severity": "warn",
                "bot_id": "bot_a",
                "detail": "x",
            },
            {
                "key": "loss_rate:bot_b:6of8",
                "pattern": "loss_rate",
                "severity": "warn",
                "bot_id": "bot_b",
                "detail": "y",
            },
        ],
    )
    monkeypatch.setattr(m, "_HITS_LOG", hits_path)
    monkeypatch.setattr(m, "_CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "audit.jsonl")

    invoked = []

    def fake_ask(hit: dict[str, Any]) -> str:
        invoked.append(hit["key"])
        return f"Diagnosis for {hit['bot_id']}: looks like normal variance."

    monkeypatch.setattr(m, "_ask_hermes_to_investigate", fake_ask)

    result = m.run_once(dry_run=True)
    assert result["n_investigated"] == 2
    assert sorted(invoked) == ["loss_rate:bot_b:6of8", "loss_streak:bot_a:4"]
    # dry-run: no Telegram sent
    assert result.get("n_sent", 0) == 0


def test_run_once_caps_investigations_per_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard cap at 5 investigations per cron tick to bound runtime."""
    from eta_engine.scripts import hermes_proactive_investigator as m

    hits = []
    for i in range(8):
        hits.append(
            {
                "key": f"loss_streak:bot_{i}:4",
                "pattern": "loss_streak",
                "severity": "warn",
                "bot_id": f"bot_{i}",
                "detail": "stuff",
            }
        )
    hits_path = tmp_path / "hits.jsonl"
    _write_hits(hits_path, hits)
    monkeypatch.setattr(m, "_HITS_LOG", hits_path)
    monkeypatch.setattr(m, "_CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(m, "_ask_hermes_to_investigate", lambda h: "diagnosis")

    result = m.run_once(dry_run=True)
    assert result["n_new"] == 8
    assert result["n_candidates"] == 8
    assert result["n_investigated"] == 5


def test_run_once_honors_silence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When operator has /silence active, suppress sends (still scan + audit)."""
    from eta_engine.scripts import hermes_proactive_investigator as m

    hits_path = tmp_path / "hits.jsonl"
    _write_hits(
        hits_path,
        [
            {
                "key": "loss_streak:bot_a:5",
                "pattern": "loss_streak",
                "severity": "critical",
                "bot_id": "bot_a",
                "detail": "5",
            }
        ],
    )
    monkeypatch.setattr(m, "_HITS_LOG", hits_path)
    monkeypatch.setattr(m, "_CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(m, "_is_silenced", lambda: True)
    monkeypatch.setattr(m, "_ask_hermes_to_investigate", lambda h: "diagnosis")

    sent = []
    monkeypatch.setattr(
        "eta_engine.deploy.scripts.telegram_alerts.send_from_env",
        lambda text, priority="INFO": sent.append(text) or {"ok": True},
    )

    result = m.run_once(dry_run=False)
    assert result["reason"] == "silenced_by_operator"
    assert sent == []


def test_run_once_persists_cursor_across_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive calls don't re-investigate the same hit."""
    from eta_engine.scripts import hermes_proactive_investigator as m

    hits_path = tmp_path / "hits.jsonl"
    _write_hits(
        hits_path,
        [
            {
                "key": "loss_streak:bot_a:4",
                "pattern": "loss_streak",
                "severity": "warn",
                "bot_id": "bot_a",
                "detail": "x",
            }
        ],
    )
    monkeypatch.setattr(m, "_HITS_LOG", hits_path)
    monkeypatch.setattr(m, "_CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(m, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(m, "_ask_hermes_to_investigate", lambda h: "diagnosis")
    monkeypatch.setattr(
        "eta_engine.deploy.scripts.telegram_alerts.send_from_env",
        lambda text, priority="INFO": {"ok": True},
    )

    r1 = m.run_once(dry_run=False)
    r2 = m.run_once(dry_run=False)
    assert r1["n_investigated"] == 1
    assert r2["n_new"] == 0  # cursor advanced


def test_ask_hermes_to_investigate_omits_accept_hooks_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Autonomous proactive investigations do not auto-accept hooks by default."""
    from types import SimpleNamespace

    from eta_engine.scripts import hermes_proactive_investigator as m

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("ETA_HERMES_CLI", str(fake_exe))
    monkeypatch.delenv("ETA_HERMES_PROACTIVE_ACCEPT_HOOKS", raising=False)

    captured: dict[str, list[str]] = {}

    def fake_run(args, **_kwargs):
        captured["cmd"] = list(args)
        return SimpleNamespace(returncode=0, stdout="diagnosis", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    reply = m._ask_hermes_to_investigate(
        {
            "key": "loss_streak:bot_a:4",
            "pattern": "loss_streak",
            "severity": "warn",
            "bot_id": "bot_a",
        }
    )

    assert reply == "diagnosis"
    assert "--accept-hooks" not in captured["cmd"]


def test_ask_hermes_to_investigate_accept_hooks_requires_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from eta_engine.scripts import hermes_proactive_investigator as m

    fake_exe = tmp_path / "hermes.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("ETA_HERMES_CLI", str(fake_exe))
    monkeypatch.setenv("ETA_HERMES_PROACTIVE_ACCEPT_HOOKS", "1")

    captured: dict[str, list[str]] = {}

    def fake_run(args, **_kwargs):
        captured["cmd"] = list(args)
        return SimpleNamespace(returncode=0, stdout="diagnosis", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert m._ask_hermes_to_investigate({"pattern": "loss_streak"}) == "diagnosis"
    assert "--accept-hooks" in captured["cmd"]
