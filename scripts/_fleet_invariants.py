"""Cross-bot invariant checker -- catches "added to MnqBot but forgot the others" drift.

The Firm runs a fleet of concrete bots: MnqBot, EthPerpBot, NqBot, SolPerpBot,
XrpPerpBot, CryptoSeedBot. They share an inheritance graph rooted at BaseBot,
plus sibling chains (NqBot extends MnqBot; SolPerpBot/XrpPerpBot extend
EthPerpBot). When the operator wires a new feature into one bot (retrospective
manager, AI strategy auto-wiring, new init attribute), the others silently
fall behind. Pre-commit ruff doesn't catch it. Pytest doesn't catch it
(each bot has its own tests). The fleet drifts.

This script parses every concrete bot's AST and applies a registry of
invariants. Each invariant returns ``None`` if satisfied or a short
violation string. The output ranks violations by class and prints the
expected-vs-actual diff.

Invariants (current set)
------------------------
I1  must inherit BaseBot (transitively)
I2  must define start/stop (own or inherited)
I3  must have an active_entries property/attribute
I4  must call super().__init__() in __init__
I5  if any root bot has retrospective wiring, all root bots should
I6  paired init attrs: _router and _strategy_adapter must coexist
I7  symbol attr naming convention: must define one of
    {_tradovate_symbol, _venue_symbol}

Exit codes
----------
0  GREEN  -- all invariants satisfied
1  YELLOW -- 1..--max-yellow violations (default 3)
2  RED    -- > --max-yellow violations

Usage
-----
    python scripts/_fleet_invariants.py
    python scripts/_fleet_invariants.py --verbose
    python scripts/_fleet_invariants.py --max-yellow 5

Why this exists
---------------
The bots/*.py modifications in the working tree right now (6 files at
once) are exactly the kind of parallel edit that drifts. Adding a new
invariant to this script costs ~5 lines and prevents an entire class
of "shipped MnqBot fix, forgot to backport to NqBot" bugs.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOTS_DIR = ROOT / "bots"

# Concrete bot classes the fleet runs
CONCRETE_BOTS: dict[str, str] = {
    "MnqBot": "bots/mnq/bot.py",
    "EthPerpBot": "bots/eth_perp/bot.py",
    "NqBot": "bots/nq/bot.py",
    "SolPerpBot": "bots/sol_perp/bot.py",
    "XrpPerpBot": "bots/xrp_perp/bot.py",
    "CryptoSeedBot": "bots/crypto_seed/bot.py",
}

# Bots that inherit BaseBot directly (root of an inheritance chain)
ROOT_BOTS = {"MnqBot", "EthPerpBot", "CryptoSeedBot"}


@dataclass
class BotInfo:
    name: str
    path: Path
    bases: list[str]
    methods: set[str] = field(default_factory=set)
    init_attrs: set[str] = field(default_factory=set)
    init_calls_super: bool = False
    properties: set[str] = field(default_factory=set)
    has_class: bool = False


def _scan_bot(name: str, path: Path) -> BotInfo:
    info = BotInfo(name=name, path=path, bases=[])
    if not path.exists():
        return info
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return info
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != name:
            continue
        info.has_class = True
        info.bases = [getattr(b, "id", getattr(b, "attr", "?")) for b in node.bases]
        for member in node.body:
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info.methods.add(member.name)
                # property?
                for dec in member.decorator_list:
                    name_str = getattr(dec, "id", getattr(dec, "attr", ""))
                    if name_str == "property":
                        info.properties.add(member.name)
                # __init__: scan for super().__init__() and self.X = ...
                if member.name == "__init__":
                    for sub in ast.walk(member):
                        if (
                            isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "__init__"
                            and isinstance(sub.func.value, ast.Call)
                            and isinstance(sub.func.value.func, ast.Name)
                            and sub.func.value.func.id == "super"
                        ):
                            info.init_calls_super = True
                        if isinstance(sub, ast.Assign):
                            for t in sub.targets:
                                if (
                                    isinstance(t, ast.Attribute)
                                    and isinstance(t.value, ast.Name)
                                    and t.value.id == "self"
                                ):
                                    info.init_attrs.add(t.attr)
    return info


def _has_method_inherited(
    bot: BotInfo,
    fleet: dict[str, BotInfo],
    method: str,
) -> bool:
    """Walk parent chain checking for the method."""
    if method in bot.methods:
        return True
    for parent in bot.bases:
        if parent in fleet and _has_method_inherited(fleet[parent], fleet, method):
            return True
    # Assume BaseBot supplies it if we couldn't find it (we don't parse base_bot here)
    return parent == "BaseBot" if bot.bases else False


def _has_attr_inherited(bot: BotInfo, fleet: dict[str, BotInfo], attr: str) -> bool:
    if attr in bot.init_attrs:
        return True
    return any(parent in fleet and _has_attr_inherited(fleet[parent], fleet, attr) for parent in bot.bases)


# ---- Invariants ----
# Each invariant: (id, label, callable). Callable returns violation string or None.
# Receives (bot, fleet).

InvariantFn = "Callable[[BotInfo, dict[str, BotInfo]], str | None]"


def i1_inherits_basebot(bot: BotInfo, fleet: dict[str, BotInfo]) -> str | None:
    if not bot.has_class:
        return f"{bot.name}: class not found in {bot.path}"
    # Walk up to confirm BaseBot somewhere in the chain
    seen = {bot.name}
    queue = list(bot.bases)
    while queue:
        b = queue.pop()
        if b == "BaseBot":
            return None
        if b in seen:
            continue
        seen.add(b)
        if b in fleet:
            queue.extend(fleet[b].bases)
    return f"{bot.name}: does not inherit BaseBot (bases={bot.bases})"


def i2_has_start_stop(bot: BotInfo, fleet: dict[str, BotInfo]) -> str | None:
    missing = [m for m in ("start", "stop") if not _has_method_inherited(bot, fleet, m)]
    if missing:
        return f"{bot.name}: missing required method(s) {missing}"
    return None


def i3_has_active_entries(bot: BotInfo, fleet: dict[str, BotInfo]) -> str | None:
    if _has_method_inherited(bot, fleet, "active_entries"):
        return None
    if _has_attr_inherited(bot, fleet, "_active_entries"):
        return None
    return f"{bot.name}: no active_entries property/method (and no _active_entries init attr)"


def i4_init_calls_super(bot: BotInfo, fleet: dict[str, BotInfo]) -> str | None:  # noqa: ARG001
    # Only enforce when the bot defines its own __init__
    if "__init__" not in bot.methods:
        return None
    if not bot.init_calls_super:
        return f"{bot.name}: defines __init__ but does not call super().__init__()"
    return None


RETROSPECTIVE_ATTRS = {
    "_auto_wire_retrospective",
    "_default_retrospective_strategy",
    "_retrospective_manager",
}


def i5_retrospective_parity(bot: BotInfo, fleet: dict[str, BotInfo]) -> str | None:
    """If ANY root bot has retrospective wiring, all root bots should."""
    if bot.name not in ROOT_BOTS:
        return None
    any_root_has_it = any(RETROSPECTIVE_ATTRS.intersection(fleet[r].init_attrs) for r in ROOT_BOTS if r in fleet)
    if not any_root_has_it:
        return None
    missing = RETROSPECTIVE_ATTRS - bot.init_attrs
    if missing == RETROSPECTIVE_ATTRS:
        return f"{bot.name}: ROOT bot missing retrospective wiring (siblings have it, this one has 0/3 attrs)"
    if missing:
        return f"{bot.name}: ROOT bot has partial retrospective wiring (missing {sorted(missing)})"
    return None


def i6_router_strategy_paired(bot: BotInfo, fleet: dict[str, BotInfo]) -> str | None:  # noqa: ARG001
    if bot.name not in ROOT_BOTS:
        return None
    has_router = "_router" in bot.init_attrs
    has_strategy = "_strategy_adapter" in bot.init_attrs
    if has_router and not has_strategy:
        return f"{bot.name}: has _router but not _strategy_adapter"
    if has_strategy and not has_router:
        return f"{bot.name}: has _strategy_adapter but not _router"
    return None


SYMBOL_ATTR_CANDIDATES = {"_tradovate_symbol", "_venue_symbol", "_symbol"}


def i7_symbol_naming(bot: BotInfo, fleet: dict[str, BotInfo]) -> str | None:  # noqa: ARG001
    if bot.name not in ROOT_BOTS:
        return None
    overlap = SYMBOL_ATTR_CANDIDATES & bot.init_attrs
    if not overlap:
        return f"{bot.name}: no symbol attr (expected one of {sorted(SYMBOL_ATTR_CANDIDATES)})"
    return None


INVARIANTS: list[tuple[str, str, InvariantFn]] = [
    ("I1", "inherits BaseBot", i1_inherits_basebot),
    ("I2", "has start/stop", i2_has_start_stop),
    ("I3", "has active_entries", i3_has_active_entries),
    ("I4", "init calls super", i4_init_calls_super),
    ("I5", "retrospective wiring parity (root)", i5_retrospective_parity),
    ("I6", "router/strategy paired (root)", i6_router_strategy_paired),
    ("I7", "symbol attr present (root)", i7_symbol_naming),
]


def _build_fleet() -> dict[str, BotInfo]:
    fleet: dict[str, BotInfo] = {}
    for name, rel in CONCRETE_BOTS.items():
        fleet[name] = _scan_bot(name, ROOT / rel)
    return fleet


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--verbose", "-v", action="store_true", help="dump per-bot AST summary")
    p.add_argument(
        "--max-yellow",
        type=int,
        default=3,
        help="more than this many violations -> RED (default 3)",
    )
    args = p.parse_args(argv)

    fleet = _build_fleet()

    if args.verbose:
        print("--- Fleet AST summary ---")
        for name in CONCRETE_BOTS:
            b = fleet[name]
            print(f"\n  {name} ({b.path.relative_to(ROOT)})")
            print(f"    bases: {b.bases}")
            print(f"    methods ({len(b.methods)}): {sorted(b.methods)[:8]}{'...' if len(b.methods) > 8 else ''}")
            print(f"    init attrs: {sorted(b.init_attrs)}")
            print(f"    init calls super: {b.init_calls_super}")
        print()

    violations: list[tuple[str, str, str]] = []  # (bot, invariant_id, message)
    for inv_id, label, fn in INVARIANTS:
        for name in CONCRETE_BOTS:
            try:
                msg = fn(fleet[name], fleet)
            except Exception as e:  # noqa: BLE001 -- invariants can throw on malformed AST
                msg = f"{name}: invariant {inv_id} threw {type(e).__name__}: {e}"
            if msg:
                violations.append((name, f"{inv_id} ({label})", msg))

    n = len(violations)
    if n == 0:
        print(f"fleet-invariants: GREEN -- 0 violations across {len(CONCRETE_BOTS)} bots, {len(INVARIANTS)} invariants")
        return 0
    level = "RED" if n > args.max_yellow else "YELLOW"
    print(
        f"fleet-invariants: {level} -- {n} violation(s) across {len(CONCRETE_BOTS)} bots, {len(INVARIANTS)} invariants",
    )
    print()
    # Group by bot
    by_bot: dict[str, list[tuple[str, str]]] = {}
    for bot_name, inv_label, msg in violations:
        by_bot.setdefault(bot_name, []).append((inv_label, msg))
    for bot_name in CONCRETE_BOTS:
        if bot_name not in by_bot:
            continue
        print(f"  {bot_name}:")
        for inv_label, msg in by_bot[bot_name]:
            print(f"    - [{inv_label}] {msg}")
    print()
    print(
        "Fix: see scripts/_fleet_invariants.py INVARIANTS list for full set. "
        "Add new invariants to catch your bug class.",
    )
    return 1 if level == "YELLOW" else 2


if __name__ == "__main__":
    sys.exit(main())
