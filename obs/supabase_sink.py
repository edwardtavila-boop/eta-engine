"""
EVOLUTIONARY TRADING ALGO  //  obs.supabase_sink
=================================
Best-effort Supabase mirror for the local decision journal.

Why this exists
---------------
The local JSONL at ``docs/decision_journal.jsonl`` is the canonical
source of truth — append-only, atomic on POSIX/NTFS, never blocks
trading on a network hiccup. This module forwards each event to
Supabase ``public.decision_journal`` for cross-machine queryability
(dashboards, drift detection, future CTA-grade audit).

Configuration
-------------
Reads two env vars; silently no-ops if either is missing. That keeps
the framework operable offline / pre-Supabase / pre-CTA::

    ETA_SUPABASE_URL       https://<project>.supabase.co
    ETA_SUPABASE_ANON_KEY  publishable / anon key

Failure model
-------------
Fire-and-forget. POST timeout = 5s. HTTP errors logged at WARNING but
never raised. Trading must never break because telemetry can't reach a
remote DB.

Adapted from ``firm/the_firm_complete/btc_firm/storage/supabase_journal.py``
to operate on the unified ``JournalEvent`` schema rather than per-event
table layouts.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.obs.decision_journal import JournalEvent

LOG = logging.getLogger("eta_engine.obs.supabase_sink")

_TABLE = "decision_journal"


def _env_url() -> str:
    return os.environ.get("ETA_SUPABASE_URL", "").strip().rstrip("/")


def _env_key() -> str:
    return os.environ.get("ETA_SUPABASE_ANON_KEY", "").strip()


def is_configured() -> bool:
    """True iff both env vars are populated."""
    return bool(_env_url() and _env_key())


def post_event(event: JournalEvent) -> bool:
    """POST one ``JournalEvent`` to Supabase. Returns True on success.

    Fire-and-forget: a False return is logged at WARNING and the caller
    keeps going. The local JSONL stays authoritative.
    """
    if not is_configured():
        return False

    url = f"{_env_url()}/rest/v1/{_TABLE}"
    key = _env_key()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

    row = {
        "ts": event.ts.isoformat(),
        "actor": str(event.actor),
        "intent": event.intent,
        "rationale": event.rationale,
        "gate_checks": event.gate_checks,
        "outcome": str(event.outcome),
        "links": event.links,
        "metadata": event.metadata,
    }
    body = json.dumps(row, default=str).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status >= 400:
                txt = resp.read().decode("utf-8", errors="replace")[:200]
                LOG.warning("supabase_sink POST status=%s body=%s", resp.status, txt)
                return False
            return True
    except urllib.error.HTTPError as exc:
        body_preview = exc.read()[:200] if hasattr(exc, "read") else b""
        LOG.warning("supabase_sink HTTPError %s: %s", exc.code, body_preview)
    except urllib.error.URLError as exc:
        LOG.warning("supabase_sink URLError: %r", exc.reason)
    except Exception as exc:
        LOG.warning("supabase_sink unexpected: %r", exc)
    return False
