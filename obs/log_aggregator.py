"""Centralized log aggregator (Tier-3 #11, 2026-04-27).

Each bot + daemon writes its own log file, scattered across multiple
directories. When something goes wrong, ``tail -f`` 7 different files.

This module exposes a single ``EtaLogger`` class that bots can use to
get a structured-logging handler that:
  1. Writes the bot's own per-process log (for local debugging)
  2. ALSO appends a JSONL line to ``state/logs/eta.jsonl`` so a single
     ``tail -f`` shows the whole fleet

Usage in a bot::

    from eta_engine.obs.log_aggregator import get_eta_logger
    logger = get_eta_logger("mnq_bot", local_log_path=Path("logs/mnq.log"))
    logger.info("opened mnq long", extra={"price": 21450.0, "qty": 2})

Format of ``state/logs/eta.jsonl`` rows::

    {"ts": "2026-04-27T13:42:11Z", "level": "INFO", "bot": "mnq_bot",
     "msg": "opened mnq long", "extra": {"price": 21450.0, "qty": 2}}

Inspect with::

    Get-Content state\\logs\\eta.jsonl -Tail 50 -Wait    # PowerShell
    tail -f state/logs/eta.jsonl                          # bash
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AGGREGATE_PATH = ROOT / "state" / "logs" / "eta.jsonl"


class _JsonAggregateHandler(logging.Handler):
    """Logging handler that appends a JSONL line to a shared file."""

    def __init__(self, path: Path, bot_name: str) -> None:
        super().__init__()
        self.path = path
        self.bot_name = bot_name
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            row: dict[str, Any] = {
                "ts": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "bot": self.bot_name,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            # Pydantic-style: pull non-standard keys from extra.
            standard = {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            }
            extra = {k: v for k, v in record.__dict__.items() if k not in standard}
            if extra:
                row["extra"] = extra
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except Exception:  # noqa: BLE001
            # Never let logging failures kill the producer.
            self.handleError(record)


def get_eta_logger(
    bot_name: str,
    *,
    local_log_path: Path | None = None,
    aggregate_path: Path = DEFAULT_AGGREGATE_PATH,
    level: int = logging.INFO,
) -> logging.Logger:
    """Build a logger that writes to both a local file (optional) and
    the shared aggregate JSONL.

    Idempotent: if called twice with the same ``bot_name``, returns the
    same logger object without doubling the handlers.
    """
    name = f"eta.{bot_name}"
    logger = logging.getLogger(name)
    # Detect whether we already attached our aggregate handler
    if any(getattr(h, "_eta_aggregate", False) for h in logger.handlers):
        return logger
    logger.setLevel(level)

    # Local file handler (optional)
    if local_log_path is not None:
        local_log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(local_log_path, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))
        logger.addHandler(fh)

    # Aggregate handler -- always
    agg = _JsonAggregateHandler(aggregate_path, bot_name=bot_name)
    agg.setLevel(level)
    agg._eta_aggregate = True  # marker so we don't double-attach
    logger.addHandler(agg)

    return logger
