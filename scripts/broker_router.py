"""Broker router: consumes pending_order.json files written by the strategy supervisor.

Dispatches each pending order through the gate chain, then submits via SmartRouter.
Owns long-lived venue connections so all bots share one IBKR session.

State layout under var/eta_engine/state/router/:
  pending/    -- order inbox; supervisor writes *.pending_order.json here
  processing/ -- in-flight orders (atomic-rename lock)
  blocked/    -- gate-denied orders (audit)
  archive/<YYYY-MM-DD>/ -- terminal states (filled/rejected/etc.)
  quarantine/ -- malformed JSON
  failed/     -- venue submission errors after retries
  fill_results/ -- sidecar JSONs per submitted order
  broker_router_heartbeat.json -- liveness signal

Honors env vars:
  ETA_BROKER_ROUTER_INTERVAL_S (default 5)
  ETA_BROKER_ROUTER_PENDING_DIR (default C:/EvolutionaryTradingAlgo/var/eta_engine/state/router/pending)
  ETA_BROKER_ROUTER_STATE_ROOT (default C:/EvolutionaryTradingAlgo/var/eta_engine/state/router)
  ETA_BROKER_ROUTER_DRY_RUN (default 0)
  ETA_BROKER_ROUTER_MAX_RETRIES (default 3)
  + ETA_LIVE_MODE, ETA_GATE_BOOTSTRAP, ETA_IDEMPOTENCY_STORE etc. passed
    through to underlying systems.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import traceback
import warnings
from collections import deque
from collections.abc import Callable  # noqa: TC003 -- runtime annotation on lazy-loader return
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from eta_engine.core.secrets import SECRETS  # noqa: E402
from eta_engine.obs.decision_journal import (  # noqa: E402
    Actor,
    DecisionJournal,
    Outcome,
    default_journal,
)
from eta_engine.scripts.runtime_order_hold import (  # noqa: E402
    OrderEntryHold,
    default_hold_path,
    load_order_entry_hold,
)
from eta_engine.scripts.workspace_roots import (  # noqa: E402
    ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH,
    ETA_RUNTIME_STATE_DIR,
)
from eta_engine.venues.base import (  # noqa: E402
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    VenueBase,
)
from eta_engine.venues.router import DORMANT_BROKERS, SmartRouter  # noqa: E402
from eta_engine.venues.tradovate import TradovateVenue  # noqa: E402

logger = logging.getLogger("eta_engine.broker_router")

# ---------------------------------------------------------------------------
# Defaults & symbol mapping
# ---------------------------------------------------------------------------

#: Default router state root, anchored under canonical workspace state.
DEFAULT_STATE_ROOT = ETA_RUNTIME_STATE_DIR / "router"

#: Default pending-order directory. Mirrors the supervisor write path.
#: Operators may override with ETA_BROKER_ROUTER_PENDING_DIR.
DEFAULT_PENDING_DIR = DEFAULT_STATE_ROOT / "pending"

DEFAULT_INTERVAL_S = 5.0
DEFAULT_MAX_RETRIES = 3

#: Cap exponential retry backoff at 5 minutes. Formula:
#: ``min(BACKOFF_CAP_S, interval_s * 2 ** attempts)``.
BACKOFF_CAP_S = 300.0

#: Suffix for the retry-meta sidecar written next to a file in
#: ``processing/``. Schema:
#: ``{"attempts": int, "last_attempt_ts": isoformat-str,
#:   "last_reject_reason": str}``.
RETRY_META_SUFFIX = ".retry_meta.json"

#: Canonical empty retry-meta payload for fresh files.
_EMPTY_RETRY_META: dict[str, Any] = {
    "attempts": 0, "last_attempt_ts": "", "last_reject_reason": "",
}

#: Operator escape hatch — set ``ETA_GATE_BOOTSTRAP=1`` to allow first-run
#: operation when the gate-chain module cannot be imported. Mirrors the
#: pattern in ``firm/eta_engine/src/mnq/risk/gate_chain.py``.
_GATE_BOOTSTRAP_ENV = "ETA_GATE_BOOTSTRAP"
_READINESS_ENFORCE_ENV = "ETA_BROKER_ROUTER_ENFORCE_READINESS"
_LIVE_MONEY_ENV = "ETA_LIVE_MONEY"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        logger.warning("invalid integer env %s=%r; using %s", name, os.environ.get(name), default)
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        logger.warning("invalid float env %s=%r; using %s", name, os.environ.get(name), default)
        return default


def _gate_bootstrap_enabled() -> bool:
    """True iff ``ETA_GATE_BOOTSTRAP=1`` is set in the environment."""
    return os.environ.get(_GATE_BOOTSTRAP_ENV, "").strip() == "1"


def _readiness_enforced() -> bool:
    """True iff broker routing must honor the strategy-readiness matrix."""
    return os.environ.get(_READINESS_ENFORCE_ENV, "").strip() == "1"


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _load_build_default_chain() -> Callable[..., object]:
    """Lazy-import :func:`build_default_chain` from the firm submodule.

    Extracted into a module-level function so tests can monkeypatch the
    import shim without poking at ``sys.modules``. Raises ``ImportError``
    when the firm/eta_engine submodule is unavailable.
    """
    firm_src = ROOT.parent / "firm" / "eta_engine" / "src"
    if firm_src.is_dir() and str(firm_src) not in sys.path:
        sys.path.insert(0, str(firm_src))
    from mnq.risk.gate_chain import build_default_chain  # type: ignore[import-not-found]
    return build_default_chain

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
    # Crypto-native symbols pass through unchanged for non-US-person flows.
    ("BTCUSDT", "bybit"): "BTCUSDT",
    ("ETHUSDT", "bybit"): "ETHUSDT",
    ("SOLUSDT", "bybit"): "SOLUSDT",
    ("XRPUSDT", "bybit"): "XRPUSDT",
}

#: Recognized futures roots that don't need symbol normalization.
_FUTURES_ROOTS = (
    "MNQ", "NQ", "ES", "MES", "RTY", "M2K",
    "MYM",
    "MBT", "MET", "NG", "CL", "GC", "MGC", "MCL",
    "ZN", "ZB", "6E", "M6E",
)

#: Recognized crypto bases; used for asset-class detection. Tracks the
#: base coin tokens our supervisor emits (e.g. "BTC", "ETH", "SOL", "XRP",
#: "AVAX", "LINK", "DOGE") plus a few extras tradeable on Alpaca paper.
_CRYPTO_BASES: frozenset[str] = frozenset({
    "BTC", "ETH", "SOL", "XRP", "AVAX", "LINK", "DOGE",
    "BCH", "LTC", "AAVE", "BAT", "CRV", "DOT", "GRT", "MKR",
    "PEPE", "SHIB", "SUSHI", "UNI", "XTZ", "YFI", "USDC", "USDT",
})

#: Quote suffixes that mark a symbol as crypto when paired with a known base
#: ("BTCUSDT", "ETHUSD", "BTC/USD"). Order matters for greedy matching.
_CRYPTO_QUOTE_SUFFIXES: tuple[str, ...] = ("USDT", "USDC", "USD")

#: Regex for CME month-coded suffix (F/G/H/J/K/M/N/Q/U/V/X/Z + 1-2 digits)
#: used to recognize futures month-codes like ``MNQM6`` or ``ESH26``.
_FUTURES_MONTH_SUFFIX_RE = re.compile(r"^[FGHJKMNQUVXZ]\d{1,2}$")

#: Asset-class literal type for routing resolution.
AssetClass = Literal["crypto", "futures", "equity"]


def _asset_class_for_symbol(symbol: str) -> AssetClass:
    """Classify a raw supervisor symbol token into a routing asset class.

    Detection order:
      1. Futures roots (and ``<root><digits>`` / month-coded forms like
         ``MNQM6``, ``MNQ1``) -> ``"futures"``.
      2. Crypto natives, slash-quoted forms (``BTC/USD``), or stable-quote
         suffix forms (``BTCUSDT``, ``ETHUSD``) -> ``"crypto"``.
      3. Bare crypto bases (``BTC``, ``ETH``, ``SOL``, ...) -> ``"crypto"``.
      4. Anything else -> ``"equity"``.

    Examples:
        >>> _asset_class_for_symbol("BTC")
        'crypto'
        >>> _asset_class_for_symbol("BTCUSDT")
        'crypto'
        >>> _asset_class_for_symbol("BTC/USD")
        'crypto'
        >>> _asset_class_for_symbol("MNQ")
        'futures'
        >>> _asset_class_for_symbol("MNQM6")
        'futures'
        >>> _asset_class_for_symbol("/MNQ")
        'futures'
        >>> _asset_class_for_symbol("SPY")
        'equity'
    """
    raw = (symbol or "").strip().upper().lstrip("/")
    if not raw:
        return "equity"

    # 1. Futures roots (exact, numeric-suffix, or CME month-coded suffix).
    for root in _FUTURES_ROOTS:
        if raw == root:
            return "futures"
        if raw.startswith(root):
            suffix = raw[len(root):]
            if not suffix:
                return "futures"
            if suffix.isdigit():
                return "futures"
            if _FUTURES_MONTH_SUFFIX_RE.fullmatch(suffix):
                return "futures"

    # 2. Slash form (BTC/USD, ETH/USDT) is always crypto.
    if "/" in raw:
        return "crypto"

    # 3. Stable-quote suffix on a recognized crypto base.
    for q in _CRYPTO_QUOTE_SUFFIXES:
        if raw.endswith(q) and len(raw) > len(q):
            base = raw[: -len(q)]
            if base in _CRYPTO_BASES:
                return "crypto"

    # 4. Bare crypto base.
    if raw in _CRYPTO_BASES:
        return "crypto"

    return "equity"


#: Env-var prefix used by operational venue overrides. Setting
#: ``ETA_VENUE_OVERRIDE_CRYPTO=tastytrade`` forces every crypto-classified
#: symbol that lacks a per-bot pin to route through Tastytrade.
_VENUE_OVERRIDE_ENV_PREFIX = "ETA_VENUE_OVERRIDE_"

_MAX_PENDING_ORDER_AGE_S = 15 * 60
_SMOKE_SIGNAL_TOKENS = ("smoke", "test", "dryrun", "dry_run")
_MIN_CRYPTO_LIMIT_PRICE: dict[str, float] = {
    "BTC": 1_000.0,
    "BTCUSD": 1_000.0,
    "BTCUSDT": 1_000.0,
    "ETH": 100.0,
    "ETHUSD": 100.0,
    "ETHUSDT": 100.0,
    "SOL": 1.0,
    "SOLUSD": 1.0,
    "SOLUSDT": 1.0,
    "XRP": 0.01,
    "XRPUSD": 0.01,
    "XRPUSDT": 0.01,
}

# ---------------------------------------------------------------------------
# Per-bot routing config (eta_engine/configs/bot_broker_routing.yaml)
# ---------------------------------------------------------------------------

#: Default routing-config path; operators override via ``ETA_BROKER_ROUTING_CONFIG``.
DEFAULT_ROUTING_CONFIG_PATH = ROOT / "configs" / "bot_broker_routing.yaml"
_ROUTING_CONFIG_ENV = "ETA_BROKER_ROUTING_CONFIG"


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    """Parsed view of ``bot_broker_routing.yaml`` (v1 + v2 schema).

    Schema v1 (legacy, still accepted):
      ``default.venue`` is the only fallback when a bot is not listed
      under ``bots:``. ``symbol_overrides`` lives under ``default``.

    Schema v2 (current):
      Adds two top-level blocks:

      * ``defaults`` — per-asset-class default venues
        (e.g. ``defaults.crypto: alpaca``, ``defaults.futures: ibkr``).
      * ``failover`` — ordered fallback chains, keyed by asset class
        (e.g. ``failover.crypto: [alpaca, tastytrade]``).

      ``default`` (without ``s``) keeps its v1 meaning so v1 yaml files
      still load.

    Resolution order (see :meth:`venue_for`):
      1. Per-bot explicit override (highest).
      2. Env override ``ETA_VENUE_OVERRIDE_<ASSET_CLASS>`` (e.g.
         ``ETA_VENUE_OVERRIDE_CRYPTO=tastytrade``).
      3. ``defaults.<asset_class>`` from yaml (v2).
      4. ``default.venue`` (v1 back-compat).
      5. ``"ibkr"`` (last resort).

    Missing file -> permissive ``ibkr``-for-all default + WARNING.
    Malformed YAML or wrong shape -> ``ValueError`` (fail loud).
    """

    default_venue: str
    symbol_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    per_bot: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Per-asset-class defaults parsed from v2 ``defaults`` block.
    #: Example: ``{"crypto": "alpaca", "futures": "ibkr"}``.
    asset_class_defaults: dict[str, str] = field(default_factory=dict)
    #: Per-asset-class failover chains parsed from v2 ``failover`` block.
    #: Example: ``{"crypto": ("alpaca", "tastytrade")}``.
    failover_chains: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Prop-fund account aliases for controlled Tradovate testing.
    #: DORMANT by default; aliases are inert unless ETA_TRADOVATE_ENABLED=1.
    #: Example: ``{"blusky_50k": {"venue": "tradovate", ...}}``.
    prop_accounts: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Detected schema version (1 or 2). v1 inputs surface as 1 even when
    #: the operator omits the ``version`` key — defensive default.
    version: int = 1

    @classmethod
    def load(cls, path: Path | None = None) -> RoutingConfig:
        """Load and parse the YAML. Path resolution: arg > env > default.

        Accepts both v1 and v2 schemas. v2-only fields (``defaults``,
        ``failover``) are optional; missing/empty maps default to ``{}``.
        """
        resolved = cls._resolve_path(path)
        if not resolved.is_file():
            logger.warning(
                "routing config not found at %s; using permissive default "
                "(venue=ibkr for all bots, no symbol overrides)", resolved,
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

        # Schema version (defensive default 1 when omitted).
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
            symbol_overrides[str(sym)] = {
                str(v).strip().lower(): str(s) for v, s in mapping.items()
            }

        bots_raw = raw.get("bots") or {}
        if not isinstance(bots_raw, dict):
            raise ValueError("routing config 'bots' must be a mapping")
        per_bot: dict[str, dict[str, str]] = {}
        for bot_id, mapping in bots_raw.items():
            if not isinstance(mapping, dict):
                raise ValueError(f"bots[{bot_id!r}] must be a mapping")
            per_bot[str(bot_id)] = {
                str(k): str(v).strip().lower() for k, v in mapping.items()
            }

        # v2 extensions (back-compat: missing keys -> {}).
        defaults_raw = raw.get("defaults") or {}
        if not isinstance(defaults_raw, dict):
            raise ValueError("routing config 'defaults' must be a mapping")
        asset_class_defaults: dict[str, str] = {}
        for klass, venue_name in defaults_raw.items():
            if not isinstance(klass, str) or not isinstance(venue_name, str):
                raise ValueError(
                    "routing config 'defaults' must map asset-class strings "
                    "to venue strings",
                )
            asset_class_defaults[klass.strip().lower()] = venue_name.strip().lower()

        failover_raw = raw.get("failover") or {}
        if not isinstance(failover_raw, dict):
            raise ValueError("routing config 'failover' must be a mapping")
        failover_chains: dict[str, tuple[str, ...]] = {}
        for klass, chain in failover_raw.items():
            if not isinstance(klass, str):
                raise ValueError("routing config 'failover' keys must be strings")
            if not isinstance(chain, list) or not all(
                isinstance(v, str) for v in chain
            ):
                raise ValueError(
                    f"routing config 'failover[{klass!r}]' must be a list of "
                    f"venue strings",
                )
            failover_chains[klass.strip().lower()] = tuple(
                v.strip().lower() for v in chain if v.strip()
            )

        prop_accounts_raw = raw.get("prop_accounts") or {}
        if not isinstance(prop_accounts_raw, dict):
            raise ValueError("routing config 'prop_accounts' must be a mapping")
        prop_accounts: dict[str, dict[str, str]] = {}
        for alias, mapping in prop_accounts_raw.items():
            if not isinstance(mapping, dict):
                raise ValueError(f"prop_accounts[{alias!r}] must be a mapping")
            clean_alias = str(alias).strip().lower()
            clean_mapping = {
                str(k).strip(): str(v).strip()
                for k, v in mapping.items()
                if k is not None and v is not None
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
        """Resolve the venue for ``(bot_id, symbol)``.

        Resolution order (highest priority first):
          1. Per-bot explicit override (``bots[bot_id].venue``).
          2. Env override ``ETA_VENUE_OVERRIDE_<ASSET_CLASS>`` when
             ``symbol`` is provided (asset class derived from the symbol).
          3. ``defaults.<asset_class>`` from yaml v2 (when ``symbol`` is
             provided).
          4. Top-level ``default.venue`` (v1 back-compat).
          5. ``"ibkr"`` (last resort).

        Backwards-compatible: callers that pass only ``bot_id`` retain
        the v1 behaviour (per-bot override -> ``default.venue``); a
        :class:`DeprecationWarning` is emitted to nudge callers toward
        the symbol-aware form. The deprecation does NOT yet downgrade
        functionality — it's an early-warning shim.
        """
        # 1. Per-bot explicit override always wins.
        bot_cfg = self.per_bot.get(bot_id) or {}
        bot_pin = (bot_cfg.get("venue") or "").strip().lower()
        if bot_pin:
            return bot_pin

        # No-symbol form: emit a deprecation hint, then collapse to v1
        # behaviour (default_venue) so existing callers keep working.
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

        # 2. Env override per asset class.
        env_key = _VENUE_OVERRIDE_ENV_PREFIX + klass.upper()
        env_val = (os.environ.get(env_key) or "").strip().lower()
        if env_val:
            return env_val

        # 3. Asset-class default from yaml v2.
        klass_default = (self.asset_class_defaults.get(klass) or "").strip().lower()
        if klass_default:
            return klass_default

        # 4. v1 default.venue back-compat.
        if self.default_venue:
            return self.default_venue.strip().lower()

        # 5. Last resort.
        return "ibkr"

    def failover_chain(
        self,
        bot_id: str,
        symbol: str,
    ) -> tuple[str, ...]:
        """Return the ordered venue chain to try for ``(bot_id, symbol)``.

        The first element is the resolved primary (per :meth:`venue_for`);
        subsequent elements come from ``failover.<asset_class>`` minus
        any duplicate of the primary. When no chain is configured, the
        return is ``(primary,)``.
        """
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
        """Return the configured prop account for a bot, if any.

        The returned mapping includes an ``alias`` key so downstream code
        can journal the human-readable account target without reverse
        lookups. Unknown aliases fail loud because a misspelled prop
        account must not silently route to the default broker.
        """
        bot_cfg = self.per_bot.get(bot_id) or {}
        alias = (bot_cfg.get("account_alias") or "").strip().lower()
        if not alias:
            return None
        account = self.prop_accounts.get(alias)
        if account is None:
            raise ValueError(f"unknown prop account alias for bot={bot_id!r}: {alias!r}")
        return {"alias": alias, **account}

    def map_symbol(self, raw_symbol: str, venue: str) -> str:
        """Translate raw -> venue-specific symbol via ``symbol_overrides``.

        ``"tasty"``/``"tastytrade"`` are accepted aliases. If the raw
        symbol is in overrides but the venue isn't listed there, raise
        ``ValueError``. Otherwise fall through to futures-root
        pass-through + legacy ``_SYMBOL_TABLE`` + stable-quote pass-through.
        """
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
                f"unsupported (symbol, venue) pair via routing config: "
                f"({raw_symbol!r}, {venue!r})",
            )

        # 2. Futures pass-through (strips month-coded suffixes).
        for root in _FUTURES_ROOTS:
            if up == root:
                return root
            if up.startswith(root):
                suffix = up[len(root):]
                if suffix.isdigit() or (
                    len(suffix) >= 2
                    and suffix[0] in "FGHJKMNQUVXZ"
                    and suffix[1:].isdigit()
                ):
                    return root

        # 3. Legacy table fallback (kept for the bybit/okx unit-test paths).
        legacy_venue = "tastytrade" if venue_norm == "tasty" else venue_norm
        key = (up, legacy_venue)
        if key in _SYMBOL_TABLE:
            return _SYMBOL_TABLE[key]

        # 4. Already-normalized stable-quote pass-through.
        if up.endswith(("USD", "USDT", "USDC")):
            return up

        msg = (
            f"unsupported (symbol, venue) pair: ({raw_symbol!r}, {venue!r})"
        )
        raise ValueError(msg)


def normalize_symbol(raw_symbol: str, target_venue: str) -> str:
    """Translate a supervisor's raw symbol to the venue's expected form.

    Backwards-compatible shim that delegates to
    :meth:`RoutingConfig.map_symbol`. The config file is read on each
    call (cheap; ~1 KB YAML); callers that need to amortize the cost
    should hold their own ``RoutingConfig`` instance.

    Args:
        raw_symbol: Symbol string from the pending-order JSON.
        target_venue: Venue name (``"ibkr"``, ``"tasty"``, etc.).

    Returns:
        The venue-ready symbol string.

    Raises:
        ValueError: If the (symbol, venue) pair is unsupported.
    """
    return RoutingConfig.load().map_symbol(raw_symbol, target_venue)


# ---------------------------------------------------------------------------
# Pending-order parsing
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PendingOrder:
    """One row of the supervisor pending-order JSONL contract.

    ``stop_price`` and ``target_price`` carry the bracket the supervisor
    computed at entry. When both are populated the venue layer attaches
    a parent + STP child + LMT child OCO group; when both are ``None``
    the venue layer rejects the entry as naked (fail-closed). Older
    pending-order files without these fields parse with both set to
    ``None`` so the rejection happens downstream in the venue's
    bracket-required check rather than at parse time.

    ``reduce_only`` distinguishes EXIT orders (close an open position)
    from ENTRY orders. It is critical for the SUPERVISOR_LOCAL bracket
    style (e.g. Alpaca crypto, IBKR-PAXOS crypto) where the supervisor
    owns stop/target management: when a paper-bracket leg fires, the
    supervisor must ship a reduce_only=True order so the venue (a)
    skips bracket attachment (a bracket on an exit either re-opens a
    position or is rejected by the broker) and (b) treats the order
    as a position-reducer rather than a fresh entry. Without the
    round-trip preserving this flag, exits would be ambiguous from
    entries on the wire and the broker could double the position.
    Older pending-order files without this flag parse with
    ``reduce_only=False`` so back-compat is preserved.
    """

    ts: str
    signal_id: str
    side: str
    qty: float
    symbol: str
    limit_price: float
    bot_id: str
    stop_price: float | None = None
    target_price: float | None = None
    reduce_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_pending_file(path: Path) -> PendingOrder:
    """Parse one ``<bot_id>.pending_order.json`` file.

    Bot id is taken from the filename stem (everything before the first ``.``).
    ``stop_price`` and ``target_price`` are optional for back-compat with
    older files; when absent the resulting :class:`PendingOrder` carries
    ``None`` for both and the venue's bracket-required check enforces
    the actual rejection downstream.

    Raises:
        ValueError: when JSON is malformed or any required field is missing.
    """
    name = path.name
    if not name.endswith(".pending_order.json"):
        raise ValueError(f"unexpected filename pattern: {name!r}")
    bot_id = name[: -len(".pending_order.json")]
    if not bot_id:
        raise ValueError(f"empty bot_id in filename: {name!r}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"json read failed: {exc}") from exc

    required = ("ts", "signal_id", "side", "qty", "symbol", "limit_price")
    missing = [k for k in required if k not in payload]
    if missing:
        raise ValueError(f"missing fields {missing} in {name!r}")

    side = str(payload["side"]).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"invalid side {side!r}")

    try:
        qty = float(payload["qty"])
        limit_price = float(payload["limit_price"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"non-numeric qty/limit_price: {exc}") from exc

    if qty <= 0.0:
        raise ValueError(f"non-positive qty {qty}")

    # Optional bracket fields. None => caller (venue layer) decides
    # whether the order is acceptable; this parser stays permissive so
    # back-compat files (without brackets) still load.
    stop_raw = payload.get("stop_price")
    target_raw = payload.get("target_price")
    try:
        stop_price = float(stop_raw) if stop_raw is not None else None
        target_price = float(target_raw) if target_raw is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError(f"non-numeric stop/target: {exc}") from exc

    # Optional reduce_only flag. Default False so older pending files
    # without this field continue to parse as entries — entries are the
    # dominant case so the conservative default is safe. Anything that
    # python's bool() considers truthy is treated as True; explicit
    # JSON ``true``/``false`` round-trips losslessly.
    reduce_only = bool(payload.get("reduce_only", False))

    return PendingOrder(
        ts=str(payload["ts"]),
        signal_id=str(payload["signal_id"]),
        side=side,
        qty=qty,
        symbol=str(payload["symbol"]),
        limit_price=limit_price,
        bot_id=bot_id,
        stop_price=stop_price,
        target_price=target_price,
        reduce_only=reduce_only,
    )


def pending_order_sanity_denial(order: PendingOrder) -> str:
    """Return a fail-closed reason for obviously unsafe live-routing intents.

    This is intentionally conservative. The supervisor can still simulate
    entries in ``paper_live`` without a broker round-trip, but the broker
    router must not transmit stale smoke files, naked entries, or impossible
    crypto prices to a real paper broker session.
    """
    signal_id = order.signal_id.strip().lower()
    if any(token in signal_id for token in _SMOKE_SIGNAL_TOKENS):
        return f"signal_id contains non-live token: {order.signal_id!r}"

    try:
        ts = datetime.fromisoformat(order.ts.replace("Z", "+00:00"))
    except ValueError:
        return f"invalid pending order ts: {order.ts!r}"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age_s = (datetime.now(UTC) - ts.astimezone(UTC)).total_seconds()
    if age_s > _MAX_PENDING_ORDER_AGE_S:
        return (
            f"stale pending order age_s={age_s:.1f} "
            f"max_s={_MAX_PENDING_ORDER_AGE_S}"
        )

    # Reduce-only EXITs intentionally carry no bracket — the position is
    # being closed, not opened. A bracket on an exit either re-opens the
    # position (fresh OCO siblings) or is rejected by the venue. Skip the
    # bracket-required and geometry checks for reduce_only orders;
    # entries still require stop+target to fail closed against naked
    # positions.
    if not order.reduce_only:
        if order.stop_price is None or order.target_price is None:
            return "missing bracket fields: stop_price and target_price are required"

        entry = float(order.limit_price)
        stop = float(order.stop_price)
        target = float(order.target_price)
        if entry <= 0.0 or stop <= 0.0 or target <= 0.0:
            return (
                "non-positive bracket geometry: "
                f"entry={entry} stop={stop} target={target}"
            )
        if order.side == "BUY" and not (stop < entry < target):
            return (
                "invalid BUY bracket geometry: "
                f"stop={stop} entry={entry} target={target}"
            )
        if order.side == "SELL" and not (target < entry < stop):
            return (
                "invalid SELL bracket geometry: "
                f"target={target} entry={entry} stop={stop}"
            )
    else:
        entry = float(order.limit_price)
        if entry <= 0.0:
            return f"non-positive exit limit_price: entry={entry}"

    symbol = order.symbol.strip().upper().lstrip("/")
    min_price = _MIN_CRYPTO_LIMIT_PRICE.get(symbol)
    if min_price is not None and entry < min_price:
        return (
            f"implausible {symbol} limit_price={entry} "
            f"below minimum sanity price={min_price}"
        )

    return ""


# ---------------------------------------------------------------------------
# BrokerRouter
# ---------------------------------------------------------------------------


class BrokerRouter:
    """Long-running consumer for supervisor-emitted pending orders.

    One instance owns the SmartRouter (which owns venue sessions) and the
    decision journal. Safe to run as a single process; if multiple
    instances ever race on the same pending dir, the atomic-rename lock
    in :meth:`_process_pending_file` prevents double-submission.
    """

    def __init__(
        self,
        pending_dir: Path,
        state_root: Path,
        smart_router: SmartRouter,
        journal: DecisionJournal,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        dry_run: bool = False,
        max_retries: int = DEFAULT_MAX_RETRIES,
        gate_chain: object | None = None,
        routing_config: RoutingConfig | None = None,
        order_hold_path: Path | None = None,
    ) -> None:
        self.pending_dir = Path(pending_dir)
        self.state_root = Path(state_root)
        self.smart_router = smart_router
        self.journal = journal
        self.interval_s = max(0.5, float(interval_s))
        self.dry_run = bool(dry_run)
        self.max_retries = max(1, int(max_retries))
        # Optional override hook: tests / shadow envs can inject a callable
        # gate-chain (or object with .evaluate(**kwargs)). When None, the
        # production lazy-import path runs.
        self.gate_chain = gate_chain
        # Per-bot routing config: tests inject; production loads from YAML.
        self.routing_config = (
            routing_config if routing_config is not None else RoutingConfig.load()
        )
        self._prop_venue_cache: dict[str, VenueBase] = {}
        self.order_hold_path = Path(order_hold_path) if order_hold_path else default_hold_path()

        self.processing_dir = self.state_root / "processing"
        self.blocked_dir = self.state_root / "blocked"
        self.archive_dir = self.state_root / "archive"
        self.quarantine_dir = self.state_root / "quarantine"
        self.failed_dir = self.state_root / "failed"
        self.fill_results_dir = self.state_root / "fill_results"
        self.heartbeat_path = self.state_root / "broker_router_heartbeat.json"
        self.gate_pre_trade_path = self.state_root / "pre_trade_gate.json"
        self.gate_heat_state_path = self.state_root / "heat_state.json"
        self.gate_journal_path = self.state_root / "gate_journal.sqlite"

        for d in (
            self.processing_dir,
            self.blocked_dir,
            self.archive_dir,
            self.quarantine_dir,
            self.failed_dir,
            self.fill_results_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        self._stopped = False
        self._retry_counts: dict[str, int] = {}
        # Bounded recent-event ring for heartbeat reporting.
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=32)
        self._counts: dict[str, int] = {
            "parsed": 0,
            "blocked": 0,
            "submitted": 0,
            "filled": 0,
            "rejected": 0,
            "failed": 0,
            "quarantined": 0,
            "held": 0,
        }

    # -- lifecycle ----------------------------------------------------------

    def request_stop(self) -> None:
        """Signal the run loop to drain and exit on next iteration boundary."""
        self._stopped = True

    async def run(self) -> None:
        """Main poll loop. Stops on SIGINT/SIGTERM or :meth:`request_stop`."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, AttributeError):
                loop.add_signal_handler(sig, self.request_stop)

        logger.info(
            "broker_router starting pending=%s state=%s dry_run=%s interval_s=%.1f",
            self.pending_dir, self.state_root, self.dry_run, self.interval_s,
        )
        while not self._stopped:
            await self._tick()
            if self._stopped:
                break
            try:
                await asyncio.sleep(self.interval_s)
            except asyncio.CancelledError:
                break
        logger.info("broker_router stopped")

    async def run_once(self) -> None:
        """Single-pass scan + heartbeat. Used by ``--once`` and tests."""
        await self._tick()

    async def _tick(self) -> None:
        """One poll: scan pending + processing dirs, dispatch each, heartbeat.

        Two scans happen each tick:

        1. Fresh files in ``pending_dir/*.pending_order.json`` -- moved to
           ``processing/`` and run through the lifecycle with empty
           retry-meta.
        2. Retry files already in ``processing/`` -- re-run the lifecycle
           if the per-attempt exponential backoff has elapsed. This is
           how venue-rejected orders eventually retry without a fresh
           supervisor write.
        """
        hold = self._order_entry_hold()
        if hold.active and hold.scope == "all":
            self._counts["held"] += 1
            self._record_event("runtime", "order_entry_hold", hold.reason)
            logger.warning(
                "broker_router order-entry hold active; skipping poll reason=%s path=%s",
                hold.reason,
                hold.path,
            )
            self._emit_heartbeat(hold=hold)
            return

        # Stamp liveness before order evaluation so the fail-closed gate chain
        # can trust this router's heartbeat even on the first tick after restart.
        self._emit_heartbeat(hold=hold)

        try:
            pending_paths = sorted(self.pending_dir.glob("*.pending_order.json"))
        except OSError as exc:
            logger.warning("pending dir scan failed: %s", exc)
            pending_paths = []
        for path in pending_paths:
            if self._stopped:
                break
            try:
                await self._process_pending_file(path)
            except Exception:  # noqa: BLE001 -- one bad file must not kill the loop
                logger.error(
                    "unhandled exception processing %s:\n%s",
                    path, traceback.format_exc(),
                )

        if not self.dry_run:
            try:
                processing_paths = sorted(
                    self.processing_dir.glob("*.pending_order.json")
                )
            except OSError as exc:
                logger.warning("processing dir scan failed: %s", exc)
                processing_paths = []
            for target in processing_paths:
                if self._stopped:
                    break
                try:
                    await self._process_retry_file(target)
                except Exception:  # noqa: BLE001
                    logger.error(
                        "unhandled exception in retry %s:\n%s",
                        target, traceback.format_exc(),
                    )

        self._emit_heartbeat(hold=hold)

    # -- per-file lifecycle -------------------------------------------------

    async def _process_pending_file(self, path: Path) -> None:
        """Fresh-file entry: move-to-processing, then run the lifecycle."""
        if self._hold_blocks_file(path):
            return
        if self.dry_run:
            target = path
        else:
            target = self.processing_dir / path.name
            try:
                self._atomic_move(path, target)
            except OSError as exc:
                logger.info("skip (move failed, likely raced): %s (%s)", path.name, exc)
                return
        await self._run_lifecycle(target, retry_meta=_EMPTY_RETRY_META.copy())

    async def _process_retry_file(self, target: Path) -> None:
        """Re-process a file already in ``processing/`` (sidecar-driven)."""
        if target.name.endswith(RETRY_META_SUFFIX):
            return
        if not target.name.endswith(".pending_order.json"):
            return
        retry_meta = self._load_retry_meta(target)
        attempts = int(retry_meta.get("attempts", 0))
        if attempts >= self.max_retries:
            logger.warning("retry file at max_retries: %s", target.name)
            self._counts["failed"] += 1
            self._record_event(target.name, "failed", "max_retries_on_retry_scan")
            self._move_to_failed_with_meta(target, retry_meta)
            return
        if self._should_backoff(retry_meta):
            return
        if self._hold_blocks_file(target):
            return
        await self._run_lifecycle(target, retry_meta=retry_meta)

    def _retry_meta_path(self, target: Path) -> Path:
        return target.with_name(target.name + RETRY_META_SUFFIX)

    def _load_retry_meta(self, target: Path) -> dict[str, Any]:
        """Read the retry-meta sidecar; any failure -> empty meta."""
        try:
            payload = json.loads(
                self._retry_meta_path(target).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return _EMPTY_RETRY_META.copy()
        return {
            "attempts": int(payload.get("attempts", 0) or 0),
            "last_attempt_ts": str(payload.get("last_attempt_ts", "") or ""),
            "last_reject_reason": str(payload.get("last_reject_reason", "") or ""),
        }

    def _save_retry_meta(self, target: Path, meta: dict[str, Any]) -> None:
        self._write_sidecar(self._retry_meta_path(target), meta)

    def _clear_retry_meta(self, target: Path) -> None:
        with contextlib.suppress(OSError):
            self._retry_meta_path(target).unlink()

    def _should_backoff(self, retry_meta: dict[str, Any]) -> bool:
        """``min(BACKOFF_CAP_S, interval_s * 2**attempts)`` not yet elapsed."""
        attempts = int(retry_meta.get("attempts", 0) or 0)
        if attempts <= 0:
            return False
        last_ts = retry_meta.get("last_attempt_ts", "")
        if not last_ts:
            return False
        try:
            last_dt = datetime.fromisoformat(last_ts)
        except (TypeError, ValueError):
            return False
        elapsed = (datetime.now(UTC) - last_dt).total_seconds()
        return elapsed < min(BACKOFF_CAP_S, self.interval_s * (2 ** attempts))

    def _move_to_failed_with_meta(
        self, target: Path, retry_meta: dict[str, Any],
    ) -> None:
        """Move target -> failed/ and persist meta alongside for forensics."""
        with contextlib.suppress(OSError):
            self._atomic_move(target, self.failed_dir / target.name)
        self._write_sidecar(
            self.failed_dir / (target.name + RETRY_META_SUFFIX), retry_meta,
        )
        self._clear_retry_meta(target)

    async def _run_lifecycle(
        self, target: Path, *, retry_meta: dict[str, Any],
    ) -> None:
        """Shared parse->gate->submit pipeline. Used for fresh + retry paths."""
        # 2. Parse.
        try:
            order = parse_pending_file(target)
        except ValueError as exc:
            self._counts["quarantined"] += 1
            self._record_event(target.name, "quarantined", str(exc))
            if not self.dry_run:
                with contextlib.suppress(OSError):
                    self._atomic_move(target, self.quarantine_dir / target.name)
                self._clear_retry_meta(target)
            self._safe_journal(
                actor=Actor.STRATEGY_ROUTER,
                intent="pending_order_quarantined",
                rationale=f"parse failed: {exc}",
                outcome=Outcome.NOTED,
                links=[f"file:{target.name}"],
                metadata={"path": str(target), "error": str(exc)},
            )
            return
        except Exception as exc:  # noqa: BLE001
            self._handle_processing_error(target, f"parse_pending_file raised: {exc}")
            return
        self._counts["parsed"] += 1

        # 2b. Router-local sanity checks. These run before portfolio gates
        # so stale smoke files or naked broker entries cannot hit a venue.
        sanity_denial = pending_order_sanity_denial(order)
        if sanity_denial:
            denied = {
                "gate": "pending_order_sanity",
                "allow": False,
                "reason": sanity_denial,
                "context": {"order": order.to_dict()},
            }
            self._handle_blocked(
                order,
                target,
                denied,
                [denied],
                ["-pending_order_sanity"],
            )
            return

        # 3. Strategy-readiness approval gate.
        readiness_denial = self._readiness_denial(order)
        if readiness_denial:
            denied = {
                "gate": "strategy_readiness",
                "allow": False,
                "reason": readiness_denial,
                "context": {"order": order.to_dict()},
            }
            self._handle_blocked(
                order,
                target,
                denied,
                [denied],
                ["-strategy_readiness"],
            )
            return

        # 4. Gate-chain evaluation.
        try:
            gate_results = await self._evaluate_gates(order)
        except Exception as exc:  # noqa: BLE001
            self._handle_routing_error(order, target, f"gate evaluation failed: {exc}")
            return
        gate_checks_summary = [
            ("+" if r["allow"] else "-") + r["gate"] for r in gate_results
        ]
        denied = next((r for r in gate_results if not r["allow"]), None)
        if denied is not None:
            self._handle_blocked(order, target, denied, gate_results, gate_checks_summary)
            return

        # 4. Resolve target venue + symbol from the per-bot routing config.
        # ValueError -> quarantine (operator config error, retry after fix).
        # We pass the symbol so v2 asset-class defaults and the
        # ETA_VENUE_OVERRIDE_<CLASS> env override can apply.
        try:
            target_venue_name = self.routing_config.venue_for(
                order.bot_id, symbol=order.symbol,
            )
            venue_symbol = self.routing_config.map_symbol(
                order.symbol, target_venue_name,
            )
            prop_account = self.routing_config.prop_account_for(order.bot_id)
        except ValueError as exc:
            self._handle_routing_config_unsupported(order, target, str(exc))
            return
        if target_venue_name in DORMANT_BROKERS:
            self._handle_dormant_broker(order, target, target_venue_name)
            return

        # 5. Pick the live venue adapter by name; fall back to choose_venue
        # for stand-ins that don't expose a name-based lookup.
        if prop_account is not None:
            try:
                venue = self._resolve_prop_account_venue(prop_account)
            except ValueError as exc:
                self._handle_routing_error(order, target, str(exc))
                return
        else:
            venue = self._resolve_venue_adapter(target_venue_name, order)
        if venue is None:
            try:
                venue = self.smart_router.choose_venue(
                    order.symbol, order.qty, urgency="normal",
                )
            except Exception as exc:  # noqa: BLE001
                self._handle_routing_error(
                    order, target, f"choose_venue failed: {exc}",
                )
                return

        # 6. Build OrderRequest. Brackets pass through verbatim from the
        # supervisor's pending-order JSON; the venue layer enforces the
        # bracket-required check (naked entries get rejected there).
        # ``reduce_only`` tracks supervisor-local exits — venues skip
        # bracket attachment and route the order as a position-closer
        # when this is True. Without forwarding this field, every exit
        # the supervisor wrote as a pending JSON would arrive at the
        # broker indistinguishable from a fresh entry, either doubling
        # the position or being rejected by the bracket-required check.
        side_enum = Side.BUY if order.side == "BUY" else Side.SELL
        request = OrderRequest(
            symbol=venue_symbol,
            side=side_enum,
            qty=order.qty,
            order_type=OrderType.LIMIT,
            price=order.limit_price,
            client_order_id=order.signal_id,
            bot_id=order.bot_id,
            stop_price=order.stop_price,
            target_price=order.target_price,
            reduce_only=order.reduce_only,
        )

        # 7. Dry-run short-circuit: log, do not submit, do not move.
        if self.dry_run:
            logger.info(
                "[dry_run] would submit signal=%s bot=%s venue=%s symbol=%s "
                "side=%s qty=%s limit=%s",
                order.signal_id, order.bot_id, venue.name, venue_symbol,
                order.side, order.qty, order.limit_price,
            )
            self._safe_journal(
                actor=Actor.STRATEGY_ROUTER,
                intent="pending_order_dry_run",
                rationale="dry_run=True; no venue submission",
                gate_checks=gate_checks_summary,
                outcome=Outcome.NOTED,
                links=[f"signal:{order.signal_id}", f"bot:{order.bot_id}"],
                metadata={"venue": venue.name, "venue_symbol": venue_symbol},
            )
            return

        # 8. Submit. Venue handles its own idempotency / fleet / cap gates.
        await self._submit_and_finalize(
            order, target, venue, request, gate_checks_summary,
            retry_meta=retry_meta,
        )

    def _handle_blocked(
        self,
        order: PendingOrder,
        target: Path,
        denied: dict[str, Any],
        gate_results: list[dict[str, Any]],
        gate_checks_summary: list[str],
    ) -> None:
        """Move to blocked/, journal a BLOCKED event.

        Distinguishes the ``gate_chain_import_failed`` DENY (security
        regression backstop) from a normal gate denial via the journal
        intent and the block_meta reason field.
        """
        is_import_failed = denied["gate"] == "gate_chain_import_failed"
        self._counts["blocked"] += 1
        self._record_event(target.name, "blocked", denied["gate"])
        block_meta = {
            "denied_gate": denied["gate"],
            "reason": (
                "gate_chain_import_failed" if is_import_failed else denied["reason"]
            ),
            "context": denied["context"],
            "all_gates": gate_results,
            "order": order.to_dict(),
        }
        if not self.dry_run:
            self._write_sidecar(
                self.blocked_dir / f"{order.signal_id}_block.json", block_meta,
            )
            with contextlib.suppress(OSError):
                self._atomic_move(target, self.blocked_dir / target.name)
            self._clear_retry_meta(target)
        intent = (
            "gate_chain_import_failed" if is_import_failed else "pending_order_blocked"
        )
        rationale = (
            f"gate_chain import failed; fail-closed DENY. detail={denied['reason']}"
            if is_import_failed
            else f"gate={denied['gate']} reason={denied['reason']}"
        )
        self._safe_journal(
            actor=Actor.STRATEGY_ROUTER,
            intent=intent,
            rationale=rationale,
            gate_checks=gate_checks_summary,
            outcome=Outcome.BLOCKED,
            links=[f"signal:{order.signal_id}", f"bot:{order.bot_id}"],
            metadata=block_meta,
        )

    def _resolve_venue_adapter(
        self, venue_name: str, order: PendingOrder,
    ) -> VenueBase | None:
        """Look up a venue adapter on the SmartRouter by name.

        Tries (in order): ``_venue_by_name(name)``, ``_venue_map[name]``,
        ``venue_map[name]``, and ``getattr(smart_router, name)``. Returns
        ``None`` when none of those expose the venue -- the caller then
        falls back to the legacy ``choose_venue`` path.
        """
        _ = order  # reserved: future per-bot/per-qty hook
        sr = self.smart_router
        by_name = getattr(sr, "_venue_by_name", None)
        if callable(by_name):
            try:
                venue = by_name(venue_name)
            except Exception:  # noqa: BLE001
                venue = None
            if venue is not None:
                return venue
        for attr in ("_venue_map", "venue_map"):
            mapping = getattr(sr, attr, None)
            if isinstance(mapping, dict):
                venue = mapping.get(venue_name)
                if venue is not None:
                    return venue
        venue = getattr(sr, venue_name, None)
        if venue is not None and hasattr(venue, "place_order"):
            return venue
        return None

    def _resolve_prop_account_venue(self, account: dict[str, str]) -> VenueBase | None:
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
            secret_val = SECRETS.get(key, required=False)
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
        # DORMANT context: never fall back to global credentials for prop aliases.
        missing = [name for name in required if not _cred(name)]
        if missing:
            raise ValueError(f"prop account {alias} missing Tradovate credential envs: {', '.join(missing)}")

        env_name = (account.get("env") or "demo").strip().lower()
        demo = env_name != "live"
        venue = TradovateVenue(
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

    def _handle_routing_config_unsupported(
        self, order: PendingOrder, target: Path, reason: str,
    ) -> None:
        """Quarantine an unmappable (bot, symbol, venue) triple. NOTED journal."""
        self._counts["quarantined"] += 1
        self._record_event(
            target.name, "quarantined", "routing_config_unsupported_pair",
        )
        if not self.dry_run:
            with contextlib.suppress(OSError):
                self._atomic_move(target, self.quarantine_dir / target.name)
            self._clear_retry_meta(target)
        self._safe_journal(
            actor=Actor.STRATEGY_ROUTER,
            intent="pending_order_quarantined",
            rationale=f"routing_config_unsupported_pair: {reason}",
            outcome=Outcome.NOTED,
            links=[
                f"signal:{order.signal_id}", f"bot:{order.bot_id}",
                f"file:{target.name}",
            ],
            metadata={
                "reason": "routing_config_unsupported_pair",
                "detail": reason, "order": order.to_dict(),
            },
        )

    def _handle_dormant_broker(
        self,
        order: PendingOrder,
        target: Path,
        venue_name: str,
    ) -> None:
        """Fail closed when routing config points at a dormant broker."""
        reason = (
            f"broker_dormancy: venue={venue_name!r} is dormant; set "
            "ETA_TRADOVATE_ENABLED=1 only for approved prop-fund testing"
        )
        self._counts["failed"] += 1
        self._record_event(target.name, "broker_dormant", reason)
        if not self.dry_run:
            with contextlib.suppress(OSError):
                self._atomic_move(target, self.failed_dir / target.name)
            self._clear_retry_meta(target)
        self._safe_journal(
            actor=Actor.STRATEGY_ROUTER,
            intent="pending_order_broker_dormant",
            rationale=reason,
            outcome=Outcome.FAILED,
            links=[f"signal:{order.signal_id}", f"bot:{order.bot_id}", f"venue:{venue_name}"],
            metadata={"reason": "broker_dormant", "detail": reason, "order": order.to_dict()},
        )

    def _handle_processing_error(self, target: Path, reason: str) -> None:
        """Fail one inconsistent work item without killing the router loop."""
        self._counts["failed"] += 1
        self._record_event(target.name, "processing_error", reason)
        if not self.dry_run:
            with contextlib.suppress(OSError):
                self._atomic_move(target, self.failed_dir / target.name)
            self._clear_retry_meta(target)
        self._safe_journal(
            actor=Actor.STRATEGY_ROUTER,
            intent="pending_order_processing_error",
            rationale=reason,
            outcome=Outcome.FAILED,
            links=[f"file:{target.name}"],
            metadata={"reason": reason, "path": str(target)},
        )

    def _hold_blocks_file(self, path: Path) -> bool:
        """Return True when the runtime hold blocks this pending order.

        ``scope=all`` is handled at the tick level. Scoped holds need the
        pending order's resolved venue/asset class so an IBKR/futures
        incident can pause futures while Alpaca crypto keeps routing.
        Parse/config errors are intentionally left to the normal lifecycle
        so bad files still quarantine instead of hiding behind a hold.
        """
        hold = self._order_entry_hold()
        if not hold.active:
            return False
        if hold.scope == "all":
            blocks = True
            venue_name = "*"
            asset_class = "*"
        else:
            try:
                order = parse_pending_file(path)
                venue_name = self.routing_config.venue_for(
                    order.bot_id, symbol=order.symbol,
                )
                asset_class = _asset_class_for_symbol(order.symbol)
            except Exception:  # noqa: BLE001
                return False
            blocks = hold.blocks(venue=venue_name, asset_class=asset_class)
        if not blocks:
            return False
        self._counts["held"] += 1
        self._record_event(path.name, "order_entry_hold", hold.reason)
        logger.warning(
            "pending order held in place: file=%s scope=%s reason=%s "
            "venue=%s class=%s path=%s",
            path,
            hold.scope,
            hold.reason,
            venue_name,
            asset_class,
            hold.path,
        )
        return True

    #: Exception substrings that mark a TRANSIENT venue failure (i.e.
    #: worth retrying on the next venue in the chain). Anything not
    #: matching is treated as deterministic and stays on the same venue
    #: so the existing retry-meta machinery applies.
    _TRANSIENT_FAILURE_TOKENS: tuple[str, ...] = (
        "timeout", "timed out", "connection", "connectionerror",
        "network", "unreachable", "reset by peer", "temporarily",
        "503", "502", "504", "gateway",
    )

    @classmethod
    def _is_transient_failure(cls, exc: BaseException) -> bool:
        """True iff the exception text looks like a transport-level glitch.

        Exact-class matches for timeout / connection-error subclasses
        cover the common case; the fallback string scan picks up venue
        SDK exceptions that don't subclass anything standard.
        """
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return True
        msg = str(exc).lower()
        return any(token in msg for token in cls._TRANSIENT_FAILURE_TOKENS)

    async def _place_with_failover_chain(
        self,
        order: PendingOrder,
        primary: VenueBase,
        request: OrderRequest,
    ) -> tuple[Any, VenueBase]:
        """Submit ``request`` with failover across the configured chain.

        Resolves the failover chain via
        :meth:`RoutingConfig.failover_chain` (asset-class derived from
        the symbol). The primary already comes resolved by the lifecycle
        and is always tried first. If the primary's circuit is open OR
        a transient failure raises, the next chain entry is consulted
        in order. Deterministic rejects (REJECTED status, deterministic
        exceptions) abort the chain immediately — those have to surface
        through the normal retry-meta path so per-venue rejects don't
        silently mask each other.

        Returns ``(result, venue_used)``. Raises only on the LAST
        venue's transient failure (so the caller still sees a real
        exception when no fallback succeeds).
        """
        chain = self.routing_config.failover_chain(order.bot_id, order.symbol)
        # Cap attempts at len(chain). When the chain is just (primary,),
        # this collapses to a single attempt with no failover behaviour.
        attempted: list[str] = []
        last_exc: BaseException | None = None
        venue: VenueBase | None = primary
        chain_idx = 0

        while venue is not None and chain_idx < len(chain):
            attempted.append(venue.name)
            # Per-venue circuit breaker check (when the SmartRouter
            # exposes them).
            circuit = self._venue_circuit(venue.name)
            if circuit is not None and circuit.is_open():
                logger.warning(
                    "broker_router failover hop: venue=%s circuit OPEN; "
                    "trying next-in-chain bot=%s signal=%s",
                    venue.name, order.bot_id, order.signal_id,
                )
                chain_idx += 1
                venue = self._next_chain_venue(chain, chain_idx, order)
                continue

            try:
                result = await venue.place_order(request)
                if circuit is not None:
                    circuit.record_success()
                return result, venue
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if circuit is not None:
                    circuit.record_failure()
                if not self._is_transient_failure(exc):
                    # Deterministic — abort failover so rejection logic
                    # records it on the resolved primary and the
                    # retry-meta sidecar drives backoff there.
                    raise
                logger.warning(
                    "broker_router failover hop: venue=%s transient failure "
                    "(%s); trying next-in-chain bot=%s signal=%s",
                    venue.name, exc, order.bot_id, order.signal_id,
                )
                chain_idx += 1
                venue = self._next_chain_venue(chain, chain_idx, order)

        # All venues in the chain raised transient failures — re-raise
        # the last so the caller routes via _handle_routing_error.
        if last_exc is not None:
            raise last_exc
        msg = (
            f"failover chain exhausted with no attempts attempted={attempted!r} "
            f"chain={chain!r}"
        )
        raise RuntimeError(msg)

    def _next_chain_venue(
        self,
        chain: tuple[str, ...],
        idx: int,
        order: PendingOrder,
    ) -> VenueBase | None:
        """Look up the venue adapter for ``chain[idx]``, or ``None``."""
        if idx >= len(chain):
            return None
        return self._resolve_venue_adapter(chain[idx], order)

    def _venue_circuit(self, venue_name: str) -> object | None:
        """Return the per-venue CircuitBreaker on the SmartRouter, or None.

        Defensive lookup: the SmartRouter normally exposes
        ``_venue_circuits``, but tests may inject a stand-in router that
        omits the attribute entirely.
        """
        circuits = getattr(self.smart_router, "_venue_circuits", None)
        if isinstance(circuits, dict):
            return circuits.get(venue_name)
        return None

    def venue_circuit_states(self) -> dict[str, str]:
        """Snapshot every venue circuit as ``{name: closed|open|half-open}``.

        ``half-open`` is reported when the breaker is configured but has
        previously recorded failures shy of the threshold (i.e. partial
        degradation). The dashboard ``/api/brokers`` reads this on its
        next refresh via the heartbeat sidecar.
        """
        circuits = getattr(self.smart_router, "_venue_circuits", None)
        if not isinstance(circuits, dict):
            return {}
        out: dict[str, str] = {}
        for name, breaker in circuits.items():
            try:
                if breaker.is_open():
                    out[name] = "open"
                    continue
                # ``_failures`` is a private internal but the public
                # ``is_open()`` already exercised the half-open reset
                # above; reading it here is forensic only.
                failures = int(getattr(breaker, "_failures", 0) or 0)
                if failures > 0:
                    out[name] = "half-open"
                else:
                    out[name] = "closed"
            except Exception:  # noqa: BLE001 — defensive read for ops surface
                out[name] = "unknown"
        return out

    async def _submit_and_finalize(
        self,
        order: PendingOrder,
        target: Path,
        venue: VenueBase,
        request: OrderRequest,
        gate_checks_summary: list[str],
        *,
        retry_meta: dict[str, Any],
    ) -> None:
        """Send the order, classify the result, archive or fail.

        Wraps :meth:`_place_with_failover_chain` so a TRANSIENT failure
        (network/timeout) on the resolved primary venue retries the
        next venue in the configured failover chain. Deterministic
        rejects (e.g. crypto-disabled) are NOT retried — those go
        through the normal reject/retry-meta machinery on the SAME
        venue so operators see consistent counters.
        """
        self._counts["submitted"] += 1
        try:
            result, venue = await self._place_with_failover_chain(
                order, venue, request,
            )
        except Exception as exc:  # noqa: BLE001
            self._handle_routing_error(order, target, f"venue.place_order raised: {exc}")
            return

        sidecar_payload = {
            "signal_id": order.signal_id, "bot_id": order.bot_id,
            "venue": venue.name,
            "request": json.loads(request.model_dump_json()),
            "result": json.loads(result.model_dump_json()),
            "ts": datetime.now(UTC).isoformat(),
        }
        self._write_sidecar(
            self.fill_results_dir / f"{order.signal_id}_result.json", sidecar_payload,
        )
        links = [
            f"signal:{order.signal_id}",
            f"bot:{order.bot_id}",
            f"order:{result.order_id}",
        ]

        if result.status is OrderStatus.REJECTED:
            self._counts["rejected"] += 1
            attempts = int(retry_meta.get("attempts", 0)) + 1
            reject_reason = (
                getattr(result, "error_message", None)
                or f"venue={venue.name} rejected order_id={result.order_id}"
            )
            new_meta = {
                "attempts": attempts,
                "last_attempt_ts": datetime.now(UTC).isoformat(),
                "last_reject_reason": str(reject_reason),
            }
            self._retry_counts[order.signal_id] = attempts
            if attempts >= self.max_retries:
                self._counts["failed"] += 1
                self._record_event(target.name, "failed", "max_retries")
                self._move_to_failed_with_meta(target, new_meta)
                self._safe_journal(
                    actor=Actor.STRATEGY_ROUTER, intent="pending_order_failed",
                    rationale=(
                        f"venue={venue.name} rejected {attempts} times; "
                        f"order_id={result.order_id}"
                    ),
                    gate_checks=gate_checks_summary, outcome=Outcome.FAILED,
                    links=links, metadata=sidecar_payload,
                )
                self._retry_counts.pop(order.signal_id, None)
            else:
                # Leave file in processing/ + persist retry-meta for the
                # next tick (which honors exponential backoff).
                self._save_retry_meta(target, new_meta)
                logger.info("rejected attempt=%d/%d signal=%s; will retry",
                            attempts, self.max_retries, order.signal_id)
                self._record_event(target.name, "rejected_retry", str(attempts))
                self._safe_journal(
                    actor=Actor.STRATEGY_ROUTER, intent="pending_order_rejected_retry",
                    rationale=(
                        f"venue={venue.name} rejected attempt={attempts}/"
                        f"{self.max_retries}"
                    ),
                    gate_checks=gate_checks_summary, outcome=Outcome.NOTED,
                    links=links, metadata=sidecar_payload,
                )
            return

        # FILLED / PARTIAL / OPEN -> archive as terminal.
        self._counts["filled"] += 1
        self._record_event(target.name, "executed", result.status.value)
        self._retry_counts.pop(order.signal_id, None)
        archive_dated = self.archive_dir / datetime.now(UTC).strftime("%Y-%m-%d")
        archive_dated.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self._atomic_move(target, archive_dated / target.name)
        self._clear_retry_meta(target)
        self._safe_journal(
            actor=Actor.STRATEGY_ROUTER, intent="pending_order_executed",
            rationale=(
                f"venue={venue.name} status={result.status.value} "
                f"filled={result.filled_qty} avg_price={result.avg_price}"
            ),
            gate_checks=gate_checks_summary, outcome=Outcome.EXECUTED,
            links=links, metadata=sidecar_payload,
        )

    def _handle_routing_error(
        self, order: PendingOrder, target: Path, reason: str,
    ) -> None:
        """Move to failed/, journal, increment counters."""
        self._counts["failed"] += 1
        self._record_event(target.name, "routing_error", reason)
        if not self.dry_run:
            with contextlib.suppress(OSError):
                self._atomic_move(target, self.failed_dir / target.name)
            self._clear_retry_meta(target)
        self._safe_journal(
            actor=Actor.STRATEGY_ROUTER,
            intent="pending_order_routing_error",
            rationale=reason,
            outcome=Outcome.FAILED,
            links=[f"signal:{order.signal_id}", f"bot:{order.bot_id}"],
            metadata={"reason": reason, "order": order.to_dict()},
        )

    # -- gate chain ---------------------------------------------------------

    async def _evaluate_gates(self, order: PendingOrder) -> list[dict[str, Any]]:
        """Run the gate chain. Returns ``[{gate, allow, reason, context}, ...]``.

        On ``ImportError`` of the gate-chain module: fail-closed DENY by
        default (gate=``gate_chain_import_failed``). When
        ``ETA_GATE_BOOTSTRAP=1`` is set, log ERROR but allow through with
        gate=``import_error_bootstrap``. Mirrors the escape-hatch pattern
        in ``mnq.risk.gate_chain``.

        If the test/operator has installed an override on
        ``self.gate_chain`` (callable taking the same kwargs), that
        callable is used directly instead of the lazy-imported factory.
        """
        # Override hook: tests / shadow envs can install a direct callable.
        override = getattr(self, "gate_chain", None)
        if override is not None:
            try:
                _allow, results = self._invoke_gate_chain_override(override, order)
            except NotImplementedError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("gate_chain override raised %s; DENY (fail-closed)", exc)
                return [{"gate": "chain_error", "allow": False,
                         "reason": f"chain raised: {exc}", "context": {}}]
            return [self._normalize_gate_result(r) for r in results]

        try:
            build_default_chain = _load_build_default_chain()
        except ImportError as exc:
            tb = traceback.format_exc()
            if _gate_bootstrap_enabled():
                logger.error(
                    "gate chain import failed (%s); ETA_GATE_BOOTSTRAP=1 set, "
                    "allowing order through.\n%s", exc, tb,
                )
                return [{"gate": "import_error_bootstrap", "allow": True,
                         "reason": f"gate_chain unavailable (bootstrap): {exc}",
                         "context": {"traceback": tb}}]
            logger.error(
                "gate chain import failed (%s); fail-closed DENY. "
                "Set ETA_GATE_BOOTSTRAP=1 only if you accept the risk.\n%s",
                exc, tb,
            )
            return [{"gate": "gate_chain_import_failed", "allow": False,
                     "reason": f"gate_chain unavailable: {exc}",
                     "context": {"traceback": tb}}]

        open_positions = self._collect_open_positions()
        hold = self._order_entry_hold()
        self._sync_gate_state(hold=hold, open_positions=open_positions)
        try:
            chain = build_default_chain(
                open_positions=open_positions,
                new_symbol=order.symbol,
                new_qty=int(round(order.qty)) or 1,
                heartbeat_path=self.heartbeat_path,
                deadman_heartbeat_path=self.heartbeat_path,
                pre_trade_path=self.gate_pre_trade_path,
                deadman_pre_trade_path=self.gate_pre_trade_path,
                heat_state_path=self.gate_heat_state_path,
                journal_path=self.gate_journal_path,
            )
            _allow, results = chain.evaluate()
        except Exception as exc:  # noqa: BLE001
            logger.error("gate chain evaluation raised %s; DENY (fail-closed)", exc)
            return [{"gate": "chain_error", "allow": False,
                     "reason": f"chain raised: {exc}", "context": {}}]
        return [self._normalize_gate_result(r) for r in results]

    def _invoke_gate_chain_override(
        self, override: object, order: PendingOrder,
    ) -> tuple[bool, list[object]]:
        """Invoke a test/shadow gate_chain override. Supports two shapes:

        * Callable returning ``(allow, results)`` directly.
        * Object with an ``.evaluate(**kwargs)`` method returning the same.
        """
        kwargs = {
            "open_positions": self._collect_open_positions(),
            "new_symbol": order.symbol,
            "new_qty": int(round(order.qty)) or 1,
        }
        if callable(override):
            return override(**kwargs)
        return override.evaluate(**kwargs)

    def _collect_open_positions(self) -> dict[str, int]:
        """Pull aggregated bot positions for the correlation gate.

        Honors ``ETA_RECONCILE_DISABLED`` (force empty) and
        ``ETA_RECONCILE_ALLOW_EMPTY_STATE`` (tolerate first-boot empty).
        Defensive: any error -> empty dict (logged), unless allow-empty
        is unset and the source raises NotImplementedError -> propagated
        as a routing error by the caller via journal-FAILED.
        """
        if os.environ.get("ETA_RECONCILE_DISABLED") == "1":
            return {}
        try:
            from eta_engine.obs.position_reconciler import fetch_bot_positions
            agg = fetch_bot_positions()
        except NotImplementedError as exc:
            if os.environ.get("ETA_RECONCILE_ALLOW_EMPTY_STATE") == "1":
                logger.info("empty bot-positions tolerated: %s", exc)
                return {}
            raise
        except RuntimeError as exc:
            if os.environ.get("ETA_RECONCILE_ALLOW_EMPTY_STATE") == "1":
                logger.info("empty bot-positions tolerated: %s", exc)
                return {}
            logger.warning("fetch_bot_positions failed: %s", exc)
            return {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_bot_positions errored: %s", exc)
            return {}
        out: dict[str, int] = {}
        for symbol, by_bot in agg.items():
            net = sum(by_bot.values())
            if abs(net) > 0.0:
                out[symbol] = int(round(net))
        return out

    def _readiness_denial(self, order: PendingOrder) -> str:
        """Return a denial reason when a bot is not approved for routing."""
        if not _readiness_enforced():
            return ""
        try:
            payload = json.loads(
                ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH.read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return (
                "strategy readiness snapshot missing: "
                f"{ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH}"
            )
        except (OSError, json.JSONDecodeError) as exc:
            return f"strategy readiness snapshot unreadable: {exc}"

        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return "strategy readiness snapshot malformed: rows missing"
        match = next(
            (
                row for row in rows
                if isinstance(row, dict) and str(row.get("bot_id") or "") == order.bot_id
            ),
            None,
        )
        if not isinstance(match, dict):
            return f"bot {order.bot_id!r} missing from strategy readiness snapshot"

        if os.environ.get(_LIVE_MONEY_ENV, "").strip() == "1":
            if bool(match.get("can_live_trade")):
                return ""
            return (
                f"bot {order.bot_id!r} is not live-approved "
                f"(lane={match.get('launch_lane')}, data={match.get('data_status')})"
            )
        if bool(match.get("can_paper_trade")):
            return ""
        return (
            f"bot {order.bot_id!r} is not paper-approved "
            f"(lane={match.get('launch_lane')}, data={match.get('data_status')})"
        )

    def _sync_gate_state(
        self,
        *,
        hold: OrderEntryHold,
        open_positions: dict[str, int],
    ) -> None:
        """Keep the firm gate-chain state aligned with this live router.

        The legacy ``mnq.risk.gate_chain`` defaults read static files under
        the firm engine. In the ETA runtime the broker router is the active
        order-entry boundary, so it provides fresh canonical state under
        ``var/eta_engine/state/router`` before evaluating fail-closed gates.
        """
        now_iso = datetime.now(UTC).isoformat()
        self._write_sidecar(
            self.gate_pre_trade_path,
            {
                "ts": now_iso,
                "state": "HOT" if hold.active else "COLD",
                "reason": hold.reason or ("operator_hold" if hold.active else "router_clear"),
                "scope": hold.scope,
                "source": "broker_router",
                "hold": hold.to_dict(),
            },
        )
        self._write_sidecar(
            self.gate_heat_state_path,
            self._heat_state_snapshot(now_iso=now_iso, open_positions=open_positions),
        )
        self._ensure_gate_journal()

    def _heat_state_snapshot(
        self,
        *,
        now_iso: str,
        open_positions: dict[str, int],
    ) -> dict[str, Any]:
        """Return a conservative heat-budget snapshot for multi-bot routing."""
        nonzero_positions = {
            symbol: qty for symbol, qty in open_positions.items() if int(qty or 0) != 0
        }
        max_concurrent = max(1, _env_int("ETA_BROKER_ROUTER_GATE_MAX_CONCURRENT", 8))
        budget = max(0.01, _env_float("ETA_BROKER_ROUTER_GATE_BUDGET", 1.0))
        current_heat = min(1.0, len(nonzero_positions) / max_concurrent)
        return {
            "ts": now_iso,
            "regime": "transition",
            "current_heat": round(current_heat, 4),
            "budget": budget,
            "utilization_pct": round(current_heat / budget * 100, 1),
            "positions": len(nonzero_positions),
            "max_concurrent": max_concurrent,
            "sizing_fraction": 0.2,
            "source": "broker_router",
            "open_positions": nonzero_positions,
            "writer_version": 1,
        }

    def _ensure_gate_journal(self) -> None:
        """Ensure the governor gate has a readable SQLite journal shell."""
        try:
            self.gate_journal_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.gate_journal_path) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS events ("
                    "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "ts TEXT NOT NULL, "
                    "event_type TEXT NOT NULL, "
                    "payload TEXT NOT NULL"
                    ")"
                )
        except sqlite3.Error as exc:
            logger.warning("gate journal initialization failed %s: %s", self.gate_journal_path, exc)

    @staticmethod
    def _normalize_gate_result(r: object) -> dict[str, Any]:
        """Coerce a GateResult-shaped object into the dict shape we use."""
        return {
            "gate": getattr(r, "gate", ""),
            "allow": bool(getattr(r, "allow", False)),
            "reason": getattr(r, "reason", "") or "",
            "context": dict(getattr(r, "context", {}) or {}),
        }

    # -- IO helpers ---------------------------------------------------------

    def _atomic_move(self, src: Path, dst: Path) -> None:
        """Rename with parent-mkdir; raises OSError on collision/race."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)

    def _write_sidecar(self, path: Path, payload: dict[str, Any]) -> None:
        """Write a small JSON sidecar; failures are logged not raised."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("sidecar write failed %s: %s", path, exc)

    def _order_entry_hold(self) -> OrderEntryHold:
        """Load the shared operator order-entry hold state."""
        return load_order_entry_hold(self.order_hold_path)

    def _emit_heartbeat(self, *, hold: OrderEntryHold | None = None) -> None:
        """Write a small heartbeat snapshot for monitoring.

        Includes ``venue_circuits``: a per-venue circuit-breaker state
        dict (``closed`` / ``open`` / ``half-open``) so the dashboard's
        ``/api/brokers`` endpoint can render live broker health on its
        next refresh.
        """
        now_iso = datetime.now(UTC).isoformat()
        hold = hold if hold is not None else self._order_entry_hold()
        snap = {
            "ts": now_iso,
            "last_poll_ts": now_iso,
            "pending_dir": str(self.pending_dir),
            "state_root": str(self.state_root),
            "order_entry_hold": hold.to_dict(),
            "dry_run": self.dry_run,
            "interval_s": self.interval_s,
            "max_retries": self.max_retries,
            "counts": dict(self._counts),
            "recent_events": list(self._recent_events),
            "venue_circuits": self.venue_circuit_states(),
        }
        self._write_sidecar(self.heartbeat_path, snap)

    def _record_event(self, filename: str, kind: str, detail: str) -> None:
        self._recent_events.append({
            "ts": datetime.now(UTC).isoformat(),
            "file": filename,
            "kind": kind,
            "detail": detail,
        })

    def _safe_journal(
        self,
        *,
        actor: Actor,
        intent: str,
        rationale: str = "",
        gate_checks: list[str] | None = None,
        outcome: Outcome = Outcome.NOTED,
        links: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append to the journal; failures are logged, not raised."""
        try:
            self.journal.record(
                actor=actor,
                intent=intent,
                rationale=rationale,
                gate_checks=gate_checks or [],
                outcome=outcome,
                links=links or [],
                metadata=metadata or {},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("journal append failed (intent=%s): %s", intent, exc)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _resolve_pending_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    env = os.environ.get("ETA_BROKER_ROUTER_PENDING_DIR")
    if env:
        return Path(env)
    return DEFAULT_PENDING_DIR


def _resolve_state_root(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    env = os.environ.get("ETA_BROKER_ROUTER_STATE_ROOT")
    if env:
        return Path(env)
    return DEFAULT_STATE_ROOT


def _resolve_interval(arg: float | None) -> float:
    if arg is not None:
        return float(arg)
    env = os.environ.get("ETA_BROKER_ROUTER_INTERVAL_S")
    if env:
        try:
            return float(env)
        except ValueError:
            logger.warning("invalid ETA_BROKER_ROUTER_INTERVAL_S=%r; using default", env)
    return DEFAULT_INTERVAL_S


def _resolve_dry_run(arg: bool) -> bool:
    if arg:
        return True
    return os.environ.get("ETA_BROKER_ROUTER_DRY_RUN", "").strip() in ("1", "true", "yes")


def _resolve_max_retries(arg: int | None) -> int:
    if arg is not None:
        return int(arg)
    env = os.environ.get("ETA_BROKER_ROUTER_MAX_RETRIES")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return DEFAULT_MAX_RETRIES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="broker_router",
        description=__doc__.split("\n", 1)[0],
    )
    parser.add_argument("--interval", type=float, default=None,
                        help="Poll interval seconds (default 5).")
    parser.add_argument("--pending-dir", type=str, default=None,
                        help="Where the supervisor writes *.pending_order.json files.")
    parser.add_argument("--state-root", type=str, default=None,
                        help="Router state root for processing/blocked/archive.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and gate-check, but do not submit or move files.")
    parser.add_argument("--once", action="store_true",
                        help="Single pass, then exit.")
    parser.add_argument("--max-retries", type=int, default=None,
                        help="Max venue rejections before moving to failed/.")
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    pending_dir = _resolve_pending_dir(args.pending_dir)
    state_root = _resolve_state_root(args.state_root)
    interval_s = _resolve_interval(args.interval)
    dry_run = _resolve_dry_run(args.dry_run)
    max_retries = _resolve_max_retries(args.max_retries)

    smart_router = SmartRouter()
    journal = default_journal()
    router = BrokerRouter(
        pending_dir=pending_dir,
        state_root=state_root,
        smart_router=smart_router,
        journal=journal,
        interval_s=interval_s,
        dry_run=dry_run,
        max_retries=max_retries,
    )
    if args.once:
        asyncio.run(router.run_once())
    else:
        asyncio.run(router.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
