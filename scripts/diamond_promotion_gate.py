"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_promotion_gate
================================================================
Codified gate for promoting bots to diamond status.

Why this exists
---------------
The 2026-05-12 kaizen cycle promoted ``m2k_sweep_reclaim`` to diamond
based on n=1,151 trades + 4/4 sessions positive + +0.46R avg. Strong
evidence — but the red-team review immediately surfaced that even the
existing "proven" diamond ``eur_sweep_reclaim`` has only 4 calendar days
of activity (not 4 months as the lifetime PnL suggested). The fleet is
running paper-soak at high frequency, so the sample sizes look large
but the TEMPORAL BREADTH is narrow.

Previous wave-4 made a similar mistake: it added an ``excluded_hours_utc``
filter to mgc based on n=11 stratification evidence that turned out to be
both statistically and structurally wrong. That cost a kaizen revert
cycle.

This script codifies the gate so future kaizen passes get the same
answer no matter who runs them.

Gating criteria
---------------

Hard gates (any FAIL → REJECT, do not promote):

  H1. ``n_trades >= 100``       (sample size for stable stats)
  H2. ``avg_r >= +0.20``        (per-trade edge worth burning capital on)
  H3. ``win_rate_pct >= 45``    (mechanic not entirely lopsided)
  H4. ``n_calendar_days >= 5``  (regime / day-of-week diversity)
  H5. ``n_sessions_positive >= 2`` (not session-concentrated)

Soft gates (any FAIL → NEEDS_MORE_DATA, recheck next cycle):

  S1. ``n_trades >= 500``       (high-confidence sample)
  S2. ``avg_r >= +0.40``        (clearly-strong edge)
  S3. ``n_calendar_days >= 14`` (two trading weeks min)
  S4. ``n_sessions_positive >= 3`` (regime breadth)
  S5. ``max_single_day_share < 0.50`` (no single day dominates)

PROMOTE = all hard + soft gates pass.
NEEDS_MORE_DATA = hard gates pass but >=1 soft gate fails.
REJECT = any hard gate fails.

Output
------

- stdout report (human-readable per-bot scorecard)
- ``var/eta_engine/state/diamond_promotion_gate_latest.json`` receipt
- exit 0 = at least one PROMOTE candidate or all green;
  exit 0 = no candidates found (not an error — most cycles will have none)

Usage
-----

::

    python -m eta_engine.scripts.diamond_promotion_gate
    python -m eta_engine.scripts.diamond_promotion_gate --json
    python -m eta_engine.scripts.diamond_promotion_gate --include-existing

The ``--include-existing`` flag also evaluates current diamonds against
the gate so you can see which existing diamonds would PASS today's
criteria vs grandfathered ones. (Existing diamonds are never demoted
by this script — the gate is promotion-only. Demotion is the
``diamond_falsification_watchdog`` job.)
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
TRADE_CLOSES_CANONICAL = (
    WORKSPACE_ROOT / "var" / "eta_engine" / "state"
    / "jarvis_intel" / "trade_closes.jsonl"
)
TRADE_CLOSES_LEGACY = (
    WORKSPACE_ROOT / "eta_engine" / "state"
    / "jarvis_intel" / "trade_closes.jsonl"  # noqa: ERA001  (canonical archive — see HISTORICAL-PATH-OK)
)
OUT_LATEST = (
    WORKSPACE_ROOT / "var" / "eta_engine" / "state"
    / "diamond_promotion_gate_latest.json"
)

# Bot ids that are not real bots (internal layer-propagation events,
# pseudo-rows). Exclude from promotion analysis.
INTERNAL_BOT_IDS = frozenset({"t1", "propagate_bot"})

# Minimum sample size before a bot is even considered a candidate.
MIN_SAMPLE_FOR_CONSIDERATION = 50


# ────────────────────────────────────────────────────────────────────
# Gates
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Gate:
    name: str
    description: str
    required: float | int
    severity: str  # "hard" or "soft"


HARD_GATES = (
    Gate("H1_n_trades", "n_trades >= 100", 100, "hard"),
    Gate("H2_avg_r", "avg_r >= +0.20", 0.20, "hard"),
    Gate("H3_win_rate", "win_rate_pct >= 45", 45.0, "hard"),
    Gate("H4_calendar_days", "n_calendar_days >= 5", 5, "hard"),
    Gate("H5_sessions_positive", "n_sessions_positive >= 2", 2, "hard"),
)

SOFT_GATES = (
    Gate("S1_n_trades_high", "n_trades >= 500", 500, "soft"),
    Gate("S2_avg_r_strong", "avg_r >= +0.40", 0.40, "soft"),
    Gate("S3_calendar_days_two_weeks", "n_calendar_days >= 14", 14, "soft"),
    Gate("S4_sessions_breadth", "n_sessions_positive >= 3", 3, "soft"),
    Gate("S5_no_single_day_dominance", "max_single_day_share < 0.50", 0.50, "soft"),
)


@dataclass
class BotScorecard:
    bot_id: str
    n_trades: int = 0
    cumulative_r: float = 0.0
    avg_r: float = 0.0
    win_rate_pct: float = 0.0
    n_calendar_days: int = 0
    n_sessions_positive: int = 0
    max_single_day_share: float = 0.0
    first_day: str = ""
    last_day: str = ""
    sessions_summary: dict[str, dict[str, float]] = field(default_factory=dict)
    hard_gate_results: dict[str, bool] = field(default_factory=dict)
    soft_gate_results: dict[str, bool] = field(default_factory=dict)
    verdict: str = "REJECT"
    is_existing_diamond: bool = False
    rationale: str = ""


# ────────────────────────────────────────────────────────────────────
# IO
# ────────────────────────────────────────────────────────────────────


def _read_trades_dual_source() -> list[dict[str, Any]]:
    """Read both canonical and legacy archives, dedupe on
    (signal_id, bot_id, ts, realized_r)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for path in (TRADE_CLOSES_CANONICAL, TRADE_CLOSES_LEGACY):
        if not path.exists():
            continue
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = "|".join([
                    str(rec.get("signal_id") or ""),
                    str(rec.get("bot_id") or ""),
                    str(rec.get("ts") or ""),
                    str(rec.get("realized_r") or ""),
                ])
                if key in seen:
                    continue
                seen.add(key)
                out.append(rec)
    return out


# ────────────────────────────────────────────────────────────────────
# Gate evaluation
# ────────────────────────────────────────────────────────────────────


def _score_bot(bot_id: str, trades: list[dict[str, Any]]) -> BotScorecard:
    chk = BotScorecard(bot_id=bot_id, n_trades=len(trades))

    rs: list[float] = []
    days: Counter = Counter()
    per_session: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        r = t.get("realized_r")
        if r is None:
            continue
        try:
            r_val = float(r)
        except (TypeError, ValueError):
            continue
        rs.append(r_val)
        ts = t.get("ts", "")
        if isinstance(ts, str) and len(ts) >= 10:
            days[ts[:10]] += 1
        per_session[str(t.get("session", "?"))].append(r_val)

    if not rs:
        chk.verdict = "REJECT"
        chk.rationale = "no parsable realized_r values"
        return chk

    chk.cumulative_r = round(sum(rs), 4)
    chk.avg_r = round(sum(rs) / len(rs), 4)
    chk.win_rate_pct = round(100.0 * sum(1 for r in rs if r > 0) / len(rs), 2)
    chk.n_calendar_days = len(days)
    chk.n_sessions_positive = sum(
        1 for srs in per_session.values()
        if srs and sum(srs) / len(srs) > 0
    )
    chk.max_single_day_share = round(
        max(days.values()) / len(rs) if days else 1.0,
        4,
    )
    sorted_days = sorted(days)
    if sorted_days:
        chk.first_day = sorted_days[0]
        chk.last_day = sorted_days[-1]
    chk.sessions_summary = {
        sess: {
            "n": len(srs),
            "cum_r": round(sum(srs), 4),
            "avg_r": round(sum(srs) / len(srs), 4),
            "wr_pct": round(100.0 * sum(1 for r in srs if r > 0) / len(srs), 2),
        }
        for sess, srs in per_session.items()
        if srs
    }

    # ── Evaluate gates ────────────────────────────────────────────────
    chk.hard_gate_results = {
        "H1_n_trades": chk.n_trades >= 100,
        "H2_avg_r": chk.avg_r >= 0.20,
        "H3_win_rate": chk.win_rate_pct >= 45.0,
        "H4_calendar_days": chk.n_calendar_days >= 5,
        "H5_sessions_positive": chk.n_sessions_positive >= 2,
    }
    chk.soft_gate_results = {
        "S1_n_trades_high": chk.n_trades >= 500,
        "S2_avg_r_strong": chk.avg_r >= 0.40,
        "S3_calendar_days_two_weeks": chk.n_calendar_days >= 14,
        "S4_sessions_breadth": chk.n_sessions_positive >= 3,
        "S5_no_single_day_dominance": chk.max_single_day_share < 0.50,
    }

    all_hard_pass = all(chk.hard_gate_results.values())
    all_soft_pass = all(chk.soft_gate_results.values())

    if not all_hard_pass:
        failed = [k for k, v in chk.hard_gate_results.items() if not v]
        chk.verdict = "REJECT"
        chk.rationale = f"hard gates failed: {', '.join(failed)}"
    elif not all_soft_pass:
        failed = [k for k, v in chk.soft_gate_results.items() if not v]
        chk.verdict = "NEEDS_MORE_DATA"
        chk.rationale = (
            f"hard gates pass; soft gates need work: {', '.join(failed)}"
        )
    else:
        chk.verdict = "PROMOTE"
        chk.rationale = (
            f"all 10 gates pass — n={chk.n_trades}, avg_r={chk.avg_r:+.3f}R, "
            f"wr={chk.win_rate_pct:.1f}%, {chk.n_calendar_days} days, "
            f"{chk.n_sessions_positive}/{len(chk.sessions_summary)} sessions positive"
        )
    return chk


def _load_existing_diamonds() -> set[str]:
    try:
        sys.path.insert(0, str(ROOT.parent))
        from eta_engine.feeds.capital_allocator import (  # noqa: PLC0415
            DIAMOND_BOTS,
        )
        return set(DIAMOND_BOTS)
    except ImportError:
        return set()


def run(*, include_existing: bool = False) -> dict[str, Any]:
    existing = _load_existing_diamonds()
    trades = _read_trades_dual_source()
    by_bot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        bid = t.get("bot_id")
        if not bid or bid in INTERNAL_BOT_IDS:
            continue
        by_bot[str(bid)].append(t)

    scorecards: list[BotScorecard] = []
    for bot_id, bot_trades in by_bot.items():
        is_existing = bot_id in existing
        # Skip non-existing bots below the minimum-consideration sample
        if not is_existing and len(bot_trades) < MIN_SAMPLE_FOR_CONSIDERATION:
            continue
        if is_existing and not include_existing:
            continue
        sc = _score_bot(bot_id, bot_trades)
        sc.is_existing_diamond = is_existing
        scorecards.append(sc)

    scorecards.sort(
        key=lambda s: (
            {"PROMOTE": 0, "NEEDS_MORE_DATA": 1, "REJECT": 2}[s.verdict],
            -s.cumulative_r,
        ),
    )
    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "n_scored": len(scorecards),
        "n_promote": sum(1 for s in scorecards if s.verdict == "PROMOTE"),
        "n_needs_more": sum(
            1 for s in scorecards if s.verdict == "NEEDS_MORE_DATA"
        ),
        "n_reject": sum(1 for s in scorecards if s.verdict == "REJECT"),
        "include_existing": include_existing,
        "candidates": [asdict(s) for s in scorecards],
    }
    try:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(
            json.dumps(summary, indent=2, default=str), encoding="utf-8",
        )
    except OSError as exc:
        print(f"WARN: write_latest failed: {exc}", file=sys.stderr)
    return summary


def _print_report(summary: dict[str, Any]) -> None:
    print("=" * 100)
    print(
        f" DIAMOND PROMOTION GATE  ({summary['ts']})  "
        f"{summary['n_promote']} PROMOTE / "
        f"{summary['n_needs_more']} NEEDS_MORE / "
        f"{summary['n_reject']} REJECT",
    )
    print("=" * 100)
    if not summary["candidates"]:
        print("  No candidates above MIN_SAMPLE_FOR_CONSIDERATION=50 trades.")
        print()
        return
    for s in summary["candidates"]:
        marker = ""
        if s.get("is_existing_diamond"):
            marker = " *DIAMOND*"
        v = s["verdict"]
        symbol = {
            "PROMOTE": "[OK]",
            "NEEDS_MORE_DATA": "[..]",
            "REJECT": "[X ]",
        }.get(v, "[?]")
        print(
            f"\n  {symbol}  {s['bot_id']:30s}  {v}{marker}",
        )
        print(
            f"        n={s['n_trades']:5d}  "
            f"cum_r={s['cumulative_r']:+8.2f}  "
            f"avg_r={s['avg_r']:+.4f}  "
            f"wr={s['win_rate_pct']:5.1f}%  "
            f"days={s['n_calendar_days']:3d} "
            f"({s.get('first_day','')}..{s.get('last_day','')})  "
            f"sessions+={s['n_sessions_positive']}",
        )
        print(f"        {s['rationale']}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--include-existing", action="store_true",
        help="also score current diamonds (informational; never demotes)",
    )
    args = ap.parse_args()
    summary = run(include_existing=args.include_existing)
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print_report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
