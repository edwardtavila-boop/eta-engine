"""Tastytrade paper venue adapter.

This mirrors the MNQ runtime adapter's safety model: paper/cert only by
default, secret-file aware, and no password collection in Apex.

When credentials are present (``TASTY_SESSION_TOKEN`` + account number),
``place_order`` makes a real POST against the Tastytrade cert/sandbox
REST API and stores the server-returned ``id`` + ``status`` for
subsequent reconciliation. ``get_order_status`` polls the server for
the current state and returns a typed :class:`OrderResult` reflecting
broker-side fills.

When ``httpx`` is unavailable or the network call fails, the adapter
degrades to the pre-v0.1.58 in-memory mock behavior so tests and
offline dry-runs stay fast and deterministic. Every degraded result
carries ``raw["note"]`` with the reason so the operator can audit.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eta_engine.venues.base import (
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


logger = logging.getLogger(__name__)

TASTY_CERT_BASE_URL = "https://api.cert.tastyworks.com"
TASTY_HTTP_TIMEOUT_S = 8.0

# ---------------------------------------------------------------------------
# Server-side status -> our OrderStatus enum. Tastytrade returns strings
# like "Routed", "Received", "Filled", "Rejected", "Cancelled",
# "Expired", "Live", "In Flight", "Replaced". The left column is what
# the cert API reports; the right is the canonical Apex enum.
# ---------------------------------------------------------------------------
_TASTY_STATUS_MAP: dict[str, OrderStatus] = {
    "filled": OrderStatus.FILLED,
    "partial filled": OrderStatus.PARTIAL,
    "partially filled": OrderStatus.PARTIAL,
    "partial": OrderStatus.PARTIAL,
    "rejected": OrderStatus.REJECTED,
    "cancelled": OrderStatus.REJECTED,
    "canceled": OrderStatus.REJECTED,
    "expired": OrderStatus.REJECTED,
    "routed": OrderStatus.OPEN,
    "received": OrderStatus.OPEN,
    "live": OrderStatus.OPEN,
    "in flight": OrderStatus.OPEN,
    "replaced": OrderStatus.OPEN,
    "contingent": OrderStatus.OPEN,
}


def _map_tasty_status(raw: Any) -> OrderStatus:  # noqa: ANN401 -- server payload is untyped
    text = str(raw or "").strip().lower()
    return _TASTY_STATUS_MAP.get(text, OrderStatus.OPEN)


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
    default_source: str = "EtaEnginePaper"

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
        """Place an order against the Tastytrade cert/sandbox API.

        Falls back to in-memory mock OPEN state when creds are missing
        or the HTTP transport is unavailable. Every degraded path
        annotates ``raw["note"]`` so dashboards can show why the
        broker was bypassed.
        """
        if not self.has_credentials():
            return OrderResult(
                order_id=self.idempotency_key(request),
                status=OrderStatus.REJECTED,
                raw={"venue": self.name, "reason": "missing Tastytrade paper-routing configuration"},
            )
        client_order_id = request.client_order_id or self.idempotency_key(request)
        payload = self.build_order_payload(request, client_order_id=client_order_id)
        server = await self._post_order(payload)
        if server is None:
            # Degraded path: keep the mock OPEN so upstream lifecycle
            # bookkeeping doesn't stall, but mark the degradation.
            result = OrderResult(
                order_id=client_order_id,
                status=OrderStatus.OPEN,
                raw={
                    "venue": self.name,
                    "payload": payload,
                    "mode": "paper",
                    "note": "mock_fallback_no_transport_or_network_error",
                },
            )
            self._mock_orders[client_order_id] = result
            return result

        order_dict = server.get("data", {}).get("order") or server.get("data") or {}
        server_id = str(order_dict.get("id") or client_order_id)
        status = _map_tasty_status(order_dict.get("status"))
        filled_qty = float(order_dict.get("filled-quantity") or order_dict.get("filled_quantity") or 0.0)
        avg_price = float(
            order_dict.get("average-fill-price") or order_dict.get("average_fill_price") or 0.0,
        )
        result = OrderResult(
            order_id=server_id,
            status=status,
            filled_qty=filled_qty,
            avg_price=avg_price,
            raw={
                "venue": self.name,
                "payload": payload,
                "mode": "paper",
                "server": order_dict,
                "client_order_id": client_order_id,
            },
        )
        # Cache by both server_id and client_order_id so lookups from
        # either side work.
        self._mock_orders[server_id] = result
        self._mock_orders[client_order_id] = result
        return result

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        _ = symbol
        if not self.has_credentials():
            return self._mock_orders.pop(order_id, None) is not None
        ok = await self._delete_order(order_id)
        self._mock_orders.pop(order_id, None)
        return ok

    async def get_positions(self) -> list[dict[str, Any]]:
        if not self.has_credentials():
            return []
        resp = await self._get(f"/accounts/{self.config.account_number}/positions")
        if resp is None:
            return []
        data = resp.get("data", {})
        items = data.get("items") if isinstance(data, dict) else None
        return list(items or [])

    async def get_balance(self) -> dict[str, float]:
        if not self.has_credentials():
            return {}
        resp = await self._get(f"/accounts/{self.config.account_number}/balances")
        if resp is None:
            return {}
        data = resp.get("data", {})
        if not isinstance(data, dict):
            return {}
        out: dict[str, float] = {}
        for key in ("cash-balance", "equity-buying-power", "net-liquidating-value"):
            raw = data.get(key)
            if raw is not None:
                try:
                    out[key.replace("-", "_")] = float(raw)
                except (TypeError, ValueError):
                    continue
        return out

    async def get_net_liquidation(self) -> float | None:
        """Return broker-reported net-liquidation USD, or ``None`` if unavailable.

        R1 closure. Drives ``core.BrokerEquityReconciler`` so the
        trailing-DD tracker can cross-check its logical equity stream
        against what Tastytrade's server actually reports.

        Returns
        -------
        float | None
            Net-liquidating-value in USD, or ``None`` when credentials
            are missing, the HTTP call fails, httpx is not installed,
            or the server response is unparseable.
        """
        balance = await self.get_balance()
        raw = balance.get("net_liquidating_value")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    async def get_order_status(self, symbol: str, order_id: str) -> OrderResult | None:
        _ = symbol
        if self.has_credentials():
            resp = await self._get(f"/accounts/{self.config.account_number}/orders/{order_id}")
            if resp is not None:
                order_dict = resp.get("data", {}).get("order") or resp.get("data") or {}
                if order_dict:
                    status = _map_tasty_status(order_dict.get("status"))
                    filled_qty = float(
                        order_dict.get("filled-quantity") or order_dict.get("filled_quantity") or 0.0,
                    )
                    avg_price = float(
                        order_dict.get("average-fill-price") or order_dict.get("average_fill_price") or 0.0,
                    )
                    result = OrderResult(
                        order_id=str(order_dict.get("id") or order_id),
                        status=status,
                        filled_qty=filled_qty,
                        avg_price=avg_price,
                        raw={
                            "venue": self.name,
                            "mode": "paper",
                            "server": order_dict,
                        },
                    )
                    self._mock_orders[result.order_id] = result
                    return result
        return self._mock_orders.get(order_id)

    async def reconcile_orders(self, order_ids: list[str]) -> list[OrderResult]:
        """Refresh broker-side state for a batch of orders.

        Returns the updated :class:`OrderResult` list in the same order
        as ``order_ids``. Unknown / offline orders are re-fetched from
        the local cache only. Intended for the BTC fleet reconciler
        that drains fills after the supervisor's bar loop.
        """
        out: list[OrderResult] = []
        for oid in order_ids:
            fresh = await self.get_order_status("", oid)
            if fresh is not None:
                out.append(fresh)
        return out

    def build_order_payload(
        self,
        request: OrderRequest,
        *,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        qty = int(request.qty)
        if qty < 1:
            raise ValueError("qty must be >= 1 for Tastytrade orders")
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
        payload["client-order-id"] = client_order_id or request.client_order_id or self.idempotency_key(request)
        if request.price is not None:
            payload["price"] = str(request.price)
        if order_type != "Market":
            payload["price-effect"] = "Debit" if request.side is Side.BUY else "Credit"
        return payload

    # ------------------------------------------------------------------
    # HTTP transport (lazy-loaded httpx; every call degrades gracefully)
    # ------------------------------------------------------------------

    def _session_headers(self) -> dict[str, str]:
        return {
            "Authorization": self.config.session_token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "eta-engine-tastytrade/0.1",
        }

    async def _post_order(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """POST the order payload to the Tastytrade cert API.

        Returns the decoded response on success; returns ``None`` on
        any failure (missing httpx, network error, non-2xx) so the
        caller can degrade to mock mode.
        """
        try:
            import httpx  # noqa: PLC0415 - lazy import keeps this optional
        except ImportError:
            logger.debug("tastytrade: httpx unavailable; degrading to mock fallback")
            return None
        url = f"{self.config.base_url}/accounts/{self.config.account_number}/orders"
        try:
            async with httpx.AsyncClient(timeout=TASTY_HTTP_TIMEOUT_S) as client:
                resp = await client.post(url, json=payload, headers=self._session_headers())
        except Exception as exc:  # noqa: BLE001 -- transport errors degrade cleanly
            logger.info("tastytrade: order POST network error: %s", exc)
            return None
        if resp.status_code >= 400:
            logger.info(
                "tastytrade: order POST rejected status=%d body=%s",
                resp.status_code,
                resp.text[:200],
            )
            return None
        try:
            return resp.json()
        except ValueError as exc:
            logger.info("tastytrade: order POST invalid JSON: %s", exc)
            return None

    async def _get(self, path: str) -> dict[str, Any] | None:
        try:
            import httpx  # noqa: PLC0415 - lazy import
        except ImportError:
            return None
        url = f"{self.config.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=TASTY_HTTP_TIMEOUT_S) as client:
                resp = await client.get(url, headers=self._session_headers())
        except Exception as exc:  # noqa: BLE001
            logger.debug("tastytrade: GET %s network error: %s", path, exc)
            return None
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            logger.info(
                "tastytrade: GET %s returned %d body=%s",
                path,
                resp.status_code,
                resp.text[:200],
            )
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    async def _delete_order(self, order_id: str) -> bool:
        try:
            import httpx  # noqa: PLC0415 - lazy import
        except ImportError:
            return False
        url = f"{self.config.base_url}/accounts/{self.config.account_number}/orders/{order_id}"
        try:
            async with httpx.AsyncClient(timeout=TASTY_HTTP_TIMEOUT_S) as client:
                resp = await client.delete(url, headers=self._session_headers())
        except Exception as exc:  # noqa: BLE001
            logger.debug("tastytrade: DELETE %s network error: %s", order_id, exc)
            return False
        # Cert API returns 200 on successful cancel, 404 if already gone.
        return resp.status_code in (200, 204, 404)


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
            "ready" if not missing else "Set TASTY_ACCOUNT_NUMBER and TASTY_SESSION_TOKEN or their *_FILE variants."
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
        Path.home() / ".eta_engine" / "broker_paper",
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
        or r"C:\EvolutionaryTradingAlgo\firm_command_center"
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
