from __future__ import annotations

import json
import os
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from logging import Logger


class _OrderLike(Protocol):
    bot_id: str
    symbol: str
    qty: float


class _HoldLike(Protocol):
    active: bool
    reason: str
    scope: str

    def to_dict(self) -> dict[str, Any]: ...


class BrokerRouterGateEvaluator:
    """Own broker-router gate evaluation plus readiness snapshot checks."""

    def __init__(
        self,
        *,
        heartbeat_path: Path,
        gate_pre_trade_path: Path,
        gate_heat_state_path: Path,
        gate_journal_path: Path,
        normalize_gate_result: Callable[[object], dict[str, Any]],
        load_build_default_chain: Callable[[], Callable[..., object]],
        gate_bootstrap_enabled: Callable[[], bool],
        order_entry_hold: Callable[[], _HoldLike],
        sync_gate_state: Callable[..., None],
        readiness_enforced: Callable[[], bool],
        readiness_snapshot_path: Callable[[], Path],
        live_money_env: str,
        logger: Logger,
    ) -> None:
        self._heartbeat_path = Path(heartbeat_path)
        self._gate_pre_trade_path = Path(gate_pre_trade_path)
        self._gate_heat_state_path = Path(gate_heat_state_path)
        self._gate_journal_path = Path(gate_journal_path)
        self._normalize_gate_result = normalize_gate_result
        self._load_build_default_chain = load_build_default_chain
        self._gate_bootstrap_enabled = gate_bootstrap_enabled
        self._order_entry_hold = order_entry_hold
        self._sync_gate_state = sync_gate_state
        self._readiness_enforced = readiness_enforced
        self._readiness_snapshot_path = readiness_snapshot_path
        self._live_money_env = live_money_env
        self._logger = logger

    def evaluate_gates(self, order: _OrderLike, override: object | None) -> list[dict[str, Any]]:
        """Run the gate chain and normalize the result rows."""
        if override is not None:
            try:
                _allow, results = self.invoke_gate_chain_override(override, order)
            except NotImplementedError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._logger.error("gate_chain override raised %s; DENY (fail-closed)", exc)
                return [{"gate": "chain_error", "allow": False, "reason": f"chain raised: {exc}", "context": {}}]
            return [self._normalize_gate_result(r) for r in results]

        try:
            build_default_chain = self._load_build_default_chain()
        except ImportError as exc:
            tb = traceback.format_exc()
            if self._gate_bootstrap_enabled():
                self._logger.error(
                    "gate chain import failed (%s); ETA_GATE_BOOTSTRAP=1 set, allowing order through.\n%s",
                    exc,
                    tb,
                )
                return [
                    {
                        "gate": "import_error_bootstrap",
                        "allow": True,
                        "reason": f"gate_chain unavailable (bootstrap): {exc}",
                        "context": {"traceback": tb},
                    }
                ]
            self._logger.error(
                "gate chain import failed (%s); fail-closed DENY. "
                "Set ETA_GATE_BOOTSTRAP=1 only if you accept the risk.\n%s",
                exc,
                tb,
            )
            return [
                {
                    "gate": "gate_chain_import_failed",
                    "allow": False,
                    "reason": f"gate_chain unavailable: {exc}",
                    "context": {"traceback": tb},
                }
            ]

        open_positions = self.collect_open_positions()
        hold = self._order_entry_hold()
        self._sync_gate_state(hold=hold, open_positions=open_positions)
        try:
            chain = build_default_chain(
                open_positions=open_positions,
                new_symbol=order.symbol,
                new_qty=int(round(order.qty)) or 1,
                heartbeat_path=self._heartbeat_path,
                deadman_heartbeat_path=self._heartbeat_path,
                pre_trade_path=self._gate_pre_trade_path,
                deadman_pre_trade_path=self._gate_pre_trade_path,
                heat_state_path=self._gate_heat_state_path,
                journal_path=self._gate_journal_path,
            )
            _allow, results = chain.evaluate()
        except Exception as exc:  # noqa: BLE001
            self._logger.error("gate chain evaluation raised %s; DENY (fail-closed)", exc)
            return [{"gate": "chain_error", "allow": False, "reason": f"chain raised: {exc}", "context": {}}]
        return [self._normalize_gate_result(r) for r in results]

    def invoke_gate_chain_override(
        self,
        override: object,
        order: _OrderLike,
    ) -> tuple[bool, list[object]]:
        """Invoke a test or shadow gate_chain override."""
        kwargs = {
            "open_positions": self.collect_open_positions(),
            "new_symbol": order.symbol,
            "new_qty": int(round(order.qty)) or 1,
        }
        if callable(override):
            return override(**kwargs)
        return override.evaluate(**kwargs)

    def collect_open_positions(self) -> dict[str, int]:
        """Pull aggregated bot positions for the correlation gate."""
        if os.environ.get("ETA_RECONCILE_DISABLED") == "1":
            return {}
        try:
            from eta_engine.obs.position_reconciler import fetch_bot_positions

            agg = fetch_bot_positions()
        except NotImplementedError as exc:
            if os.environ.get("ETA_RECONCILE_ALLOW_EMPTY_STATE") == "1":
                self._logger.info("empty bot-positions tolerated: %s", exc)
                return {}
            raise
        except RuntimeError as exc:
            if os.environ.get("ETA_RECONCILE_ALLOW_EMPTY_STATE") == "1":
                self._logger.info("empty bot-positions tolerated: %s", exc)
                return {}
            self._logger.warning("fetch_bot_positions failed: %s", exc)
            return {}
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("fetch_bot_positions errored: %s", exc)
            return {}
        out: dict[str, int] = {}
        for symbol, by_bot in agg.items():
            net = sum(by_bot.values())
            if abs(net) > 0.0:
                out[symbol] = int(round(net))
        return out

    def readiness_denial(self, order: _OrderLike) -> str:
        """Return a denial reason when a bot is not approved for routing."""
        if not self._readiness_enforced():
            return ""
        snapshot_path = self._readiness_snapshot_path()
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return f"strategy readiness snapshot missing: {snapshot_path}"
        except (OSError, json.JSONDecodeError) as exc:
            return f"strategy readiness snapshot unreadable: {exc}"

        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return "strategy readiness snapshot malformed: rows missing"
        match = next(
            (row for row in rows if isinstance(row, dict) and str(row.get("bot_id") or "") == order.bot_id),
            None,
        )
        if not isinstance(match, dict):
            return f"bot {order.bot_id!r} missing from strategy readiness snapshot"

        if os.environ.get(self._live_money_env, "").strip() == "1":
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
