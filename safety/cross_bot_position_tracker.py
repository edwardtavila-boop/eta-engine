"""EVOLUTIONARY TRADING ALGO // safety.cross_bot_position_tracker.

Process-wide running net-position tracker keyed by symbol root.

Why this exists
---------------
The per-order ``position_cap`` gate refuses any single order whose
``|qty| > cap``. It does not see across bots. Two bots routed to the
same root (e.g. ``mbt_funding_basis`` and ``mbt_overnight_gap`` both
shorting MBT) can each pass the per-order cap and combine into a
fleet-level position that the operator never authorised.

Concrete failure mode logged 2026-05-07: two MBT bots stacked
qty=3 short each = 6 MBT short = 0.6 BTC notional ~$48k on a $50k
equity account. A 1.5x ATR adverse move = ~$600 MTM in one tick =
24% of an Apex Tier-A trailing buffer in seconds.

This module owns a single in-process net-position book, keyed by
symbol root. The supervisor's pre-trade gate calls
``assert_fleet_position_cap()`` BEFORE ``submit_entry`` so an order
that would push the fleet over its configured per-root cap is
rejected upstream of broker submission. Tracker mutations happen
only on broker-acknowledged events:

* ``record_entry(root, side, qty)``  - successful submit_entry
* ``record_exit(root, side, qty)``   - successful submit_exit / fill
* ``resync_from_broker(by_root)``    - broker truth wins

Persistence
-----------
The tracker mirrors the on-disk file ``cross_bot_positions.json``
under the supervisor state_dir so a process restart does not drop
running net to zero (which would let the post-restart fleet re-enter
on top of broker-side exposure). Loaded at supervisor startup, then
reconciled against broker truth before any new order can fly.

Operator interface
------------------
Per-root caps are read from environment variables on every check so
an operator can tighten without redeploy:

* ``ETA_FLEET_POSITION_CAP_<ROOT>`` -- absolute net contract cap for
  the root. Example: ``ETA_FLEET_POSITION_CAP_MBT=3``.
* ``ETA_FLEET_POSITION_CAP_DEFAULT`` -- fallback when no per-root
  override is configured.
* ``ETA_FLEET_POSITION_CAP_DISABLED`` -- truthy disables the gate
  entirely (paper / unit-test paths).

Defaults baked into :data:`DEFAULT_ROOT_CAPS` reflect the active
research-candidate scale: MBT/MET = 3 net contracts.

Prop sleeves
------------
Some instruments are not the same root but ARE the same economic bet.
The prop-fund Nasdaq lane uses MNQ and NQ together: NQ is 10x MNQ by
contract multiplier, so the tracker also exposes an equivalent-exposure
gate keyed by sleeve:

* ``NASDAQ``: ``MNQ = 1 MNQ-equivalent``, ``NQ = 10 MNQ-equivalent``.
* ``ETA_PROP_SLEEVE_CAP_NASDAQ_MNQ_EQUIV`` controls the cap.
* ``ETA_PROP_SLEEVE_CAP_DISABLED`` disables this extra sleeve gate.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ROOT_CAPS: dict[str, float] = {
    "MBT": 3.0,
    "MET": 3.0,
}

DEFAULT_FALLBACK_CAP: float = 10.0

DEFAULT_PROP_SLEEVE_CAPS: dict[str, float] = {
    "NASDAQ": 10.0,
}

PROP_SLEEVE_MULTIPLIERS: dict[str, dict[str, float]] = {
    "NASDAQ": {
        "MNQ": 1.0,
        "NQ": 10.0,
    },
}

STATE_FILENAME: str = "cross_bot_positions.json"


def _is_truthy_env(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def normalize_root(symbol: str) -> str:
    """Strip contract month digits and USD/USDT suffix; uppercase."""
    s = (symbol or "").upper().lstrip("/").rstrip("0123456789")
    for suffix in ("USDT", "USD"):
        if s.endswith(suffix) and s != suffix:
            s = s[: -len(suffix)]
    return s


def signed_delta(side: str, qty: float) -> float:
    """Convert (side, qty) into a signed delta."""
    s = (side or "").upper()
    q = abs(float(qty))
    if s in {"BUY", "LONG"}:
        return q
    if s in {"SELL", "SHORT"}:
        return -q
    raise ValueError("side must be BUY/SELL/LONG/SHORT, got " + repr(side))


class FleetPositionCapExceeded(RuntimeError):  # noqa: N818 - public API keeps existing name.
    """Raised when an order would push fleet net over the per-root cap."""

    def __init__(
        self,
        message: str,
        *,
        root: str,
        current_net: float,
        requested_delta: float,
        proposed_total: float,
        fleet_cap: float,
    ) -> None:
        super().__init__(message)
        self.root = root
        self.current_net = current_net
        self.requested_delta = requested_delta
        self.proposed_total = proposed_total
        self.fleet_cap = fleet_cap


class PropSleeveCapExceeded(RuntimeError):  # noqa: N818 - public API mirrors existing cap exception.
    """Raised when an order would overstack a correlated prop sleeve."""

    def __init__(
        self,
        message: str,
        *,
        sleeve: str,
        root: str,
        current_equiv: float,
        requested_equiv: float,
        proposed_equiv: float,
        sleeve_cap: float,
    ) -> None:
        super().__init__(message)
        self.sleeve = sleeve
        self.root = root
        self.current_equiv = current_equiv
        self.requested_equiv = requested_equiv
        self.proposed_equiv = proposed_equiv
        self.sleeve_cap = sleeve_cap


def resolve_fleet_cap(root: str) -> float:
    """Resolve the active fleet cap for a symbol root.

    Resolution order (most-specific wins):

    1. ``ETA_FLEET_POSITION_CAP_<ROOT>`` (env override)
    2. :data:`DEFAULT_ROOT_CAPS` entry
    3. ``ETA_FLEET_POSITION_CAP_DEFAULT`` (env)
    4. :data:`DEFAULT_FALLBACK_CAP`
    """
    root_u = root.upper()
    raw = os.environ.get("ETA_FLEET_POSITION_CAP_" + root_u, "").strip()
    if raw:
        try:
            return abs(float(raw))
        except ValueError:
            logger.warning(
                "ETA_FLEET_POSITION_CAP_%s=%r is not a number; falling through to defaults",
                root_u,
                raw,
            )
    if root_u in DEFAULT_ROOT_CAPS:
        return DEFAULT_ROOT_CAPS[root_u]
    raw_default = os.environ.get("ETA_FLEET_POSITION_CAP_DEFAULT", "").strip()
    if raw_default:
        try:
            return abs(float(raw_default))
        except ValueError:
            pass
    return DEFAULT_FALLBACK_CAP


def prop_sleeve_for_root(root: str) -> str | None:
    """Return the prop sleeve name for a root, or None when not sleeved."""
    root_u = normalize_root(root)
    for sleeve, multipliers in PROP_SLEEVE_MULTIPLIERS.items():
        if root_u in multipliers:
            return sleeve
    return None


def resolve_prop_sleeve_cap(sleeve: str) -> float:
    """Resolve the MNQ-equivalent cap for a correlated prop sleeve."""
    sleeve_u = (sleeve or "").upper()
    raw = os.environ.get(
        "ETA_PROP_SLEEVE_CAP_" + sleeve_u + "_MNQ_EQUIV",
        "",
    ).strip()
    if raw:
        try:
            return abs(float(raw))
        except ValueError:
            logger.warning(
                "ETA_PROP_SLEEVE_CAP_%s_MNQ_EQUIV=%r is not a number; falling through to defaults",
                sleeve_u,
                raw,
            )
    raw_default = os.environ.get("ETA_PROP_SLEEVE_CAP_DEFAULT_MNQ_EQUIV", "").strip()
    if raw_default:
        try:
            return abs(float(raw_default))
        except ValueError:
            pass
    return DEFAULT_PROP_SLEEVE_CAPS.get(sleeve_u, DEFAULT_FALLBACK_CAP)


@dataclass
class CrossBotPositionTracker:
    """In-process net-position book, keyed by symbol root."""

    state_path: Path | None = None
    disabled: bool = field(
        default_factory=lambda: _is_truthy_env("ETA_FLEET_POSITION_CAP_DISABLED"),
    )
    _net_by_root: dict[str, float] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def net_position(self, root: str) -> float:
        """Return signed net qty for ``root`` (0.0 if unknown)."""
        with self._lock:
            return float(self._net_by_root.get(normalize_root(root), 0.0))

    def snapshot(self) -> dict[str, float]:
        """Return a copy of the full {root: net_qty} map."""
        with self._lock:
            return dict(self._net_by_root)

    def assert_fleet_position_cap(
        self,
        *,
        symbol_root: str,
        side: str,
        requested_delta: float,
        fleet_cap: float | None = None,
    ) -> None:
        """Pass when the order keeps net within the cap; raise otherwise.

        ``requested_delta`` is the absolute requested qty (positive
        contracts). ``side`` is BUY/SELL/LONG/SHORT and determines the
        sign. ``fleet_cap=None`` resolves from env via
        :func:`resolve_fleet_cap`. Compares ``|current_net + signed_delta|``
        to the cap so a long+short net OUT to zero is allowed.

        No-op when the gate is disabled (``ETA_FLEET_POSITION_CAP_DISABLED``).
        """
        if self.disabled:
            return
        root = normalize_root(symbol_root)
        signed = signed_delta(side, requested_delta)
        if fleet_cap is None:
            fleet_cap = resolve_fleet_cap(root)
        cap = abs(float(fleet_cap))
        with self._lock:
            current = float(self._net_by_root.get(root, 0.0))
        proposed = current + signed
        if abs(proposed) > cap + 1e-9:
            msg = (
                "fleet position cap exceeded: root="
                + root
                + " current_net="
                + format(current, "+g")
                + " requested_delta="
                + format(signed, "+g")
                + " proposed_total="
                + format(proposed, "+g")
                + " cap="
                + format(cap, "g")
            )
            raise FleetPositionCapExceeded(
                msg,
                root=root,
                current_net=current,
                requested_delta=signed,
                proposed_total=proposed,
                fleet_cap=cap,
            )

    def assert_prop_sleeve_cap(
        self,
        *,
        symbol_root: str,
        side: str,
        requested_delta: float,
        sleeve_cap: float | None = None,
    ) -> None:
        """Pass when a correlated prop sleeve remains within its cap.

        The comparison is made in equivalent contracts, not raw contract
        count. For the Nasdaq sleeve, ``1 NQ`` counts as ``10 MNQ``.
        Opposite-side orders that reduce net sleeve exposure are allowed.
        """
        if self.disabled or _is_truthy_env("ETA_PROP_SLEEVE_CAP_DISABLED"):
            return
        root = normalize_root(symbol_root)
        sleeve = prop_sleeve_for_root(root)
        if sleeve is None:
            return
        multipliers = PROP_SLEEVE_MULTIPLIERS[sleeve]
        requested_equiv = signed_delta(side, requested_delta) * multipliers[root]
        if sleeve_cap is None:
            sleeve_cap = resolve_prop_sleeve_cap(sleeve)
        cap = abs(float(sleeve_cap))
        with self._lock:
            current = sum(
                float(self._net_by_root.get(member_root, 0.0)) * member_mult
                for member_root, member_mult in multipliers.items()
            )
        proposed = current + requested_equiv
        if abs(proposed) > cap + 1e-9:
            msg = (
                "prop sleeve cap exceeded: sleeve="
                + sleeve
                + " root="
                + root
                + " current_equiv="
                + format(current, "+g")
                + " requested_equiv="
                + format(requested_equiv, "+g")
                + " proposed_equiv="
                + format(proposed, "+g")
                + " cap="
                + format(cap, "g")
            )
            raise PropSleeveCapExceeded(
                msg,
                sleeve=sleeve,
                root=root,
                current_equiv=current,
                requested_equiv=requested_equiv,
                proposed_equiv=proposed,
                sleeve_cap=cap,
            )

    def record_entry(self, *, symbol_root: str, side: str, qty: float) -> float:
        """Apply a successful entry to the running net.

        Returns the new net for ``symbol_root``. Persists to disk if a
        ``state_path`` was configured. Caller MUST call exactly once per
        broker-acknowledged entry.
        """
        signed = signed_delta(side, qty)
        return self._apply_delta(symbol_root, signed)

    def record_exit(self, *, symbol_root: str, side: str, qty: float) -> float:
        """Apply a successful exit (close) to the running net.

        ``side`` is the EXIT side (the side the broker shipped to flatten),
        so a SELL exit on a long position decrements the long net.
        """
        signed = signed_delta(side, qty)
        return self._apply_delta(symbol_root, signed)

    def resync_from_broker(self, *, by_root: Mapping[str, float]) -> None:
        """Replace the running net for every supplied root with broker truth.

        Roots in ``by_root`` overwrite the tracker belief. Roots NOT
        in ``by_root`` are LEFT ALONE - the broker may not have queried
        every venue, so a missing key is ambiguity, not a flatten signal.
        """
        if not by_root:
            return
        with self._lock:
            for raw_root, qty in by_root.items():
                root = normalize_root(raw_root)
                self._net_by_root[root] = float(qty)
        self._persist()

    def reset(self) -> None:
        """Clear the running net (operator override / test helper)."""
        with self._lock:
            self._net_by_root.clear()
        self._persist()

    def load(self) -> int:
        """Load the running net from disk. Returns count of roots restored."""
        if self.state_path is None or not self.state_path.exists():
            return 0
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(
                "cross_bot_position_tracker: failed to load %s: %s - starting from zero (broker reconcile will fix)",
                self.state_path,
                exc,
            )
            return 0
        if not isinstance(data, dict):
            logger.warning(
                "cross_bot_position_tracker: %s is not a dict, ignoring",
                self.state_path,
            )
            return 0
        with self._lock:
            self._net_by_root.clear()
            for raw_root, qty in data.items():
                try:
                    self._net_by_root[normalize_root(str(raw_root))] = float(qty)
                except (TypeError, ValueError):
                    continue
            count = len(self._net_by_root)
        return count

    def _apply_delta(self, symbol_root: str, signed: float) -> float:
        root = normalize_root(symbol_root)
        with self._lock:
            new_net = float(self._net_by_root.get(root, 0.0)) + signed
            if abs(new_net) < 1e-9:
                new_net = 0.0
            self._net_by_root[root] = new_net
        self._persist()
        return new_net

    def _persist(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            with self._lock:
                payload = json.dumps(self._net_by_root, sort_keys=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            logger.warning(
                "cross_bot_position_tracker: failed to persist %s: %s",
                self.state_path,
                exc,
            )


_singleton: CrossBotPositionTracker | None = None


def register_cross_bot_position_tracker(
    tracker: CrossBotPositionTracker | None,
) -> None:
    """Register the process-wide tracker. Pass ``None`` to clear."""
    global _singleton
    _singleton = tracker


def get_cross_bot_position_tracker() -> CrossBotPositionTracker | None:
    """Return the registered tracker, or ``None`` if not registered."""
    return _singleton


def assert_fleet_position_cap(
    *,
    symbol_root: str,
    side: str,
    requested_delta: float,
    fleet_cap: float | None = None,
) -> None:
    """Module-level pre-trade gate - dispatches to the registered tracker.

    No-op when no tracker has been registered (paper / unit-test paths).
    Raises :class:`FleetPositionCapExceeded` when the proposed total
    would exceed the configured fleet cap for the root.
    """
    tracker = _singleton
    if tracker is None:
        return
    tracker.assert_fleet_position_cap(
        symbol_root=symbol_root,
        side=side,
        requested_delta=requested_delta,
        fleet_cap=fleet_cap,
    )


def assert_prop_sleeve_cap(
    *,
    symbol_root: str,
    side: str,
    requested_delta: float,
    sleeve_cap: float | None = None,
) -> None:
    """Module-level prop-sleeve gate - dispatches to the singleton."""
    tracker = _singleton
    if tracker is None:
        return
    tracker.assert_prop_sleeve_cap(
        symbol_root=symbol_root,
        side=side,
        requested_delta=requested_delta,
        sleeve_cap=sleeve_cap,
    )


__all__ = [
    "CrossBotPositionTracker",
    "DEFAULT_FALLBACK_CAP",
    "DEFAULT_PROP_SLEEVE_CAPS",
    "DEFAULT_ROOT_CAPS",
    "FleetPositionCapExceeded",
    "PROP_SLEEVE_MULTIPLIERS",
    "PropSleeveCapExceeded",
    "STATE_FILENAME",
    "assert_fleet_position_cap",
    "assert_prop_sleeve_cap",
    "get_cross_bot_position_tracker",
    "normalize_root",
    "prop_sleeve_for_root",
    "register_cross_bot_position_tracker",
    "resolve_fleet_cap",
    "resolve_prop_sleeve_cap",
    "signed_delta",
]
