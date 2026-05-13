"""Operator CLI: manage per-bot lifecycle state for wave-25 routing.

The wave-25 conditional-routing supervisor reads each bot's lifecycle
state and decides whether a signal goes to ``live``, ``paper``, or
``reject``. This CLI is the operator's interface to that state.

Default behaviour for any bot NOT in the lifecycle file is
``EVAL_PAPER`` (conservative). To opt a bot into live trading on the
prop-fund eval account, run::

    python -m eta_engine.scripts.manage_lifecycle set m2k_sweep_reclaim EVAL_LIVE

To list current state::

    python -m eta_engine.scripts.manage_lifecycle list

To clear a bot (revert to default EVAL_PAPER)::

    python -m eta_engine.scripts.manage_lifecycle clear m2k_sweep_reclaim

To retire a bot (refuse all signals)::

    python -m eta_engine.scripts.manage_lifecycle set foo_bot RETIRED

Pre-Monday recommended setup::

    # Most conservative: paper only
    python -m eta_engine.scripts.manage_lifecycle set m2k_sweep_reclaim EVAL_LIVE
    # Other PROP_READY bots stay in EVAL_PAPER (default)
"""
# ruff: noqa: T201
from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _print_table(state_path: Path) -> None:
    """Render the current lifecycle table."""
    from eta_engine.feeds.capital_allocator import (  # noqa: PLC0415
        DIAMOND_BOTS,
        LIFECYCLE_EVAL_PAPER,
        get_bot_lifecycle,
    )

    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            explicit = (data.get("bots") or {}).keys()
        except (OSError, json.JSONDecodeError):
            explicit = ()
    else:
        explicit = ()

    all_bots = sorted(set(DIAMOND_BOTS) | set(explicit))
    print(f"\nLifecycle state ({state_path})")
    print("=" * 70)
    print(f"  {'bot_id':<32} {'lifecycle':<14} {'explicit':<10}")
    print("  " + "-" * 60)
    for bot_id in all_bots:
        state = get_bot_lifecycle(bot_id)
        marker = "yes" if bot_id in explicit else "no (default)"
        flag = " *" if state != LIFECYCLE_EVAL_PAPER and bot_id in explicit else ""
        print(f"  {bot_id:<32} {state:<14} {marker}{flag}")
    print()
    print(f"  Default for unlisted bots: {LIFECYCLE_EVAL_PAPER}")
    print("  States that route LIVE: EVAL_LIVE, FUNDED_LIVE (* marked)")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="Show current lifecycle table")

    p_set = sub.add_parser("set", help="Set a bot's lifecycle state")
    p_set.add_argument("bot_id")
    p_set.add_argument(
        "state",
        choices=["EVAL_LIVE", "EVAL_PAPER", "FUNDED_LIVE", "RETIRED"],
    )

    p_clear = sub.add_parser("clear", help="Remove a bot's explicit entry (reverts to EVAL_PAPER)")
    p_clear.add_argument("bot_id")

    args = parser.parse_args(argv)

    from eta_engine.feeds import capital_allocator as ca  # noqa: PLC0415

    state_path: Path = ca.BOT_LIFECYCLE_STATE_PATH

    if args.cmd == "list":
        _print_table(state_path)
        return 0

    if args.cmd == "set":
        ca.set_bot_lifecycle(args.bot_id, args.state)
        print(f"set {args.bot_id} -> {args.state}")
        _print_table(state_path)
        return 0

    if args.cmd == "clear":
        if not state_path.exists():
            print(f"no lifecycle file at {state_path}; nothing to clear")
            return 0
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"could not parse {state_path}")
            return 1
        bots = data.get("bots") or {}
        if args.bot_id not in bots:
            print(f"{args.bot_id} not in lifecycle file; nothing to clear")
            return 0
        del bots[args.bot_id]
        data["bots"] = bots
        state_path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"cleared {args.bot_id} (reverted to default EVAL_PAPER)")
        _print_table(state_path)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
