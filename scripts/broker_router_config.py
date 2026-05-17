from __future__ import annotations

import logging
import os
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger("eta_engine.broker_router")

#: Translate the supervisor's raw symbol token to the form the target
#: venue expects. Keys are (raw_symbol, venue_name); when the venue is
#: unknown, the IBKR mapping is used (since IBKR is the M2 default).
_SYMBOL_TABLE: dict[tuple[str, str], str] = {
    ("BTC", "ibkr"): "BTCUSD",
    ("ETH", "ibkr"): "ETHUSD",
    ("SOL", "ibkr"): "SOLUSD",
    ("XRP", "ibkr"): "XRPUSD",
    ("BTC", "tastytrade"): "BTCUSDT",
    ("ETH", "tastytrade"): "ETHUSDT",
    ("SOL", "tastytrade"): "SOLUSDT",
    ("XRP", "tastytrade"): "XRPUSDT",
    ("BTCUSDT", "bybit"): "BTCUSDT",
    ("ETHUSDT", "bybit"): "ETHUSDT",
    ("SOLUSDT", "bybit"): "SOLUSDT",
    ("XRPUSDT", "bybit"): "XRPUSDT",
}

_FUTURES_ROOTS = (
    "MNQ",
    "NQ",
    "ES",
    "MES",
    "RTY",
    "M2K",
    "MYM",
    "MBT",
    "MET",
    "NG",
    "CL",
    "GC",
    "MGC",
    "MCL",
    "ZN",
    "ZB",
    "6E",
    "M6E",
)

_CRYPTO_BASES: frozenset[str] = frozenset(
    {
        "BTC",
        "ETH",
        "SOL",
        "XRP",
        "AVAX",
        "LINK",
        "DOGE",
        "BCH",
        "LTC",
        "AAVE",
        "BAT",
        "CRV",
        "DOT",
        "GRT",
        "MKR",
        "PEPE",
        "SHIB",
        "SUSHI",
        "UNI",
        "XTZ",
        "YFI",
        "USDC",
        "USDT",
    }
)

_CRYPTO_QUOTE_SUFFIXES: tuple[str, ...] = ("USDT", "USDC", "USD")
_FUTURES_MONTH_SUFFIX_RE = re.compile(r"^[FGHJKMNQUVXZ]\d{1,2}$")
AssetClass = Literal["crypto", "futures", "equity"]


def _asset_class_for_symbol(symbol: str) -> AssetClass:
    """Classify a raw supervisor symbol token into a routing asset class."""
    raw = (symbol or "").strip().upper().lstrip("/")
    if not raw:
        return "equity"
    for root in _FUTURES_ROOTS:
        if raw == root:
            return "futures"
        if raw.startswith(root):
            suffix = raw[len(root) :]
            if not suffix:
                return "futures"
            if suffix.isdigit():
                return "futures"
            if _FUTURES_MONTH_SUFFIX_RE.fullmatch(suffix):
                return "futures"
    if "/" in raw:
        return "crypto"
    for q in _CRYPTO_QUOTE_SUFFIXES:
        if raw.endswith(q) and len(raw) > len(q):
            base = raw[: -len(q)]
            if base in _CRYPTO_BASES:
                return "crypto"
    if raw in _CRYPTO_BASES:
        return "crypto"
    return "equity"


_VENUE_OVERRIDE_ENV_PREFIX = "ETA_VENUE_OVERRIDE_"
DEFAULT_ROUTING_CONFIG_PATH = ROOT / "configs" / "bot_broker_routing.yaml"
_ROUTING_CONFIG_ENV = "ETA_BROKER_ROUTING_CONFIG"


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    """Parsed view of ``bot_broker_routing.yaml`` (v1 + v2 schema)."""

    default_venue: str
    symbol_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    per_bot: dict[str, dict[str, str]] = field(default_factory=dict)
    asset_class_defaults: dict[str, str] = field(default_factory=dict)
    failover_chains: dict[str, tuple[str, ...]] = field(default_factory=dict)
    prop_accounts: dict[str, dict[str, str]] = field(default_factory=dict)
    version: int = 1

    @classmethod
    def load(cls, path: Path | None = None) -> RoutingConfig:
        resolved = cls._resolve_path(path)
        if not resolved.is_file():
            logger.warning(
                "routing config not found at %s; using permissive default "
                "(venue=ibkr for all bots, no symbol overrides)",
                resolved,
            )
            return cls(default_venue="ibkr", symbol_overrides={}, per_bot={})

        try:
            raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(
                f"routing config YAML parse failed at {resolved}: {exc}",
            ) from exc

        if not isinstance(raw, dict):
            raise ValueError(
                f"routing config root must be a mapping; got {type(raw).__name__}",
            )

        try:
            version = int(raw.get("version") or 1)
        except (TypeError, ValueError):
            version = 1

        default_block = raw.get("default") or {}
        if not isinstance(default_block, dict):
            raise ValueError("routing config 'default' must be a mapping")
        default_venue = str(default_block.get("venue", "ibkr") or "ibkr").strip().lower()

        overrides_raw = default_block.get("symbol_overrides") or {}
        if not isinstance(overrides_raw, dict):
            raise ValueError(
                "routing config 'default.symbol_overrides' must be a mapping",
            )
        symbol_overrides: dict[str, dict[str, str]] = {}
        for sym, mapping in overrides_raw.items():
            if not isinstance(mapping, dict):
                raise ValueError(
                    f"symbol_overrides[{sym!r}] must be a mapping of venue->symbol",
                )
            symbol_overrides[str(sym)] = {str(v).strip().lower(): str(s) for v, s in mapping.items()}

        bots_raw = raw.get("bots") or {}
        if not isinstance(bots_raw, dict):
            raise ValueError("routing config 'bots' must be a mapping")
        per_bot: dict[str, dict[str, str]] = {}
        for bot_id, mapping in bots_raw.items():
            if not isinstance(mapping, dict):
                raise ValueError(f"bots[{bot_id!r}] must be a mapping")
            per_bot[str(bot_id)] = {str(k): str(v).strip().lower() for k, v in mapping.items()}

        defaults_raw = raw.get("defaults") or {}
        if not isinstance(defaults_raw, dict):
            raise ValueError("routing config 'defaults' must be a mapping")
        asset_class_defaults: dict[str, str] = {}
        for klass, venue_name in defaults_raw.items():
            if not isinstance(klass, str) or not isinstance(venue_name, str):
                raise ValueError(
                    "routing config 'defaults' must map asset-class strings to venue strings",
                )
            asset_class_defaults[klass.strip().lower()] = venue_name.strip().lower()

        failover_raw = raw.get("failover") or {}
        if not isinstance(failover_raw, dict):
            raise ValueError("routing config 'failover' must be a mapping")
        failover_chains: dict[str, tuple[str, ...]] = {}
        for klass, chain in failover_raw.items():
            if not isinstance(klass, str):
                raise ValueError("routing config 'failover' keys must be strings")
            if not isinstance(chain, list) or not all(isinstance(v, str) for v in chain):
                raise ValueError(
                    f"routing config 'failover[{klass!r}]' must be a list of venue strings",
                )
            failover_chains[klass.strip().lower()] = tuple(v.strip().lower() for v in chain if v.strip())

        prop_accounts_raw = raw.get("prop_accounts") or {}
        if not isinstance(prop_accounts_raw, dict):
            raise ValueError("routing config 'prop_accounts' must be a mapping")
        prop_accounts: dict[str, dict[str, str]] = {}
        for alias, mapping in prop_accounts_raw.items():
            if not isinstance(mapping, dict):
                raise ValueError(f"prop_accounts[{alias!r}] must be a mapping")
            clean_alias = str(alias).strip().lower()
            clean_mapping = {
                str(k).strip(): str(v).strip() for k, v in mapping.items() if k is not None and v is not None
            }
            for lower_key in ("venue", "env", "bot_policy"):
                if lower_key in clean_mapping:
                    clean_mapping[lower_key] = clean_mapping[lower_key].lower()
            prop_accounts[clean_alias] = clean_mapping

        return cls(
            default_venue=default_venue,
            symbol_overrides=symbol_overrides,
            per_bot=per_bot,
            asset_class_defaults=asset_class_defaults,
            failover_chains=failover_chains,
            prop_accounts=prop_accounts,
            version=version,
        )

    @staticmethod
    def _resolve_path(path: Path | None) -> Path:
        if path is not None:
            return Path(path)
        env = os.environ.get(_ROUTING_CONFIG_ENV)
        return Path(env) if env else DEFAULT_ROUTING_CONFIG_PATH

    def venue_for(
        self,
        bot_id: str,
        symbol: str | None = None,
    ) -> str:
        bot_cfg = self.per_bot.get(bot_id) or {}
        bot_pin = (bot_cfg.get("venue") or "").strip().lower()
        if bot_pin:
            return bot_pin
        if symbol is None:
            warnings.warn(
                "RoutingConfig.venue_for(bot_id) without a symbol is "
                "deprecated; pass venue_for(bot_id, symbol=...) so the "
                "asset-class default + ETA_VENUE_OVERRIDE_<CLASS> env "
                "override can apply.",
                DeprecationWarning,
                stacklevel=2,
            )
            return self.default_venue or "ibkr"

        klass = _asset_class_for_symbol(symbol)
        env_key = _VENUE_OVERRIDE_ENV_PREFIX + klass.upper()
        env_val = (os.environ.get(env_key) or "").strip().lower()
        if env_val:
            return env_val
        klass_default = (self.asset_class_defaults.get(klass) or "").strip().lower()
        if klass_default:
            return klass_default
        if self.default_venue:
            return self.default_venue.strip().lower()
        return "ibkr"

    def failover_chain(
        self,
        bot_id: str,
        symbol: str,
    ) -> tuple[str, ...]:
        primary = self.venue_for(bot_id, symbol=symbol)
        klass = _asset_class_for_symbol(symbol)
        chain = self.failover_chains.get(klass, ())
        ordered: list[str] = [primary]
        for venue in chain:
            v = (venue or "").strip().lower()
            if not v or v in ordered:
                continue
            ordered.append(v)
        return tuple(ordered)

    def prop_account_for(self, bot_id: str) -> dict[str, str] | None:
        bot_cfg = self.per_bot.get(bot_id) or {}
        alias = (bot_cfg.get("account_alias") or "").strip().lower()
        if not alias:
            return None
        account = self.prop_accounts.get(alias)
        if account is None:
            raise ValueError(f"unknown prop account alias for bot={bot_id!r}: {alias!r}")
        return {"alias": alias, **account}

    def map_symbol(self, raw_symbol: str, venue: str) -> str:
        up = raw_symbol.strip().upper()
        venue_norm = venue.strip().lower()
        if venue_norm == "tastytrade":
            venue_norm = "tasty"

        override = self.symbol_overrides.get(up)
        if override is not None:
            mapped = override.get(venue_norm)
            if mapped is not None:
                return mapped
            raise ValueError(
                f"unsupported (symbol, venue) pair via routing config: ({raw_symbol!r}, {venue!r})",
            )

        for root in _FUTURES_ROOTS:
            if up == root:
                return root
            if up.startswith(root):
                suffix = up[len(root) :]
                if suffix.isdigit() or (len(suffix) >= 2 and suffix[0] in "FGHJKMNQUVXZ" and suffix[1:].isdigit()):
                    return root

        legacy_venue = "tastytrade" if venue_norm == "tasty" else venue_norm
        key = (up, legacy_venue)
        if key in _SYMBOL_TABLE:
            return _SYMBOL_TABLE[key]

        if up.endswith(("USD", "USDT", "USDC")):
            return up

        msg = f"unsupported (symbol, venue) pair: ({raw_symbol!r}, {venue!r})"
        raise ValueError(msg)


def normalize_symbol(raw_symbol: str, target_venue: str) -> str:
    """Backwards-compatible shim that delegates to ``RoutingConfig.map_symbol``."""
    return RoutingConfig.load().map_symbol(raw_symbol, target_venue)
