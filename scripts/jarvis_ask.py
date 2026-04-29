"""JARVIS NL query CLI (Tier-2 #10 wiring, 2026-04-27).

Wraps ``brain/jarvis_v3/nl_query.py`` so the operator can ask questions
about the audit log from the command line:

    python scripts/jarvis_ask.py why <request_id>
    python scripts/jarvis_ask.py count denied --hours 24
    python scripts/jarvis_ask.py list approved --hours 6
    python scripts/jarvis_ask.py reasons --hours 24
    python scripts/jarvis_ask.py subsystem bot.mnq --hours 24
    python scripts/jarvis_ask.py last-binding --hours 6

Each subcommand calls the corresponding ``nl_query.*`` function. Output
is JSON (default) or human-readable text (--text).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

logger = logging.getLogger("jarvis_ask")


def _print_result(result: object, *, text: bool) -> None:
    if text:
        print()
        print(f"  query: {getattr(result, 'query', '?')}")
        print(f"  answer: {getattr(result, 'answer', '?')}")
        evidence = getattr(result, "evidence", None) or []
        if evidence:
            print(f"  evidence ({len(evidence)} items):")
            for e in evidence[:10]:
                print(f"    - {e}")
        print()
    else:
        if hasattr(result, "model_dump_json"):
            print(result.model_dump_json(indent=2))
        else:
            print(json.dumps(result, default=str, indent=2))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--audit-glob", default=str(ROOT / "state" / "jarvis_audit" / "*.jsonl"))
    p.add_argument("--text", action="store_true", help="Human-readable output (default: JSON)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_why = sub.add_parser("why", help="why_verdict(request_id)")
    p_why.add_argument("request_id")

    p_count = sub.add_parser("count", help="count_verdict(verdict, hours=24)")
    p_count.add_argument("verdict")
    p_count.add_argument("--hours", type=float, default=24.0)

    p_list = sub.add_parser("list", help="list_verdict(verdict, hours=6, limit=10)")
    p_list.add_argument("verdict")
    p_list.add_argument("--hours", type=float, default=6.0)
    p_list.add_argument("--limit", type=int, default=10)

    p_reasons = sub.add_parser("reasons", help="reason_freq(hours=24)")
    p_reasons.add_argument("--hours", type=float, default=24.0)

    p_subsys = sub.add_parser("subsystem", help="subsystem_stats(subsystem, hours=24)")
    p_subsys.add_argument("subsystem")
    p_subsys.add_argument("--hours", type=float, default=24.0)

    p_lb = sub.add_parser("last-binding", help="last_binding(hours=6)")
    p_lb.add_argument("--hours", type=float, default=6.0)

    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Resolve audit path -- support glob -> first match
    glob = Path(args.audit_glob)
    if glob.is_file():
        audit_path = glob
    else:
        matches = list(glob.parent.glob(glob.name))
        if not matches:
            print(f"  no audit files at {args.audit_glob}", file=sys.stderr)
            return 1
        audit_path = matches[0]

    from eta_engine.brain.jarvis_v3 import nl_query

    cmd_to_fn = {
        "why":          lambda: nl_query.why_verdict(audit_path, args.request_id),
        "count":        lambda: nl_query.count_verdict(audit_path, args.verdict, hours=args.hours),
        "list":         lambda: nl_query.list_verdict(audit_path, args.verdict,
                                                      hours=args.hours, limit=args.limit),
        "reasons":      lambda: nl_query.reason_freq(audit_path, hours=args.hours),
        "subsystem":    lambda: nl_query.subsystem_stats(audit_path, args.subsystem, hours=args.hours),
        "last-binding": lambda: nl_query.last_binding(audit_path, hours=args.hours),
    }
    fn = cmd_to_fn.get(args.cmd)
    if fn is None:
        print(f"  unknown command: {args.cmd}", file=sys.stderr)
        return 1

    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001
        print(f"  query failed: {exc}", file=sys.stderr)
        return 1

    _print_result(result, text=args.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
