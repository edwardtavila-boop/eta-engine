"""Edge analyzer — which bots actually have edge?

Compares each bot's LAB sharpe / win-rate / expectancy (from
``reports/lab_reports/*/`` JSONs the strategy lab produced via
walk-forward) against its LIVE performance (from
``trade_closes.jsonl`` the supervisor writes on every closed trade).

A bot belongs to one of four tiers:

  DIAMOND   — lab sharpe > 1.0  AND  live sharpe > 0.5
              ("won in backtest, still winning live")
  LAB-ONLY  — lab sharpe > 1.0  AND  live sharpe ≤ 0.5
              ("looked great in backtest; live is degraded — possible
              curve fit or regime change")
  LIVE-ONLY — lab sharpe ≤ 1.0  AND  live sharpe > 0.5
              ("lab undersold it; live shows real edge — promote")
  NOISE     — both ≤ thresholds  ("no demonstrable edge anywhere")

Tier thresholds are env-tunable:
  ETA_EDGE_LAB_SHARPE_MIN     default 1.0
  ETA_EDGE_LIVE_SHARPE_MIN    default 0.5
  ETA_EDGE_LIVE_MIN_CLOSES    default 30   (need this many for honest live sharpe)

Usage:
    python -m eta_engine.scripts.edge_analyzer
    python -m eta_engine.scripts.edge_analyzer --json
    python -m eta_engine.scripts.edge_analyzer --asset crypto
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import math
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_LAB_REPORTS_DIR = Path(r"C:\EvolutionaryTradingAlgo\reports\lab_reports")
_TRADE_CLOSES = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\trade_closes.jsonl"
)


_CRYPTO_ROOTS = {"BTC", "ETH", "SOL", "AVAX", "LINK", "DOGE", "MBT", "MET"}
_FUTURES_ROOTS = {"MNQ", "NQ", "ES", "MES", "MNQ1", "NQ1", "RTY", "GC",
                  "CL", "NG", "ZN", "6E"}


def _root(symbol: str) -> str:
    s = symbol.upper().lstrip("/").rstrip("0123456789")
    for suf in ("USDT", "USD"):
        if s.endswith(suf):
            return s[: -len(suf)] or s
    return s


def _asset_class(symbol: str) -> str:
    r = _root(symbol)
    if r in _CRYPTO_ROOTS:
        return "crypto"
    if r in _FUTURES_ROOTS:
        return "futures"
    return "other"


# ─── Lab metrics ────────────────────────────────────────────────


def _scan_lab_reports() -> dict[str, dict[str, Any]]:
    """Return {bot_id: lab_metrics} from the most recent JSON in each
    bots' lab_reports subdirectory."""
    out: dict[str, dict[str, Any]] = {}
    if not _LAB_REPORTS_DIR.exists():
        return out
    for sub in _LAB_REPORTS_DIR.iterdir():
        if not sub.is_dir():
            continue
        bot_id = sub.name
        # Most recent JSON in the directory
        candidates = sorted(sub.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            continue
        try:
            payload = json.loads(candidates[-1].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # LabResult fields the engine emits
        out[bot_id] = {
            "lab_path": str(candidates[-1]),
            "lab_sharpe": payload.get("sharpe", 0.0) or 0.0,
            "lab_win_rate": payload.get("win_rate", 0.0) or 0.0,
            "lab_expectancy_r": payload.get("expectancy_r", payload.get("expectancy", 0.0)) or 0.0,
            "lab_n_trades": payload.get("n_trades", 0) or 0,
            "lab_max_drawdown": payload.get("max_drawdown", 0.0) or 0.0,
            "lab_profit_factor": payload.get("profit_factor", 0.0) or 0.0,
            "lab_symbol": payload.get("symbol", ""),
            "lab_strategy_kind": payload.get("strategy_kind", ""),
        }
    return out


# ─── Live metrics ───────────────────────────────────────────────


def _scan_live_closes() -> dict[str, list[dict[str, Any]]]:
    """Group trade_closes.jsonl rows by bot_id."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not _TRADE_CLOSES.exists():
        return out
    try:
        with _TRADE_CLOSES.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                bid = rec.get("bot_id")
                if bid:
                    out[bid].append(rec)
    except OSError:
        pass
    return out


def _live_metrics(closes: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute live sharpe/win_rate/expectancy from a list of close
    records. Sharpe is annualized assuming daily-ish cadence (252)."""
    rs: list[float] = []
    pnls: list[float] = []
    for c in closes:
        r = c.get("realized_r")
        if r is not None:
            with contextlib.suppress(TypeError, ValueError):
                rs.append(float(r))
        # Both shapes: top-level realized_pnl (legacy) or extra={}
        pnl = c.get("realized_pnl")
        if pnl is None:
            extra = c.get("extra") or {}
            pnl = extra.get("realized_pnl") if isinstance(extra, dict) else None
        if pnl is not None:
            with contextlib.suppress(TypeError, ValueError):
                pnls.append(float(pnl))

    n = len(rs)
    if n == 0:
        return {
            "live_n_closes": 0, "live_sharpe": 0.0, "live_win_rate": 0.0,
            "live_expectancy_r": 0.0, "live_realized_pnl_usd": 0.0,
        }

    mean_r = sum(rs) / n
    var_r = sum((r - mean_r) ** 2 for r in rs) / n
    std_r = math.sqrt(var_r)
    sharpe = (mean_r / std_r) * math.sqrt(252) if std_r > 0 else 0.0
    wins = sum(1 for r in rs if r > 0)
    return {
        "live_n_closes": n,
        "live_sharpe": round(sharpe, 4),
        "live_win_rate": round(wins / n, 4),
        "live_expectancy_r": round(mean_r, 4),
        "live_realized_pnl_usd": round(sum(pnls), 2) if pnls else 0.0,
    }


# ─── Tier classification ────────────────────────────────────────


def _classify_tier(lab: dict[str, Any], live: dict[str, Any]) -> str:
    lab_sharpe_min = float(os.getenv("ETA_EDGE_LAB_SHARPE_MIN", "1.0"))
    live_sharpe_min = float(os.getenv("ETA_EDGE_LIVE_SHARPE_MIN", "0.5"))
    live_min_closes = int(os.getenv("ETA_EDGE_LIVE_MIN_CLOSES", "30"))

    lab_ok = float(lab.get("lab_sharpe", 0)) >= lab_sharpe_min
    if live.get("live_n_closes", 0) < live_min_closes:
        return "INSUFFICIENT_LIVE_DATA"
    live_ok = float(live.get("live_sharpe", 0)) >= live_sharpe_min

    if lab_ok and live_ok:
        return "DIAMOND"
    if lab_ok and not live_ok:
        return "LAB_ONLY"
    if (not lab_ok) and live_ok:
        return "LIVE_ONLY"
    return "NOISE"


# ─── Combine ────────────────────────────────────────────────────


def analyze() -> list[dict[str, Any]]:
    lab_by_bot = _scan_lab_reports()
    closes_by_bot = _scan_live_closes()
    bot_ids = set(lab_by_bot) | set(closes_by_bot)

    rows: list[dict[str, Any]] = []
    for bid in bot_ids:
        lab = lab_by_bot.get(bid, {})
        live = _live_metrics(closes_by_bot.get(bid, []))
        symbol = lab.get("lab_symbol") or _infer_symbol_from_bot_id(bid)
        tier = _classify_tier(lab, live)
        rows.append({
            "bot_id": bid,
            "symbol": symbol,
            "asset": _asset_class(symbol),
            "tier": tier,
            **lab,
            **live,
        })
    return rows


def _infer_symbol_from_bot_id(bot_id: str) -> str:
    """Fallback when lab report missing — guess from bot_id prefix."""
    bid = bot_id.lower()
    for token in ("btc", "eth", "sol", "avax", "link", "doge",
                  "mnq", "nq", "es", "gc", "cl", "ng", "zn", "6e"):
        if token in bid:
            return token.upper()
    return "?"


def _print_text(rows: list[dict[str, Any]], asset_filter: str | None = None) -> None:
    if asset_filter:
        rows = [r for r in rows if r["asset"] == asset_filter]
    rows.sort(key=lambda r: (
        {"DIAMOND": 0, "LIVE_ONLY": 1, "LAB_ONLY": 2,
         "INSUFFICIENT_LIVE_DATA": 3, "NOISE": 4}.get(r["tier"], 9),
        -float(r.get("lab_sharpe", 0) or 0),
    ))

    by_tier: dict[str, int] = defaultdict(int)
    for r in rows:
        by_tier[r["tier"]] += 1

    print("=" * 102)
    print(f" EDGE ANALYZER — {len(rows)} bots")
    for tier in ("DIAMOND", "LIVE_ONLY", "LAB_ONLY", "INSUFFICIENT_LIVE_DATA", "NOISE"):
        if by_tier[tier]:
            print(f"   {tier}: {by_tier[tier]}")
    print("=" * 102)
    print(
        f"{'bot_id':<28} {'sym':<6} {'tier':<10} "
        f"{'lab_shp':>8} {'lab_wr':>7} {'lab_n':>5} | "
        f"{'live_shp':>8} {'live_wr':>7} {'live_n':>5} {'live_pnl':>10}"
    )
    print("-" * 102)
    for r in rows:
        print(
            f"{r['bot_id']:<28} {str(r.get('symbol', '?')):<6} "
            f"{r['tier']:<10} "
            f"{float(r.get('lab_sharpe', 0)):>8.2f} "
            f"{float(r.get('lab_win_rate', 0)):>7.1%} "
            f"{int(r.get('lab_n_trades', 0)):>5} | "
            f"{float(r.get('live_sharpe', 0)):>8.2f} "
            f"{float(r.get('live_win_rate', 0)):>7.1%} "
            f"{int(r.get('live_n_closes', 0)):>5} "
            f"${float(r.get('live_realized_pnl_usd', 0)):>+9.2f}"
        )
    print("=" * 102)
    print("\nLEGEND:")
    print("  DIAMOND   = lab sharpe > 1.0  AND  live sharpe > 0.5  -> keep, size up")
    print("  LIVE_ONLY = lab tepid but live winning  -> consider promotion")
    print("  LAB_ONLY  = lab great, live degraded    -> review for curve fit / regime")
    print("  NOISE     = neither lab nor live shows edge  -> research or retire")
    print("  INSUFFICIENT_LIVE_DATA = need more closes for honest live sharpe")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true")
    p.add_argument("--asset", choices=("crypto", "futures", "other"), default=None)
    args = p.parse_args(argv)
    rows = analyze()
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        _print_text(rows, asset_filter=args.asset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
