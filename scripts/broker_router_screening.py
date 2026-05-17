from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from eta_engine.obs.decision_journal import Actor, Outcome

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping
    from logging import Logger


class _OrderLike(Protocol):
    signal_id: str
    bot_id: str

    def to_dict(self) -> dict[str, Any]: ...


class BrokerRouterScreening:
    """Own parse quarantine, local deny checks, and blocked-file handling."""

    def __init__(
        self,
        *,
        counts: MutableMapping[str, int],
        dry_run: bool,
        quarantine_dir: Path,
        blocked_dir: Path,
        parse_pending_file: Callable[[Path], _OrderLike],
        pending_order_sanity_denial: Callable[[_OrderLike], str],
        readiness_denial: Callable[[_OrderLike], str],
        daily_loss_killswitch_denial: Callable[[_OrderLike], dict[str, Any] | None],
        atomic_move: Callable[[Path, Path], None],
        clear_retry_meta: Callable[[Path], None],
        write_sidecar: Callable[[Path, dict[str, Any]], None],
        record_event: Callable[[str, str, str], None],
        safe_journal: Callable[..., None],
        handle_processing_error: Callable[[Path, str], None],
        logger: Logger,
    ) -> None:
        self._counts = counts
        self._dry_run = bool(dry_run)
        self._quarantine_dir = Path(quarantine_dir)
        self._blocked_dir = Path(blocked_dir)
        self._parse_pending_file = parse_pending_file
        self._pending_order_sanity_denial = pending_order_sanity_denial
        self._readiness_denial = readiness_denial
        self._daily_loss_killswitch_denial = daily_loss_killswitch_denial
        self._atomic_move = atomic_move
        self._clear_retry_meta = clear_retry_meta
        self._write_sidecar = write_sidecar
        self._record_event = record_event
        self._safe_journal = safe_journal
        self._handle_processing_error = handle_processing_error
        self._logger = logger

    def parse_target(self, target: Path) -> _OrderLike | None:
        """Parse a pending file or consume the failure with quarantine/journal."""
        try:
            order = self._parse_pending_file(target)
        except ValueError as exc:
            self._counts["quarantined"] += 1
            self._record_event(target.name, "quarantined", str(exc))
            if not self._dry_run:
                with contextlib.suppress(OSError):
                    self._atomic_move(target, self._quarantine_dir / target.name)
                self._clear_retry_meta(target)
            self._safe_journal(
                actor=Actor.STRATEGY_ROUTER,
                intent="pending_order_quarantined",
                rationale=f"parse failed: {exc}",
                outcome=Outcome.NOTED,
                links=[f"file:{target.name}"],
                metadata={"path": str(target), "error": str(exc)},
            )
            return None
        except Exception as exc:  # noqa: BLE001
            self._handle_processing_error(target, f"parse_pending_file raised: {exc}")
            return None
        self._counts["parsed"] += 1
        return order

    def local_denial(
        self,
        order: _OrderLike,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
        """Return the first local deny plus its synthetic gate summary, if any."""
        sanity_denial = self._pending_order_sanity_denial(order)
        if sanity_denial:
            denied = {
                "gate": "pending_order_sanity",
                "allow": False,
                "reason": sanity_denial,
                "context": {"order": order.to_dict()},
            }
            return denied, [denied], ["-pending_order_sanity"]

        readiness_denial = self._readiness_denial(order)
        if readiness_denial:
            denied = {
                "gate": "strategy_readiness",
                "allow": False,
                "reason": readiness_denial,
                "context": {"order": order.to_dict()},
            }
            return denied, [denied], ["-strategy_readiness"]

        killswitch_denial = self._daily_loss_killswitch_denial(order)
        if killswitch_denial:
            return killswitch_denial, [killswitch_denial], ["-daily_loss_killswitch"]
        return None, [], []

    def handle_blocked(
        self,
        order: _OrderLike,
        target: Path,
        denied: dict[str, Any],
        gate_results: list[dict[str, Any]],
        gate_checks_summary: list[str],
    ) -> None:
        """Move to blocked/, emit sidecar, and journal a BLOCKED event."""
        is_import_failed = denied["gate"] == "gate_chain_import_failed"
        self._counts["blocked"] += 1
        self._record_event(target.name, "blocked", denied["gate"])
        block_meta = {
            "denied_gate": denied["gate"],
            "reason": ("gate_chain_import_failed" if is_import_failed else denied["reason"]),
            "context": denied["context"],
            "all_gates": gate_results,
            "order": order.to_dict(),
        }
        if not self._dry_run:
            self._write_sidecar(
                self._blocked_dir / f"{order.signal_id}_block.json",
                block_meta,
            )
            with contextlib.suppress(OSError):
                self._atomic_move(target, self._blocked_dir / target.name)
            self._clear_retry_meta(target)
        intent = "gate_chain_import_failed" if is_import_failed else "pending_order_blocked"
        rationale = (
            f"gate_chain import failed; fail-closed DENY. detail={denied['reason']}"
            if is_import_failed
            else f"gate={denied['gate']} reason={denied['reason']}"
        )
        self._safe_journal(
            actor=Actor.STRATEGY_ROUTER,
            intent=intent,
            rationale=rationale,
            gate_checks=gate_checks_summary,
            outcome=Outcome.BLOCKED,
            links=[f"signal:{order.signal_id}", f"bot:{order.bot_id}"],
            metadata=block_meta,
        )
