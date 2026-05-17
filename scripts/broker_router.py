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
import os
import sys
from collections import deque
from collections.abc import Callable  # noqa: TC003 -- runtime annotation on lazy-loader return
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eta_engine.venues.base import OrderRequest, VenueBase

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from eta_engine.core.execution_lanes import (  # noqa: E402
    daily_loss_gate_mode_for_lane,
    gate_advisory,
    gate_inactive,
)
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
    load_build_default_chain,
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


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        logger.warning("invalid integer env %s=%r; using %s", name, os.environ.get(name), default)
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        logger.warning("invalid float env %s=%r; using %s", name, os.environ.get(name), default)
        return default


def _gate_bootstrap_enabled() -> bool:
    """True iff ``ETA_GATE_BOOTSTRAP=1`` is set in the environment."""
    return os.environ.get(_GATE_BOOTSTRAP_ENV, "").strip() == "1"


def router_daily_loss_killswitch_denial(order: PendingOrder) -> dict[str, Any] | None:
    """Return a router-side daily-loss denial for new entries.

    The supervisor also checks this, but the router is the last process
    before the broker. Enforcing here protects against stale pending files
    and paper-live advisory supervisor modes. Reduce-only exits remain
    allowed so already-open risk can still be flattened.
    """
    if order.reduce_only:
        return None
    gate_mode = str(order.daily_loss_gate_mode or "").strip().lower()
    if not gate_mode:
        gate_mode = daily_loss_gate_mode_for_lane(order.execution_lane)
    if gate_advisory(gate_mode) or gate_inactive(gate_mode):
        return None
    try:
        from eta_engine.scripts.daily_loss_killswitch import (  # noqa: PLC0415
            is_killswitch_tripped,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "gate": "daily_loss_killswitch",
            "allow": False,
            "reason": f"daily_loss_killswitch_unavailable:{type(exc).__name__}:{exc}",
            "context": {"order": order.to_dict()},
        }
    try:
        tripped, reason = is_killswitch_tripped()
    except Exception as exc:  # noqa: BLE001
        return {
            "gate": "daily_loss_killswitch",
            "allow": False,
            "reason": f"daily_loss_killswitch_error:{type(exc).__name__}:{exc}",
            "context": {"order": order.to_dict()},
        }
    if not tripped:
        return None
    return {
        "gate": "daily_loss_killswitch",
        "allow": False,
        "reason": str(reason),
        "context": {"order": order.to_dict()},
    }


def _readiness_enforced() -> bool:
    """True iff broker routing must honor the strategy-readiness matrix."""
    return os.environ.get(_READINESS_ENFORCE_ENV, "").strip() == "1"


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _load_build_default_chain() -> Callable[..., object]:
    """Compatibility shim for the extracted entrypoint import helper."""
    return load_build_default_chain(root=ROOT, sys_path=sys.path)


# Routing config + pending-order parsing now live in dedicated helper modules.
# Keep this compatibility shim explicit so downstream imports do not silently
# drift while ``broker_router`` continues shrinking.
_COMPAT_EXPORTS: dict[str, object] = {
    "PendingOrder": _PendingOrder,
    "normalize_symbol": _normalize_symbol,
    "parse_pending_file": _parse_pending_file,
    "pending_order_sanity_denial": _pending_order_sanity_denial,
    "_normalize_futures_symbol": _normalize_futures_symbol_impl,
    "ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH": _ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH,
}
globals().update(_COMPAT_EXPORTS)

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


def _first_nonempty_text(*values: object) -> str:
    """Return the first non-empty stringified value."""
    for value in values:
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _extract_broker_fill_ts(result: object) -> str:
    """Best-effort broker fill timestamp from an OrderResult.

    Prefer the canonical ``OrderResult.filled_at`` field when the venue layer
    exposes it. Older adapters still stash timing hints under ``raw``; keep
    those legacy fallbacks so downstream telemetry can distinguish broker fill
    time from router sidecar write time during cutover.
    """
    canonical = _first_nonempty_text(getattr(result, "filled_at", None))
    if canonical:
        return canonical
    raw = getattr(result, "raw", None)
    if not isinstance(raw, dict):
        return ""
    server = raw.get("server") if isinstance(raw.get("server"), dict) else {}
    direct = _first_nonempty_text(
        raw.get("filled_at"),
        raw.get("execution_time"),
        raw.get("executed_at"),
        server.get("filled-at"),
        server.get("filled_at"),
        server.get("execution-time"),
        server.get("execution_time"),
        server.get("executed-at"),
        server.get("executed_at"),
        server.get("updated-at"),
        server.get("updated_at"),
    )
    if direct:
        return direct
    ib_statuses = raw.get("ib_statuses")
    if isinstance(ib_statuses, list):
        for item in ib_statuses:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "").strip().lower() != "filled":
                continue
            candidate = _first_nonempty_text(
                item.get("filled_at"),
                item.get("execution_time"),
                item.get("executed_at"),
                item.get("time"),
                item.get("timestamp"),
                item.get("lastFillTime"),
            )
            if candidate:
                return candidate
    return ""


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


def _resolve_pending_dir(arg: str | None) -> Path:
    return resolve_pending_dir(arg, default_pending_dir=DEFAULT_PENDING_DIR)


def _resolve_state_root(arg: str | None) -> Path:
    return resolve_state_root(arg, default_state_root=DEFAULT_STATE_ROOT)


def _resolve_interval(arg: float | None) -> float:
    return resolve_interval(arg, default_interval_s=DEFAULT_INTERVAL_S, logger=logger)


def _resolve_dry_run(arg: bool) -> bool:
    return resolve_dry_run(arg)


def _resolve_max_retries(arg: int | None) -> int:
    return resolve_max_retries(arg, default_max_retries=DEFAULT_MAX_RETRIES)


def main(argv: list[str] | None = None) -> int:
    return broker_router_main(
        argv,
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


if __name__ == "__main__":
    raise SystemExit(main())
