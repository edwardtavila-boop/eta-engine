"""Bridge: read JARVIS strategy supervisor heartbeat, produce dashboard rows.

The wave-12 ``jarvis_strategy_supervisor.py`` writes its 16-bot roster to
``state/jarvis_intel/supervisor/heartbeat.json`` every tick. The legacy
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
from pathlib import Path
from typing import Any

# ROOT = the repo root (parents[1] of scripts/)
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HEARTBEAT_PATH = (
    ROOT / "state" / "jarvis_intel" / "supervisor" / "heartbeat.json"
)


def jarvis_supervisor_bot_accounts(
    *,
    heartbeat_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Read the supervisor heartbeat, return rows in dashboard shape.

    Parameters
    ----------
    heartbeat_path:
        Path to the supervisor heartbeat JSON. Defaults to the canonical
        location ``<repo>/state/jarvis_intel/supervisor/heartbeat.json``.
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
    mode = str(hb.get("mode") or "paper_sim")
    accounts: list[dict[str, Any]] = []
    for bot in raw_bots:
        if not isinstance(bot, dict):
            continue
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
        running = bool(bot.get("open_position")) or n_entries > 0
        status = "running" if running else "idle"
        accounts.append({
            "id": str(bot.get("bot_id") or ""),
            "name": str(bot.get("bot_id") or ""),
            "broker": "paper-sim",
            "strategy": str(bot.get("strategy_kind") or ""),
            "mode": mode,
            "status": status,
            "confirmed": True,
            "today": {
                "trades": n_exits,
                "wins": wins,
                "losses": losses,
                "pnl": realized_pnl,
                "max_drawdown": 0.0,
            },
            "open_position": bot.get("open_position") or {},
            "source": "jarvis_strategy_supervisor",
            "updated_at": str(bot.get("last_bar_ts") or hb_ts),
            # Extra fields the dashboard ignores but useful for clients
            # reading the JSON directly:
            "symbol": str(bot.get("symbol") or ""),
            "direction": str(bot.get("direction") or ""),
            "last_jarvis_verdict": last_verdict,
        })
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
        row for row in existing_list
        if isinstance(row, dict) and str(row.get("id") or "") not in sup_ids
    ] + sup_accounts
    return {**payload, "bot_accounts": merged}


__all__ = [
    "DEFAULT_HEARTBEAT_PATH",
    "jarvis_supervisor_bot_accounts",
    "merge_supervisor_into_payload",
]
