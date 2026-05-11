"""Tests for capture_rotation — verify hot/cold lifecycle + apply gate."""
from __future__ import annotations

import gzip
from datetime import date, timedelta
from pathlib import Path

import pytest

from eta_engine.scripts import capture_rotation as cr


@pytest.fixture()
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    ticks = tmp_path / "ticks"
    depth = tmp_path / "depth"
    logs = tmp_path / "logs"
    for d in (ticks, depth, logs):
        d.mkdir()
    monkeypatch.setattr(cr, "TICKS_DIR", ticks)
    monkeypatch.setattr(cr, "DEPTH_DIR", depth)
    monkeypatch.setattr(cr, "LOG_DIR", logs)
    monkeypatch.setattr(cr, "ROTATION_LOG", logs / "capture_rotation.jsonl")
    return {"ticks": ticks, "depth": depth, "logs": logs}


def _make_capture(d: Path, sym: str, file_date: date, content: bytes = b"x" * 50_000) -> Path:
    p = d / f"{sym}_{file_date.strftime('%Y%m%d')}.jsonl"
    p.write_bytes(content)
    return p


# ── _date_from_filename ───────────────────────────────────────────


def test_date_from_jsonl_filename() -> None:
    assert cr._date_from_filename(Path("MNQ_20260508.jsonl")) == date(2026, 5, 8)


def test_date_from_gz_filename() -> None:
    assert cr._date_from_filename(Path("MNQ_20260508.jsonl.gz")) == date(2026, 5, 8)


def test_date_from_bad_filename() -> None:
    assert cr._date_from_filename(Path("garbage.jsonl")) is None
    assert cr._date_from_filename(Path("MNQ_notadate.jsonl")) is None


# ── _gzip_in_place ────────────────────────────────────────────────


def test_gzip_in_place_creates_compressed_copy(isolated_dirs: dict) -> None:
    src = _make_capture(isolated_dirs["ticks"], "MNQ", date(2026, 1, 1))
    dst = cr._gzip_in_place(src)
    assert dst.exists()
    assert dst.suffix == ".gz"
    assert src.exists()  # gzip in-place doesn't delete src
    # Content roundtrip
    with gzip.open(dst, "rb") as f:
        assert f.read() == src.read_bytes()


# ── _process_kind: dry-run ────────────────────────────────────────


def test_process_kind_dryrun_does_not_mutate(isolated_dirs: dict) -> None:
    today = date(2026, 5, 11)
    old = today - timedelta(days=20)
    p = _make_capture(isolated_dirs["ticks"], "MNQ", old)
    out = cr._process_kind(isolated_dirs["ticks"], "ticks", today,
                            keep_days=14, cold_days=90, apply=False)
    assert p.exists()  # untouched
    assert out["n_compressed"] == 0
    assert out["actions"][0]["outcome"] == "would-compress"


# ── _process_kind: apply ──────────────────────────────────────────


def test_process_kind_apply_compresses_old(isolated_dirs: dict) -> None:
    today = date(2026, 5, 11)
    old = today - timedelta(days=20)
    p = _make_capture(isolated_dirs["ticks"], "MNQ", old, content=b"y" * 100_000)
    out = cr._process_kind(isolated_dirs["ticks"], "ticks", today,
                            keep_days=14, cold_days=90, apply=True)
    assert not p.exists()  # source deleted
    gz = p.with_suffix(p.suffix + ".gz")
    assert gz.exists()  # compressed sibling
    assert out["n_compressed"] == 1
    assert out["actions"][0]["outcome"] == "compressed"
    # Compression ratio sanity (highly-compressible repeated bytes)
    assert out["actions"][0]["compression_ratio"] > 5


def test_process_kind_apply_keeps_recent(isolated_dirs: dict) -> None:
    today = date(2026, 5, 11)
    recent = today - timedelta(days=5)
    p = _make_capture(isolated_dirs["ticks"], "MNQ", recent)
    out = cr._process_kind(isolated_dirs["ticks"], "ticks", today,
                            keep_days=14, cold_days=90, apply=True)
    assert p.exists()  # within hot window
    assert out["n_compressed"] == 0
    assert out["actions"][0]["outcome"] == "kept-hot"


def test_process_kind_apply_cold_archives_old_gz(isolated_dirs: dict) -> None:
    today = date(2026, 5, 11)
    very_old = today - timedelta(days=120)  # past cold-days=90
    raw = _make_capture(isolated_dirs["depth"], "MNQ", very_old)
    gz = cr._gzip_in_place(raw)
    raw.unlink()
    out = cr._process_kind(isolated_dirs["depth"], "depth", today,
                            keep_days=14, cold_days=90, apply=True)
    assert not gz.exists()
    assert (isolated_dirs["depth"] / "cold" / "2026" / "01" / gz.name).exists()
    assert out["n_cold_archived"] == 1


# ── _process_kind: missing dir ────────────────────────────────────


def test_process_kind_missing_dir(tmp_path: Path) -> None:
    out = cr._process_kind(tmp_path / "nope", "ticks", date(2026, 5, 11),
                            keep_days=14, cold_days=90, apply=True)
    assert out["n_compressed"] == 0
    assert out["note"] == "dir missing"


# ── main(): writes digest + respects --apply ──────────────────────


def test_main_dryrun_writes_digest(isolated_dirs: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    today = date.today()
    old = today - timedelta(days=20)
    _make_capture(isolated_dirs["ticks"], "MNQ", old)
    monkeypatch.setattr("sys.argv", ["capture_rotation"])
    rc = cr.main()
    assert rc == 0
    assert cr.ROTATION_LOG.exists()
    # Verify the file we created is still there (dry-run)
    assert any(p.name.startswith("MNQ_") for p in isolated_dirs["ticks"].iterdir())


def test_main_apply_actually_compresses(isolated_dirs: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    today = date.today()
    old = today - timedelta(days=30)
    p = _make_capture(isolated_dirs["depth"], "NQ", old, content=b"z" * 250_000)
    monkeypatch.setattr("sys.argv", ["capture_rotation", "--apply"])
    rc = cr.main()
    assert rc == 0
    assert not p.exists()
    assert p.with_suffix(p.suffix + ".gz").exists()
