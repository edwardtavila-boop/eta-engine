"""
PnL right now — one-shot terminal command.

Operator runs this from any shell that has eta_engine on the path:

    python -m eta_engine.scripts.pnl_now
    python -m eta_engine.scripts.pnl_now --window 168    # last 7 days
    python -m eta_engine.scripts.pnl_now --json          # machine-readable

No SSH tunnel, no Hermes chat, no LLM cost. Just reads
trade_closes.jsonl and renders the operator-friendly summary to stdout.

Designed for the "I'm at my desk, just tell me my R" use case.

Exit code 0 always (this is informational, not a check).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from eta_engine.brain.jarvis_v3 import pnl_summary


def _fmt_r(v: float) -> str:
    return f"{v:+.2f}R"


def _win_rate_pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def render(window_hours: float) -> str:
    """Build the operator-friendly text summary (ASCII-only for Windows cp1252)."""
    multi = pnl_summary.multi_window_summary()
    today = multi.get("today") or {}
    week = multi.get("week") or {}
    month = multi.get("month") or {}

    bar = "=" * 56
    sep = "-" * 48
    dash = "-"

    lines = [
        "",
        bar,
        "  PnL  -  " + datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        bar,
        "",
        f"  Today  : {_fmt_r(today.get('total_r', 0.0)):>9}   "
        f"{today.get('n_trades', 0):>4} trades   "
        f"W/L {today.get('n_wins', 0):>3}/{today.get('n_losses', 0):<3}   "
        f"{_win_rate_pct(today.get('win_rate', 0.0))}",
        f"  7-day  : {_fmt_r(week.get('total_r', 0.0)):>9}   "
        f"{week.get('n_trades', 0):>4} trades   "
        f"W/L {week.get('n_wins', 0):>3}/{week.get('n_losses', 0):<3}   "
        f"{_win_rate_pct(week.get('win_rate', 0.0))}",
        f"  30-day : {_fmt_r(month.get('total_r', 0.0)):>9}   "
        f"{month.get('n_trades', 0):>4} trades   "
        f"W/L {month.get('n_wins', 0):>3}/{month.get('n_losses', 0):<3}   "
        f"{_win_rate_pct(month.get('win_rate', 0.0))}",
        "",
    ]

    # Top performers + worst (today)
    top = today.get("top_performers") or []
    worst = today.get("worst_performers") or []
    if top or worst:
        lines.append("--- Today's bot rollup " + dash * 28)
        lines.append("")
        lines.append(f"  {'WINNERS':<35}  |  LOSERS")
        lines.append(f"  {sep[:35]}  |  {sep[:35]}")
        n = max(len(top), len(worst), 1)
        for i in range(min(n, 5)):
            w = top[i] if i < len(top) else None
            losing = worst[i] if i < len(worst) else None
            w_str = f"{w['bot_id']:<25} {_fmt_r(w['total_r']):>9}" if w else dash
            l_str = f"{losing['bot_id']:<25} {_fmt_r(losing['total_r']):>9}" if losing else dash
            lines.append(f"  {w_str:<35}  |  {l_str:<35}")
        lines.append("")

    # Recent trades
    s_window = pnl_summary.summarize(window_hours=window_hours)
    if s_window.recent:
        lines.append(f"--- Last {len(s_window.recent)} trades ({int(window_hours)}h window) " + dash * 16)
        lines.append("")
        for t in s_window.recent:
            ts_short = (t.ts or "")[11:19] or dash
            wl = "W" if t.win else ("L" if t.r < 0 else ".")
            lines.append(f"  {ts_short}  {t.bot_id:<28}  {_fmt_r(t.r):>9}  {wl}")
        lines.append("")
    else:
        lines.append(f"  (no trades in last {int(window_hours)}h)")
        lines.append("")

    # Best / worst single trade
    if today.get("best_trade"):
        best = today["best_trade"]
        worst_single = today.get("worst_trade") or best
        lines.append("--- Today's extremes " + dash * 32)
        lines.append("")
        lines.append(f"  [BEST]  {_fmt_r(best['r']):>9}  {best['bot_id']:<28}  @ {(best.get('ts') or '')[11:19]}")
        lines.append(
            f"  [WORST] {_fmt_r(worst_single['r']):>9}  {worst_single['bot_id']:<28}  "
            f"@ {(worst_single.get('ts') or '')[11:19]}"
        )
        lines.append("")

    lines.append(bar)
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot PnL summary for the operator. No LLM, no SSH.",
    )
    parser.add_argument(
        "--window", type=float, default=24.0, help="Hours back for the 'recent trades' list (default 24)"
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of human text")
    args = parser.parse_args(argv)

    if args.json:
        payload = pnl_summary.multi_window_summary()
        print(json.dumps(payload, default=str, indent=2))
    else:
        print(render(window_hours=args.window))
    return 0


if __name__ == "__main__":
    sys.exit(main())
