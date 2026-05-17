"""Shared alert-channel config helpers for ETA operator surfaces.

Telegram credentials can come from either process environment variables or
the canonical workspace secrets files under ``C:/EvolutionaryTradingAlgo``.
Env vars win when both are present so runtime overrides still work.
"""

from __future__ import annotations

import os
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ENGINE_ROOT.parent if ENGINE_ROOT.name == "eta_engine" else ENGINE_ROOT

_PLACEHOLDER_MARKERS = (
    "place your",
    "placeholder",
    "replace me",
    "change me",
    "changeme",
    "set me",
    "todo",
    "tbd",
    "your_token_here",
    "your secret here",
)


def workspace_root() -> Path:
    return Path(os.environ.get("ETA_WORKSPACE_ROOT") or WORKSPACE_ROOT)


def _looks_like_placeholder(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return True
    lowered = stripped.casefold()
    if lowered in {"none", "null"}:
        return True
    if stripped.startswith("<") and stripped.endswith(">"):
        return True
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _read_secret_text(rel_path: str) -> str:
    try:
        value = (workspace_root() / rel_path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return "" if _looks_like_placeholder(value) else value


def _env_or_secret(env_key: str, rel_path: str | None = None) -> str:
    value = os.environ.get(env_key, "").strip()
    if value:
        return value
    if rel_path is None:
        return ""
    return _read_secret_text(rel_path)


def get_telegram_bot_token() -> str:
    return _env_or_secret("ETA_TELEGRAM_BOT_TOKEN", "secrets/telegram_bot_token.txt")


def get_telegram_chat_id() -> str:
    return _env_or_secret("ETA_TELEGRAM_CHAT_ID", "secrets/telegram_chat_id.txt")


def get_discord_webhook_url() -> str:
    return _env_or_secret("ETA_DISCORD_WEBHOOK_URL")


def get_generic_webhook_url() -> str:
    return _env_or_secret("ETA_GENERIC_WEBHOOK_URL")


def telegram_configured() -> bool:
    return bool(get_telegram_bot_token() and get_telegram_chat_id())


def discord_configured() -> bool:
    return bool(get_discord_webhook_url())


def generic_configured() -> bool:
    return bool(get_generic_webhook_url())


def configured_channels() -> list[str]:
    channels: list[str] = []
    if telegram_configured():
        channels.append("telegram")
    if discord_configured():
        channels.append("discord")
    if generic_configured():
        channels.append("generic")
    return channels
