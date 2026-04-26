"""APEX PREDATOR  //  obs.mcc_push_sender
==========================================
Sends VAPID-signed Web-Push notifications to phones / desktops that
subscribed via the JARVIS Master Command Center
(``POST /api/push/subscribe``).

Architecture
------------
The MCC stores subscriptions to ``~/.local/state/apex_predator/mcc_push_subscriptions.jsonl``.
This module reads them and -- if the optional :pypi:`pywebpush` dependency
is installed and ``MCC_VAPID_*`` env vars are set -- sends one push per
subscription. The :class:`apex_predator.obs.alert_dispatcher.AlertDispatcher`
delegates to :func:`send_to_all` when its routing config lists the
``mcc_push`` channel.

Optional dependency
-------------------
``pywebpush`` is **optional**. When it's missing or the VAPID env vars
are not set, this module no-ops with a logger warning -- the rest of
the alert dispatcher continues to work. Install with::

    pip install pywebpush

Required env vars to enable sending::

    MCC_VAPID_PUBLIC_KEY    base64url-encoded P-256 public key (87 chars)
    MCC_VAPID_PRIVATE_KEY   base64url-encoded P-256 private key (43 chars)
    MCC_VAPID_SUBJECT       mailto:<your-email> or https://<your-domain>

Generate a key pair once::

    python -c "from py_vapid import Vapid01; v = Vapid01(); v.generate_keys(); print(v.public_key, v.private_key)"

Then drop them in ``.env`` and restart the MCC service.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Path -- MUST stay in sync with scripts.jarvis_dashboard PUSH_SUBSCRIPTIONS.
PUSH_SUBSCRIPTIONS: Path = Path("~/.local/state/apex_predator/mcc_push_subscriptions.jsonl").expanduser()

# Default TTL for push messages on the push service (seconds).
DEFAULT_TTL: int = 3600

# Severity -> notification preset (level + sound hint via the SW).
_SEVERITY_DEFAULTS: dict[str, dict[str, Any]] = {
    "critical": {"urgency": "high", "ttl": 120, "tag": "mcc-critical"},
    "warn": {"urgency": "normal", "ttl": 1800, "tag": "mcc-warn"},
    "info": {"urgency": "low", "ttl": 3600, "tag": "mcc-info"},
}


@dataclass(frozen=True)
class PushResult:
    """Per-call summary suitable for the alert-dispatcher journal."""

    attempted: int
    delivered: int
    failed: int
    skipped: list[str]  # human-readable reasons (missing-deps / no-keys / no-subs)

    @property
    def ok(self) -> bool:
        return self.attempted > 0 and self.failed == 0


# ---------------------------------------------------------------------------
# Subscription IO
# ---------------------------------------------------------------------------


def read_subscriptions(path: Path | None = None) -> list[dict[str, Any]]:
    """Return every well-formed subscription record from the JSONL file."""
    p = path or PUSH_SUBSCRIPTIONS
    if not p.exists():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if "endpoint" in rec and "keys" in rec and isinstance(rec["keys"], dict):
            out.append(rec)
    return out


def _vapid_claims() -> dict[str, str] | None:
    """Build VAPID signer config from env. Returns None if incomplete."""
    pub = os.environ.get("MCC_VAPID_PUBLIC_KEY", "").strip()
    priv = os.environ.get("MCC_VAPID_PRIVATE_KEY", "").strip()
    sub = os.environ.get("MCC_VAPID_SUBJECT", "").strip()
    if not (pub and priv and sub):
        return None
    return {"public": pub, "private": priv, "sub": sub}


def _format_payload(severity: str, title: str, body: str, extra: dict[str, Any] | None) -> str:
    """Serialize the push payload the service worker will render."""
    payload = {
        "title": title,
        "body": body,
        "severity": severity,
        "url": "/",
    }
    if extra:
        payload["extra"] = extra
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


def send_to_all(
    severity: str,
    title: str,
    body: str,
    *,
    extra: dict[str, Any] | None = None,
    subscriptions_path: Path | None = None,
) -> PushResult:
    """Send one push per stored subscription. Safe when deps/env missing.

    Returns :class:`PushResult` with delivered / failed counts. Never
    raises -- a failed send is logged and counted, never propagated.
    """
    skipped: list[str] = []

    # 1. Optional dep
    try:
        from pywebpush import WebPushException, webpush  # type: ignore[import-not-found]
    except ImportError:
        skipped.append("pywebpush-not-installed")
        logger.info("mcc_push_sender: pywebpush not installed; no-op")
        return PushResult(attempted=0, delivered=0, failed=0, skipped=skipped)

    # 2. VAPID config
    vapid = _vapid_claims()
    if vapid is None:
        skipped.append("vapid-env-missing")
        logger.info("mcc_push_sender: MCC_VAPID_* env not set; no-op")
        return PushResult(attempted=0, delivered=0, failed=0, skipped=skipped)

    # 3. Subscriptions
    subs = read_subscriptions(subscriptions_path)
    if not subs:
        skipped.append("no-subscriptions")
        return PushResult(attempted=0, delivered=0, failed=0, skipped=skipped)

    sev_cfg = _SEVERITY_DEFAULTS.get(severity.lower(), _SEVERITY_DEFAULTS["info"])
    payload = _format_payload(severity, title, body, extra)

    delivered = 0
    failed = 0
    for sub in subs:
        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
                data=payload,
                vapid_private_key=vapid["private"],
                vapid_claims={"sub": vapid["sub"]},
                ttl=int(sev_cfg.get("ttl", DEFAULT_TTL)),
                headers={"Urgency": str(sev_cfg.get("urgency", "normal")), "Topic": str(sev_cfg.get("tag", ""))},
            )
            delivered += 1
        except WebPushException as exc:
            failed += 1
            # 410 GONE => subscription is dead; the next housekeeping cycle
            # can prune it. We don't prune inline (writes from a sender path
            # are an accident waiting to happen during a critical alert).
            logger.warning(
                "mcc_push_sender: WebPush failed (endpoint=%s..., status=%s): %s",
                sub["endpoint"][:60],
                getattr(exc.response, "status_code", "?"),
                exc,
            )
        except Exception as exc:  # noqa: BLE001 -- alerts must never crash the dispatcher
            failed += 1
            logger.warning(
                "mcc_push_sender: unexpected error for endpoint=%s...: %s",
                sub["endpoint"][:60],
                exc,
            )

    return PushResult(
        attempted=len(subs),
        delivered=delivered,
        failed=failed,
        skipped=skipped,
    )
