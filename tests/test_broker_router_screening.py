from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from eta_engine.obs.decision_journal import Outcome
from eta_engine.scripts.broker_router_screening import BrokerRouterScreening


def _make_helper(tmp_path: Path):
    counts = {"quarantined": 0, "parsed": 0, "blocked": 0}
    moved: list[tuple[Path, Path]] = []
    cleared: list[Path] = []
    sidecars: dict[Path, dict[str, Any]] = {}
    events: list[tuple[str, str, str]] = []
    journals: list[dict[str, Any]] = []
    processing_errors: list[tuple[Path, str]] = []

    helper = BrokerRouterScreening(
        counts=counts,
        dry_run=False,
        quarantine_dir=tmp_path / "quarantine",
        blocked_dir=tmp_path / "blocked",
        parse_pending_file=None,  # type: ignore[arg-type]
        pending_order_sanity_denial=lambda order: "",
        readiness_denial=lambda order: "",
        daily_loss_killswitch_denial=lambda order: None,
        atomic_move=lambda src, dst: moved.append((src, dst)),
        clear_retry_meta=lambda path: cleared.append(path),
        write_sidecar=lambda path, payload: sidecars.setdefault(path, dict(payload)),
        record_event=lambda filename, kind, detail: events.append((filename, kind, detail)),
        safe_journal=lambda **kwargs: journals.append(dict(kwargs)),
        handle_processing_error=lambda path, reason: processing_errors.append((path, reason)),
        logger=logging.getLogger("test_broker_router_screening"),
    )
    return helper, counts, moved, cleared, sidecars, events, journals, processing_errors


def test_parse_target_quarantines_value_error(tmp_path: Path) -> None:
    helper, counts, moved, cleared, _sidecars, events, journals, _processing_errors = _make_helper(tmp_path)
    helper._parse_pending_file = lambda path: (_ for _ in ()).throw(ValueError("bad json"))  # type: ignore[attr-defined]
    target = tmp_path / "processing" / "alpha.pending_order.json"

    order = helper.parse_target(target)

    assert order is None
    assert counts["quarantined"] == 1
    assert moved == [(target, tmp_path / "quarantine" / target.name)]
    assert cleared == [target]
    assert events == [("alpha.pending_order.json", "quarantined", "bad json")]
    assert journals[0]["intent"] == "pending_order_quarantined"
    assert journals[0]["outcome"] == Outcome.NOTED


def test_local_denial_prefers_pending_order_sanity(tmp_path: Path) -> None:
    helper, _counts, _moved, _cleared, _sidecars, _events, _journals, _processing_errors = _make_helper(tmp_path)
    helper._pending_order_sanity_denial = lambda order: "smoke artifact"  # type: ignore[attr-defined]
    helper._readiness_denial = lambda order: "should not run"  # type: ignore[attr-defined]
    order = SimpleNamespace(signal_id="sig-1", bot_id="alpha", to_dict=lambda: {"bot_id": "alpha"})

    denied, gate_results, gate_summary = helper.local_denial(order)

    assert denied is not None
    assert denied["gate"] == "pending_order_sanity"
    assert gate_results == [denied]
    assert gate_summary == ["-pending_order_sanity"]


def test_handle_blocked_writes_sidecar_and_journals(tmp_path: Path) -> None:
    helper, counts, moved, cleared, sidecars, events, journals, _processing_errors = _make_helper(tmp_path)
    order = SimpleNamespace(signal_id="sig-1", bot_id="alpha", to_dict=lambda: {"bot_id": "alpha"})
    denied = {"gate": "strategy_readiness", "reason": "not approved", "context": {"order": {"bot_id": "alpha"}}}
    target = tmp_path / "processing" / "alpha.pending_order.json"

    helper.handle_blocked(order, target, denied, [denied], ["-strategy_readiness"])

    assert counts["blocked"] == 1
    assert events == [("alpha.pending_order.json", "blocked", "strategy_readiness")]
    sidecar_path = tmp_path / "blocked" / "sig-1_block.json"
    assert sidecar_path in sidecars
    assert moved == [(target, tmp_path / "blocked" / target.name)]
    assert cleared == [target]
    assert journals[0]["intent"] == "pending_order_blocked"
    assert journals[0]["outcome"] == Outcome.BLOCKED
