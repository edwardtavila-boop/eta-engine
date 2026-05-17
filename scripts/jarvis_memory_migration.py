"""Audit or seed canonical JARVIS second-brain memory.

JARVIS reads from ``var/eta_engine/state/memory`` and only falls back to the
legacy child-repo ``state/memory`` mirror when canonical files are
absent. This helper makes that migration repeatable without touching external
or broker state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402

MEMORY_FILES = ("episodic.jsonl", "semantic.json", "procedural.jsonl")


def _workspace_relative_guard(path: Path) -> Path:
    """Resolve ``path`` and ensure it remains inside the canonical workspace."""
    resolved = path.resolve()
    workspace = workspace_roots.WORKSPACE_ROOT.resolve()
    if resolved == workspace or workspace in resolved.parents:
        return resolved
    raise ValueError(f"path outside canonical workspace: {resolved}")


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, tmp)
    tmp.replace(destination)


def _memory_pairs(
    *,
    canonical_dir: Path | None = None,
    legacy_dir: Path | None = None,
) -> list[tuple[str, Path, Path]]:
    canonical = _workspace_relative_guard(canonical_dir or (workspace_roots.ETA_RUNTIME_STATE_DIR / "memory"))
    legacy = _workspace_relative_guard(legacy_dir or (workspace_roots.ETA_ENGINE_ROOT / "state" / "memory"))
    return [(name, legacy / name, canonical / name) for name in MEMORY_FILES]


def audit_memory(
    *,
    canonical_dir: Path | None = None,
    legacy_dir: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Return the migration plan without writing files."""
    files: list[dict[str, Any]] = []
    for name, source, destination in _memory_pairs(canonical_dir=canonical_dir, legacy_dir=legacy_dir):
        source_exists = source.exists() and source.is_file()
        destination_exists = destination.exists() and destination.is_file()
        source_bytes = _file_size(source)
        destination_bytes = _file_size(destination)
        source_hash = _sha256(source)
        destination_hash = _sha256(destination)
        if not source_exists or source_bytes <= 0:
            action = "missing_source"
        elif force:
            action = "copy" if source_hash != destination_hash else "already_current"
        elif destination_exists and destination_bytes > 0:
            action = "already_current" if source_hash == destination_hash else "canonical_present"
        else:
            action = "copy"
        files.append(
            {
                "file": name,
                "source": str(source),
                "destination": str(destination),
                "source_exists": source_exists,
                "destination_exists": destination_exists,
                "source_bytes": source_bytes,
                "destination_bytes": destination_bytes,
                "source_sha256": source_hash,
                "destination_sha256": destination_hash,
                "action": action,
            }
        )
    copy_count = sum(1 for item in files if item["action"] == "copy")
    missing_count = sum(1 for item in files if item["action"] == "missing_source")
    canonical_present_count = sum(1 for item in files if item["action"] == "canonical_present")
    if copy_count:
        status = "needs_migration"
    elif canonical_present_count:
        status = "canonical_present"
    elif missing_count == len(files):
        status = "no_legacy_memory"
    else:
        status = "current"
    return {
        "schema_version": 1,
        "source": "jarvis_memory_migration",
        "status": status,
        "copy_count": copy_count,
        "missing_source_count": missing_count,
        "canonical_present_count": canonical_present_count,
        "force": force,
        "files": files,
    }


def migrate_memory(
    *,
    canonical_dir: Path | None = None,
    legacy_dir: Path | None = None,
    dry_run: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Seed canonical memory from the legacy in-workspace memory directory."""
    report = audit_memory(canonical_dir=canonical_dir, legacy_dir=legacy_dir, force=force)
    if dry_run:
        report["dry_run"] = True
        report["copied_count"] = 0
        return report

    copied = 0
    for item in report["files"]:
        if item["action"] != "copy":
            continue
        source = Path(str(item["source"]))
        destination = Path(str(item["destination"]))
        _atomic_copy(source, destination)
        item["destination_exists"] = destination.exists()
        item["destination_bytes"] = _file_size(destination)
        item["destination_sha256"] = _sha256(destination)
        item["copied"] = item["source_sha256"] == item["destination_sha256"]
        copied += 1 if item["copied"] else 0
    report["dry_run"] = False
    report["copied_count"] = copied
    report["status"] = "migrated" if copied else report["status"]
    return report


def _render_text(report: dict[str, Any]) -> str:
    return (
        "jarvis_memory_migration "
        f"status={report.get('status')} dry_run={report.get('dry_run')} "
        f"copy_count={report.get('copy_count')} copied_count={report.get('copied_count')} "
        f"missing_source_count={report.get('missing_source_count')} "
        f"canonical_present_count={report.get('canonical_present_count')}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis_memory_migration")
    parser.add_argument("--json", action="store_true", help="print JSON report")
    parser.add_argument("--apply", action="store_true", help="copy missing canonical memory files")
    parser.add_argument("--force", action="store_true", help="overwrite canonical files when hashes differ")
    parser.add_argument("--canonical-dir", type=Path, default=None, help="override canonical memory dir")
    parser.add_argument("--legacy-dir", type=Path, default=None, help="override legacy memory dir")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = migrate_memory(
        canonical_dir=args.canonical_dir,
        legacy_dir=args.legacy_dir,
        dry_run=not args.apply,
        force=args.force,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
