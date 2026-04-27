"""
EVOLUTIONARY TRADING ALGO  //  backtest.tearsheet
=====================================
Markdown tearsheet + ASCII drawdown sparkline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.models import BacktestResult, Trade


class TearsheetBuilder:
    """Render a BacktestResult as a markdown report."""

    @classmethod
    def from_result(cls, result: BacktestResult) -> str:
        parts: list[str] = []
        parts.append(cls._headline(result))
        parts.append(cls._trade_distribution(result.trades))
        parts.append(cls._regime_breakdown(result.trades))
        parts.append(cls._exit_breakdown(result.trades))
        parts.append(cls._confluence_distribution(result.trades))
        parts.append(cls._drawdown_chart(result.trades, result.max_dd_pct))
        return "\n\n".join(parts) + "\n"

    # ── Sections ──

    @staticmethod
    def _headline(r: BacktestResult) -> str:
        lines = [
            f"# EVOLUTIONARY TRADING ALGO — Tearsheet: `{r.strategy_id}`",
            "",
            "## Headline Metrics",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Trades | {r.n_trades} |",
            f"| Win Rate | {r.win_rate * 100:.2f}% |",
            f"| Avg Win (R) | {r.avg_win_r:+.3f} |",
            f"| Avg Loss (R) | -{r.avg_loss_r:.3f} |",
            f"| Expectancy (R) | {r.expectancy_r:+.4f} |",
            f"| Profit Factor | {r.profit_factor:.3f} |",
            f"| Sharpe | {r.sharpe:.3f} |",
            f"| Sortino | {r.sortino:.3f} |",
            f"| Max DD | {r.max_dd_pct:.2f}% |",
            f"| Total Return | {r.total_return_pct:+.2f}% |",
        ]
        return "\n".join(lines)

    @staticmethod
    def _trade_distribution(trades: list[Trade]) -> str:
        if not trades:
            return "## Trade Distribution\n\n_No trades._"
        buckets = {"<-2R": 0, "-2..-1R": 0, "-1..0R": 0, "0..1R": 0, "1..2R": 0, ">2R": 0}
        for t in trades:
            r = t.pnl_r
            if r < -2:
                buckets["<-2R"] += 1
            elif r < -1:
                buckets["-2..-1R"] += 1
            elif r < 0:
                buckets["-1..0R"] += 1
            elif r < 1:
                buckets["0..1R"] += 1
            elif r < 2:
                buckets["1..2R"] += 1
            else:
                buckets[">2R"] += 1
        lines = ["## Trade Distribution", "", "| Bucket | Count |", "|---|---|"]
        for k, v in buckets.items():
            lines.append(f"| {k} | {v} |")
        return "\n".join(lines)

    @staticmethod
    def _regime_breakdown(trades: list[Trade]) -> str:
        if not trades:
            return "## Regime Breakdown\n\n_No trades._"
        # Group trades by regime label. Trades whose regime is None are
        # bucketed under '(unlabeled)' so the gap is visible.
        groups: dict[str, list[Trade]] = {}
        for t in trades:
            key = t.regime or "(unlabeled)"
            groups.setdefault(key, []).append(t)
        if set(groups) == {"(unlabeled)"}:
            return (
                "## Regime Breakdown\n\n"
                "_regime tags not attached to trades in this run "
                "(ctx_builder did not populate `regime`)._"
            )
        lines = [
            "## Regime Breakdown",
            "",
            "| Regime | Trades | Win Rate | Avg R | Sum R |",
            "|---|---|---|---|---|",
        ]
        for regime, group in sorted(groups.items()):
            n = len(group)
            wins = sum(1 for t in group if t.pnl_r > 0.0)
            sum_r = sum(t.pnl_r for t in group)
            avg_r = sum_r / n if n else 0.0
            wr = (wins / n * 100.0) if n else 0.0
            lines.append(f"| {regime} | {n} | {wr:.1f}% | {avg_r:+.3f} | {sum_r:+.3f} |")
        return "\n".join(lines)

    @staticmethod
    def _exit_breakdown(trades: list[Trade]) -> str:
        if not trades:
            return "## Exit Reason Breakdown\n\n_No trades._"
        groups: dict[str, list[Trade]] = {}
        for t in trades:
            key = t.exit_reason or "(unlabeled)"
            groups.setdefault(key, []).append(t)
        lines = [
            "## Exit Reason Breakdown",
            "",
            "| Exit Reason | Trades | Win Rate | Avg R |",
            "|---|---|---|---|",
        ]
        for reason, group in sorted(groups.items()):
            n = len(group)
            wins = sum(1 for t in group if t.pnl_r > 0.0)
            avg_r = sum(t.pnl_r for t in group) / n if n else 0.0
            wr = (wins / n * 100.0) if n else 0.0
            lines.append(f"| `{reason}` | {n} | {wr:.1f}% | {avg_r:+.3f} |")
        return "\n".join(lines)

    @staticmethod
    def _confluence_distribution(trades: list[Trade]) -> str:
        if not trades:
            return "## Confluence Distribution\n\n_No trades._"
        buckets: dict[str, int] = {"7.0-7.5": 0, "7.5-8.0": 0, "8.0-9.0": 0, "9.0-10.0": 0}
        for t in trades:
            s = t.confluence_score
            if s < 7.5:
                buckets["7.0-7.5"] += 1
            elif s < 8.0:
                buckets["7.5-8.0"] += 1
            elif s < 9.0:
                buckets["8.0-9.0"] += 1
            else:
                buckets["9.0-10.0"] += 1
        lines = ["## Confluence Distribution", "", "| Score | Trades |", "|---|---|"]
        for k, v in buckets.items():
            lines.append(f"| {k} | {v} |")
        return "\n".join(lines)

    @staticmethod
    def _drawdown_chart(trades: list[Trade], max_dd_pct: float) -> str:
        if not trades:
            return "## Drawdown\n\n_No trades._"
        # Build cumulative pnl_r curve → underwater
        cum = 0.0
        peak = 0.0
        underwater: list[float] = []
        for t in trades:
            cum += t.pnl_r
            if cum > peak:
                peak = cum
            underwater.append(cum - peak)  # <= 0
        spark = _sparkline(underwater)
        return f"## Drawdown\n\n```\nmax_dd={max_dd_pct:.2f}%\n{spark}\n```"


def _sparkline(values: list[float]) -> str:
    """Render a list of non-positive floats as an ASCII sparkline."""
    if not values:
        return ""
    chars = " .:-=+*#%@"  # shallow -> deep
    lo = min(values)
    if lo == 0.0:
        return chars[0] * len(values)
    out = []
    for v in values:
        # v is <= 0; map |v|/|lo| -> 0..1 -> index
        frac = min(1.0, abs(v) / abs(lo))
        idx = min(len(chars) - 1, int(frac * (len(chars) - 1)))
        out.append(chars[idx])
    return "".join(out)
