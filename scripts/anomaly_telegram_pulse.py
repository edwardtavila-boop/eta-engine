"""
Anomaly Telegram pulse — cron entrypoint (every 15 min).

This is the operator's promised replacement for the noisy
``Watchdog auto-healed: <services>`` spam. Instead of pinging on every
routine restart, the cron fires this module which:

  1. Runs ``anomaly_watcher.scan()`` — returns ONLY new post-dedup hits
  2. If nothing new, exits silently (zero Telegram traffic on calm runs)
  3. If something new, formats a compact message and sends ONE Telegram

This means the operator's phone only buzzes when something genuinely
material happened (3+ loss streak, 5-of-8 loss rate, etc.) and the
watcher itself dedupes the same anomaly for ``DEDUP_HOURS=4`` so the
"a bot is bleeding" alert fires once, not 16 times.

Run manually for smoke test:
    python -m eta_engine.scripts.anomaly_telegram_pulse --dry-run
    python -m eta_engine.scripts.anomaly_telegram_pulse           # real send

Cron schedule:
    eta_engine/deploy/anomaly_pulse_task.xml (Windows Task Scheduler)

Never raises — cron exit code is always 0 on a successful pass. Failures
are logged but don't break the scheduled task chain.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.scripts.anomaly_telegram_pulse")

_WORKSPACE = Path(r"C:\EvolutionaryTradingAlgo")
_PULSE_LOG = _WORKSPACE / "var" / "anomaly_pulse.jsonl"

_SEV_EMOJI = {
    "critical": "\U0001f6a8",  # 🚨 — fleet drawdown, loss streak ≥5
    "warn": "⚠️",  # ⚠️ — loss streak, loss rate, stale bot, suspicious win
    "info": "\U0001f389",  # 🎉 — win streak, fleet hot day (celebrate!)
}

# Positive patterns get the 🎉 emoji and "celebration" header treatment.
# Negative patterns get the standard "anomaly pulse" header.
_POSITIVE_PATTERNS = frozenset({"win_streak", "fleet_hot_day"})

# Telegram MarkdownV1 special chars that would crash the parser if
# left raw in free-form bot ids or detail strings (e.g. ``mnq_floor``
# has underscores that Telegram interprets as italic delimiters).
# We escape only the chars that matter for V1, not V2 (which is
# stricter and would require escaping `.`, `!`, etc.).
_MD_SPECIAL = ("_", "*", "[", "]", "`")


def _md_escape(text: str) -> str:
    """Escape Telegram MarkdownV1 specials so free-form text won't crash parser."""
    out = text
    for ch in _MD_SPECIAL:
        out = out.replace(ch, "\\" + ch)
    return out


def _sev_priority(hits: list[dict[str, Any]]) -> str:
    """Map fleet-wide severity to Telegram priority tag."""
    sevs = {str(h.get("severity") or "info").lower() for h in hits}
    if "critical" in sevs:
        return "CRITICAL"
    if "warn" in sevs:
        return "WARN"
    return "INFO"


def _format_hit_line(hit: dict[str, Any]) -> str:
    """One-liner per anomaly hit. Escapes Markdown specials in all free-form fields."""
    sev = str(hit.get("severity") or "info").lower()
    emoji = _SEV_EMOJI.get(sev, "")
    pattern = _md_escape(str(hit.get("pattern") or "anomaly"))
    bot = str(hit.get("bot_id") or "?").replace("`", "")  # backticks delimit the code span
    detail = _md_escape(str(hit.get("detail") or ""))
    # Fleet-level events use a sentinel bot_id "__fleet__"; render as "fleet"
    # without backticks so it doesn't look like a code identifier.
    if bot == "__fleet__":
        return f"{emoji} *{pattern}* — {detail}"
    return f"{emoji} *{pattern}* `{bot}` — {detail}"


def _format_message(hits: list[dict[str, Any]]) -> str:
    """Build a single Telegram message for one or more hits.

    Header adapts to the mix of positive vs negative events:
      * All positive  → "Fleet celebration — 1 new event"
      * All negative  → "Anomaly pulse — 3 new hits"
      * Mixed         → "Fleet pulse — 1 issue, 2 wins"
    """
    n = len(hits)
    patterns_in_play = {str(h.get("pattern") or "") for h in hits}
    n_pos = sum(1 for h in hits if str(h.get("pattern") or "") in _POSITIVE_PATTERNS)
    n_neg = n - n_pos

    if n == 0:
        header = "*Fleet pulse* — no events"
    elif n_pos == n:
        # All positive — celebrate
        header = f"\U0001f389 *Fleet celebration* — {n} new event" + ("s" if n != 1 else "")
    elif n_neg == n:
        # All negative — keep legacy "Anomaly pulse" wording
        header = f"*Anomaly pulse* — {n} new hit" + ("s" if n != 1 else "")
    else:
        # Mixed
        header = (
            f"*Fleet pulse* — {n_neg} issue"
            + ("s" if n_neg != 1 else "")
            + f", {n_pos} win"
            + ("s" if n_pos != 1 else "")
        )

    del patterns_in_play  # not currently used; reserved for future per-pattern grouping
    lines = [header, ""]

    # Sort by severity (critical first), then by pattern
    sev_order = {"critical": 0, "warn": 1, "info": 2}
    sorted_hits = sorted(
        hits,
        key=lambda h: (
            sev_order.get(str(h.get("severity") or "info").lower(), 9),
            str(h.get("pattern") or ""),
            str(h.get("bot_id") or ""),
        ),
    )

    # Cap at 10 hits per message — anything more, summarize
    for hit in sorted_hits[:10]:
        lines.append(_format_hit_line(hit))

    if n > 10:
        lines.append("")
        lines.append(f"  ...and {n - 10} more (see anomaly_watcher log)")

    # Suggested skill is the same across most hits — pick the most common.
    # For pure-celebration (all positive) messages we suppress this footer
    # because there's nothing to "investigate" — the operator just gets to
    # enjoy the win.
    if n_pos != n:
        skills: dict[str, int] = {}
        for h in sorted_hits:
            s = str(h.get("suggested_skill") or "")
            if s:
                skills[s] = skills.get(s, 0) + 1
        if skills:
            top_skill = max(skills.items(), key=lambda kv: kv[1])[0].replace("`", "")
            lines.append("")
            lines.append(f"Suggested: `{top_skill}`")

    return "\n".join(lines)


def _append_pulse_log(record: dict[str, Any]) -> None:
    """Best-effort write to var/anomaly_pulse.jsonl. Never raises."""
    try:
        _PULSE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _PULSE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        logger.warning("anomaly_pulse log append failed: %s", exc)


def _is_silenced() -> bool:
    """True when /silence is in effect. Best-effort, never raises."""
    try:
        from eta_engine.scripts import telegram_inbound_bot

        return telegram_inbound_bot.is_silenced()
    except Exception:  # noqa: BLE001
        return False


def run_pulse(*, dry_run: bool = False) -> dict[str, Any]:
    """Single pulse cycle. Returns a summary dict.

    Side effects:
      * Calls anomaly_watcher.scan() (which itself dedupes + appends hits)
      * If hits exist and not dry-run: sends one Telegram message
      * Appends a JSONL line to var/anomaly_pulse.jsonl for audit
      * Respects operator /silence command — if silenced, scans still
        run (so dedup state stays current) but the send is suppressed.
    """
    asof = datetime.now(UTC).isoformat()
    silenced = _is_silenced()
    try:
        from eta_engine.brain.jarvis_v3 import anomaly_watcher

        hits_objs = anomaly_watcher.scan()
        hits = [h.to_dict() for h in hits_objs]
    except Exception as exc:  # noqa: BLE001
        logger.exception("anomaly_watcher.scan() crashed: %s", exc)
        record = {"asof": asof, "ok": False, "error": str(exc)[:300], "n_new": 0}
        _append_pulse_log(record)
        return record

    if not hits:
        record = {"asof": asof, "ok": True, "n_new": 0, "sent": False, "reason": "no_new_hits"}
        _append_pulse_log(record)
        return record

    if silenced and not dry_run:
        record = {
            "asof": asof,
            "ok": True,
            "n_new": len(hits),
            "sent": False,
            "reason": "silenced_by_operator",
        }
        _append_pulse_log(record)
        return record

    message = _format_message(hits)
    priority = _sev_priority(hits)

    if dry_run:
        record = {
            "asof": asof,
            "ok": True,
            "n_new": len(hits),
            "sent": False,
            "priority": priority,
            "reason": "dry_run",
            "preview": message,
        }
        _append_pulse_log(record)
        return record

    # Real send
    try:
        from eta_engine.deploy.scripts.telegram_alerts import send_from_env

        send_result = send_from_env(message, priority=priority)
    except Exception as exc:  # noqa: BLE001
        logger.exception("telegram send crashed: %s", exc)
        send_result = {"ok": False, "error": str(exc)[:300]}

    record = {
        "asof": asof,
        "ok": True,
        "n_new": len(hits),
        "sent": bool(send_result.get("ok")),
        "priority": priority,
        "telegram_result": send_result,
    }
    _append_pulse_log(record)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Anomaly Telegram pulse. Scans for new (post-dedup) anomalies "
            "and sends one Telegram message if any are found. Quiet otherwise."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan + format the message but skip the actual Telegram send",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the result envelope as JSON instead of human text",
    )
    args = parser.parse_args(argv)

    result = run_pulse(dry_run=args.dry_run)

    if args.json:
        print(json.dumps(result, default=str, indent=2))
    elif result.get("n_new", 0) == 0:
        print(f"[anomaly_pulse] {result.get('asof')} -- quiet ({result.get('reason', 'no_new')})")
    else:
        print(
            f"[anomaly_pulse] {result.get('asof')} -- "
            f"{result.get('n_new')} new hits, "
            f"sent={result.get('sent')}, priority={result.get('priority')}"
        )
        if args.dry_run and result.get("preview"):
            print("--- message preview ---")
            print(result["preview"])

    # Cron-friendly: always exit 0 unless catastrophic.
    return 0


if __name__ == "__main__":
    sys.exit(main())
