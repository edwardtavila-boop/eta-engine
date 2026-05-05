"""Unified broker connection probes and report writing.

This module gives the repo a single automation surface for broker
connectivity:

* build supported venue adapters from secrets,
* probe them read-only,
* preserve unsupported broker names as explicit ``UNAVAILABLE`` rows,
* and write a compact JSON report for preflight / operator use.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

_LOG = logging.getLogger(__name__)

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
    from collections.abc import Iterable

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
