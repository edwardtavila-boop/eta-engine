"""Per-bot performance scoreboard.

Reads supervisor heartbeat + paper P&L + fill log to produce a sortable
operator-facing table. No fancy stats yet — just the metrics that
matter for "which bots are working":

  bot_id  symbol  in/out  realized_pnl  win_rate  avg_R  open_pos

Usage:
    python -m eta_engine.scripts.bot_scoreboard
    python -m eta_engine.scripts.bot_scoreboard --sort pnl
    python -m eta_engine.scripts.bot_scoreboard --asset crypto
    python -m eta_engine.scripts.bot_scoreboard --top 10
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_HEARTBEAT_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\eta_engine\state\jarvis_intel\supervisor\heartbeat.json"
)
_TRADE_CLOSES_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\trade_closes.jsonl"
)


_CRYPTO_ROOTS = {"BTC", "ETH", "SOL", "AVAX", "LINK", "DOGE", "MBT", "MET"}
_FUTURES_ROOTS = {"MNQ", "NQ", "ES", "MES", "MNQ1", "NQ1", "ES1", "MES1",
                  "RTY", "M2K", "GC", "CL", "NG", "ZN", "6E"}


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


def _load_heartbeat() -> dict[str, Any]:
    if not _HEARTBEAT_PATH.exists():
        return {}
    try:
        return json.loads(_HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_closes() -> list[dict[str, Any]]:
    """Parse trade_closes.jsonl. Each line is one closed trade with
    bot_id, side, entry_price, exit_price, qty, realized_pnl,
    realized_r."""
    if not _TRADE_CLOSES_PATH.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with _TRADE_CLOSES_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _bot_metrics(bot: dict[str, Any], closes: list[dict[str, Any]]) -> dict[str, Any]:
    bid = bot.get("bot_id", "")
    bot_closes = [c for c in closes if c.get("bot_id") == bid]
    n_closes = len(bot_closes)
    rs = [
        float(c.get("realized_r", 0) or 0)
        for c in bot_closes
        if c.get("realized_r") is not None
    ]
    pnls = [
        float(c.get("realized_pnl", 0) or 0)
        for c in bot_closes
        if c.get("realized_pnl") is not None
    ]
    wins = [r for r in rs if r > 0]
    win_rate = (len(wins) / len(rs)) if rs else 0.0
    avg_r = (sum(rs) / len(rs)) if rs else 0.0
    realized_pnl = sum(pnls) if pnls else float(bot.get("realized_pnl", 0) or 0)
    open_pos = bot.get("open_position")
    return {
        "bot_id": bid,
        "symbol": bot.get("symbol", ""),
        "asset": _asset_class(bot.get("symbol", "")),
        "in": bot.get("n_entries", 0),
        "out": bot.get("n_exits", 0),
        "closes": n_closes,
        "realized_pnl": realized_pnl,
        "win_rate": win_rate,
        "avg_r": avg_r,
        "open_pos": (
            f"{open_pos['side']} {open_pos['qty']} @ {open_pos['entry_price']:.2f}"
            if open_pos else "-"
        ),
    }


def _format_row(m: dict[str, Any]) -> str:
    return (
        f"{m['bot_id']:<28} {m['symbol']:<6} {m['asset']:<7} "
        f"{m['in']:>4}/{m['out']:<4} cls={m['closes']:<3} "
        f"pnl=${m['realized_pnl']:>+9.2f} "
        f"wr={m['win_rate']:>5.1%} avgR={m['avg_r']:>+5.2f}  "
        f"{m['open_pos']:<26}"
    )


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_closes = sum(r["closes"] for r in rows)
    total_pnl = sum(r["realized_pnl"] for r in rows)
    if total_closes:
        all_rs = []
        for r in rows:
            # weighted contribution by close count for an aggregate view
            all_rs.extend([r["avg_r"]] * r["closes"])
        agg_avg_r = sum(all_rs) / len(all_rs) if all_rs else 0.0
    else:
        agg_avg_r = 0.0
    return {
        "n_bots": len(rows),
        "total_closes": total_closes,
        "total_realized_pnl": total_pnl,
        "agg_avg_r": agg_avg_r,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--sort", default="pnl",
        choices=("pnl", "win_rate", "avg_r", "closes", "in", "bot_id"),
        help="Column to sort by (descending). Default: pnl.",
    )
    p.add_argument(
        "--asset", default=None, choices=("crypto", "futures", "other"),
        help="Filter to one asset class.",
    )
    p.add_argument("--top", type=int, default=None, help="Show top N bots.")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of table.")
    args = p.parse_args(argv)

    hb = _load_heartbeat()
    bots = hb.get("bots", [])
    closes = _load_closes()

    rows = [_bot_metrics(b, closes) for b in bots]
    if args.asset:
        rows = [r for r in rows if r["asset"] == args.asset]
    sort_key = {"pnl": "realized_pnl", "win_rate": "win_rate",
                "avg_r": "avg_r", "closes": "closes", "in": "in",
                "bot_id": "bot_id"}[args.sort]
    rows.sort(key=lambda r: r[sort_key], reverse=(args.sort != "bot_id"))
    if args.top:
        rows = rows[: args.top]

    if args.json:
        print(json.dumps({"rows": rows, "summary": _summary(rows)}, indent=2, default=str))
        return 0

    print(f"{'bot_id':<28} {'sym':<6} {'asset':<7} {'in/out':<10} "
          f"{'closes':<7} {'pnl':<14} {'wr':<7} {'avgR':<6}  open_pos")
    print("-" * 130)
    for r in rows:
        print(_format_row(r))
    s = _summary(rows)
    print("-" * 130)
    print(
        f"TOTALS: bots={s['n_bots']} closes={s['total_closes']} "
        f"pnl=${s['total_realized_pnl']:+.2f} avgR={s['agg_avg_r']:+.2f}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
