"""Elite scoreboard — quant-grade per-bot scoring + portfolio diagnostics.

Brings the elite-mindset framework into the live system: each bot scored
against profit factor, Sharpe, max drawdown, expectancy, sample size,
and correlation to siblings, then tier-classified ELITE / PRODUCER /
MARGINAL / DECAY / INSUFFICIENT.

The output is the operator's quarterly-review surface: who to scale up,
who to evolve, who to retire.

Tier rules (env-tunable via ETA_ELITE_*):
    ELITE:        PF ≥ 1.8  AND Sharpe ≥ 1.5 AND MaxDD < 15R  AND n ≥ 50
                  AND expectancy_R > 0  AND rolling_decay_pct < 30
    PRODUCER:     PF ≥ 1.3  AND Sharpe ≥ 0.7 AND expectancy_R > 0
    MARGINAL:     PF ≥ 1.0  AND expectancy_R > 0  (breakeven-positive)
    DECAY:        rolling_sharpe collapsed > 50% from peak
    INSUFFICIENT: n < 30 closes

Plus portfolio diagnostics:
    * fleet correlation matrix (R-stream pairwise) — find redundancies
    * regime-conditional PF — split each bot's record by Sage composite_bias
    * rolling Sharpe (last 30 vs all) — edge-decay flag

Usage:
    python -m eta_engine.scripts.elite_scoreboard
    python -m eta_engine.scripts.elite_scoreboard --json
    python -m eta_engine.scripts.elite_scoreboard --since 2026-05-04T23:31:00
    python -m eta_engine.scripts.elite_scoreboard --bot btc_optimized
    python -m eta_engine.scripts.elite_scoreboard --correlations
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

_TRADE_CLOSES = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\trade_closes.jsonl",
)


def _load_closes(since_iso: str | None = None) -> list[dict[str, Any]]:
    """Return every close record (post-filter)."""
    if not _TRADE_CLOSES.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with _TRADE_CLOSES.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_iso and str(rec.get("ts", "")) < since_iso:
                    continue
                out.append(rec)
    except OSError:
        return []
    return out


def _per_bot_metrics(closes: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the elite metric set for one bot's trade list.

    realized_r is normalized by the bracket-distance denominator (post
    brake-fix), so it's lab-comparable. Profit factor is sum of winning
    R / abs(sum of losing R). Sharpe is annualized assuming daily-ish
    cadence (252) — for 5m / 1h bots this is a rough proxy but it's
    consistent across the fleet. Max drawdown is in R units (peak-to-
    trough of cumulative R). Rolling decay = (peak rolling-30 Sharpe -
    current rolling-30 Sharpe) / peak.
    """
    rs: list[float] = []
    pnls: list[float] = []
    for c in closes:
        r = c.get("realized_r")
        if r is None:
            continue
        with contextlib.suppress(TypeError, ValueError):
            rs.append(float(r))
        # PnL (extra dict) — present on post-fix closes
        extra = c.get("extra") or {}
        if isinstance(extra, dict):
            pnl = extra.get("realized_pnl")
            if pnl is not None:
                with contextlib.suppress(TypeError, ValueError):
                    pnls.append(float(pnl))

    n = len(rs)
    if n == 0:
        return {"n": 0}

    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    sum_wins = sum(wins)
    sum_losses_abs = abs(sum(losses))
    profit_factor = (sum_wins / sum_losses_abs) if sum_losses_abs > 0 else float("inf")
    expectancy_r = sum(rs) / n
    win_rate = len(wins) / n if n else 0.0

    mean_r = expectancy_r
    if n > 1:
        var_r = sum((r - mean_r) ** 2 for r in rs) / n
        std_r = math.sqrt(var_r)
        sharpe = (mean_r / std_r) * math.sqrt(252) if std_r > 0 else 0.0
    else:
        std_r = 0.0
        sharpe = 0.0

    # Max drawdown in R (peak-to-trough of cumulative R series)
    cum = 0.0
    peak = 0.0
    max_dd_r = 0.0
    for r in rs:
        cum += r
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd_r:
            max_dd_r = dd

    # Rolling 30-trade Sharpe vs peak rolling Sharpe — edge-decay flag
    rolling_window = 30
    rolling_sharpes: list[float] = []
    if n >= rolling_window + 1:
        for i in range(rolling_window, n + 1):
            window = rs[i - rolling_window : i]
            wm = sum(window) / rolling_window
            wstd = math.sqrt(
                sum((x - wm) ** 2 for x in window) / rolling_window,
            )
            ws = (wm / wstd) * math.sqrt(252) if wstd > 0 else 0.0
            rolling_sharpes.append(ws)
        rolling_now = rolling_sharpes[-1]
        rolling_peak = max(rolling_sharpes) if rolling_sharpes else 0.0
        rolling_decay_pct = (
            max(0.0, (rolling_peak - rolling_now) / rolling_peak)
            if rolling_peak > 0.5 else 0.0
        )
    else:
        rolling_now = 0.0
        rolling_peak = 0.0
        rolling_decay_pct = 0.0

    avg_win = (sum_wins / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0

    return {
        "n": n,
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else None,
        "sharpe": round(sharpe, 3),
        "expectancy_r": round(expectancy_r, 4),
        "std_r": round(std_r, 4),
        "max_drawdown_r": round(max_dd_r, 3),
        "avg_win_r": round(avg_win, 4),
        "avg_loss_r": round(avg_loss, 4),
        "sum_pnl_usd": round(sum(pnls), 2) if pnls else 0.0,
        "rolling_30_sharpe_now": round(rolling_now, 3),
        "rolling_30_sharpe_peak": round(rolling_peak, 3),
        "rolling_decay_pct": round(rolling_decay_pct, 3),
    }


def _classify_tier(m: dict[str, Any]) -> str:
    """Apply the tier rules in priority order."""
    n = int(m.get("n", 0))
    if n < int(os.getenv("ETA_ELITE_MIN_N", "30")):
        return "INSUFFICIENT"

    decay_threshold = float(os.getenv("ETA_ELITE_DECAY_THRESHOLD", "0.50"))
    if m.get("rolling_decay_pct", 0) > decay_threshold:
        return "DECAY"

    pf = m.get("profit_factor")
    sharpe = m.get("sharpe", 0)
    max_dd = m.get("max_drawdown_r", float("inf"))
    expectancy = m.get("expectancy_r", 0)

    elite_pf = float(os.getenv("ETA_ELITE_PF_MIN", "1.8"))
    elite_sharpe = float(os.getenv("ETA_ELITE_SHARPE_MIN", "1.5"))
    elite_dd = float(os.getenv("ETA_ELITE_MAX_DD_R", "15.0"))
    elite_n = int(os.getenv("ETA_ELITE_N_MIN", "50"))

    if (
        pf is not None and pf >= elite_pf
        and sharpe >= elite_sharpe
        and max_dd < elite_dd
        and n >= elite_n
        and expectancy > 0
    ):
        return "ELITE"

    producer_pf = float(os.getenv("ETA_ELITE_PRODUCER_PF", "1.3"))
    producer_sharpe = float(os.getenv("ETA_ELITE_PRODUCER_SHARPE", "0.7"))
    if (
        pf is not None and pf >= producer_pf
        and sharpe >= producer_sharpe
        and expectancy > 0
    ):
        return "PRODUCER"

    if pf is not None and pf >= 1.0 and expectancy > 0:
        return "MARGINAL"

    return "DECAY"


def _correlation_matrix(closes_by_bot: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, float]]:
    """Pairwise correlation of R-streams across bots. Bots sharing
    the same trade-by-trade R-pattern indicate redundancy — operator
    should pick one or rotate. Pearson correlation, computed only for
    bots with ≥10 trades.
    """
    streams: dict[str, list[float]] = {}
    for bot_id, recs in closes_by_bot.items():
        rs: list[float] = []
        for c in recs:
            r = c.get("realized_r")
            if r is None:
                continue
            with contextlib.suppress(TypeError, ValueError):
                rs.append(float(r))
        if len(rs) >= 10:
            streams[bot_id] = rs

    matrix: dict[str, dict[str, float]] = {}
    bots = sorted(streams)
    for i, a in enumerate(bots):
        matrix[a] = {}
        for b in bots[i:]:
            if a == b:
                matrix[a][b] = 1.0
                continue
            la = streams[a]
            lb = streams[b]
            # Align by length — take the tail of the longer to match
            n_pair = min(len(la), len(lb))
            sa = la[-n_pair:]
            sb = lb[-n_pair:]
            if n_pair < 10:
                continue
            ma = sum(sa) / n_pair
            mb = sum(sb) / n_pair
            num = sum((sa[k] - ma) * (sb[k] - mb) for k in range(n_pair))
            denom = math.sqrt(
                sum((sa[k] - ma) ** 2 for k in range(n_pair))
                * sum((sb[k] - mb) ** 2 for k in range(n_pair)),
            )
            corr = (num / denom) if denom > 0 else 0.0
            matrix[a][b] = round(corr, 3)
            matrix.setdefault(b, {})[a] = round(corr, 3)
    return matrix


def _by_regime(closes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Split a bot's closes by regime label (live regime at close time)
    and compute per-regime metrics. Reveals which strategy works in
    which regime — the elite framework's 'low-correlation across
    regimes' principle made measurable.
    """
    by_regime: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in closes:
        regime = str(c.get("regime", "unknown")) or "unknown"
        by_regime[regime].append(c)
    return {regime: _per_bot_metrics(recs) for regime, recs in by_regime.items()}


def analyze(since_iso: str | None = None) -> dict[str, Any]:
    """Full fleet analysis."""
    closes = _load_closes(since_iso=since_iso)
    closes_by_bot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in closes:
        bid = c.get("bot_id")
        if bid:
            closes_by_bot[bid].append(c)

    bots: dict[str, dict[str, Any]] = {}
    for bot_id, recs in closes_by_bot.items():
        m = _per_bot_metrics(recs)
        m["bot_id"] = bot_id
        m["tier"] = _classify_tier(m)
        m["by_regime"] = _by_regime(recs)
        bots[bot_id] = m

    correlations = _correlation_matrix(closes_by_bot)

    tier_counts: dict[str, int] = defaultdict(int)
    for m in bots.values():
        tier_counts[m["tier"]] += 1

    return {
        "total_closes": len(closes),
        "n_bots": len(bots),
        "tier_counts": dict(tier_counts),
        "bots": bots,
        "correlations": correlations,
        "since_iso": since_iso,
    }


def _print_text(report: dict[str, Any], show_correlations: bool = False) -> None:
    print("=" * 102)
    print(
        f" ELITE SCOREBOARD — {report['n_bots']} bots, "
        f"{report['total_closes']} closes" + (
            f" (since {report['since_iso']})"
            if report.get("since_iso") else ""
        ),
    )
    if report["tier_counts"]:
        tc = report["tier_counts"]
        print(
            f" tiers: ELITE={tc.get('ELITE', 0)}  "
            f"PRODUCER={tc.get('PRODUCER', 0)}  "
            f"MARGINAL={tc.get('MARGINAL', 0)}  "
            f"DECAY={tc.get('DECAY', 0)}  "
            f"INSUFFICIENT={tc.get('INSUFFICIENT', 0)}",
        )
    print("=" * 102)
    print(
        f"{'bot_id':<25} {'tier':<13} "
        f"{'n':>4} {'wr':>5} {'PF':>6} {'sharpe':>7} "
        f"{'exp_R':>7} {'maxDD':>7} {'rolling':>8} {'pnl_$':>9}",
    )
    print("-" * 102)

    tier_order = {
        "ELITE": 0, "PRODUCER": 1, "MARGINAL": 2,
        "DECAY": 3, "INSUFFICIENT": 4,
    }
    sorted_bots = sorted(
        report["bots"].values(),
        key=lambda b: (
            tier_order.get(b["tier"], 9),
            -float(b.get("sharpe") or 0),
        ),
    )
    for m in sorted_bots:
        pf = m.get("profit_factor")
        pf_str = f"{pf:>6.2f}" if pf is not None else f"{'inf':>6}"
        n = m.get("n", 0)
        wr = m.get("win_rate", 0)
        sharpe = m.get("sharpe", 0)
        exp_r = m.get("expectancy_r", 0)
        max_dd = m.get("max_drawdown_r", 0)
        roll_now = m.get("rolling_30_sharpe_now", 0)
        pnl = m.get("sum_pnl_usd", 0)
        print(
            f"{m['bot_id']:<25} {m['tier']:<13} "
            f"{n:>4} {wr*100:>4.1f}% {pf_str} "
            f"{sharpe:>7.2f} {exp_r:>+7.4f} {max_dd:>7.2f}R "
            f"{roll_now:>8.2f} ${pnl:>+8.2f}",
        )
    print("=" * 102)

    # High-correlation pairs (potential redundancies)
    if report.get("correlations"):
        high_pairs: list[tuple[str, str, float]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for a, row in report["correlations"].items():
            for b, corr in row.items():
                if a == b:
                    continue
                key = tuple(sorted((a, b)))
                if key in seen_pairs:
                    continue
                if abs(corr) >= 0.70:
                    seen_pairs.add(key)
                    high_pairs.append((a, b, corr))
        if high_pairs:
            print("\nHIGH-CORRELATION PAIRS (|r| ≥ 0.70 — redundancy candidates):")
            for a, b, corr in sorted(high_pairs, key=lambda t: -abs(t[2])):
                print(f"  {a:<25} ↔ {b:<25} corr={corr:>+6.3f}")
            print()
            print("  Elite framework: keep 5-15 UNCORRELATED strategies.")
            print("  Pairs with |corr|>0.7 are redundant — pick one.")

        if show_correlations:
            print("\n* FULL CORRELATION MATRIX *")
            bots = sorted(report["correlations"])
            print("       " + "".join(f"{b[:6]:>7}" for b in bots))
            for a in bots:
                row = report["correlations"][a]
                cells = "".join(
                    f"{row.get(b, 0):>+6.2f} " if a != b else f"{'  *  ':>7}"
                    for b in bots
                )
                print(f"{a[:6]:<7}{cells}")


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(AttributeError, ValueError):
        import sys as _sys
        _sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bot", default=None, help="Single bot detail")
    p.add_argument("--since", default=None, help="ISO ts filter")
    p.add_argument("--correlations", action="store_true", help="Print full corr matrix")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    report = analyze(since_iso=args.since)
    if args.bot:
        b = report["bots"].get(args.bot)
        if not b:
            print(f"!! {args.bot}: no closes recorded")
            return 1
        print(json.dumps(b, indent=2, default=str))
        return 0
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_text(report, show_correlations=args.correlations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
