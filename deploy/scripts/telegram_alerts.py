"""
Deploy // telegram_alerts
=========================
Telegram alert webhook adapter for the Evolutionary Trading Algo stack.

Bridges the ``brain.jarvis_v3.next_level.voice.VoiceHub`` outbound-message
port to actual Telegram Bot API calls. When JARVIS emits a CRITICAL alert
(quota FREEZE, anomaly RED, kill-switch trip, service crash spike), this
module wires it to your phone.

One-time setup (operator):
  1. Create a bot via @BotFather on Telegram. Save the bot token.
  2. DM your new bot (so it has your chat), then open
     https://api.telegram.org/bot<TOKEN>/getUpdates and copy your chat_id.
  3. Add to .env:
       TELEGRAM_BOT_TOKEN=<bot_token>
       TELEGRAM_CHAT_ID=<chat_id>
  4. Restart Apex-Avengers-Fleet -- adapter picks up the creds.

Usage from code:
  from deploy.scripts.telegram_alerts import TelegramAdapter
  adapter = TelegramAdapter.from_env()
  if adapter:
      adapter.send("[kill_switch_trip] equity_dd 5.1% >= kill threshold")

Use as VoiceHub sender:
  hub = VoiceHub(audit_path=..., sender=adapter.as_voice_sender())
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger("telegram_alerts")


class TelegramAdapter:
    """Thin adapter over Telegram's HTTP Bot API."""

    def __init__(self, bot_token: str, chat_id: str, state_dir: Path | None = None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.state_dir = state_dir
        self.api_base = f"https://api.telegram.org/bot{bot_token}"

    # ------------------------------------------------------------------
    # Bootstrapping
    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, state_dir: Path | None = None) -> TelegramAdapter | None:
        """Build adapter from env vars. Returns None if unconfigured."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat:
            return None
        return cls(bot_token=token, chat_id=chat, state_dir=state_dir)

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------
    def send(self, text: str, *, priority: str = "INFO", parse_mode: str = "Markdown") -> dict:
        """Send a message to the configured chat. Returns response dict."""
        try:
            import httpx
        except ImportError:
            return {"ok": False, "error": "httpx not installed"}

        prefix = {"INFO": "\u2139\ufe0f", "WARN": "\u26a0\ufe0f", "CRITICAL": "\U0001f6a8"}.get(priority, "")
        body = f"{prefix} {text}" if prefix else text
        if len(body) > 4000:
            body = body[:3990] + "\u2026"
        try:
            resp = httpx.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": body,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=10.0,
            )
            result = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram send failed: %s", exc)
            result = {"ok": False, "error": str(exc)[:200]}

        if self.state_dir is not None:
            self._record_send(body, priority, result)
        return result

    def _record_send(self, body: str, priority: str, result: dict) -> None:
        """Append to a rolling JSONL log for audit/troubleshooting."""
        if not self.state_dir:
            return
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.state_dir / "telegram_alerts.jsonl"
            with log_path.open("a", encoding="utf-8") as fp:
                fp.write(
                    json.dumps(
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "priority": priority,
                            "body_preview": body[:200],
                            "ok": bool(result.get("ok", False)),
                            "error": result.get("description") or result.get("error"),
                        }
                    )
                    + "\n"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram record failed: %s", exc)

    def test(self) -> dict:
        """Send a heartbeat test message."""
        return self.send(
            f"*Evolutionary Trading Algo adapter online* \nTime: `{datetime.now(UTC).isoformat()}`\nChat: `{self.chat_id}`",
            priority="INFO",
        )

    # ------------------------------------------------------------------
    # VoiceHub bridge
    # ------------------------------------------------------------------
    def as_voice_sender(self) -> Callable[[str, str, str], None]:
        """Return a callable usable as VoiceHub.sender.

        VoiceHub calls: sender(channel, text, priority) -> None.
        We ignore channel (we're only Telegram) but respect priority.
        """

        def _send(channel: str, text: str, priority: str) -> None:  # noqa: ARG001
            self.send(text, priority=priority)

        return _send


def send_from_env(text: str, priority: str = "INFO") -> dict:
    """One-shot helper. Reads env, sends, returns response. Used by cron hooks."""
    adapter = TelegramAdapter.from_env()
    if adapter is None:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN/CHAT_ID missing"}
    return adapter.send(text, priority=priority)


if __name__ == "__main__":
    import sys

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    adapter = TelegramAdapter.from_env()
    if not adapter:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        sys.exit(2)
    result = adapter.test()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)
