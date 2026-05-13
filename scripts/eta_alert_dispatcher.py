"""ETA alert dispatcher — detect state changes that warrant operator attention.

Watches a small set of operator-meaningful state surfaces and emits a
human-readable event when they change since the last run. Designed to
be invoked from a scheduled task (every 5-15 minutes); each run
compares the current snapshot against the previous snapshot it wrote
itself, so it's stateless across reboots.

Detected events:
  - FM_BREAKER_TRIPPED            (fm_breaker.tripped flips True)
  - FM_BREAKER_RECOVERED          (fm_breaker.tripped flips False on a new day)
  - LAUNCH_READINESS_FLIPPED      (overall_verdict changes)
  - PROP_READY_GAINED <bot_id>    (new bot joins prop_ready_bots)
  - PROP_READY_LOST <bot_id>      (bot drops from prop_ready_bots)
  - SUPERVISOR_BOT_COUNT_CHANGED  (n_bots changes)
  - FM_DAILY_SPEND_OVER_HALF      (spent > 0.5 × cap, one-shot warning)

Output:
  - var/eta_engine/state/eta_events.jsonl        — append-only event log
  - var/eta_engine/state/eta_alert_snapshot.json — last seen state snapshot

The script never emits duplicate events. A flip from APPROVED to NO_GO
fires exactly one LAUNCH_READINESS_FLIPPED record; subsequent runs
that see the same NO_GO state stay silent until something else changes.

Future hook: when ETA_ALERT_WEBHOOK_URL is set, the dispatcher POSTs
each new event to the URL (JSON body) for Telegram/Slack relay. Today
the writes are local-only — operator runs `Get-Content eta_events.jsonl
-Tail 20` to see recent activity.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATE_DIR = Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state")
HEARTBEAT_PATH = STATE_DIR / "jarvis_intel" / "supervisor" / "heartbeat.json"
LEADERBOARD_PATH = STATE_DIR / "diamond_leaderboard_latest.json"
LAUNCH_READINESS_PATH = STATE_DIR / "diamond_prop_launch_readiness_latest.json"

EVENTS_LOG = STATE_DIR / "eta_events.jsonl"
SNAPSHOT_PATH = STATE_DIR / "eta_alert_snapshot.json"

_WEBHOOK_URL = os.environ.get("ETA_ALERT_WEBHOOK_URL", "").strip()
_WEBHOOK_TIMEOUT_S = 5.0


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _capture_snapshot() -> dict[str, Any]:
    """Build the current snapshot of operator-meaningful state."""
    hb = _load_json(HEARTBEAT_PATH)
    lb = _load_json(LEADERBOARD_PATH)
    lr = _load_json(LAUNCH_READINESS_PATH)

    fm_breaker = hb.get("fm_breaker") or {}
    snap: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "supervisor_n_bots": hb.get("n_bots"),
        "supervisor_tick_count": hb.get("tick_count"),
        "fm_breaker_tripped": bool(fm_breaker.get("tripped", False)),
        "fm_spent_today_usd": float(fm_breaker.get("spent_today_usd", 0.0) or 0.0),
        "fm_cap_usd": float(fm_breaker.get("cap_usd", 0.0) or 0.0),
        "launch_verdict": lr.get("overall_verdict"),
        "launch_summary": lr.get("summary"),
        "prop_ready_bots": sorted(lb.get("prop_ready_bots") or []),
        "n_prop_ready": int(lb.get("n_prop_ready") or 0),
    }
    return snap


def _emit_event(kind: str, **detail: object) -> dict[str, Any]:
    """Append one event to the JSONL + post to webhook if configured."""
    event = {
        "ts": datetime.now(UTC).isoformat(),
        "kind": kind,
        **detail,
    }
    try:
        EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
    except OSError:
        pass

    if _WEBHOOK_URL:
        try:
            req = urllib.request.Request(
                _WEBHOOK_URL,
                data=json.dumps(event).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT_S)  # noqa: S310 — operator-configured URL
        except (urllib.error.URLError, OSError):
            # Webhook failures must never crash the dispatcher.
            pass

    return event


def _diff_and_emit(prev: dict[str, Any], curr: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare snapshots; emit one event per detected change."""
    events: list[dict[str, Any]] = []

    # Breaker state changes
    prev_tripped = bool(prev.get("fm_breaker_tripped", False))
    curr_tripped = bool(curr.get("fm_breaker_tripped", False))
    if curr_tripped and not prev_tripped:
        events.append(
            _emit_event(
                "FM_BREAKER_TRIPPED",
                spent_today_usd=curr.get("fm_spent_today_usd"),
                cap_usd=curr.get("fm_cap_usd"),
            )
        )
    elif prev_tripped and not curr_tripped:
        events.append(
            _emit_event(
                "FM_BREAKER_RECOVERED",
                spent_today_usd=curr.get("fm_spent_today_usd"),
                cap_usd=curr.get("fm_cap_usd"),
            )
        )

    # Over-half-cap warning (one-shot)
    prev_over_half = float(prev.get("fm_spent_today_usd", 0.0) or 0.0) > (
        float(prev.get("fm_cap_usd", 0.0) or 0.0) * 0.5
    )
    curr_over_half = float(curr.get("fm_spent_today_usd", 0.0) or 0.0) > (
        float(curr.get("fm_cap_usd", 0.0) or 0.0) * 0.5
    )
    if curr_over_half and not prev_over_half:
        events.append(
            _emit_event(
                "FM_DAILY_SPEND_OVER_HALF",
                spent_today_usd=curr.get("fm_spent_today_usd"),
                cap_usd=curr.get("fm_cap_usd"),
            )
        )

    # Launch readiness verdict flip
    prev_verdict = prev.get("launch_verdict")
    curr_verdict = curr.get("launch_verdict")
    if prev_verdict and curr_verdict and prev_verdict != curr_verdict:
        events.append(
            _emit_event(
                "LAUNCH_READINESS_FLIPPED",
                prev=prev_verdict,
                curr=curr_verdict,
                summary=curr.get("launch_summary"),
            )
        )

    # PROP_READY membership changes
    prev_pr = set(prev.get("prop_ready_bots") or [])
    curr_pr = set(curr.get("prop_ready_bots") or [])
    for added in sorted(curr_pr - prev_pr):
        events.append(_emit_event("PROP_READY_GAINED", bot_id=added))
    for dropped in sorted(prev_pr - curr_pr):
        events.append(_emit_event("PROP_READY_LOST", bot_id=dropped))

    # Supervisor bot count changed
    prev_n = prev.get("supervisor_n_bots")
    curr_n = curr.get("supervisor_n_bots")
    if prev_n is not None and curr_n is not None and prev_n != curr_n:
        events.append(
            _emit_event(
                "SUPERVISOR_BOT_COUNT_CHANGED",
                prev=int(prev_n),
                curr=int(curr_n),
                delta=int(curr_n) - int(prev_n),
            )
        )

    return events


def main() -> int:
    prev = _load_json(SNAPSHOT_PATH)
    curr = _capture_snapshot()
    events = _diff_and_emit(prev, curr) if prev else []
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(curr, indent=2, default=str), encoding="utf-8")

    if events:
        print(f"emitted {len(events)} event(s):")
        for e in events:
            print(f"  {e['kind']}: {json.dumps({k: v for k, v in e.items() if k != 'ts'})}")
    else:
        if not prev:
            print("first run — captured initial snapshot, no events emitted")
        else:
            print("no state changes — silent run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
