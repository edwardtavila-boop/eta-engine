"""Interactive Brokers Client Portal paper venue adapter.

This adapter expects an operator-managed IBKR Client Portal Gateway session.
It keeps the same paper-only defaults and env names used by the MNQ runtime.
"""

from __future__ import annotations

import json
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


IBKR_CLIENT_PORTAL_BASE_URL = "https://127.0.0.1:5000/v1/api"


class IbkrConfigError(ValueError):
    """Raised when IBKR paper routing is not safely configured."""


@dataclass(frozen=True, slots=True)
class IbkrClientPortalConfig:
    """Connection settings for an authenticated IBKR Client Portal Gateway."""

    base_url: str = IBKR_CLIENT_PORTAL_BASE_URL
    account_id: str = ""
    venue_type: str = "paper"
    require_paper_account: bool = True
    symbol_conids: Mapping[str, int] | None = None
    default_exchange: str = "CME"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> IbkrClientPortalConfig:
        env_map = _broker_paper_env(env)
        return cls(
            base_url=str(env_map.get("IBKR_CP_BASE_URL") or IBKR_CLIENT_PORTAL_BASE_URL).rstrip("/"),
            account_id=_env_or_file(env_map, "IBKR_ACCOUNT_ID"),
            venue_type=str(env_map.get("IBKR_VENUE_TYPE", "paper")).strip().lower(),
            require_paper_account=_env_bool(env_map.get("IBKR_REQUIRE_PAPER_ACCOUNT"), default=True),
            symbol_conids=_load_symbol_conids(env_map),
            default_exchange=str(env_map.get("IBKR_DEFAULT_EXCHANGE") or "CME").strip() or "CME",
        )

    def missing_requirements(self) -> list[str]:
        missing: list[str] = []
        if self.venue_type != "paper":
            missing.append("IBKR_VENUE_TYPE=paper")
        if not self.account_id:
            missing.append("IBKR_ACCOUNT_ID")
        if self.require_paper_account and self.account_id and not self.account_id.startswith("DU"):
            missing.append("IBKR_ACCOUNT_ID must be a paper account id beginning with DU")
        if not self.symbol_conids:
            missing.append("IBKR_SYMBOL_CONID_MAP or IBKR_CONID_<SYMBOL>")
        return missing

    def require_ready(self) -> None:
        missing = self.missing_requirements()
        if missing:
            raise IbkrConfigError("; ".join(missing))

    def conid_for(self, symbol: str) -> int | None:
        if not self.symbol_conids:
            return None
        return self.symbol_conids.get(symbol.upper().lstrip("/"))


class IbkrClientPortalVenue(VenueBase):
    """Paper-order adapter for the IBKR Client Portal Web API."""

    name: str = "ibkr"

    def __init__(self, config: IbkrClientPortalConfig | None = None) -> None:
        self.config = config if config is not None else IbkrClientPortalConfig.from_env()
        super().__init__(self.config.account_id, "")
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
                "account_configured": bool(self.config.account_id),
                "paper_account_confirmed": self.config.account_id.startswith("DU"),
                "conid_map_configured": bool(self.config.symbol_conids),
                "missing": missing,
                "operator_action": (
                    "ready"
                    if not missing
                    else "Start IBKR Client Portal/TWS paper, set IBKR_ACCOUNT_ID=DU..., and configure conids."
                ),
            },
            error="; ".join(missing),
        )

    async def place_order(self, request: OrderRequest) -> OrderResult:
        if not self.has_credentials():
            return OrderResult(
                order_id=self.idempotency_key(request),
                status=OrderStatus.REJECTED,
                raw={"venue": self.name, "reason": "missing IBKR paper-routing configuration"},
            )
        conid = self.config.conid_for(request.symbol)
        if conid is None:
            return OrderResult(
                order_id=self.idempotency_key(request),
                status=OrderStatus.REJECTED,
                raw={"venue": self.name, "reason": f"missing IBKR conid for {request.symbol}"},
            )
        order_id = self.idempotency_key(request)
        result = OrderResult(
            order_id=order_id,
            status=OrderStatus.OPEN,
            raw={"venue": self.name, "payload": self.build_order_payload(request, conid=conid), "mode": "paper"},
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

    def build_order_payload(self, request: OrderRequest, *, conid: int) -> dict[str, Any]:
        qty = int(request.qty)
        if qty < 1:
            raise ValueError("qty must be >= 1 for IBKR futures orders")
        order_type = _ibkr_order_type(request.order_type)
        payload: dict[str, Any] = {
            "acctId": self.config.account_id,
            "conid": conid,
            "cOID": request.client_order_id or self.idempotency_key(request),
            "orderType": order_type,
            "listingExchange": self.config.default_exchange,
            "side": "BUY" if request.side is Side.BUY else "SELL",
            "ticker": request.symbol.upper().lstrip("/"),
            "tif": "DAY",
            "quantity": qty,
        }
        if order_type == "LMT":
            if request.price is None:
                raise ValueError("limit order requires price")
            payload["price"] = float(request.price)
        return payload


def ibkr_paper_readiness(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return a JSON-safe readiness summary for operator surfaces."""

    config = IbkrClientPortalConfig.from_env(env)
    missing = config.missing_requirements()
    return {
        "adapter_available": True,
        "ready": not missing,
        "mode": "paper",
        "base_url": config.base_url,
        "account_configured": bool(config.account_id),
        "paper_account_confirmed": config.account_id.startswith("DU"),
        "conid_map_configured": bool(config.symbol_conids),
        "missing": missing,
        "reason": "ready" if not missing else "missing paper-routing configuration",
        "operator_action": (
            "ready"
            if not missing
            else "Start IBKR Client Portal/TWS paper, set IBKR_ACCOUNT_ID=DU..., and configure conids."
        ),
        "checked_utc": datetime.now(UTC).isoformat(),
    }


def _load_symbol_conids(env: Mapping[str, str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    raw = _env_or_file(env, "IBKR_SYMBOL_CONID_MAP")
    if raw:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise IbkrConfigError("IBKR_SYMBOL_CONID_MAP must be a JSON object")
        for symbol, conid in parsed.items():
            mapping[str(symbol).upper().lstrip("/")] = int(conid)
    prefix = "IBKR_CONID_"
    for key, value in env.items():
        if key.startswith(prefix) and str(value).strip():
            mapping[key[len(prefix):].upper().lstrip("/")] = int(value)
    return mapping


def _env_or_file(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name) or "").strip()
    if value:
        return value
    file_value = str(env.get(f"{name}_FILE") or "").strip()
    if not file_value:
        return ""
    path = Path(file_value).expanduser()
    if not path.exists():
        raise IbkrConfigError(f"{name}_FILE does not exist: {path}")
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
            "IBKR_ACCOUNT_ID_FILE": default_root / "ibkr_account_id.txt",
            "IBKR_SYMBOL_CONID_MAP_FILE": default_root / "ibkr_symbol_conids.json",
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


def _ibkr_order_type(order_type: OrderType) -> str:
    if order_type is OrderType.MARKET:
        return "MKT"
    if order_type in {OrderType.LIMIT, OrderType.POST_ONLY}:
        return "LMT"
    raise ValueError(f"unsupported IBKR order_type={order_type!r}")
