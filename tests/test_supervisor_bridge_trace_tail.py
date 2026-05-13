"""Heartbeat carries last 3 trace lines so operator sees JARVIS thinking."""

from __future__ import annotations

import json
from unittest.mock import patch

from eta_engine.scripts.jarvis_supervisor_bridge import (
    jarvis_supervisor_bot_accounts,
)


def _write_heartbeat(path) -> None:
    """Write a minimal heartbeat JSON with one bot."""
    payload = {
        "ts": "2026-05-11T20:00:00Z",
        "mode": "paper_live",
        "bots": [
            {
                "bot_id": "bot1",
                "n_entries": 0,
                "n_exits": 0,
                "realized_pnl": 0.0,
                "open_position": None,
                "last_jarvis_verdict": "NONE",
                "symbol": "MNQ",
                "strategy_kind": "sweep_reclaim",
                "mode": "paper_live",
            },
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_heartbeat_includes_trace_tail_when_trace_file_has_records(tmp_path):
    """3 trace records on disk → bridge attaches them to accounts[0]."""
    hb_path = tmp_path / "heartbeat.json"
    _write_heartbeat(hb_path)
    trace_file = tmp_path / "jarvis_trace.jsonl"
    trace_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "consult_id": "a",
                        "bot_id": "bot1",
                        "verdict": {},
                        "ts": "t1",
                        "final_size": 1.0,
                    }
                ),
                json.dumps(
                    {
                        "consult_id": "b",
                        "bot_id": "bot2",
                        "verdict": {},
                        "ts": "t2",
                        "final_size": 0.5,
                    }
                ),
                json.dumps(
                    {
                        "consult_id": "c",
                        "bot_id": "bot3",
                        "verdict": {},
                        "ts": "t3",
                        "final_size": 0.0,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with patch(
        "eta_engine.brain.jarvis_v3.trace_emitter.DEFAULT_TRACE_PATH",
        trace_file,
    ):
        out = jarvis_supervisor_bot_accounts(heartbeat_path=hb_path)
    assert out, "bridge returned empty accounts"
    tail = out[0].get("jarvis_trace_tail")
    assert tail is not None, f"no trace_tail in first account: {out[0].keys()}"
    assert len(tail) == 3
    ids = [r.get("consult_id") for r in tail]
    assert "a" in ids and "b" in ids and "c" in ids


def test_heartbeat_skips_trace_tail_when_file_missing(tmp_path):
    """Missing trace file → bridge silently skips trace_tail; bots still returned."""
    hb_path = tmp_path / "heartbeat.json"
    _write_heartbeat(hb_path)
    missing = tmp_path / "no_such_file.jsonl"
    with patch(
        "eta_engine.brain.jarvis_v3.trace_emitter.DEFAULT_TRACE_PATH",
        missing,
    ):
        out = jarvis_supervisor_bot_accounts(heartbeat_path=hb_path)
    assert len(out) == 1
    tail = out[0].get("jarvis_trace_tail")
    assert tail in (None, [])


def test_heartbeat_never_crashes_when_trace_emitter_raises(tmp_path, monkeypatch):
    """trace_emitter.tail() raising must not crash the heartbeat bridge."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    def boom(n=3, path=None):
        raise RuntimeError("trace tail exploded")

    monkeypatch.setattr(trace_emitter, "tail", boom)

    hb_path = tmp_path / "heartbeat.json"
    _write_heartbeat(hb_path)
    out = jarvis_supervisor_bot_accounts(heartbeat_path=hb_path)
    assert len(out) == 1
    assert out[0].get("jarvis_trace_tail") in (None, [])
