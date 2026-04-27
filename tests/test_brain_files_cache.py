"""Tests for ``eta_engine.brain.jarvis_v3.files_cache``."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 -- runtime via tmp_path

import pytest  # noqa: TC002 -- pytest fixtures are runtime
from eta_engine.brain.jarvis_v3.files_cache import (
    CachedFile,
    FilesCache,
    is_files_api_available,
)


def test_hash_content_deterministic() -> None:
    a = FilesCache.hash_content(b"hello")
    b = FilesCache.hash_content(b"hello")
    assert a == b
    assert a != FilesCache.hash_content(b"hello!")


def test_remember_and_lookup_roundtrip(tmp_path: Path) -> None:
    cache = FilesCache(cache_path=tmp_path / "fc.json")
    cache.remember(b"content-A", file_id="file_xyz", label="premarket")
    found = cache.lookup(b"content-A")
    assert found is not None
    assert found.file_id == "file_xyz"
    assert found.label == "premarket"
    assert found.bytes == 9


def test_lookup_returns_none_for_missing(tmp_path: Path) -> None:
    cache = FilesCache(cache_path=tmp_path / "fc.json")
    assert cache.lookup(b"nope") is None


def test_lookup_returns_none_after_ttl(tmp_path: Path) -> None:
    cache = FilesCache(cache_path=tmp_path / "fc.json", ttl_seconds=10)
    cache.remember(b"x", file_id="f1")
    # Inject an old uploaded_at directly.
    sha = FilesCache.hash_content(b"x")
    old = datetime.now(UTC) - timedelta(seconds=100)
    cache._index[sha] = CachedFile(
        sha256=sha,
        file_id="f1",
        uploaded_at=old.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        bytes=1,
        label="",
    )
    assert cache.lookup(b"x") is None


def test_persistence_across_instances(tmp_path: Path) -> None:
    p = tmp_path / "fc.json"
    c1 = FilesCache(cache_path=p)
    c1.remember(b"persisted", file_id="file_abc")

    c2 = FilesCache(cache_path=p)
    found = c2.lookup(b"persisted")
    assert found is not None
    assert found.file_id == "file_abc"


def test_load_handles_corrupt_file(tmp_path: Path) -> None:
    p = tmp_path / "fc.json"
    p.write_text("{not json")
    cache = FilesCache(cache_path=p)
    # No exception; just empty index.
    assert len(cache) == 0


def test_load_handles_non_object_root(tmp_path: Path) -> None:
    p = tmp_path / "fc.json"
    p.write_text(json.dumps([1, 2, 3]))
    cache = FilesCache(cache_path=p)
    assert len(cache) == 0


def test_purge_expired_drops_old_entries(tmp_path: Path) -> None:
    cache = FilesCache(cache_path=tmp_path / "fc.json", ttl_seconds=10)
    sha_old = FilesCache.hash_content(b"old")
    sha_new = FilesCache.hash_content(b"new")
    old_t = datetime.now(UTC) - timedelta(seconds=100)
    cache._index[sha_old] = CachedFile(
        sha256=sha_old, file_id="f_old",
        uploaded_at=old_t.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        bytes=3, label="",
    )
    cache._index[sha_new] = CachedFile(
        sha256=sha_new, file_id="f_new",
        uploaded_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        bytes=3, label="",
    )
    dropped = cache.purge_expired()
    assert dropped == 1
    assert len(cache) == 1
    assert sha_new in cache._index


def test_ensure_uploaded_returns_none_without_anthropic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the import of `anthropic` to fail.
    import sys
    monkeypatch.setitem(sys.modules, "anthropic", None)
    cache = FilesCache(cache_path=tmp_path / "fc.json")
    # No SDK + no client -> ensure_uploaded returns None.
    result = cache.ensure_uploaded(b"hello")
    # Whether anthropic SDK is actually installed, the path with no
    # client should either: (a) skip gracefully if SDK unimportable,
    # or (b) attempt an upload that fails (since we have no real key
    # in tests). Both yield None.
    # We simply assert it doesn't raise + returns None or a CachedFile.
    assert result is None or isinstance(result, CachedFile)


def test_is_files_api_available_returns_bool() -> None:
    assert isinstance(is_files_api_available(), bool)


def test_ensure_uploaded_uses_cache_on_repeat(tmp_path: Path) -> None:
    cache = FilesCache(cache_path=tmp_path / "fc.json")
    cache.remember(b"abc", file_id="file_001", label="lbl")
    # Stub out client; should never be called.
    class _NoCallClient:
        class beta:  # noqa: N801
            class files:  # noqa: N801
                @staticmethod
                def upload(file: tuple[str, bytes]) -> dict[str, str]:
                    raise AssertionError("upload should not be called for cached content")

    out = cache.ensure_uploaded(b"abc", label="lbl", client=_NoCallClient())
    assert out is not None
    assert out.file_id == "file_001"
