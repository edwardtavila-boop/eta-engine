from __future__ import annotations

from types import SimpleNamespace

from eta_engine.scripts.broker_router_routing import BrokerRouterRoutingResolver


class _Venue:
    def __init__(self, name: str) -> None:
        self.name = name

    async def place_order(self, request: object) -> object:
        return request


class _TradovateVenue:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        demo: bool,
        app_id: str,
        cid: str,
        app_secret: str,
        account_id: str,
    ) -> None:
        self.name = "tradovate"
        self.api_key = api_key
        self.api_secret = api_secret
        self.demo = demo
        self.app_id = app_id
        self.cid = cid
        self.app_secret = app_secret
        self.account_id = int(account_id)


def test_resolve_venue_adapter_prefers_by_name_then_map_then_attr() -> None:
    by_name_venue = _Venue("by_name")
    map_venue = _Venue("map")
    attr_venue = _Venue("attr")

    helper = BrokerRouterRoutingResolver(
        smart_router=SimpleNamespace(
            _venue_by_name=lambda name: by_name_venue if name == "ibkr" else None,
            _venue_map={"tasty": map_venue},
            attr_only=attr_venue,
        ),
        prop_venue_cache={},
        secrets=SimpleNamespace(get=lambda key, required=False: None),
        tradovate_venue_cls=_TradovateVenue,
    )

    assert helper.resolve_venue_adapter("ibkr", object()) is by_name_venue
    assert helper.resolve_venue_adapter("tasty", object()) is map_venue
    assert helper.resolve_venue_adapter("attr_only", object()) is attr_venue
    assert helper.resolve_venue_adapter("missing", object()) is None


def test_resolve_prop_account_venue_builds_and_caches_prefixed_tradovate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    values = {
        "BLUSKY_TRADOVATE_ACCOUNT_ID": "1234567",
        "BLUSKY_TRADOVATE_USERNAME": "blusky@example.com",
        "BLUSKY_TRADOVATE_PASSWORD": "pw",
        "BLUSKY_TRADOVATE_APP_ID": "EtaEngine",
        "BLUSKY_TRADOVATE_APP_SECRET": "app-secret",
        "BLUSKY_TRADOVATE_CID": "999",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    cache: dict[str, object] = {}
    helper = BrokerRouterRoutingResolver(
        smart_router=SimpleNamespace(),
        prop_venue_cache=cache,
        secrets=SimpleNamespace(get=lambda key, required=False: None),
        tradovate_venue_cls=_TradovateVenue,
    )
    account = {
        "alias": "blusky_50k",
        "venue": "tradovate",
        "env": "demo",
        "account_id_env": "BLUSKY_TRADOVATE_ACCOUNT_ID",
        "creds_env_prefix": "BLUSKY_",
    }

    venue = helper.resolve_prop_account_venue(account)
    cached = helper.resolve_prop_account_venue(account)

    assert venue is cached
    assert venue.name == "tradovate"
    assert venue.account_id == 1234567
    assert venue.api_key == "blusky@example.com"
    assert venue.cid == "999"
    assert cache["blusky_50k"] is venue
