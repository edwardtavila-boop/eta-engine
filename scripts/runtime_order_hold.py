"""Canonical order-entry hold switch for live/paper-live runtime lanes.

The hold is intentionally tiny and file-based so operators can engage it
without touching code or scheduled-task definitions. Any malformed hold file
fails closed because an ambiguous operator safety state should not route orders.

Scope-aware contract (2026-05-06)
---------------------------------
The hold can optionally specify a ``scope`` field. Recognized values:

* ``"all"`` (or missing) -- back-compat default: holds EVERY bot.
* ``"ibkr"`` -- holds only IBKR-routed bots (futures via IB Gateway). Crypto
  bots resolved to other venues (e.g. Alpaca) keep trading.
* ``"futures"`` -- holds only futures bots. Crypto continues.
* ``"crypto"`` -- holds only crypto bots. Futures continues.
* ``"alpaca"`` -- holds only Alpaca-routed bots.

The scope is consumed at call sites that know the resolved venue + asset
class for a given bot/symbol pair (see :func:`hold_blocks_venue`). Any
malformed scope or unknown string falls back to ``"all"`` so a typo cannot
silently disable the hold.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eta_engine.scripts import workspace_roots

ORDER_HOLD_ENV = "ETA_ORDER_ENTRY_HOLD"
ORDER_HOLD_REASON_ENV = "ETA_ORDER_ENTRY_HOLD_REASON"
ORDER_HOLD_PATH_ENV = "ETA_ORDER_ENTRY_HOLD_PATH"
_TRUTHY = {"1", "true", "yes", "on", "hold", "held"}

# Recognised scope tokens. Anything else collapses to "all" so an operator
# typo cannot silently disable a hold.
_KNOWN_SCOPES: frozenset[str] = frozenset(
    {
        "all",
        "ibkr",
        "futures",
        "crypto",
        "alpaca",
        "tastytrade",
    }
)

# Mapping from scope token to the set of (venue, asset_class) tuples it
# blocks. ``("*", "*")`` is the wildcard match used by the "all" scope.
# Asset classes follow the v2 routing schema: "futures" or "crypto".
_SCOPE_BLOCKS: dict[str, frozenset[tuple[str, str]]] = {
    "all": frozenset({("*", "*")}),
    "ibkr": frozenset({("ibkr", "*")}),
    "futures": frozenset({("*", "futures")}),
    "crypto": frozenset({("*", "crypto")}),
    "alpaca": frozenset({("alpaca", "*")}),
    "tastytrade": frozenset({("tastytrade", "*")}),
}


@dataclass(frozen=True, slots=True)
class OrderEntryHold:
    """Resolved order-entry hold state."""

    active: bool
    reason: str = ""
    source: str = "none"
    scope: str = "all"
    path: Path = field(default_factory=lambda: workspace_roots.ETA_ORDER_ENTRY_HOLD_PATH)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reason": self.reason,
            "source": self.source,
            "scope": self.scope,
            "path": str(self.path),
            "payload": self.payload,
        }

    def blocks(self, *, venue: str | None, asset_class: str | None) -> bool:
        """Return True if this hold should block an entry routed to
        ``(venue, asset_class)``.

        The hold must be ``active`` AND the call's resolution must match
        the scope's block set. Unknown scope falls back to "all" so a
        misconfiguration cannot silently let entries through.
        """
        if not self.active:
            return False
        return _scope_blocks(self.scope, venue=venue, asset_class=asset_class)


def default_hold_path() -> Path:
    """Return the canonical hold path, with an env override for tests/ops."""
    override = os.getenv(ORDER_HOLD_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return workspace_roots.ETA_ORDER_ENTRY_HOLD_PATH


def _normalise_scope(raw: object) -> str:
    """Coerce a raw payload-scope value into one of ``_KNOWN_SCOPES``.

    Missing / non-string / unknown -> ``"all"`` (back-compat default,
    fails CLOSED — wider hold beats accidental scope-leak).
    """
    if not isinstance(raw, str):
        return "all"
    canonical = raw.strip().lower()
    if not canonical:
        return "all"
    return canonical if canonical in _KNOWN_SCOPES else "all"


def _scope_blocks(
    scope: str,
    *,
    venue: str | None,
    asset_class: str | None,
) -> bool:
    """Return True when ``scope`` should block ``(venue, asset_class)``.

    A missing venue or asset_class is treated as a wildcard "*" — the
    legacy callers that haven't yet plumbed routing context still get
    blocked under the "all" scope.
    """
    venue_norm = (venue or "*").strip().lower() or "*"
    klass_norm = (asset_class or "*").strip().lower() or "*"
    rules = _SCOPE_BLOCKS.get(scope, _SCOPE_BLOCKS["all"])
    for rule_venue, rule_class in rules:
        venue_match = rule_venue == "*" or rule_venue == venue_norm or venue_norm == "*"
        class_match = rule_class == "*" or rule_class == klass_norm or klass_norm == "*"
        if venue_match and class_match:
            return True
    return False


def load_order_entry_hold(path: Path | None = None) -> OrderEntryHold:
    """Resolve hold state from env and the canonical runtime file.

    Env hold wins over file state because it is the fastest process-level
    emergency brake. A malformed file fails closed.
    """
    hold_path = Path(path) if path is not None else default_hold_path()
    env_value = os.getenv(ORDER_HOLD_ENV, "").strip().lower()
    if env_value in _TRUTHY:
        reason = os.getenv(ORDER_HOLD_REASON_ENV, "").strip() or "env_hold"
        return OrderEntryHold(
            active=True,
            reason=reason,
            source=ORDER_HOLD_ENV,
            scope="all",
            path=hold_path,
            payload={ORDER_HOLD_ENV: env_value},
        )

    if not hold_path.exists():
        return OrderEntryHold(active=False, source="none", scope="all", path=hold_path)

    try:
        payload = json.loads(hold_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 -- malformed safety state fails closed
        return OrderEntryHold(
            active=True,
            reason=f"malformed_hold_file:{type(exc).__name__}",
            source="file_error",
            scope="all",
            path=hold_path,
            payload={"error": repr(exc)},
        )

    if not isinstance(payload, dict):
        return OrderEntryHold(
            active=True,
            reason="malformed_hold_file:not_object",
            source="file_error",
            scope="all",
            path=hold_path,
            payload={"raw": payload},
        )

    if "active" in payload:
        active = bool(payload.get("active"))
    elif "hold" in payload:
        # Legacy compatibility: older operator tooling wrote
        # {"hold": true|false} instead of {"active": ...}. Preserve the
        # explicit legacy intent so a cleared hold does not fail closed.
        active = bool(payload.get("hold"))
    else:
        active = True
    scope = _normalise_scope(payload.get("scope"))
    return OrderEntryHold(
        active=active,
        reason=str(payload.get("reason") or ("file_hold" if active else "")),
        source="file",
        scope=scope,
        path=hold_path,
        payload=payload,
    )


def order_entry_is_held(path: Path | None = None) -> bool:
    """Convenience boolean used by order-entry call sites."""
    return load_order_entry_hold(path).active


def hold_blocks_venue(
    *,
    venue: str | None,
    asset_class: str | None,
    path: Path | None = None,
) -> bool:
    """Resolve the hold and check whether it blocks the routing context.

    Convenience wrapper for call sites that have a resolved
    ``(venue, asset_class)`` pair. A bot routed to Alpaca crypto can call
    this with ``venue="alpaca", asset_class="crypto"`` and a hold whose
    ``scope="ibkr"`` will return False (i.e. allow the entry).
    """
    return load_order_entry_hold(path).blocks(venue=venue, asset_class=asset_class)


def write_order_entry_hold(
    *,
    active: bool,
    reason: str,
    scope: str = "all",
    path: Path | None = None,
) -> Path:
    """Write an operator hold state under the canonical runtime path.

    Uses an atomic ``.tmp + os.replace`` write so a watchdog or supervisor
    polling the file never observes a partial write.
    """
    hold_path = Path(path) if path is not None else default_hold_path()
    hold_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_scope = _normalise_scope(scope)
    payload = (
        json.dumps(
            {
                "active": bool(active),
                "reason": reason,
                "scope": canonical_scope,
                "set_at_utc": datetime.now(UTC).isoformat(),
                # Legacy field kept for back-compat with operators / tools
                # that previously expected ``ts``.
                "ts": datetime.now(UTC).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    tmp = hold_path.with_suffix(hold_path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, hold_path)
    return hold_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    status = sub.add_parser("status", help="Print resolved hold state as JSON.")
    status.add_argument("--path", type=Path, default=None)
    status.add_argument(
        "--json",
        action="store_true",
        help="Compatibility no-op; status always prints JSON.",
    )
    set_cmd = sub.add_parser("set", help="Engage the order-entry hold.")
    set_cmd.add_argument("--reason", default="operator_hold")
    set_cmd.add_argument(
        "--scope",
        default="all",
        choices=sorted(_KNOWN_SCOPES),
        help="Hold scope: all (default) | ibkr | futures | crypto | alpaca | tastytrade.",
    )
    set_cmd.add_argument("--path", type=Path, default=None)
    clear = sub.add_parser("clear", help="Clear the order-entry hold.")
    clear.add_argument("--reason", default="operator_clear")
    clear.add_argument("--path", type=Path, default=None)
    ns = parser.parse_args(argv)

    if ns.cmd == "status":
        print(json.dumps(load_order_entry_hold(ns.path).to_dict(), indent=2))
        return 0
    if ns.cmd == "set":
        path = write_order_entry_hold(
            active=True,
            reason=ns.reason,
            scope=ns.scope,
            path=ns.path,
        )
        print(path)
        return 0
    if ns.cmd == "clear":
        path = write_order_entry_hold(
            active=False,
            reason=ns.reason,
            scope="all",
            path=ns.path,
        )
        print(path)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
