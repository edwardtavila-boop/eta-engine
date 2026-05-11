"""
EVOLUTIONARY TRADING ALGO  //  strategies.l2_registry_adapter
=============================================================
Adapter that exposes L2 strategy promotion decisions in the same
shape as the per_bot_registry / supercharge verdict cache, so the
operator's existing dashboard picks them up automatically.

Why this exists
---------------
The L2 strategies live in a separate registry (l2_strategy_registry)
because their data shape (depth snapshots) doesn't match the
bar-based StrategyAssignment.  But the operator's daily dashboard
reads the supercharge verdict cache and shows per-bot GREEN /
YELLOW / RED summaries.  Without an adapter, L2 strategies are
invisible to the operator's daily workflow.

This module reads the latest l2_promotion_decisions.jsonl entries
and writes a verdict_cache.json entry per L2 bot in the same
schema the existing fleet uses.

Schema mapping
--------------
l2_promotion_decisions:
    {bot_id, current_status, recommended_status, notes}

verdict_cache (existing format):
    {bot_id: {"verdict": "GREEN"|"YELLOW"|"RED",
              "ts": iso,
              "reason": str,
              "extras": dict}}

Mapping rules:
    current_status == "live" AND recommended_status in (live, paper)
        → GREEN (running, no demotion recommended)
    current_status == "paper" AND recommended_status == "live"
        → GREEN (promotion candidate ready)
    current_status == "shadow" AND recommended_status == "paper"
        → YELLOW (promotion candidate but operator action needed)
    recommended_status == "retired"
        → RED (falsification triggered)
    otherwise
        → YELLOW (active but no promotion path)
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
PROMOTION_LOG = LOG_DIR / "l2_promotion_decisions.jsonl"
VERDICT_CACHE = LOG_DIR / "verdict_cache.json"


def _verdict_for(current: str, recommended: str) -> tuple[str, str]:
    """Map (current_status, recommended_status) → (verdict, reason)."""
    if recommended == "retired":
        return "RED", "falsification triggered"
    if current == "live" and recommended in ("live", "paper"):
        return "GREEN", f"live (rec: {recommended})"
    if current == "paper" and recommended == "live":
        return "GREEN", "promotion candidate ready for live cutover"
    if current == "shadow" and recommended == "paper":
        return "YELLOW", "promotion candidate ready for paper"
    if current == recommended:
        return "YELLOW", f"holding at {current} (no promotion criteria met)"
    return "YELLOW", f"transition: {current} → {recommended}"


def _read_latest_promotion_per_bot(*,
                                       _path: Path | None = None) -> dict[str, dict]:
    """Read promotion log, return latest entry per bot_id."""
    path = _path if _path is not None else PROMOTION_LOG
    latest: dict[str, dict] = {}
    if not path.exists():
        return latest
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                bot_id = rec.get("bot_id")
                if not bot_id:
                    continue
                # Keep the latest entry per bot
                latest[bot_id] = rec
    except OSError:
        return {}
    return latest


def sync_l2_to_verdict_cache(*, _promotion_path: Path | None = None,
                                _cache_path: Path | None = None) -> dict:
    """Read latest promotion decisions and merge them into
    verdict_cache.json.  Returns summary {n_synced, bot_ids}.
    """
    cache_path = _cache_path if _cache_path is not None else VERDICT_CACHE
    cache: dict[str, dict] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
        # Defensive: ensure dict shape
        if not isinstance(cache, dict):
            cache = {}

    latest = _read_latest_promotion_per_bot(_path=_promotion_path)
    synced: list[str] = []
    for bot_id, rec in latest.items():
        current = rec.get("current_status", "?")
        recommended = rec.get("recommended_status", "?")
        verdict, reason = _verdict_for(current, recommended)
        cache[bot_id] = {
            "verdict": verdict,
            "ts": rec.get("ts") or datetime.now(UTC).isoformat(),
            "reason": reason,
            "extras": {
                "source": "l2_registry_adapter",
                "current_status": current,
                "recommended_status": recommended,
                "notes": rec.get("notes", []),
            },
        }
        synced.append(bot_id)

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"l2_registry_adapter WARN: could not write verdict_cache: {e}",
              file=sys.stderr)

    return {
        "n_synced": len(synced),
        "bot_ids": synced,
        "ts": datetime.now(UTC).isoformat(),
    }


def main() -> int:
    """CLI entry-point — typically called as a daily cron after
    l2_promotion_evaluator finishes."""
    summary = sync_l2_to_verdict_cache()
    print(f"Synced {summary['n_synced']} L2 bots to verdict_cache: "
          f"{summary['bot_ids']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
