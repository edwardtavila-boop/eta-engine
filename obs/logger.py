"""
EVOLUTIONARY TRADING ALGO  //  obs.logger
=============================
Structured JSON logger -- stdout + optional file sink.

Every log record is a single JSON line with ISO-8601 UTC timestamp. Sticky
context can be bound via `with_context(**kwargs)` returning a child logger.
Audit records are always written, regardless of the logger's level.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AUDIT_LEVEL = 60  # above CRITICAL


class _JsonFormatter(logging.Formatter):
    """Render each record as a JSON line with extra fields merged in."""

    RESERVED = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in self.RESERVED:
                continue
            if key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


logging.addLevelName(AUDIT_LEVEL, "AUDIT")


class StructuredLogger:
    """Stdlib logging.Logger wrapper that emits JSON lines with sticky context."""

    def __init__(
        self,
        name: str = "eta_engine",
        file_sink: Path | str | None = None,
        level: int = logging.INFO,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.context: dict[str, Any] = dict(context or {})
        self._logger = logging.getLogger(name)
        self._logger.setLevel(level)
        self._logger.propagate = False
        if not self._logger.handlers:
            fmt = _JsonFormatter()
            stdout_h = logging.StreamHandler(sys.stdout)
            stdout_h.setFormatter(fmt)
            self._logger.addHandler(stdout_h)
            if file_sink is not None:
                path = Path(file_sink)
                path.parent.mkdir(parents=True, exist_ok=True)
                file_h = logging.FileHandler(path, encoding="utf-8")
                file_h.setFormatter(fmt)
                self._logger.addHandler(file_h)

    def with_context(self, **kwargs: Any) -> StructuredLogger:
        merged = {**self.context, **kwargs}
        clone = StructuredLogger.__new__(StructuredLogger)
        clone.name = self.name
        clone._logger = self._logger
        clone.context = merged
        return clone

    def _emit(self, level: int, message: str, extra: dict[str, Any]) -> None:
        merged = {**self.context, **extra}
        self._logger.log(level, message, extra=merged)

    def info(self, message: str, **extra: Any) -> None:
        self._emit(logging.INFO, message, extra)

    def warn(self, message: str, **extra: Any) -> None:
        self._emit(logging.WARNING, message, extra)

    def error(self, message: str, **extra: Any) -> None:
        self._emit(logging.ERROR, message, extra)

    def critical(self, message: str, **extra: Any) -> None:
        self._emit(logging.CRITICAL, message, extra)

    def audit(self, message: str, **extra: Any) -> None:
        """Audit records always emit regardless of current level."""
        prev = self._logger.level
        try:
            self._logger.setLevel(min(prev, AUDIT_LEVEL))
            self._emit(AUDIT_LEVEL, message, extra)
        finally:
            self._logger.setLevel(prev)
