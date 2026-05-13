"""Operator CLI: verify Telegram alert channel is wired correctly.

The wave-24 dispatcher pushes prop-fund HALT/WATCH alerts to whichever
push channels are configured via env vars:

  ETA_TELEGRAM_BOT_TOKEN  + ETA_TELEGRAM_CHAT_ID    → Telegram
  ETA_DISCORD_WEBHOOK_URL                            → Discord
  ETA_GENERIC_WEBHOOK_URL                            → generic POST

This script:
  1. Reports which channels are detected as configured.
  2. Optionally sends a test message to verify the credentials work
     end-to-end (--send-test).

Usage::

    python -m eta_engine.scripts.verify_telegram          # status only
    python -m eta_engine.scripts.verify_telegram --send-test
"""
# ruff: noqa: T201, S310
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime


def _check_telegram() -> tuple[bool, str]:
    token = os.environ.get("ETA_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ETA_TELEGRAM_CHAT_ID", "").strip()
    if not token and not chat_id:
        return False, "ETA_TELEGRAM_BOT_TOKEN and ETA_TELEGRAM_CHAT_ID both unset"
    if not token:
        return False, "ETA_TELEGRAM_BOT_TOKEN unset (chat_id is set)"
    if not chat_id:
        return False, "ETA_TELEGRAM_CHAT_ID unset (bot_token is set)"
    return True, f"telegram configured (token=***{token[-6:]}, chat_id={chat_id})"


def _check_discord() -> tuple[bool, str]:
    url = os.environ.get("ETA_DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return False, "ETA_DISCORD_WEBHOOK_URL unset"
    return True, f"discord configured (url=***{url[-20:]})"


def _check_generic() -> tuple[bool, str]:
    url = os.environ.get("ETA_GENERIC_WEBHOOK_URL", "").strip()
    if not url:
        return False, "ETA_GENERIC_WEBHOOK_URL unset"
    return True, f"generic webhook configured (url=***{url[-20:]})"


def _send_telegram_test() -> tuple[bool, str]:
    token = os.environ.get("ETA_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ETA_TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat_id):
        return False, "telegram not configured"
    text = (
        "*ETA wave-25 Telegram verification*\n"
        f"_{datetime.now(UTC).isoformat()}_\n"
        "If you see this, your alert channel is wired correctly.\n"
        "Prop-fund HALT/WATCH alerts will land here."
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
    ).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)  # noqa: S310
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            body = resp.read().decode("utf-8")
            payload = json.loads(body)
            if not payload.get("ok"):
                return False, f"telegram API returned not-ok: {payload}"
            msg_id = payload.get("result", {}).get("message_id")
            return True, f"sent (message_id={msg_id})"
    except urllib.error.URLError as exc:
        return False, f"network error: {exc}"
    except (json.JSONDecodeError, KeyError) as exc:
        return False, f"unexpected response: {exc}"


def _send_discord_test() -> tuple[bool, str]:
    url = os.environ.get("ETA_DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return False, "discord not configured"
    payload = {
        "content": (
            f"**ETA wave-25 Discord verification**\n"
            f"_{datetime.now(UTC).isoformat()}_\n"
            "If you see this, your alert channel is wired correctly."
        ),
    }
    data = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(  # noqa: S310
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            if resp.status not in (200, 204):
                return False, f"discord webhook returned status {resp.status}"
            return True, f"sent (status={resp.status})"
    except urllib.error.URLError as exc:
        return False, f"network error: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--send-test",
        action="store_true",
        help="Send a real test message to every configured channel.",
    )
    args = parser.parse_args(argv)

    print()
    print("=" * 70)
    print(" Telegram / Discord / Generic webhook verification")
    print("=" * 70)

    channels = [
        ("telegram", _check_telegram(), _send_telegram_test),
        ("discord", _check_discord(), _send_discord_test),
        ("generic_webhook", _check_generic(), None),
    ]
    any_configured = False
    for name, (configured, status), tester in channels:
        marker = "OK" if configured else "--"
        print(f"  [{marker}] {name}: {status}")
        any_configured = any_configured or configured
        if args.send_test and configured and tester is not None:
            ok, detail = tester()
            outcome = "SENT" if ok else "FAIL"
            print(f"      test: [{outcome}] {detail}")
    print()
    if not any_configured:
        print("  No channels configured. Set env vars per docs/WAVE25_PROP_LAUNCH_OPS.md")
        return 2
    if args.send_test:
        print("  Test messages dispatched (check your Telegram / Discord client).")
    else:
        print("  Add --send-test to dispatch a real verification message.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
