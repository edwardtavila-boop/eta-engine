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
import sys
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _called_events(root: pathlib.Path = ROOT) -> set[tuple[str, str]]:
    events: set[tuple[str, str]] = set()
    for p in root.rglob("*.py"):
        rel = str(p.relative_to(root)).replace("\\", "/")
        if rel.startswith("tests/") or "__pycache__" in rel:
            continue
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r'dispatcher\.send\(\s*["\']([a-z_][a-z0-9_]*)["\']', text):
            events.add((m.group(1), rel))
    return events


def _reserved_event_notes(routing: dict[str, Any]) -> dict[str, str]:
    reserved = routing.get("reserved_events", {})
    if isinstance(reserved, dict):
        return {
            str(key): str(value).strip()
            for key, value in reserved.items()
        }
    return {}


def audit(root: pathlib.Path = ROOT) -> dict[str, Any]:
    events = _called_events(root)
    cfg = yaml.safe_load((root / "configs" / "alerts.yaml").read_text(encoding="utf-8"))
    routing = cfg.get("routing", {})
    registered = set(routing.get("events", {}).keys())
    called = {e for e, _ in events}
    missing = called - registered
    unused = registered - called
    reserved_notes = _reserved_event_notes(routing)
    reserved = set(reserved_notes)
    reserved_unused = unused & reserved
    unreserved_unused = unused - reserved
    reserved_without_reason = {event for event, reason in reserved_notes.items() if not reason}

    return {
        "called": called,
        "events": events,
        "missing": missing,
        "registered": registered,
        "reserved": reserved,
        "reserved_notes": reserved_notes,
        "reserved_unused": reserved_unused,
        "reserved_without_reason": reserved_without_reason,
        "unreserved_unused": unreserved_unused,
        "unused": unused,
    }


def main() -> int:
    report = audit(ROOT)
    called = report["called"]
    events = report["events"]
    missing = report["missing"]
    registered = report["registered"]
    reserved_unused = report["reserved_unused"]
    reserved_without_reason = report["reserved_without_reason"]
    unreserved_unused = report["unreserved_unused"]

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
    print(f"RESERVED-BUT-UNUSED (intentional future/operator routes): {len(reserved_unused)}")
    for e in sorted(reserved_unused):
        print(f"  - {e}")
    print()
    print(f"UNRESERVED REGISTERED-BUT-UNUSED (dead config candidates): {len(unreserved_unused)}")
    for e in sorted(unreserved_unused):
        print(f"  - {e}")
    print()
    print(f"RESERVED WITHOUT REASON: {len(reserved_without_reason)}")
    for e in sorted(reserved_without_reason):
        print(f"  - {e}")
    print()
    if missing:
        print(
            f"FAIL -- {len(missing)} event(s) dispatched but not "
            f"registered in alerts.yaml; AlertDispatcher will silently "
            f"drop them.",
        )
        return 1
    elif unreserved_unused:
        print(
            f"FAIL -- {len(unreserved_unused)} registered event(s) are "
            "unused and not listed under routing.reserved_events.",
        )
        return 1
    elif reserved_without_reason:
        print(
            f"FAIL -- {len(reserved_without_reason)} reserved event(s) need "
            "a non-empty reservation reason.",
        )
        return 1
    else:
        print(
            "OK -- every dispatched event is registered in alerts.yaml, "
            "and every unused registered event is explicitly reserved.",
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
