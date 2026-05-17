from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from eta_engine.obs.decision_journal import Actor, Outcome
from eta_engine.venues.base import OrderRequest, OrderType, Side

if TYPE_CHECKING:
    from collections.abc import Callable, Collection
    from logging import Logger


class _RoutingConfigLike(Protocol):
    def venue_for(self, bot_id: str, *, symbol: str) -> str: ...

    def map_symbol(self, symbol: str, venue_name: str) -> str: ...

    def prop_account_for(self, bot_id: str) -> dict[str, str] | None: ...


class _SmartRouterLike(Protocol):
    def choose_venue(
        self,
        symbol: str,
        qty: float,
        urgency: str = "normal",
    ) -> object: ...


class _OrderLike(Protocol):
    signal_id: str
    bot_id: str
    symbol: str
    qty: float
    side: str
    limit_price: float
    stop_price: float | None
    target_price: float | None
    reduce_only: bool


class _VenueLike(Protocol):
    name: str


class BrokerRouterResolution:
    """Own pre-submit venue resolution, request building, and dry-run exits."""

    def __init__(
        self,
        *,
        routing_config: _RoutingConfigLike,
        smart_router: _SmartRouterLike,
        dry_run: bool,
        dormant_brokers: Collection[str],
        resolve_venue_adapter: Callable[[str, _OrderLike], object | None],
        resolve_prop_account_venue: Callable[[dict[str, str]], object | None],
        handle_blocked: Callable[[_OrderLike, Path, dict[str, Any], list[dict[str, Any]], list[str]], None],
        handle_routing_config_unsupported: Callable[[_OrderLike, Path, str], None],
        handle_routing_error: Callable[[_OrderLike, Path, str], None],
        handle_dormant_broker: Callable[[_OrderLike, Path, str], None],
        prop_order_risk_denial: Callable[[_OrderLike, dict[str, str]], dict[str, Any] | None],
        safe_journal: Callable[..., None],
        logger: Logger,
    ) -> None:
        self._routing_config = routing_config
        self._smart_router = smart_router
        self._dry_run = bool(dry_run)
        self._dormant_brokers = {str(name).lower() for name in dormant_brokers}
        self._resolve_venue_adapter = resolve_venue_adapter
        self._resolve_prop_account_venue = resolve_prop_account_venue
        self._handle_blocked = handle_blocked
        self._handle_routing_config_unsupported = handle_routing_config_unsupported
        self._handle_routing_error = handle_routing_error
        self._handle_dormant_broker = handle_dormant_broker
        self._prop_order_risk_denial = prop_order_risk_denial
        self._safe_journal = safe_journal
        self._logger = logger

    def prepare_submission(
        self,
        order: _OrderLike,
        target: Path,
        gate_checks_summary: list[str],
    ) -> tuple[object, OrderRequest] | None:
        """Resolve venue + request, or consume the failure/dry-run locally."""
        try:
            target_venue_name = self._routing_config.venue_for(
                order.bot_id,
                symbol=order.symbol,
            )
            venue_symbol = self._routing_config.map_symbol(
                order.symbol,
                target_venue_name,
            )
            prop_account = self._routing_config.prop_account_for(order.bot_id)
        except ValueError as exc:
            self._handle_routing_config_unsupported(order, target, str(exc))
            return None

        if prop_account is not None:
            denied = self._prop_order_risk_denial(order, prop_account)
            if denied is not None:
                self._handle_blocked(
                    order,
                    target,
                    denied,
                    [denied],
                    ["-prop_risk_governor"],
                )
                return None
        if str(target_venue_name).lower() in self._dormant_brokers:
            self._handle_dormant_broker(order, target, target_venue_name)
            return None

        if prop_account is not None:
            try:
                venue = self._resolve_prop_account_venue(prop_account)
            except ValueError as exc:
                self._handle_routing_error(order, target, str(exc))
                return None
        else:
            venue = self._resolve_venue_adapter(target_venue_name, order)
        if venue is None:
            try:
                venue = self._smart_router.choose_venue(
                    order.symbol,
                    order.qty,
                    urgency="normal",
                )
            except Exception as exc:  # noqa: BLE001
                self._handle_routing_error(
                    order,
                    target,
                    f"choose_venue failed: {exc}",
                )
                return None

        side_enum = Side.BUY if order.side == "BUY" else Side.SELL
        request = OrderRequest(
            symbol=venue_symbol,
            side=side_enum,
            qty=order.qty,
            order_type=OrderType.LIMIT,
            price=order.limit_price,
            client_order_id=order.signal_id,
            bot_id=order.bot_id,
            stop_price=order.stop_price,
            target_price=order.target_price,
            reduce_only=order.reduce_only,
        )

        if self._dry_run:
            venue_name = getattr(venue, "name", target_venue_name)
            self._logger.info(
                "[dry_run] would submit signal=%s bot=%s venue=%s symbol=%s side=%s qty=%s limit=%s",
                order.signal_id,
                order.bot_id,
                venue_name,
                venue_symbol,
                order.side,
                order.qty,
                order.limit_price,
            )
            self._safe_journal(
                actor=Actor.STRATEGY_ROUTER,
                intent="pending_order_dry_run",
                rationale="dry_run=True; no venue submission",
                gate_checks=gate_checks_summary,
                outcome=Outcome.NOTED,
                links=[f"signal:{order.signal_id}", f"bot:{order.bot_id}"],
                metadata={"venue": venue_name, "venue_symbol": venue_symbol},
            )
            return None
        return venue, request
