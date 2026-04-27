"""Slack/Discord verdict-stream webhook (Tier-3 #12, 2026-04-27).

Streams the tail of JARVIS's verdict log to a Slack/Discord webhook so
the operator + inner circle can see live decisions without opening
the dashboard. Polls audit JSONL files every N seconds, fires one
webhook POST per new line.

State persists to ``state/verdict_webhook/cursor.json`` so we don't
re-fire on restart.

Configure via env vars:

  ETA_VERDICT_WEBHOOK_URL  -- Slack incoming-webhook URL OR Discord
                              webhook URL (auto-detected by hostname)
  ETA_VERDICT_WEBHOOK_LEVEL -- minimum verdict level to forward
                               (default: DENIED -- tighter than just CONDITIONAL)

Run via ``Eta-Verdict-Webhook`` every 1 min.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

logger = logging.getLogger("jarvis_verdict_webhook")

#: Default verdict levels we forward. Operator can broaden via env.
DEFAULT_FORWARD_VERDICTS = {"DENIED"}


def _format_for_slack(rec: dict[str, Any]) -> dict[str, Any]:
    """Slack incoming-webhook payload."""
    resp = rec.get("response", {}) or {}
    req = rec.get("request", {}) or {}
    return {
        "text": (
            f"*JARVIS verdict:* `{resp.get('verdict', '?')}` "
            f"*subsystem:* `{req.get('subsystem', '?')}` "
            f"*action:* `{req.get('action', '?')}`\n"
            f">*reason:* {resp.get('reason', '')}\n"
            f">*reason_code:* `{resp.get('reason_code', '')}`\n"
            f">*stress:* {resp.get('stress_composite', 0):.2f} "
            f"*session:* {resp.get('session_phase', '')} "
            f"*policy_v:* {rec.get('policy_version', 0)}"
        )
    }


def _format_for_discord(rec: dict[str, Any]) -> dict[str, Any]:
    """Discord webhook payload."""
    resp = rec.get("response", {}) or {}
    req = rec.get("request", {}) or {}
    color = {
        "APPROVED":    0x2ECC71,
        "CONDITIONAL": 0xF1C40F,
        "DEFERRED":    0x3498DB,
        "DENIED":      0xE74C3C,
    }.get(resp.get("verdict", ""), 0x95A5A6)
    return {
        "embeds": [{
            "title": f"JARVIS {resp.get('verdict', '?')}",
            "color": color,
            "fields": [
                {"name": "subsystem", "value": req.get("subsystem", "?"), "inline": True},
                {"name": "action",    "value": req.get("action", "?"),    "inline": True},
                {"name": "reason",    "value": resp.get("reason", "")[:1000], "inline": False},
                {"name": "stress",    "value": f"{resp.get('stress_composite', 0):.2f}", "inline": True},
                {"name": "session",   "value": str(resp.get("session_phase", "")), "inline": True},
                {"name": "policy_v",  "value": str(rec.get("policy_version", 0)), "inline": True},
            ],
            "timestamp": rec.get("ts", datetime.now(UTC).isoformat()),
        }]
    }


def post_webhook(url: str, payload: dict[str, Any]) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={
            "Content-Type": "application/json",
            "User-Agent": "eta-engine-verdict-webhook/1.0",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        logger.warning("webhook POST failed: %s", exc)
        return False


def _is_discord(url: str) -> bool:
    return "discord.com" in url or "discordapp.com" in url


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--audit-dir", type=Path,
                   default=ROOT / "state" / "jarvis_audit")
    p.add_argument("--cursor", type=Path,
                   default=ROOT / "state" / "verdict_webhook" / "cursor.json")
    p.add_argument("--max-per-run", type=int, default=20,
                   help="Cap number of webhook posts per invocation (anti-spam)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    url = os.environ.get("ETA_VERDICT_WEBHOOK_URL", "").strip()
    if not url:
        logger.info("ETA_VERDICT_WEBHOOK_URL not set -- nothing to forward, exiting clean")
        return 0

    level_str = os.environ.get("ETA_VERDICT_WEBHOOK_LEVEL", "")
    if level_str:
        forward = {v.strip().upper() for v in level_str.split(",") if v.strip()}
    else:
        forward = DEFAULT_FORWARD_VERDICTS

    # Load cursor
    cursor: dict[str, int] = {}
    if args.cursor.exists():
        try:
            cursor = json.loads(args.cursor.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cursor = {}

    posted = 0
    for f in sorted(args.audit_dir.glob("*.jsonl")) if args.audit_dir.exists() else []:
        last_offset = cursor.get(f.name, 0)
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        new_text = text[last_offset:]
        if not new_text:
            continue
        for line in new_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            verdict = (rec.get("response", {}) or {}).get("verdict", "")
            if verdict not in forward:
                continue
            payload = _format_for_discord(rec) if _is_discord(url) else _format_for_slack(rec)
            if not args.dry_run:
                ok = post_webhook(url, payload)
                if ok:
                    posted += 1
            else:
                logger.info("(dry-run) would post: %s", json.dumps(payload)[:200])
                posted += 1
            if posted >= args.max_per_run:
                logger.info("hit max-per-run cap (%d) -- stopping", args.max_per_run)
                break
        cursor[f.name] = len(text)
        if posted >= args.max_per_run:
            break

    if not args.dry_run:
        args.cursor.parent.mkdir(parents=True, exist_ok=True)
        args.cursor.write_text(json.dumps(cursor), encoding="utf-8")

    logger.info("posted %d webhook(s) this run", posted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
