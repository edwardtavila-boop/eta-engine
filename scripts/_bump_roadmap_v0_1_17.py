"""One-shot: bump roadmap_state.json to v0.1.17.

Records the Tradovate authorization work:
  * New scripts/authorize_tradovate.py (reusable OAuth2 front door).
  * tradovate.py: TradovateVenue gains optional app_secret param (fixes a
    latent bug where `sec` and `password` both aliased to api_secret).
  * tests/test_authorize_tradovate.py (8 new tests) +
    tests/test_venues_tradovate_http.py (+2 regression tests).
  * docs/tradovate_auth_status.json artifact written per run.

Also: P9_ROLLOUT.live_tiny_size note is updated with the new credential
contract + runnable entrypoint. Task stays pending because actual creds
are still missing from SECRETS (user-action required).

Bumps tests_passing 752 -> 762.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"


def _find_task(phase: dict, task_id: str) -> dict:
    for t in phase["tasks"]:
        if t.get("id") == task_id:
            return t
    raise KeyError(f"task {task_id} not found in phase {phase.get('id')}")


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    sa["eta_engine_tests_passing"] = 762

    sa["eta_engine_tradovate_auth"] = {
        "timestamp_utc": now,
        "entrypoint": "eta_engine.scripts.authorize_tradovate",
        "endpoint_demo": "https://demo.tradovateapi.com/v1",
        "endpoint_live": "https://live.tradovateapi.com/v1",
        "status_artifact": "eta_engine/docs/tradovate_auth_status.json",
        "required_secrets": [
            "TRADOVATE_USERNAME",
            "TRADOVATE_PASSWORD",
            "TRADOVATE_APP_ID",
            "TRADOVATE_APP_SECRET",
            "TRADOVATE_CID",
        ],
        "exit_codes": {
            "0": "AUTHORIZED -- real OAuth2 succeeded",
            "1": "FAILED -- creds present, HTTP rejected",
            "2": "STUBBED -- creds missing, fell to stub path",
        },
        "last_run": {
            "result": "STUBBED",
            "reason": "5/5 required Tradovate creds missing from SECRETS",
            "auth_path": "stub",
            "endpoint": "https://demo.tradovateapi.com/v1",
        },
        "notes": (
            "TradovateVenue.authenticate now reads `sec` from the separately-"
            "injected app_secret (falling back to api_secret for backward-"
            "compat). This fixes the silent alias bug where user password and "
            "API-app secret were the same field. Until creds land in SECRETS "
            "(env / keyring / .env), every call will stub. P9_ROLLOUT."
            "live_tiny_size remains blocked on creds."
        ),
        "new_modules": [
            "eta_engine/scripts/authorize_tradovate.py",
        ],
        "modified_modules": [
            "eta_engine/venues/tradovate.py (added app_secret ctor param; sec payload sourced from app_secret)",
        ],
        "new_test_files": [
            "tests/test_authorize_tradovate.py (8 tests)",
        ],
        "new_tests_in_existing_files": [
            "tests/test_venues_tradovate_http.py (+2 tests: distinct-sec + backward-compat)",
        ],
        "tests_new": 10,
    }

    by_id = {p["id"]: p for p in state["phases"]}
    task = _find_task(by_id["P9_ROLLOUT"], "live_tiny_size")
    task["note"] = (
        "Runnable via `python -m eta_engine.scripts.authorize_tradovate`. "
        "Exit 2 STUBBED until SECRETS carries TRADOVATE_USERNAME, "
        "TRADOVATE_PASSWORD, TRADOVATE_APP_ID, TRADOVATE_APP_SECRET, "
        "TRADOVATE_CID. Last run: STUBBED (5/5 missing). Preflight still "
        "abort-on-red until creds land."
    )

    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"bumped roadmap_state.json to v0.1.17 at {now}")
    print("  tests_passing: 752 -> 762")
    print("  shared_artifact: eta_engine_tradovate_auth added")
    print("  P9_ROLLOUT.live_tiny_size note: updated with entrypoint")


if __name__ == "__main__":
    main()
