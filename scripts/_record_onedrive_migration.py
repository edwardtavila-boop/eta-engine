"""One-shot: record the v3 framework OneDrive->projects migration in roadmap_state.json."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    sa = state["shared_artifacts"]

    # Update the locked layout decision with the new path
    decision = sa.get("codebase_layout_decision", {})
    decision["locked_at_utc"] = now
    decision["decision"] = "keep_separate_and_relocated"
    decision["projects"]["the_firm_eta_engine"]["path"] = "C:/Users/edwar/projects/mnq_bot/"
    decision["projects"]["the_firm_eta_engine"]["former_path"] = "OneDrive/The_Firm/eta_engine/"
    decision["projects"]["the_firm_eta_engine"]["migrated_at_utc"] = now
    decision["projects"]["the_firm_eta_engine"]["migration_reason"] = (
        "Plan9 share in cowork VM cannot walk NTFS junctions into OneDrive "
        "reparse-point folders; stat() returned I/O error. Moved out of "
        "OneDrive to a real directory so virtiofs can mount cleanly."
    )
    decision["rationale"] = (
        "architecturally distinct (portfolio vs framework); name collision only. "
        "Consolidation would break 648 portfolio tests and flatten two different "
        "design intents. 2026-04-17 addendum: v3 framework moved out of OneDrive "
        "to projects/mnq_bot/ to fix cowork Plan9 mount error."
    )
    sa["codebase_layout_decision"] = decision

    # Fresh rollup for the migration itself
    sa["v3_framework_onedrive_migration"] = {
        "timestamp_utc": now,
        "from_path": "C:/Users/edwar/OneDrive/The_Firm/eta_engine/",
        "to_path": "C:/Users/edwar/projects/mnq_bot/",
        "preserved_at": "C:/Users/edwar/OneDrive/The_Firm/eta_engine.migrated_20260417/",
        "files_copied": 2069,
        "bytes_copied_mb": 289.17,
        "excluded_trees": [
            ".venv",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".hypothesis",
            "pytest-cache-files-*",
            ".pnl_tmp",
        ],
        "reason": (
            "Fix RPC error -1 in Claude cowork: failed to mount via virtiofs "
            "because Plan9 couldn't stat the NTFS junction through the "
            "OneDrive reparse-point attribute."
        ),
        "followups": [
            "Delete OneDrive/The_Firm/eta_engine.migrated_20260417/ after operator confirms integrity",
            "Rebuild .venv at projects/mnq_bot/ if needed (uv sync)",
            "Update any external scripts or task schedulers referencing the OneDrive path",
        ],
    }

    state["last_updated"] = now
    state["last_updated_utc"] = now

    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"recorded v3_framework_onedrive_migration in roadmap_state.json at {now}")


if __name__ == "__main__":
    main()
