"""Bridge: read JARVIS strategy supervisor heartbeat, produce dashboard rows.

The wave-12 ``jarvis_strategy_supervisor.py`` writes its 16-bot roster to
``var/eta_engine/state/jarvis_intel/supervisor/heartbeat.json`` every tick. The legacy
``command_center.server.app:_bot_fleet_view`` builds its bot_accounts
list from a different payload (the master_dashboard service) that
doesn't see the supervisor at all -- so the fleet dashboard ended up
showing ``confirmed_bots: 0`` while 16 bots were actively trading.

This module is the bridge. It exposes one public function,
``jarvis_supervisor_bot_accounts()``, which reads the heartbeat JSON
and returns a list of dicts in the exact shape that the dashboard's
``_normalize_mnq_bot`` expects (id, name, broker, strategy, status,
today.{trades,wins,losses,pnl,max_drawdown}, ...).

The function lives in a tracked module so:
  * The legacy ``command_center.server.app`` (deployed, not in git) can
    ``from eta_engine.scripts.jarvis_supervisor_bridge import ...`` it.
  * The Option-B replacement dashboard on port 8000 can use the same
    code path tomorrow.
  * Tests can pin the contract without needing the full command_center
    package installed.

Failure modes are silent on purpose -- a missing or unparseable
heartbeat must never break the dashboard endpoint.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# ROOT = the repo root (parents[1] of scripts/)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402

DEFAULT_HEARTBEAT_PATH = workspace_roots.ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH


def jarvis_supervisor_bot_accounts(
    *,
    heartbeat_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Read the supervisor heartbeat, return rows in dashboard shape.

    Parameters
    ----------
    heartbeat_path:
        Path to the supervisor heartbeat JSON. Defaults to the canonical
        location ``<workspace>/var/eta_engine/state/jarvis_intel/supervisor/heartbeat.json``.
        Override in tests.

    Returns
    -------
    list[dict[str, Any]]
        One dict per supervisor bot, shaped to match the input that
        ``command_center.server.bot_fleet_dashboard._normalize_mnq_bot``
        expects. Empty list when the heartbeat is missing, unparseable,
        or doesn't contain a bot roster.
    """
    path = heartbeat_path or DEFAULT_HEARTBEAT_PATH
    try:
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
        hb = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return []

    raw_bots = hb.get("bots") if isinstance(hb, dict) else None
    if not isinstance(raw_bots, list):
        return []

    hb_ts = str(hb.get("ts") or "")
    # Top-level supervisor mode -- used as fallback when a per-bot mode
    # field is missing on legacy heartbeats. The default of "paper_sim"
    # remains the safety floor (no real orders) for an unparseable
    # heartbeat. See PAPER_LIVE_ROUTING_GAP.md.
    hb_mode = str(hb.get("mode") or "paper_sim")
    accounts: list[dict[str, Any]] = []
    for bot in raw_bots:
        if not isinstance(bot, dict):
            continue
        # Prefer the per-bot ``mode`` field on the heartbeat (added
        # 2026-05-04 to fix dashboard 52-bot paper_sim badge bug). Fall
        # back to top-level ``hb["mode"]`` for older heartbeats that
        # predate the per-bot field.
        bot_mode = str(bot.get("mode") or hb_mode)
        n_entries = int(bot.get("n_entries") or 0)
        n_exits = int(bot.get("n_exits") or 0)
        realized_pnl = float(bot.get("realized_pnl") or 0.0)
        # We don't track wins/losses per bot in the supervisor today;
        # use exits as a proxy for trade count, and split on PnL sign
        # so the existing win-rate computation has something to work
        # with. Approximate but better than zero.
        wins = n_exits if realized_pnl > 0 else 0
        losses = n_exits if realized_pnl < 0 else 0
        last_verdict = str(bot.get("last_jarvis_verdict") or "")
        # Diagnostic reason for last_jarvis_verdict == "NONE" set by the
        # supervisor when JARVIS bootstrap is down, the regime gate fires,
        # or consult() raised. Empty after a clean consult.
        last_verdict_reason = str(bot.get("last_jarvis_verdict_reason") or "")
        last_aggregation_reject_reason = str(bot.get("last_aggregation_reject_reason") or "")
        last_aggregation_reject_at = str(bot.get("last_aggregation_reject_at") or "")
        strategy_readiness = bot.get("strategy_readiness")
        readiness_payload = strategy_readiness if isinstance(strategy_readiness, dict) else {}
        open_position = bot.get("open_position") if isinstance(bot.get("open_position"), dict) else {}
        open_positions = 1 if open_position else 0
        running = bool(open_position) or n_entries > 0
        explicit_status = str(bot.get("status") or "").strip().lower()
        status = explicit_status if explicit_status else ("running" if running else "idle")
        last_bar_ts = str(bot.get("last_bar_ts") or "")
        # A bar refresh is market-data freshness, not a trading signal.
        # Surface true signal time when the supervisor has actually fired
        # an entry, and keep last_bar_ts separate for activity displays.
        last_signal_at = str(bot.get("last_signal_at") or bot.get("last_signal_ts") or "")
        accounts.append(
            {
                "id": str(bot.get("bot_id") or ""),
                "name": str(bot.get("bot_id") or ""),
                "broker": "paper-sim",
                "strategy": str(bot.get("strategy_kind") or ""),
                "mode": bot_mode,
                "status": status,
                "confirmed": True,
                "today": {
                    "trades": n_exits,
                    "wins": wins,
                    "losses": losses,
                    "pnl": realized_pnl,
                    "max_drawdown": 0.0,
                },
                "open_position": open_position,
                "open_positions": open_positions,
                "source": "jarvis_strategy_supervisor",
                "updated_at": hb_ts,
                "heartbeat_ts": hb_ts,
                "last_signal_ts": last_signal_at,
                "last_signal_at": last_signal_at,
                "last_bar_ts": last_bar_ts,
                # Extra fields the dashboard ignores but useful for clients
                # reading the JSON directly:
                "symbol": str(bot.get("symbol") or ""),
                "direction": str(bot.get("direction") or ""),
                "last_jarvis_verdict": last_verdict,
                "last_jarvis_verdict_reason": last_verdict_reason,
                "last_aggregation_reject_reason": last_aggregation_reject_reason,
                "last_aggregation_reject_at": last_aggregation_reject_at,
                "strategy_readiness": readiness_payload,
                "launch_lane": readiness_payload.get("launch_lane"),
                "can_paper_trade": bool(readiness_payload.get("can_paper_trade")),
                "can_live_trade": bool(readiness_payload.get("can_live_trade")),
                "readiness_next_action": readiness_payload.get("next_action"),
                "execution_lane": str(bot.get("execution_lane") or ""),
                "capital_gate_scope": str(bot.get("capital_gate_scope") or ""),
                "daily_loss_gate_mode": str(bot.get("daily_loss_gate_mode") or ""),
                "daily_loss_gate_active": bool(bot.get("daily_loss_gate_active")),
                "daily_loss_gate_reason": str(bot.get("daily_loss_gate_reason") or ""),
            }
        )

    # JARVIS Supercharge live trace tail — last 3 consult reasoning lines
    # surface in the heartbeat so the operator can see JARVIS thinking in
    # real time without tailing var/eta_engine/state/jarvis_trace.jsonl.
    # Best-effort: if trace_emitter is missing, the trace file is empty,
    # or the file has been rotated, just skip — never break the bridge.
    try:
        from eta_engine.brain.jarvis_v3 import trace_emitter

        trace_tail = trace_emitter.tail(n=3)
        if trace_tail and accounts:
            accounts[0]["jarvis_trace_tail"] = trace_tail
    except Exception:  # noqa: BLE001 — heartbeat must never crash on observability
        pass

    return accounts


def merge_supervisor_into_payload(
    payload: dict[str, Any],
    *,
    heartbeat_path: Path | None = None,
) -> dict[str, Any]:
    """Layer supervisor bots into a master_dashboard ``bot_accounts`` field.

    Returns a fresh dict so the cached source payload is not mutated.
    Supervisor rows take precedence over legacy entries with the same id
    (they're strictly newer / live). When the supervisor isn't running
    (no heartbeat), returns the original payload unchanged.
    """
    sup_accounts = jarvis_supervisor_bot_accounts(heartbeat_path=heartbeat_path)
    if not sup_accounts:
        return payload

    existing = payload.get("bot_accounts")
    existing_list = list(existing) if isinstance(existing, list) else []
    sup_ids = {acc["id"] for acc in sup_accounts}
    merged = [
        row for row in existing_list if isinstance(row, dict) and str(row.get("id") or "") not in sup_ids
    ] + sup_accounts
    return {**payload, "bot_accounts": merged}


__all__ = [
    "DEFAULT_HEARTBEAT_PATH",
    "jarvis_supervisor_bot_accounts",
    "merge_supervisor_into_payload",
]
