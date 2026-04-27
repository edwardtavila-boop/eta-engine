"""One-shot audit: walk dispatcher.send(EVENT, ...) call sites vs alerts.yaml.

Prints every event used in code, every event registered in
configs/alerts.yaml, and the diff (used-but-unregistered + the
opposite). Used-but-unregistered events get silently dropped by
AlertDispatcher (logged-only, no Pushover/email/SMS), which was the
v0.1.63 R1 Red-Team B2 finding.
"""

from __future__ import annotations

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> None:
    events: set[tuple[str, str]] = set()
    for p in ROOT.rglob("*.py"):
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        if rel.startswith("tests/") or "__pycache__" in rel:
            continue
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r'dispatcher\.send\(\s*["\']([a-z_][a-z0-9_]*)["\']', text):
            events.add((m.group(1), rel))

    cfg = yaml.safe_load((ROOT / "configs" / "alerts.yaml").read_text(encoding="utf-8"))
    registered = set(cfg.get("routing", {}).get("events", {}).keys())

    called = {e for e, _ in events}
    missing = called - registered
    unused = registered - called

    print(f"EVENTS USED IN CODE: {len(called)}")
    for e in sorted(called):
        marker = "  [REGISTERED]" if e in registered else "  [** MISSING **]"
        src_files = sorted({s for ev, s in events if ev == e})
        print(f"  {e}{marker}")
        for s in src_files[:3]:
            print(f"      <- {s}")
    print()
    print(f"MISSING (used but not registered): {len(missing)}")
    for e in sorted(missing):
        print(f"  - {e}")
    print()
    print(f"REGISTERED-BUT-UNUSED (dead config): {len(unused)}")
    for e in sorted(unused):
        print(f"  - {e}")
    print()
    if missing:
        print(
            f"FAIL -- {len(missing)} event(s) dispatched but not "
            f"registered in alerts.yaml; AlertDispatcher will silently "
            f"drop them.",
        )
    else:
        print(
            "OK -- every dispatched event is registered in alerts.yaml.",
        )


if __name__ == "__main__":
    main()
