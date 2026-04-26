"""
Worktree-root conftest.

The ``apex_predator`` package is editable-installed in the venv pointing to
``C:\\apex_predator`` (the canonical install). This worktree ships its own
copy of the package (different branch). Without intervention, tests run
inside this worktree would import the canonical install's code -- so any
changes in the worktree wouldn't be exercised.

This conftest rebinds ``sys.modules["apex_predator"]`` to the worktree's
package source so ``import apex_predator.<...>`` resolves to worktree files.
Effect is scoped to the test session only; it does NOT modify the venv or
the canonical install.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_WORKTREE_ROOT = Path(__file__).resolve().parent


def _rebind_apex_predator_to_worktree() -> None:
    # Drop cached apex_predator modules so subsequent imports re-resolve.
    for mod_name in list(sys.modules):
        if mod_name == "apex_predator" or mod_name.startswith("apex_predator."):
            del sys.modules[mod_name]

    # Build a fresh module object whose __path__ points at this worktree.
    init_file = _WORKTREE_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "apex_predator",
        init_file,
        submodule_search_locations=[str(_WORKTREE_ROOT)],
    )
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules["apex_predator"] = module
    spec.loader.exec_module(module)


_rebind_apex_predator_to_worktree()
