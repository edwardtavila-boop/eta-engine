"""Broker router: consumes pending_order.json files written by the strategy supervisor.

Dispatches each pending order through the gate chain, then submits via SmartRouter.
Owns long-lived venue connections so all bots share one IBKR session.

State layout under var/eta_engine/state/router/:
  pending/    -- order inbox; supervisor writes *.pending_order.json here
  processing/ -- in-flight orders (atomic-rename lock)
  blocked/    -- gate-denied orders (audit)
  archive/<YYYY-MM-DD>/ -- terminal states (filled/rejected/etc.)
  quarantine/ -- malformed JSON
  failed/     -- venue submission errors after retries
  fill_results/ -- sidecar JSONs per submitted order
  broker_router_heartbeat.json -- liveness signal

Honors env vars:
  ETA_BROKER_ROUTER_INTERVAL_S (default 5)
  ETA_BROKER_ROUTER_PENDING_DIR (default C:/EvolutionaryTradingAlgo/var/eta_engine/state/router/pending)
  ETA_BROKER_ROUTER_STATE_ROOT (default C:/EvolutionaryTradingAlgo/var/eta_engine/state/router)
  ETA_BROKER_ROUTER_DRY_RUN (default 0)
  ETA_BROKER_ROUTER_MAX_RETRIES (default 3)
  + ETA_LIVE_MODE, ETA_GATE_BOOTSTRAP, ETA_IDEMPOTENCY_STORE etc. passed
    through to underlying systems.
"""

from __future__ import annotations

# ruff: noqa: I001

import logging
import sys
from collections import deque
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eta_engine.venues.base import OrderRequest, VenueBase

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from eta_engine.core.secrets import SECRETS  # noqa: E402
from eta_engine.obs.decision_journal import (  # noqa: E402
    Actor,
    DecisionJournal,
    Outcome,
    default_journal,
)
from eta_engine.scripts.broker_router_bootstrap import wire_router_bootstrap  # noqa: E402
from eta_engine.scripts.broker_router_components import wire_router_components  # noqa: E402
from eta_engine.scripts.broker_router_config import (  # noqa: E402
    RoutingConfig,
    _asset_class_for_symbol,
    normalize_symbol as _normalize_symbol,
)
from eta_engine.scripts.broker_router_entrypoint import (  # noqa: E402
    main as broker_router_main,
    resolve_dry_run,
    resolve_interval,
    resolve_max_retries,
    resolve_pending_dir,
    resolve_state_root,
)
from eta_engine.scripts.broker_router_failover import BrokerRouterFailover  # noqa: E402
from eta_engine.scripts.broker_router_pending import (  # noqa: E402
    PendingOrder as _PendingOrder,
    _normalize_futures_symbol as _normalize_futures_symbol_impl,
    parse_pending_file as _parse_pending_file,
    pending_order_sanity_denial as _pending_order_sanity_denial,
)
from eta_engine.scripts.broker_router_policy import (  # noqa: E402
    router_daily_loss_killswitch_denial as _router_daily_loss_killswitch_denial,
)
from eta_engine.scripts.broker_router_utils import (  # noqa: E402
    env_float as _env_float_impl,
    env_int as _env_int_impl,
    extract_broker_fill_ts as _extract_broker_fill_ts,
    gate_bootstrap_enabled as _gate_bootstrap_enabled,
    load_build_default_chain_for_router as _load_build_default_chain_impl,
    readiness_enforced as _readiness_enforced,
)
from eta_engine.scripts.broker_router_routing import BrokerRouterRoutingResolver  # noqa: E402
from eta_engine.scripts.broker_router_state import BrokerRouterStateIO  # noqa: E402
from eta_engine.scripts.runtime_order_hold import (  # noqa: E402
    OrderEntryHold,
    default_hold_path,
    load_order_entry_hold,
)
from eta_engine.scripts.workspace_roots import (  # noqa: E402
    ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH as _ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH,
    ETA_RUNTIME_STATE_DIR,
)
from eta_engine.venues.router import SmartRouter  # noqa: E402
# Tradovate remains DORMANT by operator policy; keep the adapter import
# available only for explicit un-dormancy / credential reactivation flows.
from eta_engine.venues.tradovate import TradovateVenue  # noqa: E402

logger = logging.getLogger("eta_engine.broker_router")

# ---------------------------------------------------------------------------
# Defaults & symbol mapping
# ---------------------------------------------------------------------------

#: Default router state root, anchored under canonical workspace state.
DEFAULT_STATE_ROOT = ETA_RUNTIME_STATE_DIR / "router"

#: Default pending-order directory. Mirrors the supervisor write path.
#: Operators may override with ETA_BROKER_ROUTER_PENDING_DIR.
DEFAULT_PENDING_DIR = DEFAULT_STATE_ROOT / "pending"

DEFAULT_INTERVAL_S = 5.0
DEFAULT_MAX_RETRIES = 3

#: Cap exponential retry backoff at 5 minutes. Formula:
#: ``min(BACKOFF_CAP_S, interval_s * 2 ** attempts)``.
BACKOFF_CAP_S = 300.0

#: Suffix for the retry-meta sidecar written next to a file in
#: ``processing/``. Schema:
#: ``{"attempts": int, "last_attempt_ts": isoformat-str,
#:   "last_reject_reason": str}``.
RETRY_META_SUFFIX = ".retry_meta.json"

#: Operator escape hatch — set ``ETA_GATE_BOOTSTRAP=1`` to allow first-run
#: operation when the gate-chain module cannot be imported. Mirrors the
#: pattern in ``firm/eta_engine/src/mnq/risk/gate_chain.py``.
_GATE_BOOTSTRAP_ENV = "ETA_GATE_BOOTSTRAP"
_READINESS_ENFORCE_ENV = "ETA_BROKER_ROUTER_ENFORCE_READINESS"
_LIVE_MONEY_ENV = "ETA_LIVE_MONEY"

_env_int = partial(_env_int_impl, logger=logger)
_env_float = partial(_env_float_impl, logger=logger)

_load_build_default_chain = partial(_load_build_default_chain_impl, root=ROOT, sys_path=sys.path)


# Routing config + pending-order parsing now live in dedicated helper modules.
# Keep this compatibility shim explicit so downstream imports do not silently
# drift while ``broker_router`` continues shrinking.
PendingOrder = _PendingOrder
normalize_symbol = _normalize_symbol
parse_pending_file = _parse_pending_file
pending_order_sanity_denial = _pending_order_sanity_denial
router_daily_loss_killswitch_denial = _router_daily_loss_killswitch_denial
_normalize_futures_symbol = _normalize_futures_symbol_impl
ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH = _ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH

__all__ = [
    "BACKOFF_CAP_S",
    "BrokerRouter",
    "DEFAULT_INTERVAL_S",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_PENDING_DIR",
    "DEFAULT_STATE_ROOT",
    "ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH",
    "PendingOrder",
    "RETRY_META_SUFFIX",
    "RoutingConfig",
    "_normalize_futures_symbol",
    "main",
    "normalize_symbol",
    "parse_pending_file",
    "pending_order_sanity_denial",
    "router_daily_loss_killswitch_denial",
]



# ---------------------------------------------------------------------------
# BrokerRouter
# ---------------------------------------------------------------------------


class BrokerRouter:
    """Long-running consumer for supervisor-emitted pending orders.

    One instance owns the SmartRouter (which owns venue sessions) and the
    decision journal. Safe to run as a single process; if multiple
    instances ever race on the same pending dir, the atomic-rename lock
    in :meth:`_process_pending_file` prevents double-submission.
    """

    def __init__(
        self,
        pending_dir: Path,
        state_root: Path,
        smart_router: SmartRouter,
        journal: DecisionJournal,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        dry_run: bool = False,
        max_retries: int = DEFAULT_MAX_RETRIES,
        gate_chain: object | None = None,
        routing_config: RoutingConfig | None = None,
        order_hold_path: Path | None = None,
    ) -> None:
        self.pending_dir = Path(pending_dir)
        self.state_root = Path(state_root)
        self.smart_router = smart_router
        self.journal = journal
        self.interval_s = max(0.5, float(interval_s))
        self.dry_run = bool(dry_run)
        self.max_retries = max(1, int(max_retries))
        # Optional override hook: tests / shadow envs can inject a callable
        # gate-chain (or object with .evaluate(**kwargs)). When None, the
        # production lazy-import path runs.
        self.gate_chain = gate_chain
        # Per-bot routing config: tests inject; production loads from YAML.
        self.routing_config = routing_config if routing_config is not None else RoutingConfig.load()
        self.order_hold_path = Path(order_hold_path) if order_hold_path else default_hold_path()
        wire_router_bootstrap(
            self,
            failover_cls=BrokerRouterFailover,
            routing_resolver_cls=BrokerRouterRoutingResolver,
            retry_meta_suffix=RETRY_META_SUFFIX,
            secrets=SECRETS,
            state_io_cls=BrokerRouterStateIO,
            tradovate_venue_cls=TradovateVenue,
            logger=logger,
        )

        self._stopped = False
        self._retry_counts: dict[str, int] = {}
        # Bounded recent-event ring for heartbeat reporting.
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=32)
        self._counts: dict[str, int] = {
            "parsed": 0,
            "blocked": 0,
            "submitted": 0,
            "filled": 0,
            "rejected": 0,
            "failed": 0,
            "quarantined": 0,
            "held": 0,
        }
        wire_router_components(
            self,
            asset_class_for_symbol=_asset_class_for_symbol,
            backoff_cap_s=BACKOFF_CAP_S,
            daily_loss_killswitch_denial=router_daily_loss_killswitch_denial,
            env_float=_env_float,
            env_int=_env_int,
            extract_broker_fill_ts=_extract_broker_fill_ts,
            gate_bootstrap_enabled=_gate_bootstrap_enabled,
            live_money_env=_LIVE_MONEY_ENV,
            load_build_default_chain=lambda: _load_build_default_chain(),
            logger=logger,
            parse_pending_file=lambda path: parse_pending_file(path),
            pending_order_sanity_denial=lambda order: pending_order_sanity_denial(order),
            readiness_enforced=_readiness_enforced,
            readiness_snapshot_path=lambda: ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH,
            retry_meta_suffix=RETRY_META_SUFFIX,
        )

    # -- lifecycle ----------------------------------------------------------

    def request_stop(self) -> None:
        """Signal the run loop to drain and exit on next iteration boundary."""
        self._runtime.request_stop()

    async def run(self) -> None:
        """Main poll loop. Stops on SIGINT/SIGTERM or :meth:`request_stop`."""
        await self._runtime.run()

    async def run_once(self) -> None:
        """Single-pass scan + heartbeat. Used by ``--once`` and tests."""
        await self._runtime.run_once()

    async def _tick(self) -> None:
        """One poll: scan pending + processing dirs, dispatch each, heartbeat."""
        await self._polling.tick(stopped=lambda: self._stopped)

    # -- per-file lifecycle -------------------------------------------------

    async def _process_pending_file(self, path: Path) -> None:
        await self._lifecycle.process_pending_file(path)

    async def _process_retry_file(self, target: Path) -> None:
        await self._lifecycle.process_retry_file_with_backoff(target, should_backoff=self._should_backoff)

    def _retry_meta_path(self, target: Path) -> Path:
        return self._state_io.retry_meta_path(target)

    def _load_retry_meta(self, target: Path) -> dict[str, Any]:
        return self._state_io.load_retry_meta(target)

    def _save_retry_meta(self, target: Path, meta: dict[str, Any]) -> None:
        self._state_io.save_retry_meta(target, meta)

    def _clear_retry_meta(self, target: Path) -> None:
        self._state_io.clear_retry_meta(target)

    def _should_backoff(self, retry_meta: dict[str, Any]) -> bool:
        return self._lifecycle.should_backoff(retry_meta)

    def _move_to_failed_with_meta(
        self,
        target: Path,
        retry_meta: dict[str, Any],
    ) -> None:
        self._state_io.move_to_failed_with_meta(target, retry_meta)

    async def _run_lifecycle(
        self,
        target: Path,
        *,
        retry_meta: dict[str, Any],
    ) -> None:
        """Shared parse->gate->submit pipeline. Used for fresh + retry paths."""
        order = self._screening.parse_target(target)
        if order is None:
            return

        denied, local_gate_results, local_gate_summary = self._screening.local_denial(order)
        if denied is not None:
            self._handle_blocked(order, target, denied, local_gate_results, local_gate_summary)
            return

        # 4. Gate-chain evaluation.
        try:
            gate_results = await self._evaluate_gates(order)
        except Exception as exc:  # noqa: BLE001
            self._handle_routing_error(order, target, f"gate evaluation failed: {exc}")
            return
        gate_checks_summary = [("+" if r["allow"] else "-") + r["gate"] for r in gate_results]
        denied = next((r for r in gate_results if not r["allow"]), None)
        if denied is not None:
            self._handle_blocked(order, target, denied, gate_results, gate_checks_summary)
            return

        # 5. Resolve venue/request or consume the failure/dry-run locally.
        prepared = self._resolution.prepare_submission(
            order,
            target,
            gate_checks_summary,
        )
        if prepared is None:
            return
        venue, request = prepared

        # 8. Submit. Venue handles its own idempotency / fleet / cap gates.
        await self._submit_and_finalize(
            order,
            target,
            venue,
            request,
            gate_checks_summary,
            retry_meta=retry_meta,
        )

    def _handle_blocked(
        self,
        order: PendingOrder,
        target: Path,
        denied: dict[str, Any],
        gate_results: list[dict[str, Any]],
        gate_checks_summary: list[str],
    ) -> None:
        self._screening.handle_blocked(order, target, denied, gate_results, gate_checks_summary)

    def _resolve_venue_adapter(
        self,
        venue_name: str,
        order: PendingOrder,
    ) -> VenueBase | None:
        return self._routing.resolve_venue_adapter(venue_name, order)

    def _resolve_prop_account_venue(self, account: dict[str, str]) -> VenueBase | None:
        return self._routing.resolve_prop_account_venue(account)

    def _handle_routing_config_unsupported(
        self,
        order: PendingOrder,
        target: Path,
        reason: str,
    ) -> None:
        self._errors.handle_routing_config_unsupported(order, target, reason)

    def _handle_dormant_broker(
        self,
        order: PendingOrder,
        target: Path,
        venue_name: str,
    ) -> None:
        self._errors.handle_dormant_broker(order, target, venue_name)

    def _handle_processing_error(self, target: Path, reason: str) -> None:
        self._errors.handle_processing_error(target, reason)

    def _hold_blocks_file(self, path: Path) -> bool:
        """Return True when the runtime hold blocks this pending order."""
        return self._polling.hold_blocks_file(path)

    #: Exception substrings that mark a TRANSIENT venue failure (i.e.
    #: worth retrying on the next venue in the chain). Anything not
    #: matching is treated as deterministic and stays on the same venue
    #: so the existing retry-meta machinery applies.
    _TRANSIENT_FAILURE_TOKENS: tuple[str, ...] = (
        "timeout",
        "timed out",
        "connection",
        "connectionerror",
        "network",
        "unreachable",
        "reset by peer",
        "temporarily",
        "503",
        "502",
        "504",
        "gateway",
    )

    @classmethod
    def _is_transient_failure(cls, exc: BaseException) -> bool:
        """True iff the exception text looks like a transport-level glitch.

        Exact-class matches for timeout / connection-error subclasses
        cover the common case; the fallback string scan picks up venue
        SDK exceptions that don't subclass anything standard.
        """
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return True
        msg = str(exc).lower()
        return any(token in msg for token in cls._TRANSIENT_FAILURE_TOKENS)

    async def _place_with_failover_chain(
        self,
        order: PendingOrder,
        primary: VenueBase,
        request: OrderRequest,
    ) -> tuple[Any, VenueBase]:
        return await self._failover.place_with_failover_chain(order, primary, request)

    def _next_chain_venue(
        self,
        chain: tuple[str, ...],
        idx: int,
        order: PendingOrder,
    ) -> VenueBase | None:
        return self._failover.next_chain_venue(chain, idx, order)

    def _venue_circuit(self, venue_name: str) -> object | None:
        return self._failover.venue_circuit(venue_name)

    def venue_circuit_states(self) -> dict[str, str]:
        return self._failover.venue_circuit_states()

    async def _submit_and_finalize(
        self,
        order: PendingOrder,
        target: Path,
        venue: VenueBase,
        request: OrderRequest,
        gate_checks_summary: list[str],
        *,
        retry_meta: dict[str, Any],
    ) -> None:
        await self._submission.submit_and_finalize(
            order,
            target,
            venue,
            request,
            gate_checks_summary,
            retry_meta=retry_meta,
        )

    def _handle_routing_error(
        self,
        order: PendingOrder,
        target: Path,
        reason: str,
    ) -> None:
        self._errors.handle_routing_error(order, target, reason)

    # -- gate chain ---------------------------------------------------------

    async def _evaluate_gates(self, order: PendingOrder) -> list[dict[str, Any]]:
        return self._gates.evaluate_gates(order, getattr(self, "gate_chain", None))

    def _invoke_gate_chain_override(
        self,
        override: object,
        order: PendingOrder,
    ) -> tuple[bool, list[object]]:
        return self._gates.invoke_gate_chain_override(override, order)

    def _collect_open_positions(self) -> dict[str, int]:
        return self._gates.collect_open_positions()

    def _readiness_denial(self, order: PendingOrder) -> str:
        return self._gates.readiness_denial(order)

    def _sync_gate_state(
        self,
        *,
        hold: OrderEntryHold,
        open_positions: dict[str, int],
    ) -> None:
        self._ops.sync_gate_state(hold=hold, open_positions=open_positions)

    def _heat_state_snapshot(
        self,
        *,
        now_iso: str,
        open_positions: dict[str, int],
    ) -> dict[str, Any]:
        return self._ops.heat_state_snapshot(now_iso=now_iso, open_positions=open_positions)

    def _ensure_gate_journal(self) -> None:
        self._ops.ensure_gate_journal()

    @staticmethod
    def _normalize_gate_result(r: object) -> dict[str, Any]:
        """Coerce a GateResult-shaped object into the dict shape we use."""
        return {
            "gate": getattr(r, "gate", ""),
            "allow": bool(getattr(r, "allow", False)),
            "reason": getattr(r, "reason", "") or "",
            "context": dict(getattr(r, "context", {}) or {}),
        }

    # -- IO helpers ---------------------------------------------------------

    def _atomic_move(self, src: Path, dst: Path) -> None:
        self._state_io.atomic_move(src, dst)

    def _write_sidecar(self, path: Path, payload: dict[str, Any]) -> None:
        self._state_io.write_sidecar(path, payload)

    def _order_entry_hold(self) -> OrderEntryHold:
        """Load the shared operator order-entry hold state."""
        return load_order_entry_hold(self.order_hold_path)

    def _emit_heartbeat(self, *, hold: OrderEntryHold | None = None) -> None:
        self._ops.emit_heartbeat(hold=hold)

    def _record_event(self, filename: str, kind: str, detail: str) -> None:
        self._reporting.record_event(filename, kind, detail)

    def _safe_journal(
        self,
        *,
        actor: Actor,
        intent: str,
        rationale: str = "",
        gate_checks: list[str] | None = None,
        outcome: Outcome = Outcome.NOTED,
        links: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._reporting.safe_journal(
            actor=actor,
            intent=intent,
            rationale=rationale,
            gate_checks=gate_checks,
            outcome=outcome,
            links=links,
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


_resolve_pending_dir = partial(resolve_pending_dir, default_pending_dir=DEFAULT_PENDING_DIR)
_resolve_state_root = partial(resolve_state_root, default_state_root=DEFAULT_STATE_ROOT)
_resolve_interval = partial(resolve_interval, default_interval_s=DEFAULT_INTERVAL_S, logger=logger)
_resolve_dry_run = resolve_dry_run
_resolve_max_retries = partial(resolve_max_retries, default_max_retries=DEFAULT_MAX_RETRIES)

_broker_router_main = partial(
    broker_router_main,
    description=__doc__.split("\n", 1)[0],
    default_pending_dir=DEFAULT_PENDING_DIR,
    default_state_root=DEFAULT_STATE_ROOT,
    default_interval_s=DEFAULT_INTERVAL_S,
    default_max_retries=DEFAULT_MAX_RETRIES,
    broker_router_cls=BrokerRouter,
    smart_router_cls=SmartRouter,
    default_journal_factory=default_journal,
    logger=logger,
)


def main(argv: list[str] | None = None) -> int:
    return _broker_router_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
