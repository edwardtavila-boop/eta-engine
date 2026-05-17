from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from logging import Logger


class SupervisorPersistenceStore:
    """Own the supervisor's restart-safe position and pending-order files."""

    def __init__(
        self,
        *,
        state_dir: Path,
        bf_dir: Path,
        bots_ref: Callable[[], list[Any]],
        logger: Logger,
        atomic_write_text: Callable[[Path, str], None],
        round_to_tick: Callable[[float, str], float],
    ) -> None:
        self._state_dir = state_dir
        self._bf_dir = bf_dir
        self._bf_dir.mkdir(parents=True, exist_ok=True)
        self._bots_ref = bots_ref
        self._logger = logger
        self._atomic_write_text = atomic_write_text
        self._round_to_tick = round_to_tick

    def open_positions_dir(self) -> Path:
        return self._state_dir / "bots"

    def open_position_path(self, bot_id: str) -> Path:
        return self.open_positions_dir() / bot_id / "open_position.json"

    def persist_open_position(self, bot: Any) -> None:
        """Write bot.open_position to disk (atomic). No-op if None."""
        if bot.open_position is None:
            return
        try:
            path = self.open_position_path(bot.bot_id)
            bot.open_position["symbol"] = bot.symbol
            self._atomic_write_text(path, json.dumps(bot.open_position, default=str))
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            self._logger.warning(
                "_persist_open_position(%s) failed: %s - bot state may not survive restart",
                bot.bot_id,
                exc,
            )

    def clear_persisted_open_position(self, bot: Any) -> None:
        """Delete the persisted open_position file. Safe if missing."""
        try:
            path = self.open_position_path(bot.bot_id)
            if path.exists():
                path.unlink()
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("_clear_persisted_open_position(%s) failed: %s", bot.bot_id, exc)

    def load_persisted_open_positions(self) -> int:
        """Restore bot.open_position from disk for each bot at startup."""
        if not self.open_positions_dir().exists():
            return 0
        restored = 0
        bot_by_id = {bot.bot_id: bot for bot in self._bots_ref()}
        for bot_dir in self.open_positions_dir().iterdir():
            if not bot_dir.is_dir():
                continue
            bot = bot_by_id.get(bot_dir.name)
            if bot is None:
                continue
            path = bot_dir / "open_position.json"
            if not path.exists():
                continue
            try:
                bot.open_position = json.loads(path.read_text(encoding="utf-8"))
                restored += 1
                self._logger.info(
                    "restored open_position for %s: side=%s entry_price=%s qty=%s signal_id=%s",
                    bot.bot_id,
                    bot.open_position.get("side"),
                    bot.open_position.get("entry_price"),
                    bot.open_position.get("qty"),
                    bot.open_position.get("signal_id"),
                )
            except Exception as exc:  # noqa: BLE001
                self._logger.warning(
                    "_load_persisted_open_positions: failed to read %s: %s",
                    path,
                    exc,
                )
        return restored

    def write_pending_order(
        self,
        bot: Any,
        rec: Any,
        *,
        reduce_only: bool = False,
    ) -> None:
        pos = bot.open_position or {}
        raw_stop = pos.get("bracket_stop")
        raw_target = pos.get("bracket_target")
        stop_price = self._round_to_tick(float(raw_stop), rec.symbol) if raw_stop is not None else None
        target_price = self._round_to_tick(float(raw_target), rec.symbol) if raw_target is not None else None
        limit_price = self._round_to_tick(float(rec.fill_price), rec.symbol)
        try:
            path = self._bf_dir / f"{bot.bot_id}.pending_order.json"
            path.write_text(
                json.dumps(
                    {
                        "ts": rec.fill_ts,
                        "signal_id": rec.signal_id,
                        "side": rec.side,
                        "qty": rec.qty,
                        "symbol": rec.symbol,
                        "limit_price": limit_price,
                        "stop_price": stop_price,
                        "target_price": target_price,
                        "reduce_only": bool(reduce_only),
                        "execution_lane": str(bot.execution_lane or ""),
                        "capital_gate_scope": str(bot.capital_gate_scope or ""),
                        "daily_loss_gate_mode": str(bot.daily_loss_gate_mode or ""),
                        "daily_loss_gate_active": bool(bot.daily_loss_gate_active),
                        "daily_loss_gate_reason": str(bot.daily_loss_gate_reason or ""),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            self._logger.warning("pending order write failed (%s)", exc)
