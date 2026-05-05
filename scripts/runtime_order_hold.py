"""Canonical order-entry hold switch for live/paper-live runtime lanes.

The hold is intentionally tiny and file-based so operators can engage it
without touching code or scheduled-task definitions. Any malformed hold file
fails closed because an ambiguous operator safety state should not route orders.
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


@dataclass(frozen=True, slots=True)
class OrderEntryHold:
    """Resolved order-entry hold state."""

    active: bool
    reason: str = ""
    source: str = "none"
    path: Path = field(default_factory=lambda: workspace_roots.ETA_ORDER_ENTRY_HOLD_PATH)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reason": self.reason,
            "source": self.source,
            "path": str(self.path),
            "payload": self.payload,
        }


def default_hold_path() -> Path:
    """Return the canonical hold path, with an env override for tests/ops."""
    override = os.getenv(ORDER_HOLD_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return workspace_roots.ETA_ORDER_ENTRY_HOLD_PATH


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
            path=hold_path,
            payload={ORDER_HOLD_ENV: env_value},
        )

    if not hold_path.exists():
        return OrderEntryHold(active=False, source="none", path=hold_path)

    try:
        payload = json.loads(hold_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 -- malformed safety state fails closed
        return OrderEntryHold(
            active=True,
            reason=f"malformed_hold_file:{type(exc).__name__}",
            source="file_error",
            path=hold_path,
            payload={"error": repr(exc)},
        )

    if not isinstance(payload, dict):
        return OrderEntryHold(
            active=True,
            reason="malformed_hold_file:not_object",
            source="file_error",
            path=hold_path,
            payload={"raw": payload},
        )

    active = bool(payload.get("active", True))
    return OrderEntryHold(
        active=active,
        reason=str(payload.get("reason") or ("file_hold" if active else "")),
        source="file",
        path=hold_path,
        payload=payload,
    )


def order_entry_is_held(path: Path | None = None) -> bool:
    """Convenience boolean used by order-entry call sites."""
    return load_order_entry_hold(path).active


def write_order_entry_hold(
    *,
    active: bool,
    reason: str,
    path: Path | None = None,
) -> Path:
    """Write an operator hold state under the canonical runtime path."""
    hold_path = Path(path) if path is not None else default_hold_path()
    hold_path.parent.mkdir(parents=True, exist_ok=True)
    hold_path.write_text(
        json.dumps(
            {
                "active": bool(active),
                "reason": reason,
                "ts": datetime.now(UTC).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
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
    set_cmd.add_argument("--path", type=Path, default=None)
    clear = sub.add_parser("clear", help="Clear the order-entry hold.")
    clear.add_argument("--reason", default="operator_clear")
    clear.add_argument("--path", type=Path, default=None)
    ns = parser.parse_args(argv)

    if ns.cmd == "status":
        print(json.dumps(load_order_entry_hold(ns.path).to_dict(), indent=2))
        return 0
    if ns.cmd == "set":
        path = write_order_entry_hold(active=True, reason=ns.reason, path=ns.path)
        print(path)
        return 0
    if ns.cmd == "clear":
        path = write_order_entry_hold(active=False, reason=ns.reason, path=ns.path)
        print(path)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
