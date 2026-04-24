"""Tastytrade paper venue adapter.

This mirrors the MNQ runtime adapter's safety model: paper/cert only by
default, secret-file aware, and no password collection in Apex.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apex_predator.venues.base import (
    ConnectionStatus,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    VenueBase,
    VenueConnectionReport,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


TASTY_CERT_BASE_URL = "https://api.cert.tastyworks.com"


class TastytradeConfigError(ValueError):
    """Raised when Tastytrade paper routing is not safely configured."""


@dataclass(frozen=True, slots=True)
class TastytradeConfig:
    """Connection settings for the Tastytrade paper/cert API."""

    base_url: str = TASTY_CERT_BASE_URL
    account_number: str = ""
    session_token: str = ""
    venue_type: str = "paper"
    require_cert_host: bool = True
    default_instrument_type: str = "Future"
    default_source: str = "ApexPredatorPaper"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TastytradeConfig:
        env_map = _broker_paper_env(env)
        return cls(
            base_url=str(env_map.get("TASTY_API_BASE_URL") or TASTY_CERT_BASE_URL).rstrip("/"),
            account_number=_env_or_file(env_map, "TASTY_ACCOUNT_NUMBER"),
            session_token=_env_or_file(env_map, "TASTY_SESSION_TOKEN"),
            venue_type=str(env_map.get("TASTY_VENUE_TYPE", "paper")).strip().lower(),
            require_cert_host=_env_bool(env_map.get("TASTY_REQUIRE_CERT_HOST"), default=True),
        )

    def missing_requirements(self) -> list[str]:
        missing: list[str] = []
        if self.venue_type != "paper":
            missing.append("TASTY_VENUE_TYPE=paper")
        if not self.account_number:
            missing.append("TASTY_ACCOUNT_NUMBER")
        if not self.session_token:
            missing.append("TASTY_SESSION_TOKEN")
        if self.require_cert_host and "cert" not in self.base_url.lower():
            missing.append("TASTY_API_BASE_URL must target the Tastytrade cert/sandbox host")
        return missing

    def require_ready(self) -> None:
        missing = self.missing_requirements()
        if missing:
            raise TastytradeConfigError("; ".join(missing))


class TastytradeVenue(VenueBase):
    """Paper-order adapter for Tastytrade cert/sandbox routing."""

    name: str = "tastytrade"

    def __init__(self, config: TastytradeConfig | None = None) -> None:
        self.config = config if config is not None else TastytradeConfig.from_env()
        super().__init__(self.config.account_number, self.config.session_token)
        self._mock_orders: dict[str, OrderResult] = {}

    def has_credentials(self) -> bool:
        return not self.config.missing_requirements()

    def connection_endpoint(self) -> str:
        return self.config.base_url

    async def connect(self) -> VenueConnectionReport:
        missing = self.config.missing_requirements()
        return VenueConnectionReport(
            venue=self.name,
            status=ConnectionStatus.READY if not missing else ConnectionStatus.STUBBED,
            creds_present=not missing,
            details={
                "mode": "paper",
                "endpoint": self.config.base_url,
                "account_configured": bool(self.config.account_number),
                "session_token_configured": bool(self.config.session_token),
                "missing": missing,
                "operator_action": (
                    "ready"
                    if not missing
                    else "Set TASTY_ACCOUNT_NUMBER and TASTY_SESSION_TOKEN or their *_FILE variants."
                ),
            },
            error="; ".join(missing),
        )

    async def place_order(self, request: OrderRequest) -> OrderResult:
        if not self.has_credentials():
            return OrderResult(
                order_id=self.idempotency_key(request),
                status=OrderStatus.REJECTED,
                raw={"venue": self.name, "reason": "missing Tastytrade paper-routing configuration"},
            )
        order_id = self.idempotency_key(request)
        result = OrderResult(
            order_id=order_id,
            status=OrderStatus.OPEN,
            raw={"venue": self.name, "payload": self.build_order_payload(request), "mode": "paper"},
        )
        self._mock_orders[order_id] = result
        return result

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        _ = symbol
        return self._mock_orders.pop(order_id, None) is not None

    async def get_positions(self) -> list[dict[str, Any]]:
        return []

    async def get_balance(self) -> dict[str, float]:
        return {}

    async def get_order_status(self, symbol: str, order_id: str) -> OrderResult | None:
        _ = symbol
        return self._mock_orders.get(order_id)

    def build_order_payload(self, request: OrderRequest) -> dict[str, Any]:
        qty = int(request.qty)
        if qty < 1:
            raise ValueError("qty must be >= 1 for Tastytrade futures orders")
        order_type = _tasty_order_type(request.order_type)
        payload: dict[str, Any] = {
            "time-in-force": "Day",
            "order-type": order_type,
            "source": self.config.default_source,
            "legs": [
                {
                    "instrument-type": self.config.default_instrument_type,
                    "symbol": _tasty_symbol(request.symbol),
                    "action": "Buy to Open" if request.side is Side.BUY else "Sell to Close",
                    "quantity": qty,
                },
            ],
        }
        payload["client-order-id"] = request.client_order_id or self.idempotency_key(request)
        if request.price is not None:
            payload["price"] = str(request.price)
        if order_type != "Market":
            payload["price-effect"] = "Debit" if request.side is Side.BUY else "Credit"
        return payload


def tastytrade_paper_readiness(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return a JSON-safe readiness summary for operator surfaces."""

    config = TastytradeConfig.from_env(env)
    missing = config.missing_requirements()
    return {
        "adapter_available": True,
        "ready": not missing,
        "mode": "paper",
        "base_url": config.base_url,
        "account_configured": bool(config.account_number),
        "session_token_configured": bool(config.session_token),
        "missing": missing,
        "reason": "ready" if not missing else "missing paper-routing configuration",
        "operator_action": (
            "ready"
            if not missing
            else "Set TASTY_ACCOUNT_NUMBER and TASTY_SESSION_TOKEN or their *_FILE variants."
        ),
        "checked_utc": datetime.now(UTC).isoformat(),
    }


def _env_or_file(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name) or "").strip()
    if value:
        return value
    file_value = str(env.get(f"{name}_FILE") or "").strip()
    if not file_value:
        return ""
    path = Path(file_value).expanduser()
    if not path.exists():
        raise TastytradeConfigError(f"{name}_FILE does not exist: {path}")
    return path.read_text(encoding="utf-8-sig").strip()


def _broker_paper_env(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """Merge process env with MNQ/VPS broker-paper note conventions."""

    env_map: dict[str, str] = dict(env or os.environ)
    broker_env_path = str(env_map.get("FIRM_BROKER_PAPER_ENV_FILE") or "").strip()
    if broker_env_path:
        _merge_missing(env_map, _read_key_value_file(Path(broker_env_path).expanduser()))
    else:
        default_env = _runtime_secret_root(env_map) / "broker_paper.env"
        if default_env.exists():
            env_map["FIRM_BROKER_PAPER_ENV_FILE"] = str(default_env)
            _merge_missing(env_map, _read_key_value_file(default_env))

    default_roots = [
        _runtime_secret_root(env_map),
        Path.home() / ".apex_predator" / "broker_paper",
    ]
    for default_root in default_roots:
        defaults = {
            "TASTY_ACCOUNT_NUMBER_FILE": default_root / "tastytrade_account_number.txt",
            "TASTY_SESSION_TOKEN_FILE": default_root / "tastytrade_session_token.txt",
        }
        for key, path in defaults.items():
            if not str(env_map.get(key) or "").strip() and path.exists():
                env_map[key] = str(path)
    return env_map


def _runtime_secret_root(env: Mapping[str, str]) -> Path:
    runtime_root = (
        str(env.get("APEX_RUNTIME_ROOT") or "").strip()
        or str(env.get("FIRM_RUNTIME_ROOT") or "").strip()
        or r"C:\TheFirm"
    )
    return Path(runtime_root) / "secrets"


def _merge_missing(target: dict[str, str], values: Mapping[str, str]) -> None:
    for key, value in values.items():
        if key and not str(target.get(key) or "").strip():
            target[key] = value


def _read_key_value_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _env_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _tasty_order_type(order_type: OrderType) -> str:
    if order_type is OrderType.MARKET:
        return "Market"
    if order_type in {OrderType.LIMIT, OrderType.POST_ONLY}:
        return "Limit"
    raise ValueError(f"unsupported Tastytrade order_type={order_type!r}")


def _tasty_symbol(symbol: str) -> str:
    value = symbol.upper().strip()
    return value if value.startswith("/") else f"/{value}"
