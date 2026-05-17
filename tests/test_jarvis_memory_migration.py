from __future__ import annotations

import json
from pathlib import Path

import pytest

from eta_engine.scripts import jarvis_memory_migration


@pytest.fixture(autouse=True)
def _workspace_root(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(jarvis_memory_migration.workspace_roots, "WORKSPACE_ROOT", tmp_path)


def _seed_memory(directory: Path, *, episodic: bytes = b'{"signal_id":"x"}\n') -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "episodic.jsonl").write_bytes(episodic)
    (directory / "semantic.json").write_text(
        json.dumps({"neutral+rth+long": {"pattern": "neutral+rth+long", "n_episodes": 1}}),
        encoding="utf-8",
    )


def test_dry_run_reports_needed_memory_migration(tmp_path: Path) -> None:
    legacy = tmp_path / "eta_engine" / "state" / "memory"
    canonical = tmp_path / "var" / "eta_engine" / "state" / "memory"
    _seed_memory(legacy)

    report = jarvis_memory_migration.migrate_memory(
        canonical_dir=canonical,
        legacy_dir=legacy,
        dry_run=True,
    )

    assert report["status"] == "needs_migration"
    assert report["dry_run"] is True
    assert report["copy_count"] == 2
    assert report["copied_count"] == 0
    assert not canonical.exists()


def test_apply_copies_missing_canonical_memory_with_matching_hashes(tmp_path: Path) -> None:
    legacy = tmp_path / "eta_engine" / "state" / "memory"
    canonical = tmp_path / "var" / "eta_engine" / "state" / "memory"
    _seed_memory(legacy)

    report = jarvis_memory_migration.migrate_memory(
        canonical_dir=canonical,
        legacy_dir=legacy,
        dry_run=False,
    )

    copied = [item for item in report["files"] if item.get("copied")]
    assert report["status"] == "migrated"
    assert report["copied_count"] == 2
    assert len(copied) == 2
    assert (canonical / "episodic.jsonl").read_bytes() == (legacy / "episodic.jsonl").read_bytes()
    assert (canonical / "semantic.json").read_bytes() == (legacy / "semantic.json").read_bytes()
    assert copied[0]["source_sha256"] == copied[0]["destination_sha256"]


def test_existing_non_empty_canonical_memory_is_not_overwritten(tmp_path: Path) -> None:
    legacy = tmp_path / "eta_engine" / "state" / "memory"
    canonical = tmp_path / "var" / "eta_engine" / "state" / "memory"
    _seed_memory(legacy, episodic=b'{"signal_id":"legacy"}\n')
    _seed_memory(canonical, episodic=b'{"signal_id":"canonical"}\n')

    report = jarvis_memory_migration.migrate_memory(
        canonical_dir=canonical,
        legacy_dir=legacy,
        dry_run=False,
    )

    episodic = next(item for item in report["files"] if item["file"] == "episodic.jsonl")
    assert episodic["action"] == "canonical_present"
    assert report["copied_count"] == 0
    assert (canonical / "episodic.jsonl").read_bytes() == b'{"signal_id":"canonical"}\n'


def test_force_overwrites_changed_canonical_memory(tmp_path: Path) -> None:
    legacy = tmp_path / "eta_engine" / "state" / "memory"
    canonical = tmp_path / "var" / "eta_engine" / "state" / "memory"
    _seed_memory(legacy, episodic=b'{"signal_id":"legacy"}\n')
    _seed_memory(canonical, episodic=b'{"signal_id":"canonical"}\n')

    report = jarvis_memory_migration.migrate_memory(
        canonical_dir=canonical,
        legacy_dir=legacy,
        dry_run=False,
        force=True,
    )

    assert report["status"] == "migrated"
    assert report["copied_count"] == 1
    assert (canonical / "episodic.jsonl").read_bytes() == b'{"signal_id":"legacy"}\n'


def test_rejects_paths_outside_canonical_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside" / "memory"

    with pytest.raises(ValueError, match="outside canonical workspace"):
        jarvis_memory_migration.audit_memory(canonical_dir=outside, legacy_dir=outside)


def test_main_json_dry_run(monkeypatch, capsys, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    legacy = tmp_path / "eta_engine" / "state" / "memory"
    canonical = tmp_path / "var" / "eta_engine" / "state" / "memory"
    _seed_memory(legacy)

    rc = jarvis_memory_migration.main(
        [
            "--json",
            "--canonical-dir",
            str(canonical),
            "--legacy-dir",
            str(legacy),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["dry_run"] is True
    assert payload["status"] == "needs_migration"
