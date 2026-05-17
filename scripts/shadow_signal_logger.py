"""Shadow signal logger — wave-25 paper-routing observability.

When the supervisor's wave-25 conditional routing decides a signal goes
to ``paper`` (lifecycle = EVAL_PAPER, soft-DD trip, etc.) instead of
``live``, the signal otherwise vanishes — the bot saw the setup but
didn't take it, and there's no audit trail.

This module logs every paper-routed signal to
``var/eta_engine/state/jarvis_intel/shadow_signals.jsonl`` so:

  1. Operator can see "what my bots WANTED to do today" on the dashboard.
  2. Future kaizen runs can replay these signals against post-hoc bar
     data to compute hypothetical R-outcomes.
  3. Lifecycle promotions (EVAL_PAPER → EVAL_LIVE) become data-driven —
     a bot with 100 shadow signals over 2 weeks that show consistent
     positive hypothetical R is an obvious promotion candidate.

Schema (one record per line):

    {
      "ts": "2026-05-13T10:32:01.123456+00:00",
      "bot_id": "m2k_sweep_reclaim",
      "signal_id": "m2k_2026-05-13T10:32:00",
      "symbol": "M2K",
      "side": "BUY",
      "qty_intended": 1,
      "lifecycle": "EVAL_PAPER",
      "route_target": "paper",
      "route_reason": "lifecycle_eval_paper",
      "prospective_loss_usd": 250.0,
      "extra": {...}        # caller-provided context
    }

Schema is intentionally lightweight — no realized_r yet, that comes
later from the paper-engine replay.

Best-effort write: if disk is full or the path is bad, the supervisor
must NOT crash. Log to stderr and move on; the live order routing is
the safety-critical path.
"""
# ruff: noqa: ANN401  -- extra dict accepts any caller value
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eta_engine.scripts import workspace_roots

logger = logging.getLogger(__name__)

#: Canonical path for the shadow signal log. Sits under jarvis_intel/
#: alongside trade_closes.jsonl so audit consumers can find it
#: predictably.
SHADOW_SIGNALS_PATH = workspace_roots.ETA_JARVIS_SHADOW_SIGNALS_PATH


def log_shadow_signal(
    *,
    bot_id: str,
    signal_id: str,
    symbol: str,
    side: str,
    qty_intended: int,
    lifecycle: str,
    route_target: str,
    route_reason: str,
    prospective_loss_usd: float,
    extra: dict[str, Any] | None = None,
    path: Path | None = None,
) -> bool:
    """Append a shadow-signal record to the JSONL log.

    Returns True on successful write, False on any error (best-effort).
    """
    target = path if path is not None else SHADOW_SIGNALS_PATH
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "bot_id": bot_id,
        "signal_id": signal_id,
        "symbol": symbol,
        "side": side,
        "qty_intended": int(qty_intended),
        "lifecycle": lifecycle,
        "route_target": route_target,
        "route_reason": route_reason,
        "prospective_loss_usd": round(float(prospective_loss_usd), 2),
        "extra": extra or {},
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.warning(
            "shadow_signal_logger: write to %s failed: %s",
            target,
            exc,
        )
        return False
    return True


def read_shadow_signals(
    *,
    bot_filter: str | None = None,
    since: datetime | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Read shadow signals, optionally filtered by bot_id and since-ts.

    Returns a list of records (in file order). Empty list if file missing.
    """
    target = path if path is not None else SHADOW_SIGNALS_PATH
    if not target.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with target.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if bot_filter and rec.get("bot_id") != bot_filter:
                    continue
                if since is not None:
                    try:
                        rec_ts = datetime.fromisoformat(
                            str(rec.get("ts", "")).replace("Z", "+00:00"),
                        )
                    except ValueError:
                        continue
                    if rec_ts.tzinfo is None:
                        rec_ts = rec_ts.replace(tzinfo=UTC)
                    if rec_ts < since:
                        continue
                out.append(rec)
    except OSError as exc:
        logger.warning(
            "shadow_signal_logger: read from %s failed: %s",
            target,
            exc,
        )
    return out


def summarize_shadow_signals(
    *,
    since: datetime | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Per-bot summary: count of shadow signals, route_reason breakdown.

    Useful for the operator dashboard and the EVAL_PAPER → EVAL_LIVE
    promotion decision.
    """
    rows = read_shadow_signals(since=since, path=path)
    by_bot: dict[str, dict[str, Any]] = {}
    for r in rows:
        bot_id = str(r.get("bot_id") or "unknown")
        b = by_bot.setdefault(
            bot_id,
            {
                "n_signals": 0,
                "by_route_reason": {},
                "by_lifecycle": {},
                "first_ts": r.get("ts"),
                "last_ts": r.get("ts"),
            },
        )
        b["n_signals"] += 1
        rr = str(r.get("route_reason") or "unknown")
        b["by_route_reason"][rr] = b["by_route_reason"].get(rr, 0) + 1
        lc = str(r.get("lifecycle") or "unknown")
        b["by_lifecycle"][lc] = b["by_lifecycle"].get(lc, 0) + 1
        b["last_ts"] = r.get("ts")
    return {
        "ts": datetime.now(UTC).isoformat(),
        "n_total": sum(b["n_signals"] for b in by_bot.values()),
        "n_bots": len(by_bot),
        "by_bot": by_bot,
    }
