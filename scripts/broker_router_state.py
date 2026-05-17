from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from logging import Logger


EMPTY_RETRY_META: dict[str, Any] = {
    "attempts": 0,
    "last_attempt_ts": "",
    "last_reject_reason": "",
}


class BrokerRouterStateIO:
    """Own broker-router filesystem paths plus small sidecar helpers."""

    def __init__(
        self,
        *,
        state_root: Path,
        retry_meta_suffix: str,
        logger: Logger,
    ) -> None:
        self.state_root = Path(state_root)
        self.retry_meta_suffix = retry_meta_suffix
        self._logger = logger

        self.processing_dir = self.state_root / "processing"
        self.blocked_dir = self.state_root / "blocked"
        self.archive_dir = self.state_root / "archive"
        self.quarantine_dir = self.state_root / "quarantine"
        self.failed_dir = self.state_root / "failed"
        self.fill_results_dir = self.state_root / "fill_results"
        self.heartbeat_path = self.state_root / "broker_router_heartbeat.json"
        self.gate_pre_trade_path = self.state_root / "pre_trade_gate.json"
        self.gate_heat_state_path = self.state_root / "heat_state.json"
        self.gate_journal_path = self.state_root / "gate_journal.sqlite"

        for path in (
            self.processing_dir,
            self.blocked_dir,
            self.archive_dir,
            self.quarantine_dir,
            self.failed_dir,
            self.fill_results_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def retry_meta_path(self, target: Path) -> Path:
        return target.with_name(target.name + self.retry_meta_suffix)

    def load_retry_meta(self, target: Path) -> dict[str, Any]:
        """Read the retry-meta sidecar; any failure -> empty meta."""
        try:
            payload = json.loads(self.retry_meta_path(target).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return EMPTY_RETRY_META.copy()
        return {
            "attempts": int(payload.get("attempts", 0) or 0),
            "last_attempt_ts": str(payload.get("last_attempt_ts", "") or ""),
            "last_reject_reason": str(payload.get("last_reject_reason", "") or ""),
        }

    def save_retry_meta(self, target: Path, meta: dict[str, Any]) -> None:
        self.write_sidecar(self.retry_meta_path(target), meta)

    def clear_retry_meta(self, target: Path) -> None:
        with contextlib.suppress(OSError):
            self.retry_meta_path(target).unlink()

    def move_to_failed_with_meta(self, target: Path, retry_meta: dict[str, Any]) -> None:
        """Move target -> failed/ and persist meta alongside for forensics."""
        with contextlib.suppress(OSError):
            self.atomic_move(target, self.failed_dir / target.name)
        self.write_sidecar(
            self.failed_dir / (target.name + self.retry_meta_suffix),
            retry_meta,
        )
        self.clear_retry_meta(target)

    def atomic_move(self, src: Path, dst: Path) -> None:
        """Rename with parent-mkdir; raises OSError on collision/race."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)

    def write_sidecar(self, path: Path, payload: dict[str, Any]) -> None:
        """Write a small JSON sidecar; failures are logged not raised."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
        except OSError as exc:
            self._logger.warning("sidecar write failed %s: %s", path, exc)
