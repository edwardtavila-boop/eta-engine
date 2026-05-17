from __future__ import annotations

import json
import logging
from pathlib import Path

from eta_engine.scripts.broker_router_state import BrokerRouterStateIO


def _make_state_io(state_root: Path) -> BrokerRouterStateIO:
    return BrokerRouterStateIO(
        state_root=state_root,
        retry_meta_suffix=".retry_meta.json",
        logger=logging.getLogger("test_broker_router_state"),
    )


def test_state_io_creates_expected_subdirs_and_paths(tmp_path: Path) -> None:
    state_io = _make_state_io(tmp_path / "state")

    assert state_io.processing_dir == tmp_path / "state" / "processing"
    assert state_io.blocked_dir == tmp_path / "state" / "blocked"
    assert state_io.archive_dir == tmp_path / "state" / "archive"
    assert state_io.quarantine_dir == tmp_path / "state" / "quarantine"
    assert state_io.failed_dir == tmp_path / "state" / "failed"
    assert state_io.fill_results_dir == tmp_path / "state" / "fill_results"
    assert state_io.heartbeat_path == tmp_path / "state" / "broker_router_heartbeat.json"
    assert state_io.gate_pre_trade_path == tmp_path / "state" / "pre_trade_gate.json"
    assert state_io.gate_heat_state_path == tmp_path / "state" / "heat_state.json"
    assert state_io.gate_journal_path == tmp_path / "state" / "gate_journal.sqlite"
    for path in (
        state_io.processing_dir,
        state_io.blocked_dir,
        state_io.archive_dir,
        state_io.quarantine_dir,
        state_io.failed_dir,
        state_io.fill_results_dir,
    ):
        assert path.is_dir()


def test_move_to_failed_with_meta_persists_sidecar_and_clears_processing_meta(tmp_path: Path) -> None:
    state_io = _make_state_io(tmp_path / "state")
    target = state_io.processing_dir / "alpha.pending_order.json"
    target.write_text("{}", encoding="utf-8")
    processing_meta = state_io.retry_meta_path(target)
    processing_meta.write_text(
        json.dumps({"attempts": 2, "last_attempt_ts": "2026-05-17T16:00:00+00:00"}),
        encoding="utf-8",
    )

    state_io.move_to_failed_with_meta(
        target,
        {
            "attempts": 3,
            "last_attempt_ts": "2026-05-17T16:05:00+00:00",
            "last_reject_reason": "venue_rejected",
        },
    )

    failed_target = state_io.failed_dir / target.name
    failed_meta = state_io.failed_dir / (target.name + ".retry_meta.json")
    assert failed_target.exists()
    assert not processing_meta.exists()
    payload = json.loads(failed_meta.read_text(encoding="utf-8"))
    assert payload["attempts"] == 3
    assert payload["last_reject_reason"] == "venue_rejected"
