"""
EVOLUTIONARY TRADING ALGO  //  brain.jarvis_v3.files_cache
==========================================================
Anthropic Files API client for the premarket-input snapshots.

Why
---
Every premarket digest call uploads ~80 KB of context (yesterday's
trades, journal tail, calibration table). Re-sending the same blob
N times a day burns 80 KB * N tokens of input context. Anthropic's
Files API lets us upload once and reference by file_id thereafter,
which the prompt-cache then recognizes as a stable input.

Public API
----------

* :class:`FilesCache` -- manages a local index of uploaded files
  keyed by content hash. ``ensure_uploaded(content)`` returns a
  ``file_id`` either from the cache or by uploading.
* The cache lives at ``~/.local/state/eta_engine/files_cache.json``
  and tracks ``{sha256: {file_id, uploaded_at, bytes, label}}``.

Optional dep on ``anthropic``: when not installed, the cache returns
``None`` and the caller is expected to fall back to inline content.

Determinism
-----------

* SHA-256 of the raw bytes is the cache key. Two callers uploading
  the same content get the same ``file_id`` back.
* TTL: 30 days by default (Anthropic Files retention is 90d, we
  refresh well before that).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = (
    Path("~/.local/state/eta_engine/files_cache.json").expanduser()
)
DEFAULT_TTL_SECONDS = 30 * 86_400  # 30 days


# ---------------------------------------------------------------------------
# Cache index entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CachedFile:
    """One uploaded blob's metadata."""
    sha256:       str
    file_id:      str
    uploaded_at:  str       # ISO 8601
    bytes:        int
    label:        str       # caller-supplied; useful for ops grep


# ---------------------------------------------------------------------------
# FilesCache facade
# ---------------------------------------------------------------------------


def is_files_api_available() -> bool:
    """True iff the ``anthropic`` SDK imports cleanly."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


class FilesCache:
    """Content-addressed Anthropic Files API cache."""

    def __init__(
        self,
        cache_path: Path | str | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        api_key: str | None = None,
    ) -> None:
        self._path = Path(cache_path).expanduser() if cache_path else DEFAULT_CACHE_PATH
        self._ttl  = ttl_seconds
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._index: dict[str, CachedFile] = {}
        self._load()

    # ------------------------------------------------------------------
    # Index I/O
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("FilesCache: load failed (%s); starting fresh", e)
            return
        if not isinstance(payload, dict):
            return
        for sha, row in payload.items():
            if isinstance(row, dict) and "file_id" in row:
                self._index[sha] = CachedFile(
                    sha256=sha,
                    file_id=row["file_id"],
                    uploaded_at=row.get("uploaded_at", ""),
                    bytes=int(row.get("bytes", 0)),
                    label=row.get("label", ""),
                )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            sha: {
                "file_id": f.file_id,
                "uploaded_at": f.uploaded_at,
                "bytes": f.bytes,
                "label": f.label,
            }
            for sha, f in self._index.items()
        }
        fd, tmp = tempfile.mkstemp(prefix=self._path.name + ".",
                                   dir=str(self._path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @staticmethod
    def hash_content(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def lookup(self, content: bytes) -> CachedFile | None:
        """Return the cached entry if present + not expired, else None."""
        sha = self.hash_content(content)
        entry = self._index.get(sha)
        if entry is None:
            return None
        # TTL check.
        if entry.uploaded_at:
            try:
                t = datetime.fromisoformat(entry.uploaded_at.replace("Z", "+00:00"))
                age = (datetime.now(UTC) - t).total_seconds()
                if age > self._ttl:
                    return None
            except ValueError:
                return None
        return entry

    def remember(
        self,
        content: bytes,
        file_id: str,
        label: str = "",
    ) -> CachedFile:
        """Insert + persist a (content, file_id) pair without uploading."""
        sha = self.hash_content(content)
        entry = CachedFile(
            sha256=sha,
            file_id=file_id,
            uploaded_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            bytes=len(content),
            label=label,
        )
        self._index[sha] = entry
        self._save()
        return entry

    def ensure_uploaded(
        self,
        content: bytes,
        label: str = "",
        client: Any | None = None,  # noqa: ANN401 -- Anthropic client is optional dep
    ) -> CachedFile | None:
        """Return a CachedFile, uploading via Anthropic Files API if needed.

        Returns ``None`` when the SDK isn't installed AND no client was
        passed -- the caller should fall back to inline content.
        """
        cached = self.lookup(content)
        if cached:
            return cached

        if client is None:
            try:
                import anthropic
            except ImportError:
                log.info(
                    "FilesCache: anthropic SDK not installed; "
                    "caller should inline content",
                )
                return None
            client = anthropic.Anthropic(api_key=self._api_key)

        try:
            # Anthropic SDK uploads via beta.files.upload(file=(name, bytes))
            resp = client.beta.files.upload(file=(label or "premarket.json", content))
            file_id = getattr(resp, "id", None) or resp.get("id")
        except Exception as e:  # noqa: BLE001 -- SDK raises a wide set
            log.warning("FilesCache: upload failed: %s", e)
            return None
        if not file_id:
            return None
        return self.remember(content, file_id, label=label)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def purge_expired(self) -> int:
        """Drop expired entries from the index. Returns count dropped."""
        now = time.time()
        dropped: list[str] = []
        for sha, entry in list(self._index.items()):
            if not entry.uploaded_at:
                continue
            try:
                t = datetime.fromisoformat(entry.uploaded_at.replace("Z", "+00:00"))
                age = now - t.timestamp()
                if age > self._ttl:
                    dropped.append(sha)
            except ValueError:
                dropped.append(sha)
        for sha in dropped:
            self._index.pop(sha, None)
        if dropped:
            self._save()
        return len(dropped)

    def __len__(self) -> int:
        return len(self._index)
