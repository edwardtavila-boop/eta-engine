"""
End-of-day debrief — single Telegram digest at session close.

Operator gets ONE comprehensive message per day at 21:30 UTC (5:30 PM ET,
after futures settlement) covering:

  1. *PnL today*  — total R, trade count, win rate
  2. *Top winners + losers*  — best/worst 3 bots by R today
  3. *Prop firm scorecard*  — per-account day_pnl, daily-loss headroom,
     trailing-DD headroom, profit-to-target progress
  4. *Notable events*  — anomaly hits fired today (loss streaks, win streaks,
     suspicious wins, stale bots, prop firm approaching alerts)
  5. *Override activity*  — what overrides Hermes applied today
  6. *Tomorrow's outlook*  — calendar events in next 24h, current regime,
     preflight verdict for tomorrow's first session

This is the "what happened, why, what to watch tomorrow" daily ritual the
operator's morning brain can rely on. Replaces 10+ separate dashboard
visits with one Markdown-formatted Telegram block.

Run modes
---------

    python -m eta_engine.scripts.daily_debrief                # send
    python -m eta_engine.scripts.daily_debrief --dry-run      # preview
    python -m eta_engine.scripts.daily_debrief --json         # machine read

Cron schedule
-------------

    eta_engine/deploy/daily_debrief_task.xml — fires 21:30 UTC weekdays
    (5:30 PM ET, after the futures pit close & post-settlement window)

Never raises — failures during digest generation become "(unavailable)"
in the message body, the operator still gets the parts that worked.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from eta_engine.scripts import workspace_roots

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("eta_engine.scripts.daily_debrief")

_DEBRIEF_LOG = workspace_roots.ETA_DAILY_DEBRIEF_LOG_PATH
_HERMES_ACTIONS_LOG_PATH = workspace_roots.ETA_HERMES_ACTIONS_LOG_PATH


# ---------------------------------------------------------------------------
# Section builders — each returns (section_title, markdown_body, raw_dict)
# ---------------------------------------------------------------------------


def _safe[T](call: Callable[[], T], default: T) -> T:
    """Best-effort wrapper — returns default on any failure, logs exception."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        logger.warning("debrief section failed: %s", exc)
        return default


def section_pnl_today() -> tuple[str, str, dict[str, Any]]:
    from eta_engine.brain.jarvis_v3 import pnl_summary

    multi = _safe(pnl_summary.multi_window_summary, {})
    today = multi.get("today") or {}
    total_r = float(today.get("total_r", 0.0) or 0.0)
    n_trades = int(today.get("n_trades", 0) or 0)
    wins = int(today.get("n_wins", 0) or 0)
    losses = int(today.get("n_losses", 0) or 0)
    win_rate = float(today.get("win_rate", 0.0) or 0.0)

    emoji = "📈" if total_r > 0 else ("📉" if total_r < 0 else "➖")
    body = (
        f"`Total      {total_r:+.2f}R   ({n_trades} trades)`\n"
        f"`Win rate   {win_rate * 100:.1f}%   (W/L {wins}/{losses})`"
    )
    return f"{emoji} *PnL today*", body, today


def section_top_performers() -> tuple[str, str, dict[str, Any]]:
    from eta_engine.brain.jarvis_v3 import pnl_summary

    multi = _safe(pnl_summary.multi_window_summary, {})
    today = multi.get("today") or {}
    top = (today.get("top_performers") or [])[:3]
    worst = (today.get("worst_performers") or [])[:3]

    def _fmt(b: dict[str, Any]) -> str:
        bot = str(b.get("bot_id") or "?")
        r = float(b.get("total_r", 0.0) or 0.0)
        n = int(b.get("n_trades", 0) or 0)
        return f"`{bot:<22}  {r:+.2f}R  ({n} trades)`"

    body_parts: list[str] = []
    if top:
        body_parts.append("*Winners*")
        body_parts.extend(_fmt(b) for b in top)
    if worst:
        if body_parts:
            body_parts.append("")
        body_parts.append("*Losers*")
        body_parts.extend(_fmt(b) for b in worst)

    body = "\n".join(body_parts) if body_parts else "_(no winners or losers recorded today)_"
    return "🏆 *Top performers*", body, {"top": top, "worst": worst}


def section_prop_firm_scorecard() -> tuple[str, str, dict[str, Any]]:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    snaps = _safe(g.aggregate_status, [])
    if not snaps:
        return "🏦 *Prop firm accounts*", "_(none registered)_", {}

    sev_emoji = {"blown": "💀", "critical": "🚨", "warn": "⚠️", "ok": "✅"}
    lines: list[str] = []
    for s in snaps:
        em = sev_emoji.get(s.severity, "")
        day = s.state.day_pnl_usd
        dlr = s.daily_loss_remaining if s.daily_loss_remaining is not None else 0.0
        ddr = s.trailing_dd_remaining if s.trailing_dd_remaining is not None else 0.0
        target_str = ""
        if s.rules.profit_target is not None and s.pct_to_target is not None:
            target_str = f"   target {s.pct_to_target * 100:+.0f}%"
        tos_str = "  _TOS_" if not s.rules.automation_allowed else ""
        lines.append(
            f"{em} `{s.rules.account_id:<22}` day {day:+,.0f}   DLR ${dlr:,.0f}   DDR ${ddr:,.0f}{target_str}{tos_str}"
        )
    raw = [s.to_dict() for s in snaps]
    return "🏦 *Prop firm accounts*", "\n".join(lines), {"snapshots": raw}


def section_notable_events() -> tuple[str, str, dict[str, Any]]:
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    hits = _safe(lambda: anomaly_watcher.recent_hits(since_hours=24), [])
    if not hits:
        return "🔍 *Notable events*", "_(none — quiet day)_", {"hits": []}

    # Group by pattern
    by_pattern: dict[str, int] = {}
    crits: list[dict[str, Any]] = []
    for h in hits:
        p = str(h.get("pattern") or "?")
        by_pattern[p] = by_pattern.get(p, 0) + 1
        if str(h.get("severity") or "") == "critical":
            crits.append(h)

    parts: list[str] = []
    parts.append("*By pattern (count)*")
    for pattern, n in sorted(by_pattern.items(), key=lambda kv: -kv[1]):
        parts.append(f"`{pattern:<32}  ×{n}`")

    if crits:
        parts.append("")
        parts.append("*CRITICAL hits*")
        for c in crits[:5]:
            detail = str(c.get("detail") or "")[:80]
            parts.append(f"🚨 `{c.get('bot_id', '?')}` — {detail}")

    return "🔍 *Notable events*", "\n".join(parts), {"by_pattern": by_pattern, "n_critical": len(crits)}


def section_override_activity() -> tuple[str, str, dict[str, Any]]:
    """Hermes overrides applied today."""
    path = _HERMES_ACTIONS_LOG_PATH
    if not path.exists():
        return "⚙️ *Override activity*", "_(no audit log yet)_", {}

    cutoff = datetime.now(UTC) - timedelta(hours=24)
    overrides_today = 0
    by_kind: dict[str, int] = {}
    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = rec.get("ts") or rec.get("asof")
                try:
                    ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    continue
                if ts < cutoff:
                    continue
                tool = str(rec.get("tool") or "")
                if "size_modifier" in tool or "school_weight" in tool or "clear_override" in tool:
                    overrides_today += 1
                    by_kind[tool] = by_kind.get(tool, 0) + 1
    except OSError as exc:
        return "⚙️ *Override activity*", f"_(log read failed: {exc})_", {}

    if overrides_today == 0:
        return "⚙️ *Override activity*", "_(no overrides applied today)_", {}

    lines = [f"*{overrides_today} overrides today*"]
    for tool, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        lines.append(f"`{tool:<30}  ×{n}`")
    return "⚙️ *Override activity*", "\n".join(lines), {"n": overrides_today, "by_kind": by_kind}


def section_tomorrow_outlook() -> tuple[str, str, dict[str, Any]]:
    """Calendar + current regime + preflight verdict."""
    parts: list[str] = []
    raw: dict[str, Any] = {}

    # Upcoming events (next 24h)
    try:
        from eta_engine.brain.jarvis_v3 import calendar_events  # type: ignore

        events = calendar_events.upcoming_events(horizon_min=24 * 60) or []
    except Exception:  # noqa: BLE001
        events = []
    if events:
        parts.append("*Calendar (next 24h)*")
        for e in events[:5]:
            ts = getattr(e, "ts_utc", None) or ""
            kind = getattr(e, "kind", None) or "?"
            sev = getattr(e, "severity", None) or 1
            parts.append(f"`{str(ts)[:16]}  {kind:<10}  sev={sev}`")
        raw["n_events"] = len(events)
    else:
        parts.append("_(no calendar events flagged in next 24h)_")
        raw["n_events"] = 0

    # Preflight verdict
    try:
        from eta_engine.brain.jarvis_v3 import preflight

        report = preflight.run_preflight()
        parts.append("")
        parts.append(f"*Preflight* — `{report.verdict}` (P{report.n_pass}/W{report.n_warn}/F{report.n_fail})")
        raw["preflight_verdict"] = report.verdict
        if report.n_fail > 0:
            parts.append("Blockers:")
            for c in report.checks:
                if c.status == "FAIL":
                    parts.append(f"❌ `{c.name}` — {c.detail[:60]}")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"_(preflight unavailable: {exc})_")

    return "🌅 *Tomorrow's outlook*", "\n".join(parts), raw


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------


def build_debrief() -> dict[str, Any]:
    """Build the full debrief envelope. Always returns a dict.

    Returned dict:
      {
        "asof": iso timestamp,
        "markdown": single formatted message body for Telegram,
        "sections": list of {title, body_md, raw}
      }
    """
    asof = datetime.now(UTC).isoformat()
    sections: list[dict[str, Any]] = []
    for builder in (
        section_pnl_today,
        section_top_performers,
        section_prop_firm_scorecard,
        section_notable_events,
        section_override_activity,
        section_tomorrow_outlook,
    ):
        try:
            title, body, raw = builder()
        except Exception as exc:  # noqa: BLE001
            title = builder.__name__
            body = f"_(section crashed: {exc})_"
            raw = {}
        sections.append({"title": title, "body_md": body, "raw": raw})

    bar = "─" * 24
    header = f"*Daily Debrief* — {asof[:16]} UTC\n{bar}"
    blocks = [header]
    for s in sections:
        blocks.append("")
        blocks.append(s["title"])
        blocks.append(s["body_md"])
    blocks.append("")
    blocks.append(bar)
    blocks.append("_via_ `python -m eta_engine.scripts.daily_debrief`")

    return {
        "asof": asof,
        "markdown": "\n".join(blocks),
        "sections": sections,
    }


# ---------------------------------------------------------------------------
# Send + audit
# ---------------------------------------------------------------------------


def _append_audit(record: dict[str, Any]) -> None:
    try:
        _DEBRIEF_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _DEBRIEF_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        logger.warning("debrief audit log append failed: %s", exc)


def send_debrief(dry_run: bool = False) -> dict[str, Any]:
    """Build + send the debrief. Returns the envelope with send_result."""
    envelope = build_debrief()
    body = envelope["markdown"]

    if dry_run:
        envelope["sent"] = False
        envelope["reason"] = "dry_run"
        _append_audit(envelope)
        return envelope

    # Honour /silence if set
    try:
        from eta_engine.scripts import telegram_inbound_bot

        if telegram_inbound_bot.is_silenced():
            envelope["sent"] = False
            envelope["reason"] = "silenced_by_operator"
            _append_audit(envelope)
            return envelope
    except Exception:  # noqa: BLE001
        pass

    try:
        from eta_engine.deploy.scripts.telegram_alerts import send_from_env

        result = send_from_env(body, priority="INFO")
    except Exception as exc:  # noqa: BLE001
        logger.exception("debrief send failed: %s", exc)
        result = {"ok": False, "error": str(exc)[:200]}

    envelope["sent"] = bool(result.get("ok"))
    envelope["telegram_result"] = result
    _append_audit(envelope)
    return envelope


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "End-of-day debrief — sends a comprehensive Telegram digest of "
            "today's PnL, anomalies, prop firm states, overrides, and "
            "tomorrow's outlook."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Build but don't send")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    envelope = send_debrief(dry_run=args.dry_run)

    if args.json:
        print(json.dumps(envelope, default=str, indent=2))
    else:
        print(envelope["markdown"])
        if not args.dry_run:
            sent = envelope.get("sent")
            reason = envelope.get("reason", "")
            print(f"\n[debrief] sent={sent} reason={reason}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
