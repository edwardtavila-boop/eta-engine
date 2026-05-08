"""Unified broker connection probes and report writing.

This module gives the repo a single automation surface for broker
connectivity:

* build supported venue adapters from secrets,
* probe them read-only,
* preserve unsupported broker names as explicit ``UNAVAILABLE`` rows,
* and write a compact JSON report for preflight / operator use.

24/7 framework additions (2026-05-06)
-------------------------------------
* :class:`IbgConnectionMonitor` -- a thin probe that consults the IB
  Gateway TCP port (default 4002) and synchronizes
  ``var/eta_engine/state/order_entry_hold.json`` with scope=``ibkr``.
  When the port is refused, the monitor sets the hold so futures bots
  pause; when the port comes back AND ``LiveIbkrVenue.connect()``
  succeeds, the monitor clears the hold automatically.
* :func:`with_transient_retry` -- retry decorator (3 attempts,
  exponential 0.5s/1.5s/4s) for transient network errors. Deterministic
  broker rejects (insufficient funds, 403 cost-basis, etc.) opt out via
  the ``DeterministicBrokerReject`` exception.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from eta_engine.core.secrets import (
    BYBIT_API_KEY,
    BYBIT_API_SECRET,
    OKX_API_KEY,
    OKX_API_SECRET,
    OKX_PASSPHRASE,
    SECRETS,
    TRADOVATE_APP_ID,
    TRADOVATE_APP_SECRET,
    TRADOVATE_CID,
    TRADOVATE_PASSWORD,
    TRADOVATE_USERNAME,
)
from eta_engine.venues.base import ConnectionStatus, VenueBase, VenueConnectionReport
from eta_engine.venues.bybit import BybitVenue
from eta_engine.venues.ibkr import IbkrClientPortalVenue
from eta_engine.venues.okx import OkxVenue
from eta_engine.venues.router import (
    ACTIVE_FUTURES_VENUES,
    DEFAULT_FUTURES_VENUE,
    DORMANT_BROKERS,
    IS_US_PERSON,
    NON_FCM_VENUES,
)
from eta_engine.venues.tastytrade import TastytradeVenue
from eta_engine.venues.tradovate import TradovateVenue

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

_LOG = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config.json"
DEFAULT_OUT_DIR = ROOT / "docs" / "broker_connections"

_UNAVAILABLE_NOTES: dict[str, str] = {
    "bitget": "Bitget adapter not implemented in the current repo",
    "binance": "Binance adapter not implemented in the current repo",
    "coinbase": "Coinbase adapter not implemented in the current repo",
    "kraken": "Kraken adapter not implemented in the current repo",
    "bitstamp": "Bitstamp adapter not implemented in the current repo",
    "gemini": "Gemini adapter not implemented in the current repo",
    "deribit": "Deribit adapter not implemented in the current repo",
}

_US_PERSON_BLOCKED_VENUES: frozenset[str] = frozenset(
    set(NON_FCM_VENUES) | {"bitget", "binance"},
)


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        item = item.strip().lower()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _extend_names(target: list[str], value: object) -> None:
    if isinstance(value, str):
        target.append(value)
        return
    if isinstance(value, list):
        target.extend(str(item) for item in value if item is not None and str(item).strip())


def _load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"config unreadable: {exc}") from exc


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_sha256(payload: object) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return _sha256_bytes(text.encode("utf-8"))


def _strip_broker_connections_hash_fields(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    sanitized.pop("broker_connections_sha256", None)
    return sanitized


def canonical_broker_connections_hash_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the exact payload shape that broker connection hashes are based on."""
    return _strip_broker_connections_hash_fields(payload)


def _secret(key: str) -> str:
    """Resolve a broker secret with fail-closed live-mode enforcement.

    When ``ETA_LIVE_MODE=1`` is set, missing/empty secrets raise
    ``RuntimeError`` so misconfigured creds cannot silently fall through
    to a mock adapter (which would have us routing paper orders while
    the operator believes the system is live). Without ``ETA_LIVE_MODE``
    set, dev/test runs still get the empty-string fallback — but every
    such fallback emits a WARNING so the paper-mode downgrade leaves a
    paper trail in the logs.
    """
    value = SECRETS.get(key, required=False) or ""
    if not value:
        live_mode = _truthy(os.environ.get("ETA_LIVE_MODE"))
        if live_mode:
            raise RuntimeError(
                f"ETA_LIVE_MODE=1 but broker secret missing for {key}; "
                "refusing to silently fall through to mock"
            )
        _LOG.warning(
            "broker secret missing for %s; falling through to mock adapter "
            "(set ETA_LIVE_MODE=1 to fail closed)",
            key,
        )
    return value


def _build_bybit(*, testnet: bool) -> BybitVenue:
    return BybitVenue(
        api_key=_secret(BYBIT_API_KEY),
        api_secret=_secret(BYBIT_API_SECRET),
        testnet=testnet,
    )


def _build_okx() -> OkxVenue:
    return OkxVenue(
        api_key=_secret(OKX_API_KEY),
        api_secret=_secret(OKX_API_SECRET),
        passphrase=_secret(OKX_PASSPHRASE),
    )


def _build_tradovate(*, demo: bool) -> TradovateVenue:
    return TradovateVenue(
        api_key=_secret(TRADOVATE_USERNAME),
        api_secret=_secret(TRADOVATE_PASSWORD),
        demo=demo,
        app_id=_secret(TRADOVATE_APP_ID) or "EtaEngine",
        cid=_secret(TRADOVATE_CID),
        app_secret=_secret(TRADOVATE_APP_SECRET),
        account_id=os.environ.get("TRADOVATE_ACCOUNT_ID"),
    )


def _build_tastytrade() -> TastytradeVenue:
    return TastytradeVenue()


def _build_ibkr() -> IbkrClientPortalVenue:
    return IbkrClientPortalVenue()


@dataclass
class BrokerConnectionSummary:
    """Serializable result for a broker connection sweep."""

    generated_at_utc: datetime
    configured_brokers: list[str]
    reports: list[VenueConnectionReport]
    config_path: str
    source: str = "broker_connect"

    def counts(self) -> dict[str, int]:
        counts = {
            "ready": 0,
            "degraded": 0,
            "stubbed": 0,
            "failed": 0,
            "unavailable": 0,
        }
        for report in self.reports:
            key = report.status.value.lower()
            if key in counts:
                counts[key] += 1
        return counts

    def overall_ok(self) -> bool:
        return all(report.status is not ConnectionStatus.FAILED for report in self.reports)

    def health(self) -> str:
        counts = self.counts()
        if counts["failed"] > 0:
            return "RED"
        if counts["degraded"] > 0:
            return "YELLOW"
        return "GREEN"

    def to_dict(self) -> dict[str, Any]:
        counts = self.counts()
        return {
            "generated_at_utc": self.generated_at_utc.isoformat(),
            "config_path": self.config_path,
            "configured_brokers": self.configured_brokers,
            "source": self.source,
            "policy": {
                "active_futures_brokers": list(ACTIVE_FUTURES_VENUES),
                "dormant_brokers": sorted(DORMANT_BROKERS),
                "is_us_person": IS_US_PERSON,
                "blocked_live_venues": sorted(_US_PERSON_BLOCKED_VENUES) if IS_US_PERSON else [],
            },
            "reports": [report.to_dict() for report in self.reports],
            "summary": {
                "health": self.health(),
                "overall_ok": self.overall_ok(),
                "ready": counts["ready"],
                "degraded": counts["degraded"],
                "stubbed": counts["stubbed"],
                "failed": counts["failed"],
                "unavailable": counts["unavailable"],
            },
        }


class BrokerConnectionManager:
    """Build and probe supported broker adapters from config + secrets."""

    def __init__(
        self,
        *,
        bybit: BybitVenue | None = None,
        okx: OkxVenue | None = None,
        tradovate: TradovateVenue | None = None,
        tastytrade: TastytradeVenue | None = None,
        ibkr: IbkrClientPortalVenue | None = None,
        config_path: Path = DEFAULT_CONFIG_PATH,
        bybit_testnet: bool | None = None,
        tradovate_demo: bool | None = None,
    ) -> None:
        if bybit_testnet is None:
            bybit_testnet = _truthy(os.environ.get("BYBIT_TESTNET"))
        if tradovate_demo is None:
            tradovate_demo = not _truthy(os.environ.get("TRADOVATE_LIVE"))
        self.config_path = config_path
        self.bybit = bybit or _build_bybit(testnet=bybit_testnet)
        self.okx = okx or _build_okx()
        self.tradovate = tradovate or _build_tradovate(demo=tradovate_demo)
        self.tastytrade = tastytrade or _build_tastytrade()
        self.ibkr = ibkr or _build_ibkr()
        self._venue_map: dict[str, VenueBase] = {
            self.bybit.name: self.bybit,
            self.okx.name: self.okx,
            self.tradovate.name: self.tradovate,
            self.tastytrade.name: self.tastytrade,
            "tasty": self.tastytrade,
            "tasty_trades": self.tastytrade,
            "tastyworks": self.tastytrade,
            self.ibkr.name: self.ibkr,
            "interactive_brokers": self.ibkr,
        }

    @classmethod
    def from_env(
        cls,
        *,
        config_path: Path = DEFAULT_CONFIG_PATH,
        bybit_testnet: bool | None = None,
        tradovate_demo: bool | None = None,
    ) -> BrokerConnectionManager:
        return cls(
            config_path=config_path,
            bybit_testnet=bybit_testnet,
            tradovate_demo=tradovate_demo,
        )

    def configured_brokers(self) -> list[str]:
        cfg = _load_config(self.config_path)
        names: list[str] = []
        _extend_names(names, cfg.get("brokers"))
        _extend_names(names, cfg.get("venues"))
        if names:
            return _dedupe(names)

        execution = cfg.get("execution")
        if isinstance(execution, dict):
            _extend_names(names, execution.get("brokers"))
            futures = execution.get("futures")
            if isinstance(futures, dict):
                dormant: list[str] = []
                _extend_names(dormant, futures.get("broker_dormant"))
                _extend_names(names, futures.get("brokers"))
                _extend_names(
                    names,
                    [
                        futures.get("broker_primary"),
                        futures.get("broker_backup"),
                    ],
                )
                _extend_names(names, futures.get("broker_backups"))
                dormant_names = set(_dedupe(dormant)) | set(DORMANT_BROKERS)
                names = [name for name in names if name.strip().lower() not in dormant_names]
        if not names:
            # Last-resort default when no config provides broker names.
            # IBKR + Tastytrade are the active futures brokers per
            # operator mandate 2026-04-24; Tradovate is DORMANT and
            # deliberately excluded from this fallback list.
            names = list(ACTIVE_FUTURES_VENUES)
        return _dedupe(names)

    def _venue_for_name(self, name: str) -> VenueBase | None:
        return self._venue_map.get(name.strip().lower())

    async def connect_name(self, name: str) -> VenueConnectionReport:
        clean = name.strip().lower()
        policy_blocked = self._policy_blocked_report(clean)
        if policy_blocked is not None:
            return policy_blocked
        venue = self._venue_for_name(clean)
        if venue is None:
            note = _UNAVAILABLE_NOTES.get(clean, "adapter not implemented in the current repo")
            return VenueConnectionReport(
                venue=clean,
                status=ConnectionStatus.UNAVAILABLE,
                creds_present=False,
                details={"reason": note},
                error=note,
            )
        try:
            report = await venue.connect()
        except Exception as exc:  # noqa: BLE001
            return VenueConnectionReport(
                venue=clean,
                status=ConnectionStatus.FAILED,
                creds_present=bool(venue.has_credentials()),
                details={"endpoint": venue.connection_endpoint() or ""},
                error=f"{type(exc).__name__}: {exc}",
            )
        if "endpoint" not in report.details:
            endpoint = venue.connection_endpoint()
            if endpoint:
                report.details["endpoint"] = endpoint
        return report

    def _policy_blocked_report(self, clean: str) -> VenueConnectionReport | None:
        venue = self._venue_for_name(clean)
        creds_present = bool(venue.has_credentials()) if venue is not None else False
        if clean in DORMANT_BROKERS:
            reason = (
                f"{clean} is DORMANT; use active futures brokers "
                f"{', '.join(ACTIVE_FUTURES_VENUES)} unless the operator reactivates it in code and docs"
            )
            return VenueConnectionReport(
                venue=clean,
                status=ConnectionStatus.FAILED,
                creds_present=creds_present,
                details={
                    "policy_state": "dormant",
                    "active_substitute": DEFAULT_FUTURES_VENUE,
                    "reason": reason,
                },
                error=reason,
            )
        if IS_US_PERSON and clean in _US_PERSON_BLOCKED_VENUES:
            reason = (
                f"{clean} is blocked for US-person live readiness; "
                "route live exposure through US-legal futures brokers instead"
            )
            return VenueConnectionReport(
                venue=clean,
                status=ConnectionStatus.FAILED,
                creds_present=creds_present,
                details={
                    "policy_state": "blocked_us_person",
                    "active_substitute": DEFAULT_FUTURES_VENUE,
                    "reason": reason,
                },
                error=reason,
            )
        return None

    async def connect(self, names: Iterable[str] | None = None) -> BrokerConnectionSummary:
        selected = _dedupe(names if names is not None else self.configured_brokers())
        reports = await asyncio.gather(*(self.connect_name(name) for name in selected))
        return BrokerConnectionSummary(
            generated_at_utc=datetime.now(UTC),
            configured_brokers=selected,
            reports=reports,
            config_path=str(self.config_path),
        )


def write_broker_connection_report(
    summary: BrokerConnectionSummary,
    *,
    out_dir: Path = DEFAULT_OUT_DIR,
    stem: str = "broker_connections",
) -> tuple[Path, Path]:
    """Persist a timestamped + latest broker connection JSON bundle."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = summary.generated_at_utc.strftime("%Y%m%dT%H%M%SZ")
    artifact = out_dir / f"{stem}_{stamp}.json"
    latest = out_dir / f"{stem}_latest.json"
    payload = summary.to_dict()
    payload["broker_connections_sha256"] = _json_sha256(canonical_broker_connections_hash_payload(payload))
    payload_text = json.dumps(payload, indent=2, default=str) + "\n"
    artifact.write_text(payload_text, encoding="utf-8")
    latest.write_text(payload_text, encoding="utf-8")
    return artifact, latest


# ─── 24/7 framework: IB Gateway connection monitor ────────────────────────
#
# Default IB Gateway paper port. The supervisor's _ensure_connected uses
# the same value, so the monitor's probe maps 1:1 onto reachability.
_IBG_DEFAULT_HOST = "127.0.0.1"
_IBG_DEFAULT_PORT = 4002
_IBG_PROBE_TIMEOUT_S = 2.0


class DeterministicBrokerReject(Exception):  # noqa: N818
    """Raised by venue adapters to opt OUT of transient retries.

    Use this for rejects that will not succeed on retry: insufficient
    funds, crypto trading disabled, 403 cost-basis under minimum, etc.
    Transport errors (ReadTimeout, ConnectionRefusedError, ConnectionResetError)
    do NOT raise this — they're considered transient and will retry.
    """


def _is_transient_error(exc: BaseException) -> bool:
    """Heuristic for retry decisions.

    Returns True for httpx/urllib transport errors and Python's stdlib
    socket/connection errors. Returns False for explicit
    :class:`DeterministicBrokerReject` and for anything that doesn't
    look like a network blip.
    """
    if isinstance(exc, DeterministicBrokerReject):
        return False
    name = type(exc).__name__
    transient_class_names = {
        "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
        "PoolTimeout", "ReadError", "WriteError", "ProtocolError",
        "RemoteProtocolError", "NetworkError", "TransportError",
        "TimeoutError",
    }
    if name in transient_class_names:
        return True
    if isinstance(exc, (
        ConnectionError, ConnectionRefusedError, ConnectionResetError,
        ConnectionAbortedError, BrokenPipeError, asyncio.TimeoutError,
        TimeoutError, socket.timeout, OSError,
    )):
        # OSError is broad — narrow it via errno where we can. The common
        # "broker is fine, network is busy" errnos are ECONNREFUSED (111),
        # ECONNRESET (104), ETIMEDOUT (110), EPIPE (32), EHOSTUNREACH (113),
        # ENETUNREACH (101). Treat everything else as deterministic so a
        # truly weird OSError doesn't get spuriously retried.
        if isinstance(exc, OSError) and not isinstance(
            exc, (ConnectionError, ConnectionRefusedError, ConnectionResetError,
                  ConnectionAbortedError, BrokenPipeError, socket.timeout),
        ):
            transient_errnos = {32, 101, 104, 110, 111, 113, 10053, 10054, 10060, 10061}
            return getattr(exc, "errno", None) in transient_errnos
        return True
    return False


_T = TypeVar("_T")


def with_transient_retry(
    *,
    attempts: int = 3,
    base_delay_s: float = 0.5,
    backoff_factor: float = 3.0,
    logger_name: str = __name__,
) -> Callable[[Callable[..., Awaitable[_T]]], Callable[..., Awaitable[_T]]]:
    """Decorator factory for async venue calls that should retry on
    transient network errors (httpx ReadTimeout, ConnectionRefusedError, etc.).

    Backoff schedule with defaults: 0.5s, 1.5s, 4.5s. Caller can override
    if a venue needs a tighter or looser cadence.

    Deterministic broker rejects (raised as
    :class:`DeterministicBrokerReject`) skip the retry path entirely so
    a 403 cost-basis or "crypto disabled" never wastes time on three
    attempts.

    Used by ``LiveIbkrVenue`` and ``AlpacaVenue`` for read-only
    connectivity calls. Order placement does NOT use this decorator
    because retried order POSTs without explicit idempotency keys can
    multiply executions on the broker side.
    """
    log = logging.getLogger(logger_name)

    def _decorate(fn: Callable[..., Awaitable[_T]]) -> Callable[..., Awaitable[_T]]:
        @functools.wraps(fn)
        async def _wrapped(*args: object, **kwargs: object) -> _T:
            last_exc: BaseException | None = None
            for attempt in range(1, max(1, int(attempts)) + 1):
                try:
                    return await fn(*args, **kwargs)
                except DeterministicBrokerReject:
                    raise
                except BaseException as exc:  # noqa: BLE001 — explicit dispatch below
                    if not _is_transient_error(exc):
                        raise
                    last_exc = exc
                    if attempt >= attempts:
                        log.warning(
                            "transient retry exhausted for %s after %d attempts: %s",
                            getattr(fn, "__qualname__", fn.__name__), attempt, exc,
                        )
                        raise
                    delay = base_delay_s * (backoff_factor ** (attempt - 1))
                    log.info(
                        "transient error on %s attempt %d/%d (%s); retrying in %.2fs",
                        getattr(fn, "__qualname__", fn.__name__),
                        attempt, attempts, exc, delay,
                    )
                    await asyncio.sleep(delay)
            # Unreachable: either we returned, raised, or the loop exhausted.
            assert last_exc is not None
            raise last_exc

        return _wrapped

    return _decorate


def _probe_ibgateway_port(
    host: str = _IBG_DEFAULT_HOST,
    port: int = _IBG_DEFAULT_PORT,
    timeout: float = _IBG_PROBE_TIMEOUT_S,
) -> bool:
    """Return True if the TCP port accepts a connection within ``timeout``.

    Uses the stdlib socket so the probe doesn't pull in httpx/ib_insync
    on a process that just wants to know "is the gateway reachable".
    Any exception => unreachable. Cleanly closes the socket.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (TimeoutError, OSError):
        return False


@dataclass
class IbgMonitorState:
    """Snapshot of the monitor's last decision for tests + diagnostics."""

    port_reachable: bool = False
    venue_connect_ok: bool = False
    last_probe_ts: str = ""
    last_action: str = ""  # "set_hold", "cleared_hold", "noop"
    last_reason: str = ""


class IbgConnectionMonitor:
    """Probe IB Gateway and synchronize the scope=ibkr order-entry hold.

    Behaviour
    ---------
    Each call to :meth:`tick` does:

    1. Probe ``socket.create_connection((host, port), timeout=2)``.
    2. If port is REFUSED:
         * Set ``order_entry_hold.json`` with ``scope="ibkr"`` and
           ``reason="ibgateway_unreachable_port_<port>"``.
         * Return state; futures bots stay parked, crypto keeps trading.
    3. If port is UP:
         * Try :meth:`LiveIbkrVenue.connect`. If it succeeds, CLEAR the
           hold so futures resumes.
         * If it fails (likely 2FA wait), keep the scope=ibkr hold
           in place but log a clear actionable hint.
    4. The hold writer is the existing
       :func:`runtime_order_hold.write_order_entry_hold` which writes
       atomically via .tmp + os.replace.

    The class is deliberately pull-based: the supervisor calls
    :meth:`tick` from its main loop (or the watchdog can call it). No
    thread / asyncio loop is started inside the monitor itself, which
    keeps tests synchronous-friendly.
    """

    def __init__(
        self,
        *,
        venue: object | None = None,
        host: str = _IBG_DEFAULT_HOST,
        port: int = _IBG_DEFAULT_PORT,
        probe_timeout_s: float = _IBG_PROBE_TIMEOUT_S,
        probe_fn: Callable[[str, int, float], bool] | None = None,
        write_hold_fn: Callable[..., Path] | None = None,
        load_hold_fn: Callable[..., Any] | None = None,
        connect_timeout_s: float = 6.0,
    ) -> None:
        self._venue = venue
        self._host = host
        self._port = int(port)
        self._probe_timeout_s = float(probe_timeout_s)
        self._connect_timeout_s = float(connect_timeout_s)
        self._probe_fn = probe_fn or _probe_ibgateway_port
        # Lazy import the runtime_order_hold helpers so a circular import
        # under the venues package never trips the broker connection
        # report code path.
        if write_hold_fn is None or load_hold_fn is None:
            from eta_engine.scripts.runtime_order_hold import (
                load_order_entry_hold,
                write_order_entry_hold,
            )
            self._write_hold = write_hold_fn or write_order_entry_hold
            self._load_hold = load_hold_fn or load_order_entry_hold
        else:
            self._write_hold = write_hold_fn
            self._load_hold = load_hold_fn

        self.state = IbgMonitorState()

    def tick(self) -> IbgMonitorState:
        """Run one probe -> hold-sync cycle. Returns the updated state."""
        now_iso = datetime.now(UTC).isoformat()
        port_ok = bool(self._probe_fn(self._host, self._port, self._probe_timeout_s))
        self.state.port_reachable = port_ok
        self.state.last_probe_ts = now_iso

        if not port_ok:
            reason = f"ibgateway_unreachable_port_{self._port}"
            try:
                self._write_hold(
                    active=True, reason=reason, scope="ibkr",
                )
                self.state.last_action = "set_hold"
                self.state.last_reason = reason
            except Exception as exc:  # noqa: BLE001
                self.state.last_action = "noop"
                self.state.last_reason = f"hold_write_failed:{type(exc).__name__}"
            self.state.venue_connect_ok = False
            return self.state

        # Port is up — try to actually connect via the venue. If that
        # succeeds we clear the hold automatically; if it keeps timing
        # out we keep the hold and surface a 2FA-wait hint.
        connect_ok = False
        try:
            connect_ok = self._venue_connect_blocking()
        except Exception as exc:  # noqa: BLE001
            connect_ok = False
            self.state.last_reason = f"connect_raised:{type(exc).__name__}"

        self.state.venue_connect_ok = connect_ok

        if connect_ok:
            try:
                self._write_hold(
                    active=False, reason="ibgateway_recovered", scope="all",
                )
                self.state.last_action = "cleared_hold"
                self.state.last_reason = "ibgateway_recovered"
            except Exception as exc:  # noqa: BLE001
                self.state.last_action = "noop"
                self.state.last_reason = f"clear_failed:{type(exc).__name__}"
            return self.state

        # Port up + connect failing => almost certainly a 2FA wait.
        # Keep the hold scope=ibkr in place and surface a hint.
        reason = f"ibgateway_port_{self._port}_up_but_connect_failing_check_2fa"
        try:
            self._write_hold(active=True, reason=reason, scope="ibkr")
            self.state.last_action = "set_hold"
            self.state.last_reason = reason
            _LOG.warning(
                "IB Gateway port %d is open but ib_insync connect is failing; "
                "operator: check IB Gateway window for an open 2FA prompt or "
                "stale session. Order-entry hold remains scope=ibkr.",
                self._port,
            )
        except Exception as exc:  # noqa: BLE001
            self.state.last_action = "noop"
            self.state.last_reason = f"hold_write_failed:{type(exc).__name__}"
        return self.state

    def _venue_connect_blocking(self) -> bool:
        """Run the venue's async ``connect()`` to a synchronous boolean.

        The venue may not be set in tests that only validate the port-
        refused path — in that case treat venue absence as 'connect ok'
        because we already proved the port is reachable. Otherwise the
        async ``connect()`` is bounded by ``connect_timeout_s``.
        """
        venue = self._venue
        if venue is None:
            return True
        connect = getattr(venue, "connect", None)
        if connect is None:
            return True
        try:
            coro = connect()
        except Exception:  # noqa: BLE001
            return False

        if not asyncio.iscoroutine(coro):
            # Sync mock: trust truthiness of the return value.
            return bool(coro)

        loop = asyncio.new_event_loop()
        try:
            report = loop.run_until_complete(
                asyncio.wait_for(coro, timeout=self._connect_timeout_s),
            )
        except TimeoutError:
            return False
        except Exception:  # noqa: BLE001
            return False
        finally:
            with contextlib.suppress(Exception):
                loop.close()
        # The venue protocol returns a VenueConnectionReport whose
        # status carries the truth. Fall through to truthiness if the
        # mock returned a bare bool.
        try:
            return getattr(report, "status", None) is ConnectionStatus.READY
        except Exception:  # noqa: BLE001
            return bool(report)


# Local import deferred so the dataclass / function definitions above
# don't drag a top-level contextlib through every call. It's cheap.
import contextlib  # noqa: E402  -- intentional import-after-helpers
