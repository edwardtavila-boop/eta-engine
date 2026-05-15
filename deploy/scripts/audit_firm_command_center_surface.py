from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from starlette.responses import Response

from eta_engine.deploy.scripts import dashboard_api

EXPECTED_SUMMARY_FIELDS = (
    "active_bots",
    "runtime_active_bots",
    "running_bots",
    "live_attached_bots",
    "live_in_trade_bots",
    "idle_live_bots",
    "inactive_runtime_bots",
    "staged_bots",
    "truth_status",
)
DEFAULT_ENDPOINT_CANDIDATES = (
    "http://127.0.0.1:8421/api/bot-fleet",
    "http://127.0.0.1:8000/api/bot-fleet",
    "http://127.0.0.1:8420/api/bot-fleet",
)


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _state_dir() -> Path:
    return _workspace_root() / "var" / "eta_engine" / "state"


def _audit_path() -> Path:
    return _state_dir() / "firm_command_center_surface_audit.json"


def _candidate_endpoints() -> list[str]:
    urls: list[str] = []
    override = str(os.getenv("ETA_FIRM_COMMAND_CENTER_SURFACE_AUDIT_URL") or "").strip()
    if override:
        urls.append(override)
    for url in DEFAULT_ENDPOINT_CANDIDATES:
        if url not in urls:
            urls.append(url)
    return urls


def _fetch_payload(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Cache-Control": "no-store"})
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _summary_view(payload: dict) -> dict:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {field: summary.get(field) for field in EXPECTED_SUMMARY_FIELDS}


def _fetch_first_payload(urls: list[str]) -> tuple[dict | None, str | None, dict[str, str]]:
    errors: dict[str, str] = {}
    for url in urls:
        try:
            return _fetch_payload(url), url, errors
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            errors[url] = f"{type(exc).__name__}: {exc}"
    return None, None, errors


def build_audit() -> dict:
    root_state = _state_dir()
    os.environ["ETA_STATE_DIR"] = str(root_state)

    direct_payload = dashboard_api.bot_fleet_roster(Response(), live_broker_probe=False)
    direct_summary = _summary_view(direct_payload)
    endpoint_candidates = _candidate_endpoints()
    served_payload, served_endpoint, served_errors = _fetch_first_payload(endpoint_candidates)
    served_error = None if served_payload is not None else "; ".join(
        f"{url} -> {message}" for url, message in served_errors.items()
    )

    served_summary = _summary_view(served_payload or {})
    served_summary_payload = (
        served_payload.get("summary")
        if isinstance(served_payload, dict) and isinstance(served_payload.get("summary"), dict)
        else {}
    )
    missing_summary_fields = [
        field
        for field in EXPECTED_SUMMARY_FIELDS
        if served_payload is not None and field not in served_summary_payload
    ]
    mismatched_fields = {
        field: {"served": served_summary.get(field), "direct": direct_summary.get(field)}
        for field in EXPECTED_SUMMARY_FIELDS
        if served_payload is not None and served_summary.get(field) != direct_summary.get(field)
    }
    served_truth_line = (
        str((served_payload or {}).get("truth_summary_line") or "")
        if served_payload is not None
        else ""
    )
    direct_truth_line = str(direct_payload.get("truth_summary_line") or "")
    truth_line_matches = served_payload is not None and served_truth_line == direct_truth_line

    audit = {
        "status": (
            "ok"
            if (
                served_payload is not None
                and not missing_summary_fields
                and not mismatched_fields
                and truth_line_matches
            )
            else "mismatch"
        ),
        "endpoint": served_endpoint,
        "candidate_endpoints": endpoint_candidates,
        "root_state_dir": str(root_state),
        "module_file": str(Path(dashboard_api.__file__).resolve()),
        "served_error": served_error,
        "served_errors": served_errors,
        "missing_summary_fields": missing_summary_fields,
        "mismatched_summary_fields": mismatched_fields,
        "served_truth_summary_line": served_truth_line,
        "direct_truth_summary_line": direct_truth_line,
        "truth_line_matches": truth_line_matches,
        "served_summary": served_summary,
        "direct_summary": direct_summary,
    }
    return audit


def main() -> int:
    audit = build_audit()
    output_path = _audit_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(json.dumps(audit, indent=2))
    return 0 if audit["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
