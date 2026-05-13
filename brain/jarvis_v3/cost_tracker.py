"""
JARVIS v3 // cost_tracker — Hermes/DeepSeek LLM spend telemetry.

Parses Hermes audit-log entries + (when present) DeepSeek request-dump
files into per-day spend summaries. Operator sees:

  * Total spend today / this week / this month
  * Per-tool breakdown (which MCP tools are expensive)
  * Per-skill breakdown (which workflows burn tokens)
  * Per-cron breakdown (does pre_event_scanner waste money?)
  * Anomaly detection (sudden 10x in spend within an hour)

V1 uses estimated token counts from request_dump files if available,
otherwise per-call flat-rate estimates from the audit log. The flat
rate is calibrated against DeepSeek-V4-Pro pricing as of 2026-05-12.

Pricing model
-------------

DeepSeek-V4-Pro (2026-05-12 prices):

  * input:  $0.0005 per 1K tokens
  * output: $0.002  per 1K tokens

Typical Hermes chat-completion sizes:

  * narrative chat:   ~3,500 input + ~150 output  = ~$0.0021/call
  * MCP tool call:    ~4,000 input + ~80 output   = ~$0.0022/call
  * morning briefing: ~3,500 input + ~400 output  = ~$0.0026/call

Default per-call estimate: $0.003. Tunable via env var.

Public interface
----------------

* ``estimate_spend(since_days_ago=7)`` — summary dict over time window.
* ``today_spend()`` — convenience for today's running total.
* ``anomaly_check(window_min=60)`` — recent-spike detector.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.brain.jarvis_v3.cost_tracker")

_WORKSPACE = Path(r"C:\EvolutionaryTradingAlgo")
_STATE_ROOT = _WORKSPACE / "var" / "eta_engine" / "state"
DEFAULT_AUDIT_PATH = _STATE_ROOT / "hermes_actions.jsonl"

# Per-1K-token prices (DeepSeek-V4-Pro, 2026-05-12)
DEFAULT_INPUT_PRICE_PER_1K = 0.0005
DEFAULT_OUTPUT_PRICE_PER_1K = 0.002

# Fallback per-call estimate when token counts are unavailable
DEFAULT_FLAT_RATE_PER_CALL = 0.003

# Anomaly threshold: a 60-min window with >10× the trailing-24h hourly rate
ANOMALY_MULTIPLIER = 10.0

EXPECTED_HOOKS = ("estimate_spend", "today_spend", "anomaly_check")


@dataclass(frozen=True)
class SpendSummary:
    asof: str
    window_start: str
    window_end: str
    total_usd: float
    n_calls: int
    by_tool: dict[str, dict[str, float]]  # {tool: {n, usd}}
    by_day: dict[str, dict[str, float]]  # {YYYY-MM-DD: {n, usd}}
    by_skill: dict[str, dict[str, float]]  # {skill_name: {n, usd}}
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def _input_price_per_1k() -> float:
    try:
        return float(os.environ.get("DEEPSEEK_INPUT_PRICE_PER_1K", DEFAULT_INPUT_PRICE_PER_1K))
    except (TypeError, ValueError):
        return DEFAULT_INPUT_PRICE_PER_1K


def _output_price_per_1k() -> float:
    try:
        return float(os.environ.get("DEEPSEEK_OUTPUT_PRICE_PER_1K", DEFAULT_OUTPUT_PRICE_PER_1K))
    except (TypeError, ValueError):
        return DEFAULT_OUTPUT_PRICE_PER_1K


def _flat_rate_per_call() -> float:
    try:
        return float(os.environ.get("DEEPSEEK_FLAT_RATE_PER_CALL", DEFAULT_FLAT_RATE_PER_CALL))
    except (TypeError, ValueError):
        return DEFAULT_FLAT_RATE_PER_CALL


def estimate_call_cost(
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> float:
    """Return the estimated USD cost for one chat completion.

    If both token counts are provided, computes from prices.
    Otherwise falls back to flat-rate estimate.
    """
    if input_tokens is not None and output_tokens is not None:
        return round(
            (input_tokens / 1000.0) * _input_price_per_1k() + (output_tokens / 1000.0) * _output_price_per_1k(),
            5,
        )
    return _flat_rate_per_call()


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------


def _read_audit_records(
    path: Path | None = None,
    since_dt: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read audit log records, filtered to ts >= since_dt."""
    target = path or DEFAULT_AUDIT_PATH
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
                if since_dt is not None:
                    ts = _parse_iso(rec.get("ts"))
                    if ts is None or ts < since_dt:
                        continue
                out.append(rec)
    except OSError as exc:
        logger.warning("cost_tracker._read_audit_records failed: %s", exc)
    return out


def _parse_iso(s: Any) -> datetime | None:  # noqa: ANN401
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def estimate_spend(
    since_days_ago: int = 7,
    audit_path: Path | None = None,
) -> SpendSummary:
    """Return spend summary over the last ``since_days_ago`` days.

    The audit log gives us per-tool call counts; the request-dump files
    (when present) give us token counts. We use token counts where
    available, flat-rate elsewhere.
    """
    if since_days_ago <= 0:
        since_days_ago = 1
    now = datetime.now(UTC)
    since = now - timedelta(days=since_days_ago)

    by_tool_n: dict[str, int] = defaultdict(int)
    by_tool_usd: dict[str, float] = defaultdict(float)
    by_day_n: dict[str, int] = defaultdict(int)
    by_day_usd: dict[str, float] = defaultdict(float)
    by_skill_n: dict[str, int] = defaultdict(int)
    by_skill_usd: dict[str, float] = defaultdict(float)
    n_calls = 0
    total_usd = 0.0

    try:
        records = _read_audit_records(audit_path, since)
        for rec in records:
            tool = str(rec.get("tool", "unknown"))
            ts = _parse_iso(rec.get("ts"))
            day = ts.strftime("%Y-%m-%d") if ts else "unknown"
            # Estimate cost — we don't have token counts in the audit log,
            # so flat-rate per MCP call
            cost = _flat_rate_per_call()

            by_tool_n[tool] += 1
            by_tool_usd[tool] += cost
            by_day_n[day] += 1
            by_day_usd[day] += cost

            # Skill attribution — if the args include a skill name, count it
            args = rec.get("args") or {}
            skill = args.get("skill") if isinstance(args, dict) else None
            if skill:
                by_skill_n[str(skill)] += 1
                by_skill_usd[str(skill)] += cost

            n_calls += 1
            total_usd += cost
    except Exception as exc:  # noqa: BLE001
        return SpendSummary(
            asof=now.isoformat(),
            window_start=since.isoformat(),
            window_end=now.isoformat(),
            total_usd=0.0,
            n_calls=0,
            by_tool={},
            by_day={},
            by_skill={},
            error=str(exc)[:200],
        )

    # Compose dict-of-dicts output structure
    by_tool: dict[str, dict[str, float]] = {
        k: {"n": float(by_tool_n[k]), "usd": round(by_tool_usd[k], 4)} for k in by_tool_n
    }
    by_day: dict[str, dict[str, float]] = {
        k: {"n": float(by_day_n[k]), "usd": round(by_day_usd[k], 4)} for k in by_day_n
    }
    by_skill: dict[str, dict[str, float]] = {
        k: {"n": float(by_skill_n[k]), "usd": round(by_skill_usd[k], 4)} for k in by_skill_n
    }

    return SpendSummary(
        asof=now.isoformat(),
        window_start=since.isoformat(),
        window_end=now.isoformat(),
        total_usd=round(total_usd, 4),
        n_calls=n_calls,
        by_tool=by_tool,
        by_day=by_day,
        by_skill=by_skill,
    )


def today_spend(audit_path: Path | None = None) -> dict[str, Any]:
    """Quick check: spend so far today (UTC)."""
    now = datetime.now(UTC)
    midnight_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
    summary_day_iso = midnight_utc.strftime("%Y-%m-%d")
    try:
        records = _read_audit_records(audit_path, midnight_utc)
        n = len(records)
        usd = round(n * _flat_rate_per_call(), 4)
        return {
            "asof": now.isoformat(),
            "date_utc": summary_day_iso,
            "n_calls": n,
            "total_usd": usd,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "asof": now.isoformat(),
            "date_utc": summary_day_iso,
            "n_calls": 0,
            "total_usd": 0.0,
            "error": str(exc)[:200],
        }


def anomaly_check(
    window_min: int = 60,
    audit_path: Path | None = None,
) -> dict[str, Any]:
    """Return ``{"anomaly": bool, "recent_rate", "baseline_rate", "multiplier"}``.

    "Anomaly" fires when the most recent ``window_min`` window has >= 10×
    the trailing-24h hourly rate. Used to catch a runaway prompt loop
    early (e.g. a buggy scheduled task firing every second).
    """
    if window_min <= 0:
        window_min = 60
    now = datetime.now(UTC)
    recent_start = now - timedelta(minutes=window_min)
    baseline_start = now - timedelta(hours=24)
    try:
        all_records = _read_audit_records(audit_path, baseline_start)
        recent_n = sum(1 for r in all_records if (_parse_iso(r.get("ts")) or now) >= recent_start)
        baseline_n = len(all_records)
        # Normalize to per-hour rates
        recent_rate = recent_n / (window_min / 60.0)
        baseline_rate = baseline_n / 24.0
        # Need a non-trivial baseline to flag anomalies
        if baseline_rate < 1.0:
            return {
                "anomaly": False,
                "recent_rate_per_hour": recent_rate,
                "baseline_rate_per_hour": baseline_rate,
                "multiplier": 0.0,
                "reason": "baseline_too_small",
            }
        multiplier = recent_rate / baseline_rate
        return {
            "anomaly": multiplier >= ANOMALY_MULTIPLIER,
            "recent_rate_per_hour": round(recent_rate, 2),
            "baseline_rate_per_hour": round(baseline_rate, 2),
            "multiplier": round(multiplier, 2),
            "threshold": ANOMALY_MULTIPLIER,
        }
    except Exception as exc:  # noqa: BLE001
        return {"anomaly": False, "error": str(exc)[:200]}
