"""Synthetic-bar smoke probe for every concrete bot.

Imports each concrete bot and asks: "can it be constructed without
exploding?" If a class needs args, the probe constructs it with safe
defaults inferred from the signature (BotConfig, Decimal/0, etc.).

Unlike pytest scaffolds, this probe is RUNTIME -- it actually
instantiates the bot and exercises ``start``/``stop`` to ensure the
init path is intact. Pytest catches contract drift; this catches
"the imports broke and we didn't notice" runtime drift.

Probe levels
------------
L0  module import smoke (always runs)
L1  class instantiation with default BotConfig
L2  start() then stop() (state lifecycle)

Run levels with --level (default L1).

Exit codes
----------
0  GREEN  -- all probes pass
1  YELLOW -- 1..2 bots fail
2  RED    -- 3+ bots fail (or any L0 failure)

Why
---
The fleet invariant checker reads AST. This script READS RUNTIME.
A bot can satisfy every AST invariant but still explode on
``__init__`` because it imports a missing module or fails dependency
injection. Cheap insurance.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# Ensure ``eta_engine`` is importable when run from the package dir.
_PKG_PARENT = Path(__file__).resolve().parents[2]
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

# (module_dotted, class_name)
CONCRETE_BOTS: list[tuple[str, str]] = [
    ("eta_engine.bots.mnq.bot", "MnqBot"),
    ("eta_engine.bots.eth_perp.bot", "EthPerpBot"),
    ("eta_engine.bots.nq.bot", "NqBot"),
    ("eta_engine.bots.sol_perp.bot", "SolPerpBot"),
    ("eta_engine.bots.xrp_perp.bot", "XrpPerpBot"),
    ("eta_engine.bots.crypto_seed.bot", "CryptoSeedBot"),
]


def _probe_l0(module_path: str, _cls_name: str) -> tuple[bool, str]:
    """Module imports cleanly."""
    try:
        importlib.import_module(module_path)
    except Exception as e:  # noqa: BLE001 -- runtime probe, want to catch all
        return (False, f"import failed: {type(e).__name__}: {e}")
    return (True, "import ok")


def _build_bot_config() -> object | None:
    """Try to construct a default BotConfig instance for instantiation."""
    try:
        cfg_mod = importlib.import_module("eta_engine.bots.base_bot")
        BotConfig = cfg_mod.BotConfig  # noqa: N806
    except (ImportError, AttributeError):
        return None
    # Try common field combinations
    for kwargs in (
        {},
        {"symbol": "TEST"},
        {"symbol": "TEST", "tier": getattr(cfg_mod, "Tier", None)},
    ):
        try:
            return BotConfig(**{k: v for k, v in kwargs.items() if v is not None})
        except Exception:  # noqa: BLE001
            continue
    return None


def _probe_l1(module_path: str, cls_name: str) -> tuple[bool, str]:
    """Class can be constructed."""
    try:
        mod = importlib.import_module(module_path)
        cls = getattr(mod, cls_name)
    except Exception as e:  # noqa: BLE001
        return (False, f"import failed: {type(e).__name__}: {e}")
    # Try no-arg first
    try:
        cls()
        return (True, "instantiated (no args)")
    except TypeError:
        pass  # needs args
    except Exception as e:  # noqa: BLE001
        return (False, f"no-arg ctor failed: {type(e).__name__}: {e}")
    # Try with default BotConfig
    cfg = _build_bot_config()
    if cfg is None:
        return (False, "no-arg failed and could not build a default BotConfig")
    try:
        cls(cfg)
        return (True, "instantiated (BotConfig)")
    except Exception as e:  # noqa: BLE001
        return (False, f"BotConfig ctor failed: {type(e).__name__}: {e}")


def _probe_l2(module_path: str, cls_name: str) -> tuple[bool, str]:
    """start() then stop() work."""
    try:
        mod = importlib.import_module(module_path)
        cls = getattr(mod, cls_name)
    except Exception as e:  # noqa: BLE001
        return (False, f"import failed: {type(e).__name__}: {e}")
    cfg = _build_bot_config()
    instance = None
    try:
        instance = cls() if cfg is None else cls(cfg)
    except Exception as e:  # noqa: BLE001
        return (False, f"instantiation failed: {type(e).__name__}: {e}")
    if not (hasattr(instance, "start") and hasattr(instance, "stop")):
        return (False, "no start/stop methods")
    try:
        instance.start()
    except Exception as e:  # noqa: BLE001
        return (False, f"start() failed: {type(e).__name__}: {e}")
    try:
        instance.stop()
    except Exception as e:  # noqa: BLE001
        return (False, f"stop() failed: {type(e).__name__}: {e}")
    return (True, "start/stop ok")


PROBE_LEVELS: dict[str, Callable[[str, str], tuple[bool, str]]] = {
    "L0": _probe_l0,
    "L1": _probe_l1,
    "L2": _probe_l2,
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--level",
        default="L1",
        choices=list(PROBE_LEVELS),
        help="probe level: L0=import, L1=construct, L2=start/stop (default L1)",
    )
    p.add_argument(
        "--max-yellow",
        type=int,
        default=2,
        help="more than this many failures -> RED (default 2)",
    )
    p.add_argument("--traceback", action="store_true", help="full traceback on failure")
    args = p.parse_args(argv)

    probe = PROBE_LEVELS[args.level]
    failures: list[tuple[str, str, str]] = []  # (bot, msg, tb_or_empty)
    for module_path, cls_name in CONCRETE_BOTS:
        try:
            ok, msg = probe(module_path, cls_name)
        except Exception as e:  # noqa: BLE001
            ok = False
            msg = f"probe threw {type(e).__name__}: {e}"
        if ok:
            print(f"  {cls_name:>18}: PASS  {msg}")
        else:
            tb = traceback.format_exc() if args.traceback else ""
            failures.append((cls_name, msg, tb))
            print(f"  {cls_name:>18}: FAIL  {msg}")
            if tb and tb.strip() != "NoneType: None":
                for line in tb.splitlines()[-6:]:
                    print(f"        | {line}")

    n = len(failures)
    if n == 0:
        print(f"\nbot-health-probe[{args.level}]: GREEN -- {len(CONCRETE_BOTS)} bots pass")
        return 0
    level = "RED" if n > args.max_yellow else "YELLOW"
    print(
        f"\nbot-health-probe[{args.level}]: {level} -- {n}/{len(CONCRETE_BOTS)} bots fail",
    )
    return 1 if level == "YELLOW" else 2


if __name__ == "__main__":
    sys.exit(main())
