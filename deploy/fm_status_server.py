"""FastAPI service exposing the Force Multiplier status contract."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

app = FastAPI(title="ETA Force Multiplier Status")


def _force_multiplier_status() -> dict[str, Any]:
    from eta_engine.brain.multi_model import force_multiplier_status

    return dict(force_multiplier_status())


def _health_snapshot() -> dict[str, Any]:
    from eta_engine.scripts import force_multiplier_health

    path = force_multiplier_health.resolve_existing_path()
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


@app.get("/api/fm/status")
async def fm_status() -> JSONResponse:
    return JSONResponse(
        content=build_status_payload(),
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/health")
@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8422)
