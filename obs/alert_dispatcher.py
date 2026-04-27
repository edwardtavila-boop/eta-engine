"""
EVOLUTIONARY TRADING ALGO  //  obs.alert_dispatcher
=======================================
Reads configs/alerts.yaml and routes events to Pushover / email / SMS.

Decision #17: Pushover primary, email digest, SMS on kill.

Design
------
- No network calls unless creds are set in env. Otherwise logs to
  docs/alerts_log.jsonl and no-ops the transport (so dryruns / CI stay clean).
- Rate limits per level.
- Events are one of the keys in routing.events in alerts.yaml. Unknown events
  are logged but not dispatched (loudly — we want to catch typos).

Usage
-----
    dispatcher = AlertDispatcher.from_yaml(Path("configs/alerts.yaml"))
    dispatcher.send("kill_switch", {"bot": "mnq", "reason": "10% DD trip-wire"})

Transports
----------
Real Pushover / SMTP / Twilio transports are wired (stdlib only: urllib +
smtplib). Each transport consults the creds map before firing — no creds →
no HTTP/SMTP. Tests monkeypatch the `_send_pushover` / `_send_email` /
`_send_sms` module functions.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import smtplib
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_S = 10.0


@dataclass
class DispatchResult:
    event: str
    level: str
    channels: list[str]
    delivered: list[str]
    blocked: list[str]
    ts: float


@dataclass
class _RateLimiter:
    per_minute: int
    _hits: deque[float] = field(default_factory=deque)

    def allow(self, now: float) -> bool:
        if self.per_minute <= 0:
            return True  # 0 means unthrottled
        # Prune older than 60s
        while self._hits and now - self._hits[0] > 60.0:
            self._hits.popleft()
        if len(self._hits) >= self.per_minute:
            return False
        self._hits.append(now)
        return True


# --------------------------------------------------------------------------- #
# Module-level transport functions (monkeypatchable)
# --------------------------------------------------------------------------- #
def _send_pushover(
    user: str,
    token: str,
    title: str,
    message: str,
    priority: int = 0,
) -> bool:
    """POST to https://api.pushover.net/1/messages.json. Sync, stdlib-only."""
    url = "https://api.pushover.net/1/messages.json"
    data = urllib.parse.urlencode(
        {
            "user": user,
            "token": token,
            "title": title[:250],
            "message": message[:1024],
            "priority": str(priority),
        }
    ).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            if resp.status != 200:
                logger.warning("pushover status=%s", resp.status)
                return False
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body) if body else {}
            return int(data.get("status", 0)) == 1
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        logger.warning("pushover send failed: %s", exc)
        return False


def _send_email(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
) -> bool:
    """Send via SMTP STARTTLS. Sync, stdlib-only."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject[:250]
    msg["From"] = from_addr
    msg["To"] = to_addr
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=_HTTP_TIMEOUT_S) as smtp:
            smtp.ehlo()
            if smtp.has_extn("STARTTLS"):
                smtp.starttls()
                smtp.ehlo()
            smtp.login(smtp_user, smtp_pass)
            smtp.sendmail(from_addr, [to_addr], msg.as_string())
            return True
    except (smtplib.SMTPException, OSError) as exc:
        logger.warning("smtp send failed: %s", exc)
        return False


def _send_sms(
    sid: str,
    token: str,
    from_number: str,
    to_number: str,
    body: str,
) -> bool:
    """POST to Twilio /Messages.json with basic auth. Sync, stdlib-only."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode(
        {
            "From": from_number,
            "To": to_number,
            "Body": body[:1600],
        }
    ).encode("utf-8")
    credentials = base64.b64encode(f"{sid}:{token}".encode()).decode("ascii")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            if resp.status not in (200, 201):
                logger.warning("twilio status=%s", resp.status)
                return False
            return True
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("twilio send failed: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# AlertDispatcher
# --------------------------------------------------------------------------- #
class AlertDispatcher:
    """Reads alerts.yaml, applies routing + rate-limits, logs every attempt."""

    def __init__(self, cfg: dict[str, Any], log_path: Path | None = None) -> None:
        self.cfg = cfg
        self.log_path = log_path or Path("eta_engine/docs/alerts_log.jsonl")
        # Build per-level rate limiters
        rl = cfg.get("rate_limit", {}) or {}
        self._rl = {
            "info": _RateLimiter(int(rl.get("info_per_minute", 10))),
            "warn": _RateLimiter(int(rl.get("warn_per_minute", 5))),
            "critical": _RateLimiter(int(rl.get("critical_per_minute", 0))),
        }

    @classmethod
    def from_yaml(cls, path: Path, log_path: Path | None = None) -> AlertDispatcher:
        with path.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        return cls(cfg or {}, log_path=log_path)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def send(self, event: str, payload: dict[str, Any]) -> DispatchResult:
        now = time.time()
        routing = (self.cfg.get("routing", {}) or {}).get("events", {}) or {}
        spec = routing.get(event)
        if spec is None:
            result = DispatchResult(
                event=event,
                level="unknown",
                channels=[],
                delivered=[],
                blocked=[f"unknown event '{event}'"],
                ts=now,
            )
            self._log(result, payload)
            return result
        level = str(spec.get("level", "info"))
        channels = list(spec.get("channels", []))

        rl = self._rl.get(level) or self._rl["info"]
        if not rl.allow(now):
            result = DispatchResult(
                event=event,
                level=level,
                channels=channels,
                delivered=[],
                blocked=[f"rate_limited:{level}"],
                ts=now,
            )
            self._log(result, payload)
            return result

        delivered: list[str] = []
        blocked: list[str] = []
        for ch in channels:
            if not self._channel_enabled(ch):
                blocked.append(f"{ch}:disabled")
                continue
            if not self._creds_present(ch):
                blocked.append(f"{ch}:creds_missing")
                continue
            ok = self._deliver(ch, event, level, payload)
            if ok:
                delivered.append(ch)
            else:
                blocked.append(f"{ch}:send_failed")
        result = DispatchResult(
            event=event,
            level=level,
            channels=channels,
            delivered=delivered,
            blocked=blocked,
            ts=now,
        )
        self._log(result, payload)
        return result

    # ------------------------------------------------------------------ #
    # Channel wiring
    # ------------------------------------------------------------------ #
    def _channel_cfg(self, name: str) -> dict[str, Any]:
        return (self.cfg.get("channels", {}) or {}).get(name, {}) or {}

    def _channel_enabled(self, name: str) -> bool:
        return bool(self._channel_cfg(name).get("enabled", False))

    def _creds_present(self, name: str) -> bool:
        ch = self._channel_cfg(name)
        keys = (ch.get("env_keys", {}) or {}).values()
        if not keys:
            return True
        return all(bool(os.environ.get(k)) for k in keys)

    def _env(self, channel: str, logical_key: str) -> str:
        """Look up the env var name from channels.<ch>.env_keys.<logical_key>, then read os.environ."""
        env_map = self._channel_cfg(channel).get("env_keys") or {}
        env_name = env_map.get(logical_key, "")
        return os.environ.get(env_name, "")

    def _format_text(self, event: str, level: str, payload: dict[str, Any]) -> tuple[str, str]:
        title = f"APEX {level.upper()} — {event}"
        # Keep body compact but informative; payload is small.
        lines = []
        for k, v in payload.items():
            lines.append(f"{k}: {v}")
        body = "\n".join(lines) if lines else json.dumps(payload, default=str)
        return title, body

    def _deliver(self, channel: str, event: str, level: str, payload: dict[str, Any]) -> bool:
        """Dispatch to the concrete transport. Returns True on success."""
        title, body = self._format_text(event, level, payload)

        if channel == "pushover":
            user = self._env("pushover", "user")
            token = self._env("pushover", "token")
            priority_map = self._channel_cfg("pushover").get("priority_map") or {}
            priority = int(priority_map.get(level, 0))
            return _send_pushover(user, token, title, body, priority)

        if channel == "email":
            ch = self._channel_cfg("email")
            smtp_host = self._env("email", "smtp_host")
            smtp_port_str = self._env("email", "smtp_port")
            smtp_user = self._env("email", "smtp_user")
            smtp_pass = self._env("email", "smtp_pass")
            try:
                smtp_port = int(smtp_port_str) if smtp_port_str else 587
            except ValueError:
                smtp_port = 587
            from_addr = str(ch.get("from") or smtp_user)
            to_addr = str(ch.get("to") or "")
            if not to_addr:
                logger.warning("email channel has no recipient configured")
                return False
            return _send_email(
                smtp_host,
                smtp_port,
                smtp_user,
                smtp_pass,
                from_addr,
                to_addr,
                title,
                body,
            )

        if channel == "sms":
            ch = self._channel_cfg("sms")
            sid = self._env("sms", "twilio_sid")
            token = self._env("sms", "twilio_token")
            from_number = self._env("sms", "from_number")
            to_number = str(ch.get("to_number") or "")
            if not to_number or to_number.upper().count("X") >= 5:
                logger.warning("sms channel has no valid to_number configured")
                return False
            sms_body = f"{title}\n{body}"[:1600]
            return _send_sms(sid, token, from_number, to_number, sms_body)

        logger.warning("unknown channel in routing: %s", channel)
        return False

    def _log(self, result: DispatchResult, payload: dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": result.ts,
            "event": result.event,
            "level": result.level,
            "channels": result.channels,
            "delivered": result.delivered,
            "blocked": result.blocked,
            "payload": payload,
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
