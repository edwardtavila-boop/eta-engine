from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping


class _SecretsLookup(Protocol):
    def get(self, key: str, *, required: bool = False) -> object: ...


class BrokerRouterRoutingResolver:
    """Own broker-router venue-resolution and prop-account venue construction."""

    def __init__(
        self,
        *,
        smart_router: object,
        prop_venue_cache: MutableMapping[str, object],
        secrets: _SecretsLookup,
        tradovate_venue_cls: Callable[..., object],
    ) -> None:
        self._smart_router = smart_router
        self._prop_venue_cache = prop_venue_cache
        self._secrets = secrets
        self._tradovate_venue_cls = tradovate_venue_cls

    def resolve_venue_adapter(
        self,
        venue_name: str,
        order: object,
    ) -> object | None:
        """Look up a venue adapter on the SmartRouter by name."""
        _ = order  # reserved: future per-bot/per-qty hook
        by_name = getattr(self._smart_router, "_venue_by_name", None)
        if callable(by_name):
            try:
                venue = by_name(venue_name)
            except Exception:  # noqa: BLE001
                venue = None
            if venue is not None:
                return venue
        for attr in ("_venue_map", "venue_map"):
            mapping = getattr(self._smart_router, attr, None)
            if isinstance(mapping, dict):
                venue = mapping.get(venue_name)
                if venue is not None:
                    return venue
        venue = getattr(self._smart_router, venue_name, None)
        if venue is not None and hasattr(venue, "place_order"):
            return venue
        return None

    def resolve_prop_account_venue(self, account: dict[str, str]) -> object | None:
        """Build/cache an account-scoped venue after DORMANT gate clearance."""
        alias = (account.get("alias") or "").strip().lower()
        venue_name = (account.get("venue") or "").strip().lower()
        if not alias:
            raise ValueError("prop account is missing alias")
        if venue_name != "tradovate":
            raise ValueError(f"unsupported prop account venue for {alias}: {venue_name!r}")
        cached = self._prop_venue_cache.get(alias)
        if cached is not None:
            return cached

        account_id_env = (account.get("account_id_env") or "").strip()
        if not account_id_env:
            raise ValueError(f"prop account {alias} missing account_id_env")

        def _secret_value(key: str) -> str:
            env_val = (os.environ.get(key) or "").strip()
            if env_val:
                return env_val
            secret_val = self._secrets.get(key, required=False)
            return str(secret_val or "").strip()

        account_id = _secret_value(account_id_env)
        if not account_id:
            raise ValueError(f"prop account {alias} missing account id secret {account_id_env}")

        prefix = (account.get("creds_env_prefix") or "").strip()

        def _cred(name: str) -> str:
            return _secret_value(f"{prefix}{name}")

        required = (
            "TRADOVATE_USERNAME",
            "TRADOVATE_PASSWORD",
            "TRADOVATE_APP_ID",
            "TRADOVATE_APP_SECRET",
            "TRADOVATE_CID",
        )
        missing = [name for name in required if not _cred(name)]
        if missing:
            missing_csv = ", ".join(missing)
            raise ValueError(
                f"prop account {alias} missing Tradovate credential envs: {missing_csv}"
            )

        env_name = (account.get("env") or "demo").strip().lower()
        demo = env_name != "live"
        venue = self._tradovate_venue_cls(
            api_key=_cred("TRADOVATE_USERNAME"),
            api_secret=_cred("TRADOVATE_PASSWORD"),
            demo=demo,
            app_id=_cred("TRADOVATE_APP_ID") or "EtaEngine",
            cid=_cred("TRADOVATE_CID"),
            app_secret=_cred("TRADOVATE_APP_SECRET"),
            account_id=account_id,
        )
        self._prop_venue_cache[alias] = venue
        return venue
