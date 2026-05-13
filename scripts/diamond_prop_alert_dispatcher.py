"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_prop_alert_dispatcher
======================================================================
Push prop-guard HALT/WATCH alerts to operator channels.

Why this exists (wave-24)
-------------------------
diamond_prop_drawdown_guard already writes RED/YELLOW alerts to the
shared alerts_log.jsonl every 15 minutes.  The dashboard reads that
file for its daily-summary panel.  But for the Monday 2026-05-18
prop-fund cutover, the operator needs PUSH delivery -- a phone
notification when HALT fires, not just a JSON file the dashboard
can render later.

This dispatcher reads alerts_log.jsonl, finds NEW prop-guard alerts
since the last cursor, and POSTs each to whatever webhook channels
the operator has configured via environment variables.  Cursor-based
de-dup means restarting the dispatcher never replays old alerts.

Channels (auto-detected from env)
---------------------------------

  ETA_TELEGRAM_BOT_TOKEN + ETA_TELEGRAM_CHAT_ID
      -> Telegram message via Bot API
      -> "https://api.telegram.org/bot{TOKEN}/sendMessage"

  ETA_DISCORD_WEBHOOK_URL
      -> Discord channel webhook
      -> POST {"content": "..."} to the URL

  ETA_GENERIC_WEBHOOK_URL
      -> Generic JSON POST (Slack-compatible payload format)
      -> POST {"text": "..."} to the URL

If no env vars are set, the dispatcher logs and exits 0 -- the
operator can configure channels later without breaking the cron.

What it does NOT do
-------------------
- Does NOT write/modify the alerts_log -- read-only consumer.
- Does NOT batch alerts -- every NEW HALT/WATCH event fires its own
  push.  This is intentional for prop-fund safety.
- Does NOT acknowledge alerts -- you receive the push and decide
  what to do.  No two-way control surface.

Output
------
- stdout: per-alert dispatch status
- ``var/eta_engine/state/diamond_prop_alert_cursor.json`` (last seen ts)
- exit 0 always (best-effort dispatch; failures are logged)

Run
---
::

    python -m eta_engine.scripts.diamond_prop_alert_dispatcher
    python -m eta_engine.scripts.diamond_prop_alert_dispatcher --dry-run
    python -m eta_engine.scripts.diamond_prop_alert_dispatcher --since 1h
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
ALERTS_LOG = WORKSPACE_ROOT / "logs" / "eta_engine" / "alerts_log.jsonl"
CURSOR_PATH = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "diamond_prop_alert_cursor.json"
OUT_LATEST = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "diamond_prop_alert_dispatcher_latest.json"

#: Sources whose alerts the dispatcher pushes.  We're conservative --
#: only the prop-guard for now.  Additional sources can be added as
#: the operator's prop-fund pipeline matures.
RELEVANT_SOURCES = frozenset(
    {
        "diamond_prop_drawdown_guard",
    }
)

#: HTTP POST timeout in seconds.  Webhook services should respond in
#: <2s; longer is a sign the channel is degraded and we should move on.
WEBHOOK_TIMEOUT_SECS = 5

#: Max alerts dispatched per invocation.  Defense against a corrupted
#: alerts_log replaying a flood.  An operator who genuinely has 100
#: alerts queued would also want to know via the dashboard, not a
#: torrent of phone notifications.
MAX_ALERTS_PER_RUN = 25


@dataclass
class DispatchResult:
    alert_id: str
    timestamp: str
    severity: str
    headline: str
    channels_attempted: list[str] = field(default_factory=list)
    channels_succeeded: list[str] = field(default_factory=list)
    channels_failed: list[dict[str, str]] = field(default_factory=list)


@dataclass
class DispatcherSummary:
    ts: str
    cursor_before: str
    cursor_after: str
    n_alerts_seen: int
    n_alerts_dispatched: int
    n_alerts_skipped_no_channels: int
    configured_channels: list[str]
    dispatches: list[DispatchResult] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# Channel detection
# ────────────────────────────────────────────────────────────────────


def _telegram_configured() -> bool:
    return bool(
        os.environ.get("ETA_TELEGRAM_BOT_TOKEN") and os.environ.get("ETA_TELEGRAM_CHAT_ID"),
    )


def _discord_configured() -> bool:
    return bool(os.environ.get("ETA_DISCORD_WEBHOOK_URL"))


def _generic_configured() -> bool:
    return bool(os.environ.get("ETA_GENERIC_WEBHOOK_URL"))


def configured_channels() -> list[str]:
    """Return the list of channel names that have env vars set."""
    out: list[str] = []
    if _telegram_configured():
        out.append("telegram")
    if _discord_configured():
        out.append("discord")
    if _generic_configured():
        out.append("generic")
    return out


# ────────────────────────────────────────────────────────────────────
# Channel senders
# ────────────────────────────────────────────────────────────────────


def _http_post_json(url: str, payload: dict[str, Any]) -> None:
    """POST a JSON payload; raises on non-2xx."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 (configured webhook URLs)
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_SECS) as resp:  # noqa: S310
        if resp.status >= 300:
            msg = f"HTTP {resp.status}"
            raise RuntimeError(msg)


def _send_telegram(text: str) -> None:
    token = os.environ.get("ETA_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("ETA_TELEGRAM_CHAT_ID", "")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    _http_post_json(
        url,
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        },
    )


def _send_discord(text: str) -> None:
    url = os.environ.get("ETA_DISCORD_WEBHOOK_URL", "")
    _http_post_json(url, {"content": text})


def _send_generic(text: str) -> None:
    url = os.environ.get("ETA_GENERIC_WEBHOOK_URL", "")
    _http_post_json(url, {"text": text})


CHANNELS = {
    "telegram": (_telegram_configured, "_send_telegram"),
    "discord": (_discord_configured, "_send_discord"),
    "generic": (_generic_configured, "_send_generic"),
}


# ────────────────────────────────────────────────────────────────────
# Cursor + log reading
# ────────────────────────────────────────────────────────────────────


def _load_cursor() -> str:
    """Return the ISO timestamp of the last alert we already dispatched.
    Empty string on first run / missing/malformed cursor file."""
    if not CURSOR_PATH.exists():
        return ""
    try:
        data = json.loads(CURSOR_PATH.read_text(encoding="utf-8"))
        return str(data.get("last_dispatched_ts") or "")
    except (OSError, json.JSONDecodeError):
        return ""


def _save_cursor(ts: str) -> None:
    try:
        CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        CURSOR_PATH.write_text(
            json.dumps({"last_dispatched_ts": ts}, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"WARN: cursor save failed: {exc}", file=sys.stderr)


def _read_new_alerts(cursor: str, since_iso: str | None = None) -> list[dict[str, Any]]:
    """Stream the alerts_log and return alerts NEWER than `cursor`
    AND from sources we care about.  If `since_iso` is provided
    (operator override), use that as the floor instead of the cursor."""
    if not ALERTS_LOG.exists():
        return []
    floor = since_iso or cursor
    out: list[dict[str, Any]] = []
    with ALERTS_LOG.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                alert = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = str(alert.get("timestamp_utc") or alert.get("ts") or "")
            if not ts:
                continue
            if floor and ts <= floor:
                continue
            if alert.get("source") not in RELEVANT_SOURCES:
                continue
            out.append(alert)
    # Sort by ts ascending so we dispatch in chronological order
    out.sort(key=lambda a: str(a.get("timestamp_utc") or a.get("ts") or ""))
    return out[:MAX_ALERTS_PER_RUN]


# ────────────────────────────────────────────────────────────────────
# Dispatch
# ────────────────────────────────────────────────────────────────────


def _format_message(alert: dict[str, Any]) -> str:
    """Build a human-readable message for an alert."""
    sev = alert.get("severity", "?")
    sev_emoji = {"RED": "🚨", "YELLOW": "⚠️", "GREEN": "✅"}.get(sev, "•")
    headline = alert.get("headline", "(no headline)")
    ts = alert.get("timestamp_utc") or alert.get("ts", "")
    return f"{sev_emoji} *PROP-FUND {sev}*\n{headline}\n_{ts}_"


def _dispatch_alert(alert: dict[str, Any], dry_run: bool) -> DispatchResult:
    """Send one alert to all configured channels."""
    text = _format_message(alert)
    res = DispatchResult(
        alert_id=str(alert.get("alert_id", "")),
        timestamp=str(alert.get("timestamp_utc") or alert.get("ts", "")),
        severity=str(alert.get("severity", "")),
        headline=str(alert.get("headline", ""))[:160],
    )
    for name, (probe, sender_name) in CHANNELS.items():
        if not probe():
            continue
        res.channels_attempted.append(name)
        if dry_run:
            res.channels_succeeded.append(f"{name}:DRY_RUN")
            continue
        try:
            sender = globals()[sender_name]
            sender(text)
            res.channels_succeeded.append(name)
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            RuntimeError,
            TimeoutError,
            OSError,
        ) as exc:
            res.channels_failed.append({"channel": name, "error": str(exc)})
    return res


def run(
    *,
    dry_run: bool = False,
    since: str | None = None,
) -> dict[str, Any]:
    cursor = _load_cursor()
    floor = _resolve_since(since)
    alerts = _read_new_alerts(cursor, since_iso=floor)
    channels = configured_channels()
    summary = DispatcherSummary(
        ts=datetime.now(UTC).isoformat(),
        cursor_before=cursor,
        cursor_after=cursor,
        n_alerts_seen=len(alerts),
        n_alerts_dispatched=0,
        n_alerts_skipped_no_channels=0,
        configured_channels=channels,
    )
    if not alerts:
        _persist_summary(summary)
        return asdict(summary)
    if not channels:
        # No channels configured, but we still advance the cursor so we
        # don't replay these once channels are added later.
        summary.n_alerts_skipped_no_channels = len(alerts)
        last_ts = str(
            alerts[-1].get("timestamp_utc") or alerts[-1].get("ts") or "",
        )
        if last_ts and not dry_run:
            _save_cursor(last_ts)
            summary.cursor_after = last_ts
        _persist_summary(summary)
        return asdict(summary)

    last_dispatched_ts = cursor
    for alert in alerts:
        result = _dispatch_alert(alert, dry_run=dry_run)
        summary.dispatches.append(result)
        if result.channels_succeeded:
            summary.n_alerts_dispatched += 1
            last_dispatched_ts = result.timestamp or last_dispatched_ts
    if last_dispatched_ts and not dry_run:
        _save_cursor(last_dispatched_ts)
        summary.cursor_after = last_dispatched_ts
    _persist_summary(summary)
    return asdict(summary)


def _resolve_since(since: str | None) -> str | None:
    """Convert --since arg ('1h', '30m', '2d', or ISO) to an ISO floor."""
    if not since:
        return None
    s = since.strip().lower()
    try:
        if s.endswith("h"):
            return (datetime.now(UTC) - timedelta(hours=int(s[:-1]))).isoformat()
        if s.endswith("m"):
            return (datetime.now(UTC) - timedelta(minutes=int(s[:-1]))).isoformat()
        if s.endswith("d"):
            return (datetime.now(UTC) - timedelta(days=int(s[:-1]))).isoformat()
    except ValueError:
        pass
    return since  # caller-supplied ISO timestamp


def _persist_summary(summary: DispatcherSummary) -> None:
    try:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(
            json.dumps(asdict(summary), indent=2, default=str),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"WARN: write_latest failed: {exc}", file=sys.stderr)


def _print(s: dict[str, Any]) -> None:
    print("=" * 90)
    print(
        f" PROP ALERT DISPATCHER  ({s['ts']})  "
        f"channels={s['configured_channels'] or '(none configured)'}  "
        f"seen={s['n_alerts_seen']}  dispatched={s['n_alerts_dispatched']}",
    )
    print("=" * 90)
    if not s["configured_channels"]:
        print("  No channels configured -- set ETA_TELEGRAM_BOT_TOKEN +")
        print("  ETA_TELEGRAM_CHAT_ID or ETA_DISCORD_WEBHOOK_URL or")
        print("  ETA_GENERIC_WEBHOOK_URL to enable push delivery.")
    if s["n_alerts_skipped_no_channels"]:
        print(
            f"  {s['n_alerts_skipped_no_channels']} alerts seen but skipped (no channels) -- cursor advanced anyway",
        )
    for d in s["dispatches"]:
        ok = ", ".join(d["channels_succeeded"]) or "-"
        fails = ", ".join(f"{f['channel']}:{f['error']}" for f in d["channels_failed"]) or "-"
        print(f"  [{d['severity']:6s}] {d['timestamp']}  {d['headline'][:50]}")
        print(f"          ok={ok}  fail={fails}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute dispatches but don't POST or advance cursor",
    )
    ap.add_argument(
        "--since",
        type=str,
        default=None,
        help="Override cursor floor: '1h' / '30m' / '2d' / ISO timestamp",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    summary = run(dry_run=args.dry_run, since=args.since)
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
