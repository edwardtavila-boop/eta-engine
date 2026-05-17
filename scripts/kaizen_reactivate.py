"""Re-enable a bot that the kaizen loop auto-deactivated.

The kaizen loop (--apply) writes a deactivation entry to
``var/eta_engine/state/kaizen_overrides.json`` after the 2-run
confirmation gate. ``per_bot_registry.is_active()`` reads this file
and drops the bot from the supervisor's load_bots() filter.

Operator override:
    python -m eta_engine.scripts.kaizen_reactivate <bot_id>
    python -m eta_engine.scripts.kaizen_reactivate <bot_id> [<bot_id> ...]
    python -m eta_engine.scripts.kaizen_reactivate --list
    python -m eta_engine.scripts.kaizen_reactivate --clear-all

Removing the override does NOT undo the registry-level ``deactivated``
flag (that's a code-level decision made when a bot was diamond-cut by
the operator). Only kaizen-applied auto-deactivations are reversible
through this tool.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from eta_engine.scripts import workspace_roots

_OVERRIDES_PATH = workspace_roots.ETA_KAIZEN_OVERRIDES_PATH
_REACTIVATE_LOG = workspace_roots.ETA_KAIZEN_REACTIVATE_LOG_PATH


def _read() -> dict[str, dict]:
    if not _OVERRIDES_PATH.exists():
        return {"deactivated": {}}
    try:
        data = json.loads(_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"deactivated": {}}
    if not isinstance(data, dict):
        return {"deactivated": {}}
    if not isinstance(data.get("deactivated"), dict):
        data["deactivated"] = {}
    return data


def _write(data: dict) -> None:
    _OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OVERRIDES_PATH.write_text(
        json.dumps(data, indent=2, default=str),
        encoding="utf-8",
    )


def _audit(action: str, bot_ids: list[str]) -> None:
    """Append a single line to the reactivate audit log."""
    try:
        _REACTIVATE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _REACTIVATE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "action": action,
                        "bot_ids": bot_ids,
                    }
                )
                + "\n",
            )
    except OSError:
        pass


def list_overrides() -> int:
    data = _read()
    deact = data.get("deactivated", {})
    if not deact:
        print("(no kaizen overrides — sidecar empty or missing)")
        return 0
    print(f"{'bot_id':<28} {'tier':<10} {'mc':<10} {'expR':<10} applied_at")
    print("-" * 88)
    for bot_id, rec in sorted(deact.items()):
        exp = rec.get("expectancy_r")
        exp_str = f"{exp:+.4f}" if isinstance(exp, (int, float)) else "-"
        print(
            f"{bot_id:<28} {rec.get('tier', '-'):<10} "
            f"{rec.get('mc_verdict', '-'):<10} {exp_str:<10} "
            f"{rec.get('applied_at', '-')}",
        )
    return 0


def reactivate(bot_ids: list[str]) -> int:
    data = _read()
    deact = data.get("deactivated", {})
    removed: list[str] = []
    not_found: list[str] = []
    for bot_id in bot_ids:
        if bot_id in deact:
            removed.append(bot_id)
            del deact[bot_id]
        else:
            not_found.append(bot_id)
    data["deactivated"] = deact
    _write(data)

    if removed:
        _audit("reactivate", removed)
        print(f"reactivated: {', '.join(removed)}")
        print("(takes effect on next supervisor restart)")
    if not_found:
        print(f"(not in sidecar — already active or never auto-deactivated): {', '.join(not_found)}")
    return 0 if removed else 1


def clear_all() -> int:
    data = _read()
    deact = data.get("deactivated", {})
    if not deact:
        print("(sidecar already empty)")
        return 0
    bot_ids = sorted(deact.keys())
    data["deactivated"] = {}
    _write(data)
    _audit("clear_all", bot_ids)
    print(f"cleared {len(bot_ids)} kaizen override(s): {', '.join(bot_ids)}")
    print("(takes effect on next supervisor restart)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("bot_ids", nargs="*", help="bot_id(s) to reactivate")
    p.add_argument("--list", action="store_true", help="show all currently auto-deactivated bots")
    p.add_argument("--clear-all", action="store_true", help="reactivate every kaizen-deactivated bot")
    args = p.parse_args(argv)

    if args.list:
        return list_overrides()
    if args.clear_all:
        return clear_all()
    if not args.bot_ids:
        p.print_help()
        return 1
    return reactivate(args.bot_ids)


if __name__ == "__main__":
    sys.exit(main())
