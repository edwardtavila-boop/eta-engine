"""
Anomaly snapshot right now — one-shot terminal command.

Operator runs this from any shell that has eta_engine on the path:

    python -m eta_engine.scripts.anomaly_now
    python -m eta_engine.scripts.anomaly_now --scan         # also run a fresh scan
    python -m eta_engine.scripts.anomaly_now --since 72     # last 72 hours
    python -m eta_engine.scripts.anomaly_now --json         # machine-readable

By default this is read-only — it tails the watcher's existing hit log
without firing the detectors. ``--scan`` runs the detectors first and
appends any NEW hits to the log before rendering.

Designed for the "I'm at my desk, anything bleeding?" use case.

Exit code:
    0 — no recent anomalies in the window
    2 — at least one anomaly present (useful for shell scripting)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any

from eta_engine.brain.jarvis_v3 import anomaly_watcher

_SEV_TAG = {
    "critical": "[CRIT]",
    "warn": "[WARN]",
    "info": "[INFO]",
}


def _sev_tag(sev: str | None) -> str:
    return _SEV_TAG.get(str(sev or "").lower(), "[----]")


def render(since_hours: int, ran_scan: bool, n_new: int, hits: list[dict[str, Any]]) -> str:
    """Build an operator-friendly text summary (ASCII-only for Windows cp1252)."""
    bar = "=" * 56
    dash = "-"

    lines = [
        "",
        bar,
        "  Anomalies  -  " + datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        bar,
        "",
    ]

    if ran_scan:
        lines.append(f"  Scan : {n_new} NEW (post-dedup) hits this run")
    lines.append(f"  Log  : showing all hits in last {since_hours}h window")
    lines.append("")

    if not hits:
        lines.append("  (clean - no anomalies in window)")
        lines.append("")
        lines.append(bar)
        lines.append("")
        return "\n".join(lines)

    # Group hits by bot for readability
    by_bot: dict[str, list[dict[str, Any]]] = {}
    for h in hits:
        bot = str(h.get("bot_id") or "unknown")
        by_bot.setdefault(bot, []).append(h)

    lines.append(f"  Bots with anomalies: {len(by_bot)}    Total hits: {len(hits)}")
    lines.append("")
    lines.append("--- Active patterns " + dash * 32)
    lines.append("")
    lines.append(f"  {'WHEN':<19}  {'SEV':<6}  {'PATTERN':<14}  {'BOT':<20}  DETAIL")
    lines.append("  " + dash * 92)

    # Sort newest first
    sorted_hits = sorted(hits, key=lambda h: str(h.get("asof") or ""), reverse=True)
    for h in sorted_hits[:20]:
        ts = str(h.get("asof") or "")[:19]
        sev = _sev_tag(h.get("severity"))
        pat = str(h.get("pattern") or "")[:14]
        bot = str(h.get("bot_id") or "")[:20]
        detail = str(h.get("detail") or "")[:50]
        lines.append(f"  {ts:<19}  {sev:<6}  {pat:<14}  {bot:<20}  {detail}")
    lines.append("")

    # Suggested skills
    suggested: dict[str, int] = {}
    for h in hits:
        s = str(h.get("suggested_skill") or "")
        if s:
            suggested[s] = suggested.get(s, 0) + 1
    if suggested:
        lines.append("--- Suggested skills " + dash * 32)
        lines.append("")
        for skill, n in sorted(suggested.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {skill:<38}  x{n}")
        lines.append("")

    lines.append(bar)
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot anomaly snapshot for the operator. No LLM, no SSH.",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Run a fresh detector pass before rendering (writes new hits to log)",
    )
    parser.add_argument(
        "--since",
        type=int,
        default=24,
        help="Hours back to read from the hit log (default 24)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human text",
    )
    args = parser.parse_args(argv)

    n_new = 0
    if args.scan:
        try:
            new = anomaly_watcher.scan()
            n_new = len(new)
        except Exception as exc:  # noqa: BLE001
            print(f"scan failed: {exc}", file=sys.stderr)

    hits = anomaly_watcher.recent_hits(since_hours=args.since)

    if args.json:
        payload = {
            "asof": datetime.now(UTC).isoformat(),
            "ran_scan": args.scan,
            "n_new": n_new,
            "since_hours": args.since,
            "n_total": len(hits),
            "hits": hits,
        }
        print(json.dumps(payload, default=str, indent=2))
    else:
        print(render(since_hours=args.since, ran_scan=args.scan, n_new=n_new, hits=hits))

    return 2 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
