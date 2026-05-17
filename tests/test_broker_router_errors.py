from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from eta_engine.scripts.broker_router_errors import BrokerRouterErrorHandlers


def _make_helper(
    tmp_path: Path,
) -> tuple[
    BrokerRouterErrorHandlers,
    dict[str, int],
    list[tuple[str, str, str]],
    list[dict[str, Any]],
    list[tuple[Path, Path]],
    list[Path],
]:
    counts = {"quarantined": 0, "failed": 0}
    events: list[tuple[str, str, str]] = []
    journals: list[dict[str, Any]] = []
    moves: list[tuple[Path, Path]] = []
    cleared: list[Path] = []

    helper = BrokerRouterErrorHandlers(
        counts=counts,
        dry_run=False,
        quarantine_dir=tmp_path / "quarantine",
        failed_dir=tmp_path / "failed",
        atomic_move=lambda src, dst: moves.append((src, dst)),
        clear_retry_meta=lambda path: cleared.append(path),
        record_event=lambda filename, kind, detail: events.append((filename, kind, detail)),
        safe_journal=lambda **kwargs: journals.append(kwargs),
    )
    return helper, counts, events, journals, moves, cleared


def _order() -> Any:
    return SimpleNamespace(
        bot_id="alpha",
        signal_id="sig-1",
        to_dict=lambda: {"bot_id": "alpha", "signal_id": "sig-1", "symbol": "MNQ"},
    )


def test_handle_routing_config_unsupported_quarantines_and_journals(tmp_path: Path) -> None:
    helper, counts, events, journals, moves, cleared = _make_helper(tmp_path)
    target = tmp_path / "alpha.pending_order.json"

    helper.handle_routing_config_unsupported(_order(), target, "unsupported pair")

    assert counts["quarantined"] == 1
    assert events == [(target.name, "quarantined", "routing_config_unsupported_pair")]
    assert moves == [(target, tmp_path / "quarantine" / target.name)]
    assert cleared == [target]
    assert journals[0]["intent"] == "pending_order_quarantined"
    assert journals[0]["metadata"]["reason"] == "routing_config_unsupported_pair"


def test_handle_dormant_broker_and_routing_error_fail_closed(tmp_path: Path) -> None:
    helper, counts, events, journals, moves, cleared = _make_helper(tmp_path)
    target = tmp_path / "alpha.pending_order.json"

    helper.handle_dormant_broker(_order(), target, "tradovate")
    helper.handle_routing_error(_order(), target, "choose_venue failed: boom")

    assert counts["failed"] == 2
    assert events[0][1] == "broker_dormant"
    assert events[1][1] == "routing_error"
    assert moves[0] == (target, tmp_path / "failed" / target.name)
    assert moves[1] == (target, tmp_path / "failed" / target.name)
    assert cleared == [target, target]
    assert journals[0]["intent"] == "pending_order_broker_dormant"
    assert journals[1]["intent"] == "pending_order_routing_error"


def test_handle_processing_error_marks_failed_without_order_context(tmp_path: Path) -> None:
    helper, counts, events, journals, moves, cleared = _make_helper(tmp_path)
    target = tmp_path / "alpha.pending_order.json"

    helper.handle_processing_error(target, "parse_pending_file raised: bad payload")

    assert counts["failed"] == 1
    assert events == [(target.name, "processing_error", "parse_pending_file raised: bad payload")]
    assert moves == [(target, tmp_path / "failed" / target.name)]
    assert cleared == [target]
    assert journals[0]["intent"] == "pending_order_processing_error"
    assert journals[0]["metadata"]["path"] == str(target)
