from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from eta_engine.obs.decision_journal import Outcome
from eta_engine.scripts.broker_router_resolution import BrokerRouterResolution
from eta_engine.venues.base import OrderType, Side


class _RoutingConfig:
    def __init__(
        self,
        *,
        venue_name: str = "ibkr",
        venue_symbol: str = "MNQ",
        prop_account: dict[str, str] | None = None,
        error: str = "",
    ) -> None:
        self._venue_name = venue_name
        self._venue_symbol = venue_symbol
        self._prop_account = prop_account
        self._error = error

    def venue_for(self, bot_id: str, *, symbol: str) -> str:
        _ = (bot_id, symbol)
        if self._error:
            raise ValueError(self._error)
        return self._venue_name

    def map_symbol(self, symbol: str, venue_name: str) -> str:
        _ = (symbol, venue_name)
        return self._venue_symbol

    def prop_account_for(self, bot_id: str) -> dict[str, str] | None:
        _ = bot_id
        return self._prop_account


class _SmartRouter:
    def __init__(self, *, venue: object | None = None) -> None:
        self._venue = venue
        self.choose_venue_calls: list[tuple[str, float, str]] = []

    def choose_venue(
        self,
        symbol: str,
        qty: float,
        urgency: str = "normal",
    ) -> object:
        self.choose_venue_calls.append((symbol, qty, urgency))
        if self._venue is None:
            raise RuntimeError("no fallback venue")
        return self._venue


def _order(**overrides: Any) -> Any:
    payload = {
        "signal_id": "sig-1",
        "bot_id": "alpha",
        "symbol": "MNQ",
        "qty": 1.0,
        "side": "BUY",
        "limit_price": 25_000.0,
        "stop_price": 24_900.0,
        "target_price": 25_100.0,
        "reduce_only": False,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def test_prepare_submission_builds_request_with_brackets_and_reduce_only(tmp_path: Path) -> None:
    venue = SimpleNamespace(name="ibkr")
    helper = BrokerRouterResolution(
        routing_config=_RoutingConfig(venue_name="ibkr", venue_symbol="MNQM6"),
        smart_router=_SmartRouter(),
        dry_run=False,
        dormant_brokers=set(),
        resolve_venue_adapter=lambda venue_name, order: venue,
        resolve_prop_account_venue=lambda account: None,
        handle_blocked=lambda *args, **kwargs: None,
        handle_routing_config_unsupported=lambda *args, **kwargs: None,
        handle_routing_error=lambda *args, **kwargs: None,
        handle_dormant_broker=lambda *args, **kwargs: None,
        prop_order_risk_denial=lambda order, account: None,
        safe_journal=lambda **kwargs: None,
        logger=logging.getLogger("test_broker_router_resolution"),
    )

    prepared = helper.prepare_submission(
        _order(side="SELL", reduce_only=True),
        tmp_path / "alpha.pending_order.json",
        ["+heartbeat"],
    )

    assert prepared is not None
    resolved_venue, request = prepared
    assert resolved_venue is venue
    assert request.symbol == "MNQM6"
    assert request.side is Side.SELL
    assert request.order_type is OrderType.LIMIT
    assert request.price == 25_000.0
    assert request.stop_price == 24_900.0
    assert request.target_price == 25_100.0
    assert request.reduce_only is True


def test_prepare_submission_dry_run_uses_choose_venue_fallback_and_journals(tmp_path: Path) -> None:
    venue = SimpleNamespace(name="fallback")
    smart_router = _SmartRouter(venue=venue)
    journal_calls: list[dict[str, Any]] = []
    helper = BrokerRouterResolution(
        routing_config=_RoutingConfig(venue_name="ibkr", venue_symbol="MNQ"),
        smart_router=smart_router,
        dry_run=True,
        dormant_brokers=set(),
        resolve_venue_adapter=lambda venue_name, order: None,
        resolve_prop_account_venue=lambda account: None,
        handle_blocked=lambda *args, **kwargs: None,
        handle_routing_config_unsupported=lambda *args, **kwargs: None,
        handle_routing_error=lambda *args, **kwargs: None,
        handle_dormant_broker=lambda *args, **kwargs: None,
        prop_order_risk_denial=lambda order, account: None,
        safe_journal=lambda **kwargs: journal_calls.append(dict(kwargs)),
        logger=logging.getLogger("test_broker_router_resolution"),
    )

    prepared = helper.prepare_submission(
        _order(),
        tmp_path / "alpha.pending_order.json",
        ["+heartbeat"],
    )

    assert prepared is None
    assert smart_router.choose_venue_calls == [("MNQ", 1.0, "normal")]
    assert journal_calls[0]["intent"] == "pending_order_dry_run"
    assert journal_calls[0]["outcome"] == Outcome.NOTED
    assert journal_calls[0]["metadata"] == {"venue": "fallback", "venue_symbol": "MNQ"}


def test_prepare_submission_blocks_prop_risk_before_venue_resolution(tmp_path: Path) -> None:
    blocked_calls: list[tuple[Any, Path, dict[str, Any], list[dict[str, Any]], list[str]]] = []
    prop_account = {"alias": "demo", "venue": "tradovate"}
    helper = BrokerRouterResolution(
        routing_config=_RoutingConfig(
            venue_name="tradovate",
            venue_symbol="MNQ",
            prop_account=prop_account,
        ),
        smart_router=_SmartRouter(),
        dry_run=False,
        dormant_brokers=set(),
        resolve_venue_adapter=lambda venue_name, order: (_ for _ in ()).throw(AssertionError("should not resolve venue")),
        resolve_prop_account_venue=lambda account: (_ for _ in ()).throw(AssertionError("should not build venue")),
        handle_blocked=lambda *args: blocked_calls.append(args),
        handle_routing_config_unsupported=lambda *args, **kwargs: None,
        handle_routing_error=lambda *args, **kwargs: None,
        handle_dormant_broker=lambda *args, **kwargs: None,
        prop_order_risk_denial=lambda order, account: {
            "gate": "prop_risk_governor",
            "allow": False,
            "reason": "prop_risk_exceeds_headroom",
            "context": {"alias": account["alias"]},
        },
        safe_journal=lambda **kwargs: None,
        logger=logging.getLogger("test_broker_router_resolution"),
    )

    prepared = helper.prepare_submission(
        _order(),
        tmp_path / "alpha.pending_order.json",
        ["+heartbeat"],
    )

    assert prepared is None
    assert blocked_calls
    denied = blocked_calls[0][2]
    gate_summary = blocked_calls[0][4]
    assert denied["gate"] == "prop_risk_governor"
    assert gate_summary == ["-prop_risk_governor"]
