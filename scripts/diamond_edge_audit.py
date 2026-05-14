"""Broker-led diamond edge audit.

This script turns the diamond fleet's closed-trade evidence into a
human-readable retune queue. It deliberately does not mutate routing;
its job is to answer: what is actually making money, what is bleeding,
and what asset-specific playbook should be retuned next?
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from eta_engine.scripts import workspace_roots  # noqa: E402

OUT_LATEST = workspace_roots.ETA_RUNTIME_STATE_DIR / "diamond_edge_audit_latest.json"

MIN_PROFIT_FACTOR = 1.10
MIN_PROP_SAMPLE = 100
MIN_AVG_R = 0.20
MIN_WIN_RATE_PCT = 45.0

ASSET_PLAYBOOKS = {
    "equity_index": (
        "Trade opening range, VWAP reclaim, and liquidity sweep continuations; "
        "avoid low-liquidity overnight unless broker closes prove it."
    ),
    "metals_energy": (
        "Trade event-driven inventory, macro impulse, and failed-break sweeps; "
        "keep stops volatility-aware and avoid generic chop signals."
    ),
    "rates_fx": (
        "Trade macro-session range expansion and rate-sensitive reversals; "
        "gate around calendar events and avoid thin-session noise."
    ),
    "cme_crypto": (
        "Trade CME crypto liquidity sweeps and volatility expansions; "
        "prefer confirmed reclaim/trend regimes over spot-style drift."
    ),
    "unknown": "Prove symbol, broker route, and session edge before any size increase.",
}

PARAMETER_FOCUS_BY_ASSET = {
    "equity_index": [
        "session predicate",
        "opening range boundary",
        "VWAP reclaim confirmation",
        "min_volume_z",
        "rr_target",
        "atr_stop_mult",
        "max_trades_per_day",
    ],
    "metals_energy": [
        "event/session gate",
        "min_wick_pct",
        "min_volume_z",
        "atr_stop_mult",
        "reclaim_window",
        "min_bars_between_trades",
        "max_trades_per_day",
    ],
    "rates_fx": [
        "London/NY overlap gate",
        "calendar-event block",
        "level_lookback",
        "min_wick_pct",
        "reclaim_window",
        "atr_stop_mult",
    ],
    "cme_crypto": [
        "CME session liquidity gate",
        "volatility expansion gate",
        "trend regime filter",
        "reclaim_window",
        "rr_target",
        "max_trades_per_day",
    ],
    "unknown": ["symbol mapping", "broker route", "session predicate", "sample size"],
}

PARAMETER_FOCUS_BY_STRATEGY = {
    "fx_range": [
        "range width",
        "RSI extreme threshold",
        "volume confirmation",
        "London/NY overlap gate",
        "calendar-event block",
        "daily loss stop",
    ],
    "orb_sage_gated": [
        "opening range minutes",
        "retest window",
        "sage_min_conviction",
        "sage_min_alignment",
        "min_volume_z",
        "overnight block",
    ],
}


def _as_float(value: Any, default: float = 0.0) -> float:  # noqa: ANN401
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _root(symbol: str) -> str:
    text = str(symbol or "").upper().lstrip("/")
    text = text.rstrip("0123456789")
    for suffix in ("USDT", "USD"):
        if text.endswith(suffix):
            return text[: -len(suffix)] or text
    return text


def _asset_sleeve(symbol: str, bot_id: str = "") -> str:
    root = _root(symbol)
    bid = bot_id.lower()
    if root in {"MNQ", "NQ", "ES", "MES", "M2K", "MYM", "RTY"}:
        return "equity_index"
    if root in {"GC", "MGC", "CL", "MCL", "NG"}:
        return "metals_energy"
    if root in {"ZN", "ZB", "ZF", "ZT", "6E", "6J", "6B", "EUR"}:
        return "rates_fx"
    if root in {"MBT", "MET"} or bid.startswith(("mbt_", "met_")):
        return "cme_crypto"
    return "unknown"


def _assignment_meta(assignments: dict[str, Any], bot_id: str) -> dict[str, Any]:
    assignment = assignments.get(bot_id)
    if assignment is None:
        return {}
    if isinstance(assignment, dict):
        return assignment
    return {
        "symbol": getattr(assignment, "symbol", ""),
        "strategy_kind": getattr(assignment, "strategy_kind", ""),
        "timeframe": getattr(assignment, "timeframe", ""),
        "strategy_id": getattr(assignment, "strategy_id", ""),
    }


def _close_symbol(row: dict[str, Any], meta: dict[str, Any]) -> str:
    symbol = row.get("symbol")
    if not symbol:
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        symbol = extra.get("symbol")
    return str(symbol or meta.get("symbol") or "")


def _pnl(row: dict[str, Any]) -> float:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    return _as_float(row.get("realized_pnl", extra.get("realized_pnl")))


def _r(row: dict[str, Any]) -> float:
    return _as_float(row.get("realized_r"))


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [_pnl(row) for row in rows]
    rs = [_r(row) for row in rows]
    gross_profit = sum(max(pnl, 0.0) for pnl in pnls)
    gross_loss = abs(sum(min(pnl, 0.0) for pnl in pnls))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    wins = sum(1 for pnl in pnls if pnl > 0)
    return {
        "n_closes": len(rows),
        "total_realized_pnl": round(sum(pnls), 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "cumulative_r": round(sum(rs), 4),
        "avg_r": round(sum(rs) / len(rs), 4) if rs else 0.0,
        "win_rate_pct": round((wins / len(pnls)) * 100, 2) if pnls else 0.0,
    }


def _session_edges(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("session") or "unknown")].append(row)
    out = []
    for session, session_rows in grouped.items():
        out.append({"session": session, **_stats(session_rows)})
    out.sort(key=lambda item: float(item["total_realized_pnl"]), reverse=True)
    return out


def _verdict(stats: dict[str, Any]) -> str:
    n = int(stats["n_closes"])
    pnl = float(stats["total_realized_pnl"])
    avg_r = float(stats["avg_r"])
    win_rate = float(stats["win_rate_pct"])
    pf = stats["profit_factor"]
    if n == 0:
        return "NO_BROKER_DATA"
    if pnl <= 0 or avg_r <= 0 or (pf is not None and float(pf) < MIN_PROFIT_FACTOR):
        return "RETUNE"
    if n < MIN_PROP_SAMPLE:
        return "PROVE_MORE"
    if avg_r >= MIN_AVG_R and win_rate >= MIN_WIN_RATE_PCT:
        return "PROP_CANDIDATE"
    return "PAPER_EDGE"


def _recommendation(
    *,
    verdict: str,
    strategy_kind: str,
    sessions: list[dict[str, Any]],
    asset_sleeve: str,
) -> str:
    playbook = ASSET_PLAYBOOKS.get(asset_sleeve, ASSET_PLAYBOOKS["unknown"])
    if verdict == "NO_BROKER_DATA":
        return f"collect broker closes first; {playbook}"
    if verdict == "PROVE_MORE":
        return f"keep paper-only and build sample toward {MIN_PROP_SAMPLE} closes; {playbook}"
    if verdict in {"PAPER_EDGE", "PROP_CANDIDATE"}:
        return f"preserve setup, do not overfit; scale only after fresh broker closes stay positive; {playbook}"

    worst = sessions[-1] if sessions else {}
    best = sessions[0] if sessions else {}
    worst_name = str(worst.get("session") or "weak session")
    best_name = str(best.get("session") or "best session")
    if float(worst.get("total_realized_pnl") or 0.0) < 0 < float(best.get("total_realized_pnl") or 0.0):
        return (
            f"block {worst_name} until retest; retune {strategy_kind or 'strategy'} "
            f"around {best_name}; {playbook}"
        )
    return f"keep paper-only; retune {strategy_kind or 'strategy'} with broker PnL as the score; {playbook}"


def _session_name(session: dict[str, Any] | None) -> str:
    if not session:
        return "unknown"
    return str(session.get("session") or "unknown")


def _parameter_focus(asset_sleeve: str, strategy_kind: str) -> list[str]:
    focus = list(PARAMETER_FOCUS_BY_ASSET.get(asset_sleeve, PARAMETER_FOCUS_BY_ASSET["unknown"]))
    for item in PARAMETER_FOCUS_BY_STRATEGY.get(strategy_kind, []):
        if item not in focus:
            focus.append(item)
    return focus


def _primary_experiment(row: dict[str, Any]) -> str:
    worst = row.get("worst_session") if isinstance(row.get("worst_session"), dict) else None
    best = row.get("best_session") if isinstance(row.get("best_session"), dict) else None
    worst_name = _session_name(worst)
    best_name = _session_name(best)
    worst_pnl = float((worst or {}).get("total_realized_pnl") or 0.0)
    best_pnl = float((best or {}).get("total_realized_pnl") or 0.0)
    if worst_pnl < 0 < best_pnl and worst_name != best_name:
        return (
            f"Paper-test blocking {worst_name} entries while concentrating fresh sample around "
            f"{best_name}; graduate only if broker closed-trade PnL and PF both improve."
        )
    return (
        "Paper-test a narrow parameter grid around the listed focus knobs; keep the incumbent "
        "routing unchanged until fresh broker closes prove a better variant."
    )


def _retune_command(bot_id: str) -> str:
    return f"python -m eta_engine.scripts.fleet_strategy_optimizer --only-bot {bot_id}"


def _paper_only_next_step(bot_id: str) -> str:
    return (
        f"Run {_retune_command(bot_id)}; compare fresh broker closes, profit factor, avg R, "
        "and session split before any promotion."
    )


def _retune_priority(row: dict[str, Any]) -> float:
    pnl = float(row.get("total_realized_pnl") or 0.0)
    pf = row.get("profit_factor")
    pf_penalty = max(0.0, MIN_PROFIT_FACTOR - float(pf)) * 100.0 if pf is not None else 25.0
    return round(abs(min(pnl, 0.0)) + pf_penalty + float(row.get("n_closes") or 0) * 0.01, 2)


def build_edge_audit(
    *,
    closes: list[dict[str, Any]],
    assignments: dict[str, Any],
    diamond_bots: set[str] | frozenset[str],
) -> dict[str, Any]:
    by_bot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in closes:
        bot_id = str(row.get("bot_id") or "")
        if bot_id in diamond_bots:
            by_bot[bot_id].append(row)

    bot_rows: list[dict[str, Any]] = []
    for bot_id in sorted(diamond_bots):
        meta = _assignment_meta(assignments, bot_id)
        rows = by_bot.get(bot_id, [])
        first_symbol = _close_symbol(rows[-1], meta) if rows else str(meta.get("symbol") or "")
        stats = _stats(rows)
        sessions = _session_edges(rows)
        verdict = _verdict(stats)
        sleeve = _asset_sleeve(first_symbol, bot_id)
        strategy_kind = str(meta.get("strategy_kind") or "")
        row = {
            "bot_id": bot_id,
            "symbol": first_symbol,
            "asset_sleeve": sleeve,
            "strategy_kind": strategy_kind,
            "timeframe": meta.get("timeframe") or "",
            **stats,
            "verdict": verdict,
            "best_session": sessions[0] if sessions else None,
            "worst_session": sessions[-1] if sessions else None,
            "session_edges": sessions,
            "asset_playbook": ASSET_PLAYBOOKS.get(sleeve, ASSET_PLAYBOOKS["unknown"]),
        }
        row["recommended_action"] = _recommendation(
            verdict=verdict,
            strategy_kind=strategy_kind,
            sessions=sessions,
            asset_sleeve=sleeve,
        )
        bot_rows.append(row)

    asset_edges = _asset_edges(bot_rows)
    retune_queue = [
        {
            "bot_id": row["bot_id"],
            "symbol": row["symbol"],
            "strategy_kind": row["strategy_kind"],
            "asset_sleeve": row["asset_sleeve"],
            "priority_score": _retune_priority(row),
            "issue_code": _issue_code(row),
            "best_session": _session_name(row.get("best_session")),
            "worst_session": _session_name(row.get("worst_session")),
            "parameter_focus": _parameter_focus(str(row["asset_sleeve"]), str(row["strategy_kind"])),
            "primary_experiment": _primary_experiment(row),
            "retune_command": _retune_command(str(row["bot_id"])),
            "paper_only_next_step": _paper_only_next_step(str(row["bot_id"])),
            "recommended_action": row["recommended_action"],
            "live_mutation_policy": "paper_only_advisory",
            "safe_to_mutate_live": False,
        }
        for row in bot_rows
        if row["verdict"] == "RETUNE"
    ]
    retune_queue.sort(key=lambda row: float(row["priority_score"]), reverse=True)

    return {
        "kind": "eta_diamond_edge_audit",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "summary": {
            "n_bots": len(bot_rows),
            "n_retune": sum(1 for row in bot_rows if row["verdict"] == "RETUNE"),
            "n_prop_candidate": sum(1 for row in bot_rows if row["verdict"] == "PROP_CANDIDATE"),
            "safe_to_mutate_live": False,
            "scoring_basis": "broker_closed_trade_pnl_first",
        },
        "asset_edges": asset_edges,
        "bots": bot_rows,
        "retune_queue": retune_queue[:10],
    }


def _issue_code(row: dict[str, Any]) -> str:
    if float(row.get("total_realized_pnl") or 0.0) <= 0:
        return "broker_pnl_negative"
    if row.get("profit_factor") is not None and float(row["profit_factor"]) < MIN_PROFIT_FACTOR:
        return "profit_factor_below_floor"
    return "edge_unproven"


def _asset_edges(bot_rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in bot_rows:
        grouped[str(row.get("asset_sleeve") or "unknown")].append(row)
    out = {}
    for sleeve, rows in sorted(grouped.items()):
        ranked = sorted(rows, key=lambda row: float(row.get("total_realized_pnl") or 0.0), reverse=True)
        out[sleeve] = {
            "n_bots": len(rows),
            "total_realized_pnl": round(sum(float(row.get("total_realized_pnl") or 0.0) for row in rows), 2),
            "playbook": ASSET_PLAYBOOKS.get(sleeve, ASSET_PLAYBOOKS["unknown"]),
            "top_positive": [
                {"bot_id": row["bot_id"], "pnl": row["total_realized_pnl"], "verdict": row["verdict"]}
                for row in ranked
                if float(row.get("total_realized_pnl") or 0.0) > 0
            ][:5],
            "top_negative": [
                {"bot_id": row["bot_id"], "pnl": row["total_realized_pnl"], "verdict": row["verdict"]}
                for row in reversed(ranked)
                if float(row.get("total_realized_pnl") or 0.0) < 0
            ][:5],
        }
    return out


def _load_normalized_closes() -> list[dict[str, Any]]:
    from eta_engine.scripts.closed_trade_ledger import (  # noqa: PLC0415
        DEFAULT_PRODUCTION_DATA_SOURCES,
        _normalize_close,
        load_close_records,
    )

    return [
        _normalize_close(row)
        for row in load_close_records(data_sources=DEFAULT_PRODUCTION_DATA_SOURCES)
    ]


def _load_assignments() -> dict[str, Any]:
    try:
        from eta_engine.strategies.per_bot_registry import all_assignments  # noqa: PLC0415
    except ImportError:
        return {}
    return {assignment.bot_id: assignment for assignment in all_assignments()}


def run() -> dict[str, Any]:
    from eta_engine.feeds.capital_allocator import DIAMOND_BOTS  # noqa: PLC0415

    report = build_edge_audit(
        closes=_load_normalized_closes(),
        assignments=_load_assignments(),
        diamond_bots=set(DIAMOND_BOTS),
    )
    workspace_roots.ensure_parent(OUT_LATEST)
    OUT_LATEST.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return report


def _print(report: dict[str, Any]) -> None:
    print("=" * 118)
    print(
        " DIAMOND EDGE AUDIT  "
        f"retune={report['summary']['n_retune']} "
        f"prop_candidates={report['summary']['n_prop_candidate']}",
    )
    print("=" * 118)
    for row in report["bots"]:
        print(
            f"{row['bot_id']:<26} {row['asset_sleeve']:<14} {row['verdict']:<14} "
            f"n={row['n_closes']:>4} pnl=${row['total_realized_pnl']:>+9.2f} "
            f"pf={str(row['profit_factor']):>6} action={row['recommended_action']}",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
