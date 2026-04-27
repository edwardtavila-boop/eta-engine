"""Safe state-file reader for the dashboard (Wave-7, 2026-04-27).

Replaces the bare ``_read_json`` in dashboard_api.py that 404s on missing
files. The dashboard must NEVER 500 / 404 on cold-start -- every endpoint
returns a recoverable JSON shape so the UI can render an empty-state
panel instead of a broken one.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def read_json_safe(path: Path) -> dict[str, Any]:
    """Read JSON from ``path``, or return a structured warning/error dict.

    Returns:
      * ``{...}``                       when file exists and parses
      * ``{"_warning": "no_data", ...}`` when file missing
      * ``{"_error_code": "state_corrupt", ...}`` when JSON parse fails
      * ``{"_error_code": "state_io_error", ...}`` when the file can't be read (permission, is a directory, etc.)

    Never raises. The dashboard relies on this to keep cold-start UI sane.
    """
    if not path.exists():
        return {"_warning": "no_data", "_path": str(path)}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("state_corrupt at %s: %s", path, exc)
        return {
            "_error_code": "state_corrupt",
            "_error_detail": str(exc),
            "_path": str(path),
        }
    except OSError as exc:
        logger.warning("state_io_error at %s: %s", path, exc)
        return {
            "_error_code": "state_io_error",
            "_error_detail": str(exc),
            "_path": str(path),
        }
