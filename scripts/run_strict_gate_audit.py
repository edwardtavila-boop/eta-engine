"""Run the strict-gate audit on every still-active bot in the registry.

Why this exists
---------------
The strict-gate audit (block-bootstrap CI + Bonferroni-adjusted p +
friction-net expR + split-half stability + Lopez-de-Prado deflated
Sharpe) is the highest-rigor evidence we have for whether a bot has a
real edge. It runs the bot's WalkForwardEngine across all available
historical bars and produces a JSON snapshot keyed by bot_id.

Usage
-----
    python -m eta_engine.scripts.run_strict_gate_audit
    python -m eta_engine.scripts.run_strict_gate_audit --output reports/foo.json
    python -m eta_engine.scripts.run_strict_gate_audit --include-deactivated

Default behaviour: skips bots flagged ``extras["deactivated"]=True`` and
writes ``reports/strict_gate_<UTCstamp>.json``.

The output is the same shape consumed by the round-1 + round-2 audit
analyses on 2026-05-07 -- a list of dicts with keys::

    bot, sym, trades, sharpe, expR_net, p_bonf, sh_def, split, L, S

L (legacy) and S (strict) are bot-level pass flags from the
WalkForwardEngine's gate evaluation. Bonferroni correction is applied
across the full submitted set, so retiring losers tightens the
penalty for survivors on the next run.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.feeds.strategy_lab.engine import WalkForwardEngine
from eta_engine.strategies.per_bot_registry import ASSIGNMENTS, is_active

# Crypto symbols route to the crypto bar history; everything else uses
# the futures (mnq_data) pool. Match the routing the round-1 audit used.
_CRYPTO_PREFIXES = ("BTC", "ETH", "SOL", "MBT", "MET", "XRP", "AVAX", "LINK", "DOGE")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the strict-gate audit across the active fleet.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=("Output JSON path. Default: reports/strict_gate_<UTCstamp>.json"),
    )
    parser.add_argument(
        "--include-deactivated",
        action="store_true",
        help="Audit every assignment in the registry, not just active ones.",
    )
    parser.add_argument(
        "--bot",
        action="append",
        default=None,
        help="Audit only the named bot(s); flag repeatable.",
    )
    args = parser.parse_args(argv)

    workspace_root = Path(__file__).resolve().parents[2]
    engine_mnq = WalkForwardEngine(bar_dir=workspace_root / "mnq_data" / "history")
    engine_crypto = WalkForwardEngine(
        bar_dir=workspace_root / "data" / "crypto" / "ibkr" / "history",
    )

    # Filter the assignment list down to the audit scope.
    targets = []
    requested = set(args.bot) if args.bot else None
    for a in ASSIGNMENTS:
        if requested is not None:
            if a.bot_id in requested:
                targets.append(a)
            continue
        if not args.include_deactivated and not is_active(a):
            continue
        targets.append(a)

    print(f"strict-gate audit: {len(targets)} bots", flush=True)

    results: list[dict] = []
    for a in targets:
        engine = engine_crypto if a.symbol.startswith(_CRYPTO_PREFIXES) else engine_mnq
        spec: dict = {
            "id": a.bot_id,
            "symbol": a.symbol,
            "timeframe": a.timeframe,
            "strategy_kind": a.strategy_kind,
            "scorer_name": a.scorer_name,
        }
        if a.extras:
            for k in ("sub_strategy_kind", "sub_strategy_extras", "min_score", "scorecard_config"):
                if k in a.extras:
                    spec[k] = a.extras[k]
        try:
            res = engine.run(spec, symbol=a.symbol)
            row = {
                "bot": a.bot_id,
                "symbol": a.symbol,
                "trades": int(getattr(res, "total_trades", 0) or 0),
                "sharpe": round(float(getattr(res, "sharpe", 0) or 0), 2),
                "expR": round(float(getattr(res, "expR", 0) or 0), 3),
                "expR_p5": round(float(getattr(res, "expR_p5", 0) or 0), 3),
                "expR_net": round(float(getattr(res, "expR_net", 0) or 0), 3),
                "p_bonf": round(float(getattr(res, "p_value_bonferroni", 1) or 1), 3),
                "split": bool(getattr(res, "split_half_sign_stable", False)),
                "sh_def": round(float(getattr(res, "sharpe_deflated", 0) or 0), 2),
                "L": bool(getattr(res, "legacy_passed", False)),
                "S": bool(getattr(res, "passed_strict", False)),
            }
        except Exception as exc:  # noqa: BLE001
            row = {"bot": a.bot_id, "error": str(exc)[:200]}
        results.append(row)
        # Live progress so a 30-min run isn't a black box.
        if "error" in row:
            print(f"  {a.bot_id:<25} ERR {row['error']}", flush=True)
        else:
            tag = ("L" if row["L"] else "_") + ("S" if row["S"] else "_")
            print(
                f"  {a.bot_id:<25} {row['symbol']:<5} "
                f"trd={row['trades']:>5} sh={row['sharpe']:>6.2f} "
                f"net={row['expR_net']:>+7.3f} sh_def={row['sh_def']:>+6.2f} "
                f"split={row['split']!s:<5} {tag}",
                flush=True,
            )

    legacy = sum(1 for r in results if r.get("L"))
    strict = sum(1 for r in results if r.get("S"))
    print(
        f"\nstrict-gate audit complete: {len(results)} bots, legacy={legacy}, strict={strict}",
        flush=True,
    )

    if args.output is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        reports_dir = workspace_root / "eta_engine" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        args.output = reports_dir / f"strict_gate_{stamp}.json"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2, default=str))
    print(f" Saved: {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
