"""Tests for the Monte Carlo equity-curve validator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from eta_engine.scripts import monte_carlo_validator as mod


def _write_close(path: Path, *, bot_id: str, realized_r: float, ts: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps({"bot_id": bot_id, "realized_r": realized_r, "ts": ts})
            + "\n"
        )


def test_load_closes_uses_patched_canonical_trade_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trade_file = tmp_path / "trade_closes.jsonl"
    _write_close(
        trade_file,
        bot_id="robust_bot",
        realized_r=1.25,
        ts="2026-05-05T00:00:00Z",
    )
    trade_file.write_text(
        trade_file.read_text(encoding="utf-8") + "{not json}\n",
        encoding="utf-8",
    )
    _write_close(
        trade_file,
        bot_id="old_bot",
        realized_r=9.0,
        ts="2026-04-01T00:00:00Z",
    )
    monkeypatch.setattr(mod, "_TRADE_CLOSES", trade_file)

    closes = mod._load_closes(since_iso="2026-05-01T00:00:00Z")

    assert closes == {"robust_bot": [1.25]}


def test_analyze_classifies_robust_dead_and_insufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trade_file = tmp_path / "trade_closes.jsonl"
    for _ in range(40):
        _write_close(
            trade_file,
            bot_id="robust_bot",
            realized_r=1.0,
            ts="2026-05-05T00:00:00Z",
        )
        _write_close(
            trade_file,
            bot_id="dead_bot",
            realized_r=-1.0,
            ts="2026-05-05T00:00:00Z",
        )
    for _ in range(5):
        _write_close(
            trade_file,
            bot_id="tiny_bot",
            realized_r=1.0,
            ts="2026-05-05T00:00:00Z",
        )
    monkeypatch.setattr(mod, "_TRADE_CLOSES", trade_file)

    report = mod.analyze(
        since_iso="2026-05-01T00:00:00Z",
        bootstraps=50,
        seed=7,
    )

    assert report["n_bots"] == 3
    assert report["bots"]["robust_bot"]["verdict"] == "ROBUST"
    assert report["bots"]["dead_bot"]["verdict"] == "DEAD"
    assert report["bots"]["tiny_bot"]["verdict"] == "INSUFFICIENT"
    assert report["verdict_counts"] == {
        "ROBUST": 1,
        "DEAD": 1,
        "INSUFFICIENT": 1,
    }
