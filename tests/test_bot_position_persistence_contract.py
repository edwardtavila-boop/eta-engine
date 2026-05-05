from __future__ import annotations

import ast
from pathlib import Path

ETA_ROOT = Path(__file__).resolve().parents[1]

BOT_STOP_CONTRACTS = {
    "bots/mnq/bot.py": {"MnqBot": "persist"},
    "bots/nq/bot.py": {"NqBot": "delegate"},
    "bots/eth_perp/bot.py": {"EthPerpBot": "persist"},
    "bots/sol_perp/bot.py": {"SolPerpBot": "delegate"},
    "bots/xrp_perp/bot.py": {"XrpPerpBot": "delegate"},
    "bots/crypto_seed/bot.py": {"CryptoSeedBot": "persist"},
    "bots/btc_hybrid/bot.py": {"BtcHybridBot": "persist"},
}


def _calls_self_persist_positions(fn: ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if (
            isinstance(callee, ast.Attribute)
            and callee.attr == "persist_positions"
            and isinstance(callee.value, ast.Name)
            and callee.value.id == "self"
        ):
            return True
    return False


def _delegates_to_super_stop(fn: ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if not (isinstance(callee, ast.Attribute) and callee.attr == "stop"):
            continue
        inner = callee.value
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == "super":
            return True
    return False


def _stop_method(module: ast.Module, class_name: str) -> ast.AsyncFunctionDef:
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and item.name == "stop":
                    return item
    raise AssertionError(f"{class_name}.stop not found")


def test_bot_stop_hooks_persist_or_delegate_positions() -> None:
    """Every concrete bot family snapshots open positions on graceful stop.

    Direct bot families must call ``self.persist_positions()``. Thin
    subclasses may delegate to a parent stop that already persists.
    """
    for rel_path, expected_by_class in BOT_STOP_CONTRACTS.items():
        module = ast.parse((ETA_ROOT / rel_path).read_text(encoding="utf-8"))
        for class_name, mode in expected_by_class.items():
            stop = _stop_method(module, class_name)
            if mode == "persist":
                assert _calls_self_persist_positions(stop), (
                    f"{class_name}.stop must call self.persist_positions() "
                    "before cleanup so broker reconciliation has shutdown state"
                )
            else:
                assert _delegates_to_super_stop(stop), (
                    f"{class_name}.stop must delegate to super().stop() so "
                    "the parent position-persistence hook runs"
                )
