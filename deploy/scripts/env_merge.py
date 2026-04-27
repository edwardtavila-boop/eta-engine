"""
Deploy // env_merge
===================
Merge a small KEY=VALUE file (e.g. .env.anthropic) into an existing .env.
Replaces lines with matching keys; appends new keys that aren't already
present. Preserves comments + blank lines.

Usage:
    python -m deploy.scripts.env_merge --target .env --source .env.anthropic
    python -m deploy.scripts.env_merge --target .env --source .env.anthropic --delete-source
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True, help="existing .env to modify")
    ap.add_argument("--source", required=True, help="KEY=VALUE file to merge in")
    ap.add_argument("--delete-source", action="store_true", help="rm the source file after merge (for temp files)")
    args = ap.parse_args(argv)

    target = Path(args.target)
    source = Path(args.source)

    if not target.exists():
        print(f"ERROR: target {target} not found", file=sys.stderr)
        return 2
    if not source.exists():
        print(f"ERROR: source {source} not found", file=sys.stderr)
        return 2

    # Parse updates from source
    updates: dict[str, str] = {}
    for raw in source.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            updates[k.strip()] = v.strip()

    print(f"[env_merge] {len(updates)} key(s) to merge: {list(updates.keys())}")

    # Walk target lines, replace matching keys
    out: list[str] = []
    seen: set[str] = set()
    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates and key not in seen:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)

    # Append any keys that weren't in the target
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
            seen.add(k)

    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"[env_merge] wrote {len(seen)} key(s) into {target}")

    if args.delete_source:
        source.unlink()
        print(f"[env_merge] deleted {source}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
