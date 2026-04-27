"""Compare installed packages vs pyproject.toml / uv.lock.

Catches three drift classes:
* installed but not pinned   (snuck in via transitive or pip install)
* pinned but not installed   (lock file changed, env not refreshed)
* version mismatch           (pinned X, have Y)

Usage
-----
    python scripts/_dependency_drift.py
    python scripts/_dependency_drift.py --json
    python scripts/_dependency_drift.py --strict  # mismatches -> RED instead of YELLOW

Inputs
------
* `python -m pip list --format=freeze` for installed packages
* pyproject.toml / requirements.txt for declared deps (best-effort parse)
* uv.lock if present for pinned versions

Exit codes
----------
0  GREEN  -- no drift
1  YELLOW -- drift below thresholds
2  RED    -- significant drift (>--max-yellow drifts, or --strict mismatch)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Packages we don't care about pinning (build/test infrastructure)
IGNORED_PKGS = {
    "pip",
    "setuptools",
    "wheel",
    "uv",
    "build",
    "ruff",
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "pytest-mock",
    "coverage",
    "mypy",
    "black",
    "isort",
}


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        return (proc.returncode, proc.stdout + proc.stderr)
    except (subprocess.TimeoutExpired, OSError):
        return (124, "")


def _installed() -> dict[str, str]:
    rc, out = _run([sys.executable, "-m", "pip", "list", "--format=freeze"])
    pkgs: dict[str, str] = {}
    if rc != 0:
        return pkgs
    for line in out.splitlines():
        if "==" not in line:
            continue
        name, _, ver = line.strip().partition("==")
        name = name.lower().replace("_", "-")
        if name in IGNORED_PKGS:
            continue
        pkgs[name] = ver
    return pkgs


def _declared_pyproject() -> dict[str, str | None]:
    """Best-effort: pull dependencies from pyproject.toml [project] or [tool.uv]."""
    pp = ROOT / "pyproject.toml"
    if not pp.exists():
        return {}
    try:
        text = pp.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str | None] = {}
    # naive line scan -- handles common shapes:
    #   "name>=1.2.3"
    #   "name == 1.2.3"
    #   "name"
    in_deps_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("dependencies", "[project.optional", "dev = ")):
            in_deps_block = True
            continue
        if stripped.startswith("[") and "depend" not in stripped:
            in_deps_block = False
        if not in_deps_block:
            continue
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(
            r'["\']([A-Za-z0-9_.\-]+)\s*([><=~!]+)?\s*([0-9A-Za-z.\-+]+)?["\']',
            stripped,
        )
        if m:
            name = m.group(1).lower().replace("_", "-")
            ver = m.group(3)
            if name not in IGNORED_PKGS:
                out[name] = ver
    return out


def _declared_lock() -> dict[str, str]:
    """Pull pinned versions from uv.lock (best-effort)."""
    lock = ROOT / "uv.lock"
    if not lock.exists():
        return {}
    try:
        text = lock.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    cur_name = None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'name\s*=\s*"([^"]+)"', s)
        if m:
            cur_name = m.group(1).lower().replace("_", "-")
            continue
        m = re.match(r'version\s*=\s*"([^"]+)"', s)
        if m and cur_name and cur_name not in IGNORED_PKGS:
            out[cur_name] = m.group(1)
            cur_name = None
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--strict", action="store_true", help="version mismatches -> RED (default: YELLOW)")
    p.add_argument("--max-yellow", type=int, default=10, help="more than this many drifts -> RED (default 10)")
    args = p.parse_args(argv)

    inst = _installed()
    decl = _declared_pyproject()
    lock = _declared_lock()
    pinned = lock if lock else {k: v for k, v in decl.items() if v}

    drifts: list[str] = []

    # Class A: installed but not declared
    extra = sorted(set(inst) - set(decl) - set(pinned))
    if extra:
        drifts.append(
            f"INSTALLED but not declared: {len(extra)} package(s)",
        )
        for name in extra[:10]:
            drifts.append(f"  +{name}=={inst[name]}")
        if len(extra) > 10:
            drifts.append(f"  ... and {len(extra) - 10} more")

    # Class B: declared/pinned but not installed
    declared_keys = set(decl) | set(pinned)
    missing = sorted(declared_keys - set(inst))
    if missing:
        drifts.append(
            f"DECLARED but not installed: {len(missing)} package(s)",
        )
        for name in missing[:10]:
            ver = pinned.get(name, decl.get(name) or "(unpinned)")
            drifts.append(f"  -{name}=={ver}")
        if len(missing) > 10:
            drifts.append(f"  ... and {len(missing) - 10} more")

    # Class C: version mismatch (only checks lock if present)
    mismatches: list[str] = []
    for name, want in pinned.items():
        have = inst.get(name)
        if have and want and have != want:
            mismatches.append(f"  ~{name}: pinned={want} installed={have}")
    if mismatches:
        drifts.append(f"VERSION mismatch: {len(mismatches)} package(s)")
        drifts.extend(mismatches[:10])
        if len(mismatches) > 10:
            drifts.append(f"  ... and {len(mismatches) - 10} more")

    n = sum(1 for d in drifts if not d.startswith("  "))
    if n == 0:
        print(
            f"dependency-drift: GREEN -- {len(inst)} installed, {len(decl)} declared, {len(pinned)} pinned",
        )
        return 0
    has_mismatch = bool(mismatches)
    n_pkgs = len(extra) + len(missing) + len(mismatches)
    level = "RED" if (args.strict and has_mismatch) or n_pkgs > args.max_yellow else "YELLOW"
    print(
        f"dependency-drift: {level} -- {n_pkgs} package drift(s) across {n} class(es)",
    )
    for line in drifts:
        print(line)
    return 1 if level == "YELLOW" else 2


if __name__ == "__main__":
    sys.exit(main())
