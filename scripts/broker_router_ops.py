from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections import deque
    from collections.abc import Callable, MutableMapping
    from logging import Logger


class _HoldLike(Protocol):
    active: bool
    reason: str
    scope: str

    def to_dict(self) -> dict[str, Any]: ...


class BrokerRouterOpsSurface:
    """Own broker-router gate snapshots plus heartbeat sidecars."""

    def __init__(
        self,
        *,
        pending_dir: Path,
        state_root: Path,
        heartbeat_path: Path,
        gate_pre_trade_path: Path,
        gate_heat_state_path: Path,
        gate_journal_path: Path,
        dry_run: bool,
        interval_s: float,
        max_retries: int,
        counts: MutableMapping[str, int],
        recent_events: deque[dict[str, Any]],
        order_entry_hold: Callable[[], _HoldLike],
        venue_circuit_states: Callable[[], dict[str, str]],
        write_sidecar: Callable[[Path, dict[str, Any]], None],
        env_int: Callable[[str, int], int],
        env_float: Callable[[str, float], float],
        logger: Logger,
    ) -> None:
        self._pending_dir = Path(pending_dir)
        self._state_root = Path(state_root)
        self._heartbeat_path = Path(heartbeat_path)
        self._gate_pre_trade_path = Path(gate_pre_trade_path)
        self._gate_heat_state_path = Path(gate_heat_state_path)
        self._gate_journal_path = Path(gate_journal_path)
        self._dry_run = bool(dry_run)
        self._interval_s = float(interval_s)
        self._max_retries = int(max_retries)
        self._counts = counts
        self._recent_events = recent_events
        self._order_entry_hold = order_entry_hold
        self._venue_circuit_states = venue_circuit_states
        self._write_sidecar = write_sidecar
        self._env_int = env_int
        self._env_float = env_float
        self._logger = logger

    def sync_gate_state(
        self,
        *,
        hold: _HoldLike,
        open_positions: dict[str, int],
    ) -> None:
        """Keep the firm gate-chain sidecars aligned with live router state."""
        now_iso = datetime.now(UTC).isoformat()
        self._write_sidecar(
            self._gate_pre_trade_path,
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
            self._gate_heat_state_path,
            self.heat_state_snapshot(now_iso=now_iso, open_positions=open_positions),
        )
        self.ensure_gate_journal()

    def heat_state_snapshot(
        self,
        *,
        now_iso: str,
        open_positions: dict[str, int],
    ) -> dict[str, Any]:
        """Return a conservative multi-bot heat-budget snapshot."""
        nonzero_positions = {
            symbol: qty for symbol, qty in open_positions.items() if int(qty or 0) != 0
        }
        max_concurrent = max(1, self._env_int("ETA_BROKER_ROUTER_GATE_MAX_CONCURRENT", 8))
        budget = max(0.01, self._env_float("ETA_BROKER_ROUTER_GATE_BUDGET", 1.0))
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

    def ensure_gate_journal(self) -> None:
        """Ensure the governor gate journal has a readable SQLite shell."""
        try:
            self._gate_journal_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._gate_journal_path) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS events ("
                    "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "ts TEXT NOT NULL, "
                    "event_type TEXT NOT NULL, "
                    "payload TEXT NOT NULL"
                    ")"
                )
        except sqlite3.Error as exc:
            self._logger.warning(
                "gate journal initialization failed %s: %s",
                self._gate_journal_path,
                exc,
            )

    def emit_heartbeat(self, *, hold: _HoldLike | None = None) -> None:
        """Write the router heartbeat used by downstream operator surfaces."""
        now_iso = datetime.now(UTC).isoformat()
        resolved_hold = hold if hold is not None else self._order_entry_hold()
        self._write_sidecar(
            self._heartbeat_path,
            {
                "ts": now_iso,
                "last_poll_ts": now_iso,
                "pending_dir": str(self._pending_dir),
                "state_root": str(self._state_root),
                "order_entry_hold": resolved_hold.to_dict(),
                "dry_run": self._dry_run,
                "interval_s": self._interval_s,
                "max_retries": self._max_retries,
                "counts": dict(self._counts),
                "recent_events": list(self._recent_events),
                "venue_circuits": self._venue_circuit_states(),
            },
        )
