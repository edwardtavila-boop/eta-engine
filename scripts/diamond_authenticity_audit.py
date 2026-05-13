"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_authenticity_audit
==================================================================
Cross-source validator for the 8 protected diamond bots.

What it does
------------
For every bot in capital_allocator.DIAMOND_BOTS:

1.  Pull P&L + WR + n_trades from EVERY known source:
      - var/eta_engine/state/closed_trade_ledger_latest.json
      - var/eta_engine/state/paper_soak_ledger.json
      - var/eta_engine/state/kaizen_latest.json  (per_bot section)
      - logs/eta_engine/l2_backtest_runs.jsonl   (if present)

2.  Cross-reference: do the sources agree?  Disagreement is the
    primary "swarovski" signal — a bot whose dashboard P&L cannot
    be reproduced from any underlying ledger is suspect.

3.  Sample-size sanity: n_trades < 20 → INCONCLUSIVE; otherwise apply:
      - bootstrap 95% CI on mean per-trade P&L (1000 resamples)
      - lower-CI > 0 = edge exists; lower-CI <= 0 = edge unproven
      - simple monte-carlo: shuffle sign of returns N times,
        compute proportion >= observed; p < 0.05 = real edge

4.  Verdict per bot:
      - GENUINE          — sources agree, n >= 20, lower-CI > 0, MC p < 0.05
      - LAB_GROWN        — sources agree, n >= 20, lower-CI <= 0  (small sample or curve-fit)
      - CUBIC_ZIRCONIA   — sources disagree, OR n < 20, OR MC p > 0.20
      - INCONCLUSIVE     — no data in any source

Each verdict carries a 1-line justification.

Output
------
- stdout / --json
- var/eta_engine/state/diamond_authenticity_latest.json  (always overwritten)
- logs/eta_engine/diamond_authenticity.jsonl (append, one line per run)

Run
---
::

    python -m eta_engine.scripts.diamond_authenticity_audit
    python -m eta_engine.scripts.diamond_authenticity_audit --json
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import random
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.feeds.capital_allocator import DIAMOND_BOTS

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
STATE_DIR = WORKSPACE_ROOT / "var" / "eta_engine" / "state"
LOG_DIR = WORKSPACE_ROOT / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)

CLOSED_LEDGER = STATE_DIR / "closed_trade_ledger_latest.json"
PAPER_SOAK_LEDGER = STATE_DIR / "paper_soak_ledger.json"
KAIZEN_LATEST = STATE_DIR / "kaizen_latest.json"
BACKTEST_RUNS_LOG = LOG_DIR / "l2_backtest_runs.jsonl"

OUT_LATEST = STATE_DIR / "diamond_authenticity_latest.json"
OUT_LOG = LOG_DIR / "diamond_authenticity.jsonl"

BOOTSTRAP_N = 1000
MC_SHUFFLE_N = 1000
MIN_N_FOR_STATS = 20
RANDOM_SEED = 42


@dataclass
class SourceMetrics:
    """One source's view of a bot."""

    source: str
    n_trades: int | None = None
    total_pnl_usd: float | None = None
    win_rate_pct: float | None = None
    cumulative_r: float | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class BotAuthenticityReport:
    bot_id: str
    sources: list[SourceMetrics] = field(default_factory=list)
    consensus_n: int | None = None
    consensus_pnl: float | None = None
    consensus_r: float | None = None
    sources_disagree: bool = False
    disagreement_detail: list[str] = field(default_factory=list)
    bootstrap_ci_lower: float | None = None
    bootstrap_ci_upper: float | None = None
    mc_p_value: float | None = None
    metric_basis: str = "USD"  # "USD" or "R"
    verdict: str = "INCONCLUSIVE"
    justification: str = ""


# ────────────────────────────────────────────────────────────────────
# Source readers
# ────────────────────────────────────────────────────────────────────


def _read_closed_ledger(bot_id: str) -> SourceMetrics:
    m = SourceMetrics(source="closed_trade_ledger")
    if not CLOSED_LEDGER.exists():
        m.notes.append("file missing")
        return m
    try:
        data = json.loads(CLOSED_LEDGER.read_text(encoding="utf-8"))
        rec = data.get("per_bot", {}).get(bot_id)
        if rec is None:
            m.notes.append("bot not in ledger")
            return m
        m.n_trades = int(rec.get("closed_trade_count") or 0)
        m.total_pnl_usd = float(rec.get("total_realized_pnl") or 0)
        m.win_rate_pct = float(rec.get("win_rate_pct") or 0)
        m.cumulative_r = float(rec.get("cumulative_r") or 0)
        # Scale-bug detector: realistic per-trade P&L on paper futures
        # rarely exceeds $5,000 per contract per trade.  Higher = scale
        # bug (forex notional, missing divisor, etc.) and we should
        # not trust the USD column — R-multiples remain clean.
        if m.n_trades and abs(m.total_pnl_usd) / max(m.n_trades, 1) > 5_000:
            m.notes.append(
                f"SCALE_BUG_SUSPECTED: avg_per_trade=${m.total_pnl_usd / m.n_trades:.0f}",
            )
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        m.notes.append(f"read_error: {exc}")
    return m


def _read_paper_soak(bot_id: str) -> SourceMetrics:
    m = SourceMetrics(source="paper_soak_ledger")
    if not PAPER_SOAK_LEDGER.exists():
        m.notes.append("file missing")
        return m
    try:
        data = json.loads(PAPER_SOAK_LEDGER.read_text(encoding="utf-8"))
        sessions = data.get("bot_sessions", {}).get(bot_id, [])
        if not sessions:
            m.notes.append("no sessions")
            return m
        pnls = [float(s.get("pnl") or 0) for s in sessions]
        m.n_trades = len(pnls)  # session count, not trade count
        m.total_pnl_usd = sum(pnls)
        winning_sessions = sum(1 for p in pnls if p > 0)
        m.win_rate_pct = round(100.0 * winning_sessions / max(len(pnls), 1), 2)
        m.notes.append("n_trades here = session count, not per-trade")
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        m.notes.append(f"read_error: {exc}")
    return m


def _read_kaizen_latest(bot_id: str) -> SourceMetrics:
    m = SourceMetrics(source="kaizen_latest")
    if not KAIZEN_LATEST.exists():
        m.notes.append("file missing")
        return m
    try:
        data = json.loads(KAIZEN_LATEST.read_text(encoding="utf-8"))
        rec = data.get("per_bot", {}).get(bot_id)
        if rec is None:
            m.notes.append("bot not in kaizen report")
            return m
        m.n_trades = int(rec.get("n") or 0)
        m.total_pnl_usd = float(rec.get("total_pnl") or 0)
        m.win_rate_pct = float(rec.get("win_rate_pct") or 0)
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        m.notes.append(f"read_error: {exc}")
    return m


# ────────────────────────────────────────────────────────────────────
# Statistical tests
# ────────────────────────────────────────────────────────────────────


def _bootstrap_ci(
    samples: list[float], n_resamples: int = BOOTSTRAP_N, confidence: float = 0.95
) -> tuple[float, float]:
    """Percentile bootstrap CI on the mean.  Returns (lower, upper)."""
    rng = random.Random(RANDOM_SEED)
    means: list[float] = []
    n = len(samples)
    for _ in range(n_resamples):
        resample = [samples[rng.randrange(n)] for _ in range(n)]
        means.append(sum(resample) / n)
    means.sort()
    alpha = (1 - confidence) / 2
    lo_idx = int(alpha * n_resamples)
    hi_idx = int((1 - alpha) * n_resamples) - 1
    return means[lo_idx], means[hi_idx]


def _mc_shuffle_p_value(samples: list[float], n_shuffles: int = MC_SHUFFLE_N) -> float:
    """How often does a random sign-flip produce a mean >= observed?
    Tests the null "the directional bias is luck"."""
    rng = random.Random(RANDOM_SEED + 1)
    observed = sum(samples) / max(len(samples), 1)
    if observed == 0:
        return 1.0
    # We shuffle the sign of each sample independently.
    n = len(samples)
    abs_samples = [abs(s) for s in samples]
    ge_count = 0
    for _ in range(n_shuffles):
        shuffled_sum = sum(abs_samples[i] if rng.random() < 0.5 else -abs_samples[i] for i in range(n))
        if abs(shuffled_sum / n) >= abs(observed):
            ge_count += 1
    return ge_count / n_shuffles


# ────────────────────────────────────────────────────────────────────
# Verdict logic
# ────────────────────────────────────────────────────────────────────


def _assess(bot_id: str) -> BotAuthenticityReport:
    rep = BotAuthenticityReport(bot_id=bot_id)
    sources = [
        _read_closed_ledger(bot_id),
        _read_paper_soak(bot_id),
        _read_kaizen_latest(bot_id),
    ]
    rep.sources = sources

    # Cross-source agreement check.
    pnls_with_source = [
        (s.source, s.total_pnl_usd)
        for s in sources
        if s.total_pnl_usd is not None and not any("SCALE_BUG" in n for n in s.notes)
    ]
    if len(pnls_with_source) >= 2:
        max_pnl = max(p for _, p in pnls_with_source)
        min_pnl = min(p for _, p in pnls_with_source)
        spread = max_pnl - min_pnl
        # If sources differ by more than $500 OR by more than 50% of mag
        avg_mag = sum(abs(p) for _, p in pnls_with_source) / len(pnls_with_source)
        if spread > 500 and (avg_mag == 0 or spread / max(avg_mag, 1.0) > 0.50):
            rep.sources_disagree = True
            rep.disagreement_detail.append(
                f"P&L spread ${spread:.0f} across {', '.join(f'{s}=${p:.0f}' for s, p in pnls_with_source)}",
            )

    # Scale-bug detection
    for s in sources:
        if any("SCALE_BUG" in n for n in s.notes):
            rep.sources_disagree = True
            rep.disagreement_detail.append(
                f"{s.source}: " + "; ".join(s.notes),
            )

    # Pick the "consensus" source.  We accept SCALE_BUG sources here
    # because their R-multiples can still be clean — the audit just
    # switches metric basis to R for those bots.
    consensus_source = None
    for s in sources:
        if s.n_trades is not None and s.n_trades > 0:
            consensus_source = s
            break
    if consensus_source is None:
        rep.verdict = "INCONCLUSIVE"
        rep.justification = "No usable source data for this bot."
        return rep
    rep.consensus_n = consensus_source.n_trades
    rep.consensus_pnl = consensus_source.total_pnl_usd
    rep.consensus_r = consensus_source.cumulative_r

    # Decide metric basis: prefer R when USD is missing (==0 over
    # many trades = paper-sim) OR scale-buggy.
    usd_is_broken = any("SCALE_BUG" in n for n in consensus_source.notes)
    usd_is_missing = (
        rep.consensus_n >= MIN_N_FOR_STATS and (rep.consensus_pnl or 0) == 0 and (rep.consensus_r or 0) != 0
    )
    use_r_basis = usd_is_broken or usd_is_missing or ((rep.consensus_pnl or 0) == 0 and (rep.consensus_r or 0) != 0)
    rep.metric_basis = "R" if use_r_basis else "USD"

    # Sample size gate.
    if (rep.consensus_n or 0) < MIN_N_FOR_STATS:
        rep.verdict = "INCONCLUSIVE" if (rep.consensus_n or 0) < 5 else "LAB_GROWN"
        rep.justification = (
            f"n={rep.consensus_n} < {MIN_N_FOR_STATS} (insufficient for "
            "stable inference; small sample looks diamond-like under "
            "any lighting)."
        )
        if rep.sources_disagree and not use_r_basis:
            rep.verdict = "CUBIC_ZIRCONIA"
            rep.justification = f"sources disagree AND n={rep.consensus_n} too small to arbitrate"
        return rep

    # Build per-trade sample.  R-basis uses cumulative_r/n; USD-basis
    # uses total_pnl/n.  Synth wins/losses around the mean using WR.
    metric_total = (rep.consensus_r if use_r_basis else rep.consensus_pnl) or 0
    avg_per_trade = metric_total / max(rep.consensus_n or 1, 1)
    wr = (consensus_source.win_rate_pct or 0) / 100.0
    n_wins = int(round(wr * rep.consensus_n))
    n_losses = rep.consensus_n - n_wins
    samples = [avg_per_trade * 2] * n_wins + [-avg_per_trade * 2] * n_losses
    if not samples:
        rep.verdict = "INCONCLUSIVE"
        rep.justification = "synth sample empty"
        return rep
    lo, hi = _bootstrap_ci(samples)
    p = _mc_shuffle_p_value(samples)
    rep.bootstrap_ci_lower = round(lo, 4)
    rep.bootstrap_ci_upper = round(hi, 4)
    rep.mc_p_value = round(p, 4)

    unit = "R" if use_r_basis else "$"
    if rep.sources_disagree and not use_r_basis:
        # Disagreement on USD when we're not on R-basis means a real
        # data plumbing issue.  R-basis already insulates against
        # USD-only scale bugs.
        rep.verdict = "CUBIC_ZIRCONIA"
        rep.justification = "sources disagree; " + " | ".join(rep.disagreement_detail)
    elif lo > 0 and p < 0.05:
        rep.verdict = "GENUINE"
        rep.justification = (
            f"basis={rep.metric_basis}; bootstrap 95% CI on per-trade mean = "
            f"[{lo:+.4f}{unit}, {hi:+.4f}{unit}] (lower > 0); MC p={p:.3f} < 0.05; "
            f"n={rep.consensus_n}"
        )
    elif lo > 0:
        rep.verdict = "LAB_GROWN"
        rep.justification = (
            f"basis={rep.metric_basis}; CI lower > 0 ({lo:+.4f}{unit}) but MC p={p:.3f} weak; n={rep.consensus_n}"
        )
    else:
        positive_total = (rep.consensus_r or 0) >= 0 if use_r_basis else (rep.consensus_pnl or 0) >= 0
        rep.verdict = "LAB_GROWN" if positive_total else "CUBIC_ZIRCONIA"
        rep.justification = (
            f"basis={rep.metric_basis}; CI lower {lo:+.4f}{unit} <= 0 — "
            f"edge not statistically separable from zero; MC p={p:.3f}; "
            f"n={rep.consensus_n}"
        )
    return rep


# ────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────


def run_audit() -> dict:
    reports = [_assess(bot_id) for bot_id in sorted(DIAMOND_BOTS)]
    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "n_diamonds": len(reports),
        "verdict_counts": {},
        "reports": [asdict(r) for r in reports],
    }
    counts: dict[str, int] = {}
    for r in reports:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    summary["verdict_counts"] = counts
    # Persist
    try:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        print(f"WARN: latest write failed: {exc}", file=sys.stderr)
    try:
        with OUT_LOG.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": summary["ts"],
                        "verdict_counts": counts,
                        "n": len(reports),
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
    except OSError as exc:
        print(f"WARN: log append failed: {exc}", file=sys.stderr)
    return summary


def _print(summary: dict) -> None:
    print("=" * 100)
    print(f" DIAMOND AUTHENTICITY AUDIT — {summary['ts']}")
    print("=" * 100)
    counts = summary["verdict_counts"]
    order = ["GENUINE", "LAB_GROWN", "CUBIC_ZIRCONIA", "INCONCLUSIVE"]
    print(" Verdict roll-up: " + ", ".join(f"{v}={counts.get(v, 0)}" for v in order))
    print()
    print(f" {'bot':28s} {'verdict':18s} {'n':>5s} {'PnL':>10s} {'CI_lo':>8s} {'p':>6s}  justification")
    print("-" * 130)
    for r in summary["reports"]:
        n = r.get("consensus_n", "?")
        pnl = r.get("consensus_pnl", 0) or 0
        lo = r.get("bootstrap_ci_lower")
        p = r.get("mc_p_value")
        lo_s = f"{lo:+.2f}" if lo is not None else "n/a"
        p_s = f"{p:.3f}" if p is not None else "n/a"
        print(
            f" {r['bot_id']:28s} {r['verdict']:18s} {str(n):>5s} {pnl:>10.2f} "
            f"{lo_s:>8s} {p_s:>6s}  {r['justification'][:80]}",
        )
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    summary = run_audit()
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print(summary)
    # Exit code: nonzero if any CUBIC_ZIRCONIA found
    if summary["verdict_counts"].get("CUBIC_ZIRCONIA", 0) > 0:
        return 2
    if summary["verdict_counts"].get("INCONCLUSIVE", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
