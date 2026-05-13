"""Alpaca paper venue adapter (crypto-focused).

Mirrors :mod:`eta_engine.venues.tastytrade` in shape and safety model:
paper-only by default, secret-file aware, and graceful degrade to in-memory
mock when ``httpx`` is unavailable or the network call fails. The intended
use is paper-crypto routing while Tastytrade cert sandbox enablement is
pending — Alpaca paper covers BTC/ETH/SOL/XRP/AVAX/LINK/DOGE/etc.

Auth model is simpler than Tastytrade: two static headers
(``APCA-API-KEY-ID`` and ``APCA-API-SECRET-KEY``) — no session-token
rotation needed.

API surface used:
* POST   /v2/orders                    — place
* GET    /v2/orders/{id}                — status
* DELETE /v2/orders/{id}                — cancel
* GET    /v2/account                    — balance
* GET    /v2/positions                  — positions
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

    from eta_engine.venues.base import ExecutionCapabilities


logger = logging.getLogger(__name__)

ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_BASE_URL = "https://api.alpaca.markets"
ALPACA_HTTP_TIMEOUT_S = 8.0

# Alpaca enforces a $10 minimum cost basis on crypto orders. The broker
# returns HTTP 403 with code 40310000 when violated; we surface the same
# constraint client-side so the order never leaves the process and the
# operator gets a deterministic reject reason.
ALPACA_CRYPTO_MIN_COST_BASIS_USD = 10.0

# Canonical crypto bases tradeable on Alpaca paper as of 2026-05.
# Used by _alpaca_crypto_base for early "instrument-not-supported"
# detection so an unsupported symbol fails fast with a clear reason.
_ALPACA_CRYPTO_BASES: frozenset[str] = frozenset(
    {
        "AAVE",
        "AVAX",
        "BAT",
        "BCH",
        "BTC",
        "CRV",
        "DOGE",
        "DOT",
        "ETH",
        "GRT",
        "LINK",
        "LTC",
        "MKR",
        "PEPE",
        "SHIB",
        "SOL",
        "SUSHI",
        "UNI",
        "USDC",
        "USDT",
        "XRP",
        "XTZ",
        "YFI",
    }
)

# Alpaca order-status string -> our canonical OrderStatus enum.
_ALPACA_STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.OPEN,
    "pending_new": OrderStatus.OPEN,
    "accepted": OrderStatus.OPEN,
    "accepted_for_bidding": OrderStatus.OPEN,
    "pending_cancel": OrderStatus.OPEN,
    "pending_replace": OrderStatus.OPEN,
    "replaced": OrderStatus.OPEN,
    "calculated": OrderStatus.OPEN,
    "held": OrderStatus.OPEN,
    "partially_filled": OrderStatus.PARTIAL,
    "filled": OrderStatus.FILLED,
    "done_for_day": OrderStatus.FILLED,
    "canceled": OrderStatus.REJECTED,
    "cancelled": OrderStatus.REJECTED,
    "expired": OrderStatus.REJECTED,
    "rejected": OrderStatus.REJECTED,
    "stopped": OrderStatus.REJECTED,
    "suspended": OrderStatus.REJECTED,
}


def _map_alpaca_status(raw: Any) -> OrderStatus:  # noqa: ANN401 — server payload is untyped
    return _ALPACA_STATUS_MAP.get(str(raw or "").strip().lower(), OrderStatus.OPEN)


class AlpacaConfigError(ValueError):
    """Raised when Alpaca paper routing is not safely configured."""


@dataclass(frozen=True, slots=True)
class AlpacaConfig:
    """Connection settings for the Alpaca paper trading API."""

    base_url: str = ALPACA_PAPER_BASE_URL
    api_key_id: str = ""
    api_secret_key: str = ""
    venue_type: str = "paper"
    require_paper_host: bool = True
    default_source: str = "EtaEnginePaper"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AlpacaConfig:
        env_map = _broker_paper_env(env)
        return cls(
            base_url=str(env_map.get("ALPACA_BASE_URL") or ALPACA_PAPER_BASE_URL).rstrip("/"),
            api_key_id=_env_or_file(env_map, "ALPACA_API_KEY_ID"),
            api_secret_key=_env_or_file(env_map, "ALPACA_API_SECRET_KEY"),
            venue_type=str(env_map.get("ALPACA_VENUE_TYPE", "paper")).strip().lower(),
            require_paper_host=_env_bool(env_map.get("ALPACA_REQUIRE_PAPER_HOST"), default=True),
        )

    def missing_requirements(self) -> list[str]:
        missing: list[str] = []
        if self.venue_type != "paper":
            missing.append("ALPACA_VENUE_TYPE=paper")
        if not self.api_key_id:
            missing.append("ALPACA_API_KEY_ID")
        if not self.api_secret_key:
            missing.append("ALPACA_API_SECRET_KEY")
        if self.require_paper_host and "paper" not in self.base_url.lower():
            missing.append("ALPACA_BASE_URL must target paper-api.alpaca.markets for paper venue")
        return missing

    def require_ready(self) -> None:
        missing = self.missing_requirements()
        if missing:
            raise AlpacaConfigError("; ".join(missing))


class AlpacaVenue(VenueBase):
    """Paper-order adapter for Alpaca crypto + equity (crypto is primary)."""

    name: str = "alpaca"

    def __init__(self, config: AlpacaConfig | None = None) -> None:
        self.config = config if config is not None else AlpacaConfig.from_env()
        super().__init__(self.config.api_key_id, self.config.api_secret_key)
        self._mock_orders: dict[str, OrderResult] = {}

    def has_credentials(self) -> bool:
        return not self.config.missing_requirements()

    def connection_endpoint(self) -> str:
        return self.config.base_url

    def execution_capabilities_for(self, symbol: str) -> ExecutionCapabilities:
        """Per-symbol execution capabilities for Alpaca.

        Crypto vs equity differ on Alpaca in TWO meaningful ways:

        1. ``bracket_style``: crypto returns HTTP 422 ``crypto orders
           not allowed for advanced order_class: otoco`` for any
           non-``simple`` order_class. The supervisor must drive
           stops/targets via tick-level ``_maybe_exit`` watching. This
           is BracketStyle.SUPERVISOR_LOCAL. Equity accepts
           ``order_class=bracket`` cleanly — BracketStyle.SERVER_OCO.

        2. ``min_cost_basis_usd``: crypto enforces $10 server-side
           (caught live as HTTP 403 code 40310000). Equity has no
           such floor.

        Both branches: ``supports_session_aware_routing=False``
        (Alpaca crypto is 24/7; Alpaca equity uses session via
        ``time_in_force=day`` on the order itself).
        """
        from eta_engine.venues.base import BracketStyle, ExecutionCapabilities

        is_crypto = bool(_alpaca_crypto_base(symbol))
        if is_crypto:
            return ExecutionCapabilities(
                bracket_style=BracketStyle.SUPERVISOR_LOCAL,
                min_cost_basis_usd=ALPACA_CRYPTO_MIN_COST_BASIS_USD,
                min_order_qty=0.0,  # per-base min from /v2/assets is enforced server-side
                supports_reduce_only=True,
                supports_session_aware_routing=False,
            )
        # Equity path: Alpaca accepts server-side OCO via
        # order_class=bracket. No cost-basis minimum on equity orders.
        return ExecutionCapabilities(
            bracket_style=BracketStyle.SERVER_OCO,
            min_cost_basis_usd=0.0,
            min_order_qty=1.0,  # whole-share floor for the equity path used here
            supports_reduce_only=True,
            supports_session_aware_routing=False,
        )

    async def connect(self) -> VenueConnectionReport:
        """Connection probe.

        Strategy:
          1. Static config check (missing creds / wrong host) — if those
             fail we return STUBBED without burning a network round-trip.
          2. Live ``/v2/account`` GET — confirms the keys are actually
             accepted by Alpaca right now and returns the broker-side
             account state (status, crypto_status, equity). Surfacing
             this on connect() is what catches expired keys, wrong
             environment (live vs paper), and account-level holds at
             startup rather than on the first order.
        """
        missing = self.config.missing_requirements()
        details: dict[str, Any] = {
            "mode": "paper",
            "endpoint": self.config.base_url,
            "key_id_configured": bool(self.config.api_key_id),
            "secret_configured": bool(self.config.api_secret_key),
            "missing": missing,
        }
        if missing:
            details["operator_action"] = "Set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY (or *_FILE variants)."
            return VenueConnectionReport(
                venue=self.name,
                status=ConnectionStatus.STUBBED,
                creds_present=False,
                details=details,
                error="; ".join(missing),
            )

        # Live probe — the keys are present, confirm they actually work.
        probe = await self._get("/v2/account")
        if probe is None or not isinstance(probe, dict):
            details["probe"] = "failed"
            details["operator_action"] = (
                "Alpaca /v2/account returned no body — keys may be invalid, "
                "rate-limited, or the host may be unreachable."
            )
            return VenueConnectionReport(
                venue=self.name,
                status=ConnectionStatus.DEGRADED,
                creds_present=True,
                details=details,
                error="alpaca live probe failed",
            )

        details["probe"] = "ok"
        details["account_number"] = probe.get("account_number", "")
        details["account_status"] = probe.get("status", "")
        details["crypto_status"] = probe.get("crypto_status", "")
        details["equity"] = probe.get("equity")
        details["buying_power"] = probe.get("buying_power")
        details["trading_blocked"] = bool(probe.get("trading_blocked"))
        details["account_blocked"] = bool(probe.get("account_blocked"))
        details["operator_action"] = "ready"

        # If trading is blocked broker-side, surface that as DEGRADED so
        # the supervisor / dashboard can fail-soft instead of pretending
        # the venue is healthy.
        broker_blocked = (
            probe.get("trading_blocked")
            or probe.get("account_blocked")
            or str(probe.get("status", "")).upper() not in {"ACTIVE", ""}
        )
        if broker_blocked:
            return VenueConnectionReport(
                venue=self.name,
                status=ConnectionStatus.DEGRADED,
                creds_present=True,
                details=details,
                error="alpaca account is not ACTIVE / trading blocked",
            )

        return VenueConnectionReport(
            venue=self.name,
            status=ConnectionStatus.READY,
            creds_present=True,
            details=details,
            error="",
        )

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Place an order on Alpaca paper.

        Pre-checks (deterministic rejects, no network round-trip):
          * Credentials configured.
          * Crypto cost basis >= ALPACA_CRYPTO_MIN_COST_BASIS_USD.
          * Non-reduce-only entries MUST have stop_price + target_price
            (bracket attachment). Naked entries are rejected at this
            layer; mirrors the IBKR live-venue safety contract.
          * Bracket geometry: stop must sit on the loss side of entry
            and target on the profit side, otherwise the broker would
            either reject or fill instantly into a worse position.

        Network path:
          * POST /v2/orders. On non-2xx, the HTTP body is captured into
            ``raw["alpaca_error"]`` so dashboards can surface the reason.
          * On transport error (httpx unavailable, network exception),
            falls through to mock OPEN with ``raw["note"]`` annotated.
        """
        if not self.has_credentials():
            return OrderResult(
                order_id=self.idempotency_key(request),
                status=OrderStatus.REJECTED,
                raw={"venue": self.name, "reason": "missing Alpaca paper-routing configuration"},
            )

        # Cost-basis pre-check for crypto. Alpaca enforces a $10 minimum
        # server-side; doing the same client-side gives the operator a
        # clear, deterministic reject before the order leaves the process.
        is_crypto = bool(_alpaca_crypto_base(request.symbol))
        if is_crypto and request.price is not None:
            est_cost_basis = float(request.price) * float(request.qty)
            if est_cost_basis < ALPACA_CRYPTO_MIN_COST_BASIS_USD:
                return OrderResult(
                    order_id=self.idempotency_key(request),
                    status=OrderStatus.REJECTED,
                    raw={
                        "venue": self.name,
                        "reason": "alpaca_min_cost_basis",
                        "min_cost_basis_usd": ALPACA_CRYPTO_MIN_COST_BASIS_USD,
                        "est_cost_basis_usd": round(est_cost_basis, 4),
                        "qty": float(request.qty),
                        "limit_price": float(request.price),
                    },
                )

        # ── BRACKET REQUIREMENT (non-reduce-only entries only) ──────────
        # Mirror the IBKR live-venue safety contract: an entry order must
        # carry both stop_price and target_price so the broker holds the
        # OCO siblings server-side. If the supervisor crashes between the
        # entry fill and the bracket attach, the position would otherwise
        # be naked at Alpaca with no automatic stop. Reduce-only orders
        # are EXITS — they intentionally bypass this check.
        if not request.reduce_only:
            stop_p = request.stop_price
            target_p = request.target_price
            if stop_p is None or target_p is None:
                return OrderResult(
                    order_id=self.idempotency_key(request),
                    status=OrderStatus.REJECTED,
                    raw={
                        "venue": self.name,
                        "reason": "naked_entry_blocked",
                        "stop_price": stop_p,
                        "target_price": target_p,
                        "symbol": request.symbol,
                    },
                )
            # Geometry check: the OCO siblings must sit on the correct
            # side of the entry reference price. For BUY: stop < entry
            # < target. For SELL: target < entry < stop. Without this,
            # Alpaca rejects the bracket server-side ("invalid order
            # legs") and the operator sees the failure only after the
            # parent fills — by which point the position is already on
            # the books with no protection.
            ref_price = request.price
            geometry_error = _validate_bracket_geometry(
                side=request.side,
                ref_price=ref_price,
                stop_price=stop_p,
                target_price=target_p,
            )
            if geometry_error is not None:
                return OrderResult(
                    order_id=self.idempotency_key(request),
                    status=OrderStatus.REJECTED,
                    raw={
                        "venue": self.name,
                        "reason": "bracket_geometry_invalid",
                        "detail": geometry_error,
                        "side": request.side.value,
                        "ref_price": ref_price,
                        "stop_price": stop_p,
                        "target_price": target_p,
                    },
                )

        client_order_id = request.client_order_id or self.idempotency_key(request)
        payload = self.build_order_payload(request, client_order_id=client_order_id)
        server, error_body = await self._post_order_with_error(payload)
        if server is None:
            result = OrderResult(
                order_id=client_order_id,
                status=OrderStatus.REJECTED if error_body else OrderStatus.OPEN,
                raw={
                    "venue": self.name,
                    "payload": payload,
                    "mode": "paper",
                    "note": ("alpaca_rejected" if error_body else "mock_fallback_no_transport_or_network_error"),
                    "alpaca_error": error_body,
                },
            )
            self._mock_orders[client_order_id] = result
            return result

        server_id = str(server.get("id") or client_order_id)
        status = _map_alpaca_status(server.get("status"))
        filled_qty = float(server.get("filled_qty") or 0.0)
        avg_price = float(server.get("filled_avg_price") or 0.0)
        result = OrderResult(
            order_id=server_id,
            status=status,
            filled_qty=filled_qty,
            avg_price=avg_price,
            raw={
                "venue": self.name,
                "payload": payload,
                "mode": "paper",
                "server": server,
                "client_order_id": client_order_id,
            },
        )
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

    async def get_order_status(self, symbol: str, order_id: str) -> OrderResult | None:
        _ = symbol
        if self.has_credentials():
            resp = await self._get(f"/v2/orders/{order_id}")
            if resp is not None:
                status = _map_alpaca_status(resp.get("status"))
                filled_qty = float(resp.get("filled_qty") or 0.0)
                avg_price = float(resp.get("filled_avg_price") or 0.0)
                result = OrderResult(
                    order_id=str(resp.get("id") or order_id),
                    status=status,
                    filled_qty=filled_qty,
                    avg_price=avg_price,
                    raw={"venue": self.name, "mode": "paper", "server": resp},
                )
                self._mock_orders[result.order_id] = result
                return result
        return self._mock_orders.get(order_id)

    async def reconcile_orders(self, order_ids: list[str]) -> list[OrderResult]:
        out: list[OrderResult] = []
        for oid in order_ids:
            fresh = await self.get_order_status("", oid)
            if fresh is not None:
                out.append(fresh)
        return out

    async def get_positions(self) -> list[dict[str, Any]]:
        if not self.has_credentials():
            return []
        resp = await self._get("/v2/positions")
        if resp is None:
            return []
        # /v2/positions returns a JSON array, not a {data: ...} envelope.
        if isinstance(resp, list):
            return resp
        return []

    async def get_balance(self) -> dict[str, float]:
        if not self.has_credentials():
            return {}
        resp = await self._get("/v2/account")
        if resp is None or not isinstance(resp, dict):
            return {}
        out: dict[str, float] = {}
        # Alpaca exposes balance under multiple keys; surface the ones
        # downstream reconcilers care about.
        for key in ("cash", "equity", "portfolio_value", "buying_power", "non_marginable_buying_power"):
            raw = resp.get(key)
            if raw is not None:
                try:
                    out[key] = float(raw)
                except (TypeError, ValueError):
                    continue
        return out

    async def get_net_liquidation(self) -> float | None:
        balance = await self.get_balance()
        raw = balance.get("equity") or balance.get("portfolio_value")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def build_order_payload(
        self,
        request: OrderRequest,
        *,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        is_crypto = bool(_alpaca_crypto_base(request.symbol))
        sym = _alpaca_symbol(request.symbol, is_crypto=is_crypto)
        order_type = _alpaca_order_type(request.order_type)
        # Crypto on Alpaca requires GTC; equities default to DAY.
        tif = "gtc" if is_crypto else "day"
        side = "buy" if request.side is Side.BUY else "sell"
        payload: dict[str, Any] = {
            "symbol": sym,
            "qty": _alpaca_quantity(request.qty, is_crypto=is_crypto),
            "side": side,
            "type": order_type,
            "time_in_force": tif,
        }
        cid = client_order_id or request.client_order_id or self.idempotency_key(request)
        # Alpaca client_order_id has length and charset constraints (<= 48
        # chars, alnum + dashes/underscores). Truncate defensively — we
        # still echo the full id back into raw["client_order_id"].
        payload["client_order_id"] = cid[:48]
        if request.price is not None and order_type in {"limit", "stop_limit"}:
            payload["limit_price"] = str(request.price)
        if request.stop_price is not None and order_type in {"stop", "stop_limit"}:
            payload["stop_price"] = str(request.stop_price)

        # ── BRACKET ATTACHMENT (entry orders only, EQUITY only) ────
        # When BOTH stop_price and target_price are populated AND this
        # is NOT a reduce-only exit AND the symbol is NOT crypto, attach
        # a server-side bracket so the parent + TP + SL become an OCO
        # group at Alpaca. Mirrors the parent + STP + LMT layout that
        # ibkr_live._build_futures_bracket_orders produces for futures.
        #
        # CRYPTO EXCEPTION: Alpaca rejects crypto orders with any
        # `order_class` other than `simple` — sending `bracket`/`oto`/
        # `oco`/`otoco` returns HTTP 422 ``{"code":42210000,"message":
        # "crypto orders not allowed for advanced order_class: otoco"}``.
        # Caught live 2026-05-06 when btc_optimized + eth_sage_daily
        # entries hit the broker. For crypto, the supervisor's exit
        # logic (``_maybe_exit`` in jarvis_strategy_supervisor.py) owns
        # stop/target management — it watches each tick's bar and
        # submits a reduce-only sell when the price pierces a level.
        # That path was already in place for the IBKR-PAXOS crypto
        # route, which has the same no-server-side-bracket constraint.
        if (
            not request.reduce_only
            and request.stop_price is not None
            and request.target_price is not None
            and not is_crypto
        ):
            payload["order_class"] = "bracket"
            payload["take_profit"] = {
                "limit_price": _format_alpaca_price(float(request.target_price)),
            }
            # Alpaca's stop_loss leg accepts an optional limit_price to
            # cap slippage on the protective side. We deliberately omit
            # it — sending only stop_price gives a STP (market on stop)
            # which always exits the position rather than risking a
            # missed protective fill if price gaps through the limit.
            payload["stop_loss"] = {
                "stop_price": _format_alpaca_price(float(request.stop_price)),
            }
        return payload

    # ------------------------------------------------------------------
    # HTTP transport — lazy httpx; every call degrades cleanly to None.
    # ------------------------------------------------------------------

    def _session_headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.config.api_key_id,
            "APCA-API-SECRET-KEY": self.config.api_secret_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "eta-engine-alpaca/0.1",
        }

    async def _post_order_with_error(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """POST an order and return (parsed_body, error_dict).

        Returns
        -------
        parsed_body, error_dict
            ``parsed_body`` is the decoded JSON on success; ``None``
            otherwise. ``error_dict`` is populated when Alpaca returned
            a non-2xx — it carries ``status_code``, ``body`` (truncated),
            and ``alpaca_code`` if present in the response. ``error_dict``
            stays ``None`` for transport-level failures (no httpx, network
            exception) so the caller can distinguish broker reject from
            transport degrade.
        """
        try:
            import httpx  # noqa: PLC0415 — lazy import keeps adapter optional
        except ImportError:
            logger.debug("alpaca: httpx unavailable; degrading to mock fallback")
            return None, None
        url = f"{self.config.base_url}/v2/orders"
        try:
            async with httpx.AsyncClient(timeout=ALPACA_HTTP_TIMEOUT_S) as client:
                resp = await client.post(url, json=payload, headers=self._session_headers())
        except Exception as exc:  # noqa: BLE001 — transport errors degrade cleanly
            logger.info("alpaca: order POST network error: %s", exc)
            return None, None
        if resp.status_code >= 400:
            body_text = resp.text[:400]
            logger.info(
                "alpaca: order POST rejected status=%d body=%s",
                resp.status_code,
                body_text,
            )
            error: dict[str, Any] = {
                "status_code": resp.status_code,
                "body": body_text,
            }
            try:
                error_json = resp.json()
                if isinstance(error_json, dict):
                    error["alpaca_code"] = error_json.get("code")
                    error["alpaca_message"] = error_json.get("message")
            except ValueError:
                pass
            return None, error
        try:
            return resp.json(), None
        except ValueError as exc:
            logger.info("alpaca: order POST invalid JSON: %s", exc)
            return None, None

    async def _post_order(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Back-compat shim: returns parsed body or ``None`` on any failure."""
        body, _err = await self._post_order_with_error(payload)
        return body

    async def _get(self, path: str) -> Any:  # noqa: ANN401 — list or dict per endpoint
        """Read-only GET with transient-error retry.

        Wraps :meth:`_get_once` with the standard transient retry decorator
        from :mod:`eta_engine.venues.connection` (3 attempts, 0.5s/1.5s/4.5s
        backoff). Deterministic broker rejects (4xx other than 404 / non-2xx
        bodies) still return ``None`` to preserve the existing
        "swallow + degrade" contract callers depend on. ImportError on
        httpx is treated as a permanent stub mode and returns ``None``
        without retries (httpx unavailable does not become available 4s later).
        """
        try:
            import httpx  # noqa: PLC0415, F401 — only checked for ImportError here
        except ImportError:
            return None
        from eta_engine.venues.connection import (  # noqa: PLC0415
            DeterministicBrokerReject,
            with_transient_retry,
        )

        retrying = with_transient_retry(logger_name=__name__)(self._get_once)
        try:
            return await retrying(path)
        except DeterministicBrokerReject:
            # Already logged at the rejection site — return None for the
            # legacy caller contract.
            return None
        except Exception as exc:  # noqa: BLE001
            # Retries exhausted on a transient error. Fall through to the
            # legacy degrade-to-None contract callers depend on.
            logger.debug("alpaca: GET %s exhausted retries: %s", path, exc)
            return None

    async def _get_once(self, path: str) -> Any:  # noqa: ANN401
        """Single GET attempt. Lets transient httpx errors propagate so
        :func:`with_transient_retry` can decide whether to retry.

        Non-2xx responses (other than 404) raise
        :class:`DeterministicBrokerReject` so we don't waste retries on
        a 403/422 that will not fix itself.
        """
        import httpx  # noqa: PLC0415

        from eta_engine.venues.connection import DeterministicBrokerReject  # noqa: PLC0415

        url = f"{self.config.base_url}{path}"
        async with httpx.AsyncClient(timeout=ALPACA_HTTP_TIMEOUT_S) as client:
            resp = await client.get(url, headers=self._session_headers())
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            logger.info(
                "alpaca: GET %s returned %d body=%s",
                path,
                resp.status_code,
                resp.text[:200],
            )
            raise DeterministicBrokerReject(
                f"alpaca GET {path} status={resp.status_code} body={resp.text[:200]}",
            )
        try:
            return resp.json()
        except ValueError:
            return None

    async def _delete_order(self, order_id: str) -> bool:
        try:
            import httpx  # noqa: PLC0415 — lazy import
        except ImportError:
            return False
        url = f"{self.config.base_url}/v2/orders/{order_id}"
        try:
            async with httpx.AsyncClient(timeout=ALPACA_HTTP_TIMEOUT_S) as client:
                resp = await client.delete(url, headers=self._session_headers())
        except Exception as exc:  # noqa: BLE001
            logger.debug("alpaca: DELETE %s network error: %s", order_id, exc)
            return False
        # 204 = canceled successfully; 207/422 = couldn't cancel (terminal state).
        return resp.status_code in (200, 204, 207)


def alpaca_paper_readiness(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """JSON-safe readiness summary for operator surfaces (mirrors tastytrade).

    The dashboard ``/api/brokers`` endpoint serializes this for the
    "Broker Paper" card. We deliberately keep this synchronous — the
    endpoint runs on each request and an ``asyncio.run`` round-trip per
    request would add 100-300 ms of latency per dashboard render. The
    live-probe (``/v2/account``) only fires from ``AlpacaVenue.connect()``,
    which the supervisor invokes once per startup.
    """
    config = AlpacaConfig.from_env(env)
    missing = config.missing_requirements()
    return {
        "adapter_available": True,
        "ready": not missing,
        "mode": "paper",
        "base_url": config.base_url,
        "key_id_configured": bool(config.api_key_id),
        "secret_configured": bool(config.api_secret_key),
        "missing": missing,
        "reason": "ready" if not missing else "missing paper-routing configuration",
        "operator_action": (
            "ready" if not missing else "Set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY or their *_FILE variants."
        ),
        "min_cost_basis_usd": ALPACA_CRYPTO_MIN_COST_BASIS_USD,
        "supported_crypto_bases": sorted(_ALPACA_CRYPTO_BASES),
        "checked_utc": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# Helpers (module-level, mirror tastytrade.py for cross-venue consistency)
# ---------------------------------------------------------------------------


def _alpaca_crypto_base(symbol: str) -> str | None:
    """Return canonical crypto base ticker, or ``None`` if not crypto.

    Accepts: ``BTC``, ``BTCUSD``, ``BTCUSDT``, ``BTC/USD``, ``BTC-USD``,
    ``/BTC``, case-insensitive. Returns the upper-case base (``"BTC"``).
    """
    raw = (symbol or "").strip().upper().lstrip("/")
    if not raw:
        return None
    head = raw.split("/", 1)[0].split("-", 1)[0].split("_", 1)[0]
    for suffix in ("USDT", "USDC", "USD"):
        if head.endswith(suffix) and len(head) > len(suffix):
            head = head[: -len(suffix)]
            break
    return head if head in _ALPACA_CRYPTO_BASES else None


def _alpaca_symbol(symbol: str, *, is_crypto: bool) -> str:
    if is_crypto:
        base = _alpaca_crypto_base(symbol) or symbol.upper().strip().lstrip("/").split("/", 1)[0]
        return f"{base}/USD"
    # Equity: Alpaca uses bare tickers (AAPL, SPY).
    return symbol.upper().strip().lstrip("/")


def _alpaca_quantity(qty: float, *, is_crypto: bool) -> str:
    """Format ``qty`` for an Alpaca order. Always JSON-string for safety.

    Crypto: preserve exact decimal precision via ``str(float)`` → Decimal,
    then trim trailing zeros. ``f"{qty:.8f}"`` rounds *to nearest*, which
    can round UP (e.g. 0.002375228 -> 0.00237523) and produce an exit
    order that requests more than the position holds. Alpaca rejects
    those with HTTP 403 ``insufficient balance``. Using ``str(qty)``
    keeps the shortest-decimal-round-trip representation Python uses,
    which matches the precision Alpaca returns for position quantities.
    """
    if is_crypto:
        from decimal import Decimal

        # str(float) returns the shortest decimal that round-trips to the
        # same float — preserves exact qty for any reasonable input
        # (including position sizes pulled back from Alpaca's API).
        d = Decimal(str(qty))
        s = format(d, "f").rstrip("0").rstrip(".")
        return s or "0"
    if qty < 1:
        # Alpaca supports fractional equity, but we don't use that path here.
        return f"{qty:.6f}".rstrip("0").rstrip(".") or "0"
    return str(int(qty))


def _alpaca_order_type(order_type: OrderType) -> str:
    if order_type is OrderType.MARKET:
        return "market"
    if order_type in {OrderType.LIMIT, OrderType.POST_ONLY}:
        return "limit"
    raise ValueError(f"unsupported Alpaca order_type={order_type!r}")


def _format_alpaca_price(price: float) -> str:
    """Format a USD price for an Alpaca bracket child leg.

    Alpaca accepts string-encoded prices on bracket children. Two cents
    of precision is plenty for crypto bracket levels (BTC at $80k uses
    full cents anyway) and keeps the JSON terse. The IBKR live venue
    rounds bracket levels at the strategy layer, so by this point the
    price is already an exit-quality number — we just stringify it.
    """
    return f"{float(price):.2f}"


def _validate_bracket_geometry(
    *,
    side: Side,
    ref_price: float | None,
    stop_price: float,
    target_price: float,
) -> str | None:
    """Sanity-check that the bracket OCO siblings sit on the right side.

    Returns ``None`` when the geometry is valid (or when ``ref_price``
    is ``None`` so we cannot reason about it — Alpaca will then enforce
    server-side). Returns a human-readable detail string when the
    bracket would invert into an instant-fill or broker-reject.

    Rules:
        BUY:  stop_price < ref_price < target_price
        SELL: target_price < ref_price < stop_price

    Equality on either side counts as invalid: a STP touching the entry
    converts to MKT immediately and a TP at entry would close before
    the position could ever profit.
    """
    if ref_price is None:
        # Market-entry brackets without a reference price: defer to
        # Alpaca's server-side validation. We could instead require a
        # ref_price for all brackets, but that would force the supervisor
        # to pass a working last-trade as price= even for MKT entries.
        return None
    if stop_price <= 0 or target_price <= 0:
        return f"non-positive bracket level (stop={stop_price}, target={target_price})"
    ref = float(ref_price)
    if side is Side.BUY:
        if not (stop_price < ref < target_price):
            return (
                f"BUY bracket requires stop < entry < target; got stop={stop_price}, entry={ref}, target={target_price}"
            )
    else:  # Side.SELL
        if not (target_price < ref < stop_price):
            return (
                f"SELL bracket requires target < entry < stop; got "
                f"target={target_price}, entry={ref}, stop={stop_price}"
            )
    return None


# ---------------------------------------------------------------------------
# Env merge — mirrors tastytrade._broker_paper_env so the same broker_paper.env
# file feeds both adapters. Auto-discovers default secret-file paths under
# firm_command_center/secrets/.
# ---------------------------------------------------------------------------


def _env_or_file(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name) or "").strip()
    if value:
        return value
    file_value = str(env.get(f"{name}_FILE") or "").strip()
    if not file_value:
        return ""
    path = Path(file_value).expanduser()
    if not path.exists():
        raise AlpacaConfigError(f"{name}_FILE does not exist: {path}")
    return path.read_text(encoding="utf-8-sig").strip()


def _broker_paper_env(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
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
            "ALPACA_API_KEY_ID_FILE": default_root / "alpaca_api_key_id.txt",
            "ALPACA_API_SECRET_KEY_FILE": default_root / "alpaca_api_secret_key.txt",
        }
        for key, path in defaults.items():
            if not str(env_map.get(key) or "").strip() and path.exists():
                env_map[key] = str(path)
    return env_map


def _runtime_secret_root(env: Mapping[str, str]) -> Path:
    runtime_root = (
        str(env.get("ETA_RUNTIME_ROOT") or "").strip()
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
