"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_leaderboard
============================================================
Diamond-program competition + leaderboard.  TOP-3 earn PROP_READY.

Operator vision (2026-05-12 wave-15 mandate)
--------------------------------------------
"Make the strategies come alive and compete to be the best in their
fields. The top 3 earn the right to prop-firm trade with real
capital."

This script ranks every diamond by a composite score that rewards:
  - real R-edge that survives sample size + variance
  - temporal breadth (not just a single lucky day)
  - direction symmetry (works in both bull and bear regimes)
  - sizing discipline (USD risk inside the watchdog floor)
  - dual-basis health (both R and USD classifications healthy)

Composite score formula
-----------------------

::

    score = (
        edge_score        # R-edge × √n  (penalises tiny samples)
        × dual_basis_mul  # 1.0 if HEALTHY/HEALTHY, lower otherwise
        × sizing_mul      # 1.0 if SIZING_OK, 0.5 if BREACHED
        × temporal_mul    # n_days / 5 capped at 1.0
        × symmetry_bonus  # 1.0 SYMMETRIC, 0.9 DOMINANT, 0.7 ONLY_EDGE
    )

Where ``edge_score = avg_r * sqrt(n)`` is the standard signal-to-noise
ratio used in DSR / Sharpe-style metrics.  A strategy with avg_r=+0.5R
on n=100 trades scores 5.0 (= 0.5 × 10).  Same +0.5R on n=400 scores
10.0 — sample size matters.

PROP_READY designation
----------------------
The TOP_PROP_READY_N=3 highest-scoring bots get the PROP_READY badge.
A bot is eligible only if:
  - n_trades >= MIN_PROP_READY_N=100 (no thin-sample candidates)
  - avg_r >= +0.20R (must have real per-trade edge)
  - watchdog classification != CRITICAL on either basis
  - sizing audit verdict != SIZING_BREACHED

If fewer than 3 bots qualify, the badge goes to those that do (could be
0, 1, 2 or 3).  No floor-fill.

The PROP_READY set is exposed as PROP_READY_BOTS_LATEST in the JSON
receipt — downstream capital allocator can read this to direct prop-fund
capital only to the elite.

Output
------
- stdout: ranked leaderboard with PROP_READY annotations
- ``var/eta_engine/state/diamond_leaderboard_latest.json``
- exit 0

Run
---
::

    python -m eta_engine.scripts.diamond_leaderboard
    python -m eta_engine.scripts.diamond_leaderboard --json
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
OUT_LATEST = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "diamond_leaderboard_latest.json"

#: How many top bots get the PROP_READY badge.
TOP_PROP_READY_N = 3


def _console_help_description(text: str | None) -> str:
    """Return argparse help text that is safe on Windows cp1252 consoles."""
    return (text or "").encode("ascii", "replace").decode("ascii")

#: Minimum trades for PROP_READY eligibility.
MIN_PROP_READY_N = 100

#: Minimum avg_r for PROP_READY eligibility.
MIN_PROP_READY_AVG_R = 0.20
MIN_PROP_READY_PROFIT_FACTOR = 1.10
MIN_PROP_READY_REALIZED_PNL = 0.0

#: Sample-size cap so a 5,000-trade noise bot doesn't sneak past a
#: 200-trade strong-edge bot just on √n.
SQRT_N_CAP = 30.0  # = sqrt(900)


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _broker_truth_rank_bucket(e: LeaderboardEntry) -> int:
    """Rank broker-proven edge above lab-only optics.

    Buckets, high to low:
    4 = broker-positive and PF-proven
    3 = broker-positive but PF unavailable
    2 = lab-only with active signal
    1 = broker-rejected, broker-negative, or weak PF with active signal
    0 = no actionable signal yet
    """
    promotion_verdict = e.sources.get("promotion_verdict")
    broker_pnl = _float_or_none(e.sources.get("broker_total_realized_pnl"))
    broker_pf = _float_or_none(e.sources.get("broker_profit_factor"))
    has_signal = e.n_trades > 0 or abs(e.composite_score) > 0

    if broker_pnl is not None and broker_pnl <= MIN_PROP_READY_REALIZED_PNL:
        return 1 if has_signal else 0
    if broker_pnl is not None and broker_pnl > MIN_PROP_READY_REALIZED_PNL:
        if broker_pf is None:
            return 3
        if broker_pf < MIN_PROP_READY_PROFIT_FACTOR:
            return 1 if has_signal else 0
        return 4
    if broker_pf is not None and broker_pf < MIN_PROP_READY_PROFIT_FACTOR:
        return 1 if has_signal else 0
    if promotion_verdict and promotion_verdict != "PROMOTE":
        return 1 if has_signal else 0
    return 2 if has_signal else 0


def _leaderboard_rank_key(e: LeaderboardEntry) -> tuple[int, float, int, str]:
    return (
        _broker_truth_rank_bucket(e),
        e.composite_score,
        e.n_trades,
        e.bot_id,
    )


@dataclass
class LeaderboardEntry:
    bot_id: str
    n_trades: int = 0
    cum_r: float = 0.0
    avg_r: float = 0.0
    win_rate_pct: float = 0.0
    n_days: int = 0
    edge_score: float = 0.0
    dual_basis_mul: float = 1.0
    sizing_mul: float = 1.0
    temporal_mul: float = 1.0
    symmetry_bonus: float = 1.0
    composite_score: float = 0.0
    rank: int = 0
    prop_ready: bool = False
    prop_ready_disqualified_for: list[str] = field(default_factory=list)
    sources: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


# ────────────────────────────────────────────────────────────────────
# Sub-audit invocation
# ────────────────────────────────────────────────────────────────────


def _safe_run(name: str, fn: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
    try:
        return fn(**kwargs)
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: {name} failed: {exc}", file=sys.stderr)
        return {}


def _gather_signals() -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    """Pull all four diamond audits' per-bot status. Returns
    (sizing, watchdog, direction, promotion_gate)."""
    sys.path.insert(0, str(WORKSPACE_ROOT))
    from eta_engine.scripts import (  # noqa: PLC0415
        diamond_direction_stratify as ds,
    )
    from eta_engine.scripts import (  # noqa: PLC0415
        diamond_falsification_watchdog as wd,
    )
    from eta_engine.scripts import (  # noqa: PLC0415
        diamond_promotion_gate as pg,
    )
    from eta_engine.scripts import (  # noqa: PLC0415
        diamond_sizing_audit as sa,
    )

    sizing = {s["bot_id"]: s for s in _safe_run("sizing", sa.run).get("statuses", [])}
    watchdog = {s["bot_id"]: s for s in _safe_run("watchdog", wd.run_watchdog).get("statuses", [])}
    direction = {s["bot_id"]: s for s in _safe_run("direction", ds.run).get("statuses", [])}
    promotion = {
        c["bot_id"]: c
        for c in _safe_run(
            "promotion",
            pg.run,
            include_existing=True,
        ).get("candidates", [])
    }
    return sizing, watchdog, direction, promotion


# ────────────────────────────────────────────────────────────────────
# Scoring
# ────────────────────────────────────────────────────────────────────


def _dual_basis_multiplier(usd_cls: str | None, r_cls: str | None) -> float:
    """Both healthy = 1.0 ; one degraded = 0.7 ; one CRITICAL = 0.3 ;
    both CRITICAL = 0.0."""
    bands = {
        "HEALTHY": 1.0,
        "WATCH": 0.85,
        "WARN": 0.5,
        "CRITICAL": 0.0,
        "INCONCLUSIVE": 0.7,
    }
    u = bands.get(usd_cls or "INCONCLUSIVE", 0.7)
    r = bands.get(r_cls or "INCONCLUSIVE", 0.7)
    return min(u, r)  # weakest link sets the multiplier


def _sizing_multiplier(verdict: str | None) -> float:
    return {
        "SIZING_OK": 1.0,
        "SIZING_TIGHT": 0.85,
        "SIZING_FRAGILE": 0.6,
        "SIZING_BREACHED": 0.3,
        "INSUFFICIENT_DATA": 0.7,
    }.get(verdict or "INSUFFICIENT_DATA", 0.7)


def _symmetry_bonus(verdict: str | None) -> float:
    return {
        "SYMMETRIC": 1.00,
        "LONG_DOMINANT": 0.92,
        "SHORT_DOMINANT": 0.92,
        "LONG_ONLY_EDGE": 0.75,
        "SHORT_ONLY_EDGE": 0.75,
        "BIDIRECTIONAL_LOSS": 0.30,
        "INSUFFICIENT_DATA": 0.85,
    }.get(verdict or "INSUFFICIENT_DATA", 0.85)


def _temporal_multiplier(n_days: int) -> float:
    """Temporal breadth: linear up to 5 days, then full 1.0."""
    if n_days <= 0:
        return 0.0
    return min(1.0, n_days / 5.0)


def _build_entry(
    bot_id: str,
    sizing: dict[str, Any] | None,
    watchdog: dict[str, Any] | None,
    direction: dict[str, Any] | None,
    promotion: dict[str, Any] | None = None,
) -> LeaderboardEntry:
    e = LeaderboardEntry(bot_id=bot_id)

    # Pull canonical metrics from sizing audit (it has the cleanest n + cum_r)
    if sizing is not None:
        e.n_trades = int(sizing.get("n_trades_with_pnl") or 0)
        e.cum_r = float(sizing.get("cum_r") or 0.0)
        # Direction stratify also has avg_r/wr; prefer it for accuracy
        # since sizing audit excludes near-zero R rows
    if direction is not None:
        # Direction stratify carries the canonical avg_r + win_rate
        long_d = direction.get("long") or {}
        short_d = direction.get("short") or {}
        n_long = direction.get("n_long", 0)
        n_short = direction.get("n_short", 0)
        n_total = n_long + n_short
        long_avg = long_d.get("avg_r") or 0.0
        short_avg = short_d.get("avg_r") or 0.0
        if n_total > 0:
            e.n_trades = max(e.n_trades, n_total)
            e.avg_r = (n_long * long_avg + n_short * short_avg) / n_total
            long_wr = long_d.get("win_rate_pct") or 0.0
            short_wr = short_d.get("win_rate_pct") or 0.0
            e.win_rate_pct = (n_long * long_wr + n_short * short_wr) / n_total

    # Edge score: avg_r × √n, capped at SQRT_N_CAP
    sqrt_n = min(math.sqrt(max(e.n_trades, 0)), SQRT_N_CAP)
    e.edge_score = round(e.avg_r * sqrt_n, 4)

    # Dual-basis multiplier
    if watchdog is not None:
        e.dual_basis_mul = _dual_basis_multiplier(
            watchdog.get("classification_usd"),
            watchdog.get("classification_r"),
        )

    # Sizing multiplier
    if sizing is not None:
        e.sizing_mul = _sizing_multiplier(sizing.get("verdict"))

    # Temporal multiplier — pulled from direction stratify n_total via
    # day count would require another pass; we approximate by
    # promoting the existing diamond_promotion_gate's n_calendar_days
    # field if present.
    if direction is not None:
        # direction stratify doesn't carry n_days directly — we use a
        # heuristic: assume 1 day per 200 trades minimum, capped at 5.
        # This will be replaced by a real n_days lookup once the
        # gather pulls promotion gate output.
        n_total = e.n_trades
        # The leaderboard takes the temporal info from the promotion
        # gate when available (see _build_entry caller in run()).

    composite = (
        abs(e.edge_score)  # use abs so scoring is direction-agnostic
        * e.dual_basis_mul
        * e.sizing_mul
        * e.symmetry_bonus
        * e.temporal_mul
    )
    # Apply sign: negative edge → negative composite (so they sort to
    # the bottom of the leaderboard).
    if e.edge_score < 0:
        composite = -composite
    e.composite_score = round(composite, 4)

    # Save a sources dict for the JSON receipt + audit trail
    e.sources = {
        "sizing_verdict": (sizing or {}).get("verdict"),
        "watchdog_classification_usd": (watchdog or {}).get(
            "classification_usd",
        ),
        "watchdog_classification_r": (watchdog or {}).get("classification_r"),
        "direction_verdict": (direction or {}).get("verdict"),
    }
    if promotion is not None:
        e.n_days = int(promotion.get("n_calendar_days") or 0)
        e.temporal_mul = _temporal_multiplier(e.n_days)
        e.sources.update(
            {
                "promotion_verdict": promotion.get("verdict"),
                "promotion_rationale": promotion.get("rationale"),
                "broker_trade_count": promotion.get("n_trades"),
                "broker_total_realized_pnl": promotion.get("total_realized_pnl"),
                "broker_profit_factor": promotion.get("profit_factor"),
                "broker_gross_profit": promotion.get("gross_profit"),
                "broker_gross_loss": promotion.get("gross_loss"),
            },
        )
    return e


def _evaluate_prop_ready(entries: list[LeaderboardEntry]) -> None:
    """Set the prop_ready flag on the top-N entries that pass the
    eligibility gate.

    Wave-16 mandate: PROP_READY is IBKR-futures-only.  Spot bots
    (BTC/ETH/SOL via Alpaca) are auto-DQ'd via
    is_ibkr_futures_eligible() — a high-scoring spot bot must not
    earn real-capital routing through a broker the operator has
    cellared (POOL_SPLIT["spot"]=0.0).
    """
    sys.path.insert(0, str(WORKSPACE_ROOT))
    from eta_engine.feeds.capital_allocator import (  # noqa: PLC0415
        is_ibkr_futures_eligible,
    )

    # Sort by broker truth first, then composite. This keeps a broker-losing
    # high-R bot from looking like the strongest live candidate.
    entries.sort(key=_leaderboard_rank_key, reverse=True)
    for i, e in enumerate(entries, start=1):
        e.rank = i
        e.sources["broker_truth_rank_bucket"] = _broker_truth_rank_bucket(e)

    # Eligibility filter
    eligible: list[LeaderboardEntry] = []
    for e in entries:
        disqual = []
        if e.n_trades < MIN_PROP_READY_N:
            disqual.append(
                f"n_trades<{MIN_PROP_READY_N} (have {e.n_trades})",
            )
        if e.avg_r < MIN_PROP_READY_AVG_R:
            disqual.append(
                f"avg_r<{MIN_PROP_READY_AVG_R} (have {e.avg_r:+.3f})",
            )
        watchdog_usd = e.sources.get("watchdog_classification_usd")
        watchdog_r = e.sources.get("watchdog_classification_r")
        if watchdog_usd == "CRITICAL" or watchdog_r == "CRITICAL":
            disqual.append("watchdog CRITICAL")
        if e.sources.get("sizing_verdict") == "SIZING_BREACHED":
            disqual.append("sizing BREACHED")
        promotion_verdict = e.sources.get("promotion_verdict")
        if promotion_verdict and promotion_verdict != "PROMOTE":
            disqual.append(f"promotion gate {promotion_verdict}")
        broker_pnl = e.sources.get("broker_total_realized_pnl")
        if broker_pnl is not None:
            try:
                if float(broker_pnl) <= MIN_PROP_READY_REALIZED_PNL:
                    disqual.append(f"broker PnL<=0 (have ${float(broker_pnl):+.2f})")
            except (TypeError, ValueError):
                disqual.append("broker PnL unavailable")
        broker_pf = e.sources.get("broker_profit_factor")
        if broker_pf is not None:
            try:
                if float(broker_pf) < MIN_PROP_READY_PROFIT_FACTOR:
                    disqual.append(
                        f"broker profit factor<{MIN_PROP_READY_PROFIT_FACTOR:.2f} (have {float(broker_pf):.2f})",
                    )
            except (TypeError, ValueError):
                disqual.append("broker profit factor unavailable")
        # Wave-16: IBKR-futures-only mandate — spot bots can't go prop
        if not is_ibkr_futures_eligible(e.bot_id):
            disqual.append(
                "not IBKR-futures eligible (Alpaca spot is cellared)",
            )
        e.prop_ready_disqualified_for = disqual
        if not disqual:
            eligible.append(e)

    # Top-N of eligible get the badge
    for e in eligible[:TOP_PROP_READY_N]:
        e.prop_ready = True


# ────────────────────────────────────────────────────────────────────
# Runner
# ────────────────────────────────────────────────────────────────────


def run() -> dict[str, Any]:
    sys.path.insert(0, str(WORKSPACE_ROOT))
    from eta_engine.feeds.capital_allocator import (  # noqa: PLC0415
        DIAMOND_BOTS,
    )

    sizing, watchdog, direction, promotion = _gather_signals()

    entries: list[LeaderboardEntry] = []
    for bot_id in sorted(DIAMOND_BOTS):
        e = _build_entry(
            bot_id,
            sizing.get(bot_id),
            watchdog.get(bot_id),
            direction.get(bot_id),
            promotion.get(bot_id),
        )
        # Symmetry bonus from direction verdict
        e.symmetry_bonus = _symmetry_bonus(
            (direction.get(bot_id) or {}).get("verdict"),
        )
        # Recompute composite to incorporate the symmetry bonus
        # (which _build_entry left at default 1.0)
        sqrt_n = min(math.sqrt(max(e.n_trades, 0)), SQRT_N_CAP)
        e.edge_score = round(e.avg_r * sqrt_n, 4)
        composite = abs(e.edge_score) * e.dual_basis_mul * e.sizing_mul * e.symmetry_bonus * e.temporal_mul
        if e.edge_score < 0:
            composite = -composite
        # Apply temporal scaling — if direction stratify n_long+n_short
        # is high we assume reasonable temporal coverage. A real n_days
        # lookup would come from promotion_gate; this is a safe approximation.
        # For now, full credit.
        if not e.n_days:
            e.temporal_mul = 1.0 if e.n_trades >= 100 else (e.n_trades / 100.0)
        composite *= e.temporal_mul
        e.composite_score = round(composite, 4)
        # Build a one-line rationale
        e.rationale = (
            f"avg_r={e.avg_r:+.3f}R × sqrt({e.n_trades})={sqrt_n:.1f} "
            f"= edge {e.edge_score:+.2f}; "
            f"× sizing {e.sizing_mul:.2f} × dual_basis {e.dual_basis_mul:.2f} "
            f"× symmetry {e.symmetry_bonus:.2f} × temporal {e.temporal_mul:.2f}"
        )
        entries.append(e)

    _evaluate_prop_ready(entries)

    prop_ready = [e.bot_id for e in entries if e.prop_ready]

    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "n_diamonds": len(entries),
        "top_prop_ready_n": TOP_PROP_READY_N,
        "min_prop_ready_n_trades": MIN_PROP_READY_N,
        "min_prop_ready_avg_r": MIN_PROP_READY_AVG_R,
        "prop_ready_bots": prop_ready,
        "n_prop_ready": len(prop_ready),
        "leaderboard": [asdict(e) for e in entries],
    }
    try:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(
            json.dumps(summary, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"WARN: write_latest failed: {exc}", file=sys.stderr)
    return summary


def _print(summary: dict[str, Any]) -> None:
    print("=" * 130)
    print(
        f" DIAMOND LEADERBOARD  ({summary['ts']})  PROP_READY: {summary['n_prop_ready']}/{summary['top_prop_ready_n']}",
    )
    print("=" * 130)
    if summary["prop_ready_bots"]:
        print(
            "  >>> PROP_READY (earned right to real-capital trading): " + ", ".join(summary["prop_ready_bots"]),
        )
    else:
        print("  >>> No bots currently qualify for PROP_READY.")
    print()
    print(
        f" {'rank':>4s}  {'bot':25s}  {'n':>5s}  {'avg_R':>7s}  {'composite':>10s}  {'PROP':>5s}  rationale",
    )
    print("-" * 130)
    for e in summary["leaderboard"]:
        prop_s = "  ★  " if e["prop_ready"] else "     "
        rationale = e["rationale"][:60]
        print(
            f" {e['rank']:>4d}  {e['bot_id']:25s}  "
            f"{e['n_trades']:>5d}  "
            f"{e['avg_r']:>+7.3f}  "
            f"{e['composite_score']:>+10.3f}  "
            f"{prop_s}  "
            f"{rationale}",
        )
        if e.get("prop_ready_disqualified_for"):
            print(
                f"                                               DQ: {'; '.join(e['prop_ready_disqualified_for'])}",
            )
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=_console_help_description(__doc__))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    summary = run()
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
