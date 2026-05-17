"""Dependency-light Force Multiplier status payload builder."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _health_snapshot() -> dict[str, Any]:
    from eta_engine.scripts import workspace_roots

    override = os.environ.get("ETA_FM_HEALTH_SNAPSHOT_PATH", "").strip()
    canonical = Path(override) if override else workspace_roots.ETA_FM_HEALTH_SNAPSHOT_PATH
    legacy = workspace_roots.ETA_LEGACY_FM_HEALTH_SNAPSHOT_PATH
    path = canonical if canonical.exists() or not legacy.exists() else legacy
    if not path.exists():
        return {
            "status": "missing",
            "path": str(path),
            "payload": None,
            "next_action": (
                "python -m eta_engine.scripts.force_multiplier_health "
                "--json-out C:\\EvolutionaryTradingAlgo\\var\\eta_engine\\state\\fm_health.json"
            ),
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - public status should fail soft
        return {
            "status": "unreadable",
            "path": str(path),
            "payload": None,
            "error": str(exc)[:200],
        }
    return {
        "status": "present",
        "path": str(path),
        "payload": payload,
    }


def build_status_payload() -> dict[str, Any]:
    snapshot = _health_snapshot()
    snapshot_payload = snapshot.get("payload")
    provider_list = snapshot_payload.get("providers", []) if isinstance(snapshot_payload, dict) else []
    providers = {
        str(provider.get("name", "")).strip() or str(provider.get("label", "")).strip(): {
            "available": bool(provider.get("ok")),
            "label": provider.get("label"),
            "message": provider.get("message"),
        }
        for provider in provider_list
        if isinstance(provider, dict)
    }
    payload: dict[str, Any] = {
        "mode": "force_multiplier",
        "status": "ok" if snapshot["status"] == "present" else "degraded",
        "providers": providers,
        "health_snapshot": snapshot,
    }
    if isinstance(snapshot_payload, dict):
        payload["all_ready"] = bool(snapshot_payload.get("all_ready"))
        payload["pass_count"] = snapshot_payload.get("pass_count")
        payload["total_count"] = snapshot_payload.get("total_count")
        payload["live"] = bool(snapshot_payload.get("live"))
    payload["generated_at"] = datetime.now(tz=UTC).isoformat()
    return payload
