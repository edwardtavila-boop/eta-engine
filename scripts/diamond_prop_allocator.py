"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_prop_allocator
==============================================================
Confluence-aware capital allocation for the PROP_READY top-3
running 24/7 on the live prop-fund account.

Operator design (2026-05-12)
----------------------------
The top-3 PROP_READY diamonds run live on a $50K prop account.
Allocation between them isn't fixed - it adapts to which bot is
showing the strongest CONFLUENCE (composite leaderboard score).

Two modes, with a clear threshold between them:

  BALANCED MODE (default)
    All 3 bots show similar composite scores; no clear leader.
    Allocation: 33.3% / 33.3% / 33.3%  (each gets ~$16,500)

  DOMINANT MODE
    Top bot's composite is at least DOMINANCE_THRESHOLD x the
    median of the other two; it's clearly the highest-confluence
    play right now.
    Allocation: 50% / 25% / 25%  (top gets $25K, others $12.5K)

The mode is recomputed every leaderboard refresh (hourly via
ETA-Diamond-LeaderboardHourly).  Intra-hour, the supervisor reads
the most recent allocation receipt - it does NOT recompute the
allocation per trade (that would chatter on score noise).

What it does NOT do
-------------------
- Does NOT enforce prop-firm drawdown rules.  That's the job of
  diamond_prop_drawdown_guard.py.  This module is purely about
  CAPITAL SPLIT between PROP_READY bots; the drawdown guard sits
  ABOVE it and can halt trading entirely when DD limits approach.
- Does NOT route to a broker.  That's the supervisor's job; this
  module produces a JSON receipt the supervisor reads.
- Does NOT decide WHICH bots are PROP_READY.  That's
  diamond_leaderboard.py's job; this module assumes the
  prop_ready_bots set is already chosen.

Output
------
- stdout: allocation table with mode + per-bot weight + USD
- ``var/eta_engine/state/diamond_prop_allocator_latest.json``
- exit 0 always

Run
---
::

    python -m eta_engine.scripts.diamond_prop_allocator
    python -m eta_engine.scripts.diamond_prop_allocator --json
    python -m eta_engine.scripts.diamond_prop_allocator --account-size 100000
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
LEADERBOARD_PATH = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "diamond_leaderboard_latest.json"
OUT_LATEST = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "diamond_prop_allocator_latest.json"

#: Default prop-account size. Operator overrides via --account-size.
DEFAULT_ACCOUNT_SIZE = 50_000.0

#: When the top bot's composite score is at least this multiple of
#: the median of the other PROP_READY bots, switch from BALANCED
#: (33/33/33) to DOMINANT (50/25/25).  Set to 1.5x by default - 50%
#: dominance is a meaningful signal, lower thresholds chatter on noise.
DEFAULT_DOMINANCE_THRESHOLD = 1.5

#: Allocation weights per mode.  Only the top-3 are considered;
#: the remainder of the account stays in cash (or is reserved for
#: drawdown buffer per diamond_prop_drawdown_guard).
BALANCED_WEIGHTS = (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)
DOMINANT_WEIGHTS = (0.50, 0.25, 0.25)


@dataclass
class BotAllocation:
    bot_id: str
    rank: int
    composite_score: float
    weight_pct: float
    capital_usd: float


@dataclass
class AllocationReceipt:
    ts: str
    mode: str  # "BALANCED" or "DOMINANT" or "DEGRADED"
    account_size: float
    dominance_threshold: float
    top_score: float | None = None
    median_other_score: float | None = None
    dominance_ratio: float | None = None
    rationale: str = ""
    allocations: list[BotAllocation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# IO
# ────────────────────────────────────────────────────────────────────


def _load_leaderboard() -> dict[str, Any]:
    """Read the leaderboard receipt or return {} if missing/malformed."""
    if not LEADERBOARD_PATH.exists():
        return {}
    try:
        return json.loads(LEADERBOARD_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ────────────────────────────────────────────────────────────────────
# Core allocation logic
# ────────────────────────────────────────────────────────────────────


def _compute_mode(
    sorted_scores: list[float],
    threshold: float,
) -> tuple[str, float | None, float | None, float | None]:
    """Decide BALANCED vs DOMINANT mode.

    Returns (mode, top_score, median_other_score, dominance_ratio).
    """
    if len(sorted_scores) < 2:
        # Only one PROP_READY bot - it gets everything (degraded mode)
        return "DEGRADED", (sorted_scores[0] if sorted_scores else None), None, None
    top = sorted_scores[0]
    others = sorted_scores[1:]
    # Median of the OTHERS (not including top)
    sorted_others = sorted(others)
    n = len(sorted_others)
    median_other = sorted_others[n // 2] if n % 2 == 1 else (sorted_others[n // 2 - 1] + sorted_others[n // 2]) / 2.0
    # Avoid division by zero on a degenerate fleet
    if median_other <= 0:
        # If others are 0 or negative composite, top is trivially dominant
        return "DOMINANT", top, median_other, float("inf")
    ratio = top / median_other
    mode = "DOMINANT" if ratio >= threshold else "BALANCED"
    return mode, top, median_other, ratio


def compute_allocation(
    leaderboard: dict[str, Any],
    account_size: float = DEFAULT_ACCOUNT_SIZE,
    dominance_threshold: float = DEFAULT_DOMINANCE_THRESHOLD,
) -> AllocationReceipt:
    """Produce a per-bot capital allocation from a leaderboard receipt.

    The leaderboard is expected to have:
      - "prop_ready_bots": list of bot_id strings (top-3 by composite)
      - "leaderboard": list of dicts with bot_id + composite_score

    Behavior is graceful at every degenerate input:
      - empty leaderboard -> DEGRADED mode, no allocations
      - 1 PROP_READY bot   -> DEGRADED mode, that bot gets account_size
      - 2 PROP_READY bots  -> DEGRADED mode, scaled BALANCED (50/50)
      - 3+ PROP_READY bots -> normal BALANCED or DOMINANT
    """
    receipt = AllocationReceipt(
        ts=datetime.now(UTC).isoformat(),
        mode="DEGRADED",
        account_size=account_size,
        dominance_threshold=dominance_threshold,
    )

    prop_ready = leaderboard.get("prop_ready_bots") or []
    if not isinstance(prop_ready, list):
        receipt.rationale = "leaderboard.prop_ready_bots not a list"
        receipt.notes.append("malformed leaderboard receipt")
        return receipt
    if not prop_ready:
        receipt.rationale = "no PROP_READY bots designated by leaderboard"
        return receipt

    # Build bot -> composite_score lookup from the full leaderboard list.
    composite_lookup: dict[str, float] = {}
    for entry in leaderboard.get("leaderboard") or []:
        bid = entry.get("bot_id")
        score = entry.get("composite_score")
        if bid and isinstance(score, (int, float)):
            composite_lookup[bid] = float(score)

    # Sort PROP_READY bots by their composite score, descending.
    scored = [(bid, composite_lookup.get(bid, 0.0)) for bid in prop_ready]
    scored.sort(key=lambda p: -p[1])
    sorted_scores = [s for _, s in scored]

    # ── 1-bot edge case: degraded mode, that bot gets everything ────
    if len(scored) == 1:
        bid, score = scored[0]
        receipt.mode = "DEGRADED"
        receipt.top_score = score
        receipt.rationale = f"only 1 PROP_READY bot ({bid}); allocating 100% to it"
        receipt.allocations.append(
            BotAllocation(
                bot_id=bid,
                rank=1,
                composite_score=score,
                weight_pct=100.0,
                capital_usd=account_size,
            )
        )
        return receipt

    # ── 2-bot edge case: degraded mode, 50/50 ────────────────────────
    if len(scored) == 2:
        receipt.mode = "DEGRADED"
        receipt.top_score = sorted_scores[0]
        receipt.rationale = "only 2 PROP_READY bots; 50/50 split (degraded BALANCED)"
        for i, (bid, score) in enumerate(scored, start=1):
            receipt.allocations.append(
                BotAllocation(
                    bot_id=bid,
                    rank=i,
                    composite_score=score,
                    weight_pct=50.0,
                    capital_usd=account_size * 0.5,
                )
            )
        return receipt

    # ── 3+ bot case: normal BALANCED or DOMINANT ────────────────────
    # Use only the top 3 for allocation (matches operator design); any
    # additional PROP_READY beyond 3 is just listed in the receipt.
    top_three = scored[:3]
    extra = scored[3:]

    mode, top_score, median_other, ratio = _compute_mode(
        [s for _, s in top_three],
        dominance_threshold,
    )
    receipt.mode = mode
    receipt.top_score = top_score
    receipt.median_other_score = median_other
    receipt.dominance_ratio = round(ratio, 4) if ratio is not None and ratio != float("inf") else None

    weights = DOMINANT_WEIGHTS if mode == "DOMINANT" else BALANCED_WEIGHTS
    for i, ((bid, score), w) in enumerate(zip(top_three, weights, strict=True), start=1):
        receipt.allocations.append(
            BotAllocation(
                bot_id=bid,
                rank=i,
                composite_score=score,
                weight_pct=round(w * 100.0, 2),
                capital_usd=round(account_size * w, 2),
            )
        )

    # Surface any extra PROP_READY bots (not allocated capital) as notes
    for bid, score in extra:
        receipt.notes.append(
            f"PROP_READY bot {bid} (score {score:.2f}) not allocated (top-3 only routed to live capital)",
        )

    if mode == "DOMINANT":
        top_bid = top_three[0][0]
        receipt.rationale = (
            f"DOMINANT — {top_bid} composite {top_score:.2f} is "
            f"{ratio:.2f}x the median of the other 2 "
            f"({median_other:.2f}); top bot earns 50% of "
            f"${account_size:,.0f}"
        )
    else:
        receipt.rationale = (
            f"BALANCED — top composite {top_score:.2f} vs median other "
            f"{median_other:.2f} (ratio {ratio:.2f} < threshold "
            f"{dominance_threshold}); 33/33/33 split"
        )
    return receipt


# ────────────────────────────────────────────────────────────────────
# Runner
# ────────────────────────────────────────────────────────────────────


def run(
    account_size: float = DEFAULT_ACCOUNT_SIZE,
    dominance_threshold: float = DEFAULT_DOMINANCE_THRESHOLD,
) -> dict[str, Any]:
    leaderboard = _load_leaderboard()
    receipt = compute_allocation(
        leaderboard,
        account_size=account_size,
        dominance_threshold=dominance_threshold,
    )
    summary = asdict(receipt)
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
    print("=" * 100)
    print(
        f" DIAMOND PROP ALLOCATOR  ({summary['ts']})  mode={summary['mode']}  account=${summary['account_size']:,.0f}",
    )
    print("=" * 100)
    print(f" {summary['rationale']}")
    print()
    print(
        f" {'rank':>4s}  {'bot':25s}  {'composite':>10s}  {'weight%':>8s}  {'capital_USD':>13s}",
    )
    print("-" * 100)
    for a in summary["allocations"]:
        print(
            f" {a['rank']:>4d}  {a['bot_id']:25s}  "
            f"{a['composite_score']:>+10.3f}  "
            f"{a['weight_pct']:>7.1f}%  "
            f"${a['capital_usd']:>12,.2f}",
        )
    if summary.get("notes"):
        print()
        for n in summary["notes"]:
            print(f"  note: {n}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--account-size",
        type=float,
        default=DEFAULT_ACCOUNT_SIZE,
        help="Total prop account size in USD (default 50000)",
    )
    ap.add_argument(
        "--dominance-threshold",
        type=float,
        default=DEFAULT_DOMINANCE_THRESHOLD,
        help="top_score / median_other_score ratio that triggers DOMINANT mode",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    summary = run(
        account_size=args.account_size,
        dominance_threshold=args.dominance_threshold,
    )
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
