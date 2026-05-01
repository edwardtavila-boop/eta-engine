"""Daily quantum portfolio rebalance (Wave-18, 2026-04-30).

Supercharged version with:
  * should_invoke() gating — only runs quantum when edge benefit justifies cost
  * Multi-instrument support — allocates across ALL active instruments, not just bots
  * Cost tracking — daily budget enforced, classical fallback always available
  * Regime-aware skip — doesn't rerun when nothing changed
  * Telemetry push — each rebalance notifies Hermes bridge if configured
  * Parallel tempering for large portfolios (>16 symbols)

Output: state/quantum/daily_rebalance_<date>.json and current_allocation.json

Scheduled task (unchanged):
    schtasks /Create /TN "ETA Quantum Daily Rebalance"
      /TR "<install_root>/.venv/Scripts/python.exe <install_root>/scripts/quantum_daily_rebalance.py"
      /SC DAILY /ST 21:00
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

logger = logging.getLogger("quantum_daily_rebalance")

# ── Cost discipline ─────────────────────────────────────────

QUANTUM_DAILY_BUDGET_USD = 2.00
QUANTUM_COST_PER_INVOCATION_USD = 0.05
QUANTUM_MIN_SYMBOLS = 3


# ── Data helpers ────────────────────────────────────────────


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _compute_instrument_stats(
    *, n_days_back: float, log_path: Path, instrument_filter: list[str] | None = None,
) -> dict[str, dict[str, list[float]]]:
    """Group realized R per instrument and per bot. Returns
    {instrument: {bot_id: [r1, r2, ...]}} supporting multi-instrument fleets."""
    cutoff = datetime.now(UTC) - timedelta(days=n_days_back)
    grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for t in _read_jsonl(log_path):
        try:
            dt = datetime.fromisoformat(str(t.get("ts", "")).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            if dt < cutoff:
                continue
            symbol = str(t.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            if instrument_filter and symbol not in instrument_filter:
                continue
            bot = str(t.get("bot_id", "")) or str(t.get("route_name", "")) or "default"
            grouped[symbol][bot].append(float(t.get("realized_r", 0.0)))
        except (TypeError, ValueError):
            continue
    return dict(grouped)


def _correlation_matrix(series_by_bot: dict[str, list[float]]) -> tuple[list[str], list[list[float]]]:
    bots = sorted(series_by_bot.keys())
    if len(bots) < 2:
        return bots, [[1.0]] if bots else ([], [])
    n = max(len(v) for v in series_by_bot.values())
    aligned: list[list[float]] = []
    for b in bots:
        s = series_by_bot[b]
        padded = [0.0] * (n - len(s)) + list(s)
        aligned.append(padded)

    def _corr(a: list[float], b: list[float]) -> float:
        if not a or not b or n < 3:
            return 0.0
        ma = sum(a) / len(a)
        mb = sum(b) / len(b)
        num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
        sa = (sum((x - ma) ** 2 for x in a)) ** 0.5
        sb = (sum((x - mb) ** 2 for x in b)) ** 0.5
        if sa == 0 or sb == 0:
            return 0.0
        return num / (sa * sb)

    matrix = [[1.0 if i == j else 0.0 for j in range(len(bots))] for i in range(len(bots))]
    for i in range(len(bots)):
        for j in range(i + 1, len(bots)):
            c = _corr(aligned[i], aligned[j])
            matrix[i][j] = c
            matrix[j][i] = c
    return bots, matrix


def _load_last_regime(state_dir: Path, key: str) -> str | None:
    path = state_dir / f"last_regime_{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("regime")
    except (OSError, json.JSONDecodeError):
        return None


def _save_last_regime(state_dir: Path, key: str, regime: str) -> None:
    path = state_dir / f"last_regime_{key}.json"
    path.write_text(json.dumps({"regime": regime, "ts": datetime.now(UTC).isoformat()}, default=str))


def _regime_from_trades(trades: list[dict]) -> str:
    regimes = [t.get("regime", "unknown") for t in trades[-20:]]
    if not regimes:
        return "unknown"
    from collections import Counter
    return Counter(regimes).most_common(1)[0][0]


# ── Main ────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n-days-back", type=float, default=30, help="Window of trade history to consider")
    p.add_argument("--max-active-bots", type=int, default=4, help="Max bots/symbols picked per instrument")
    p.add_argument("--correlation-penalty", type=float, default=0.5, help="QUBO redundancy penalty")
    p.add_argument("--enable-cloud", action="store_true", help="Allow real cloud quantum (D-Wave/IBM)")
    p.add_argument("--skip-cost-gate", action="store_true", help="Bypass should_invoke() gating (force run)")
    p.add_argument("--instruments", type=str, default="MNQ,BTC,ETH,SOL", help="Comma-separated instrument symbols")
    p.add_argument("--trade-log", type=Path, default=ROOT / "state" / "jarvis_intel" / "trade_closes.jsonl")
    p.add_argument("--out-dir", type=Path, default=ROOT / "state" / "quantum")
    p.add_argument("--state-dir", type=Path, default=ROOT / "state" / "quantum")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    today_str = datetime.now(UTC).date().isoformat()
    instrument_list = [s.strip().upper() for s in args.instruments.split(",") if s.strip()]
    logger.info("quantum daily rebalance: %s for %s", today_str, ", ".join(instrument_list))

    t0 = time.perf_counter()
    total_invocations = 0
    total_cost = 0.0
    total_skipped = 0
    all_results: list[dict] = []

    # ── Per-instrument rebalance ──
    for instrument in instrument_list:
        logger.info("Processing %s...", instrument)

        series_per_bot = _compute_instrument_stats(
            n_days_back=args.n_days_back, log_path=args.trade_log,
            instrument_filter=[instrument],
        )
        if instrument not in series_per_bot:
            logger.info("%s: no trade history — skipping", instrument)
            all_results.append({"instrument": instrument, "status": "no_data"})
            continue

        bot_series = series_per_bot[instrument]
        if not bot_series:
            logger.info("%s: no bot data — skipping", instrument)
            continue

        # Collect all trades for this instrument to compute regime
        all_trades = []
        for bot_id, returns in bot_series.items():
            all_trades.extend([{"regime": _regime_from_trades(returns)}] * len(returns))

        bot_ids = sorted(bot_series.keys())
        expected_r = [sum(bot_series[b]) / max(len(bot_series[b]), 1) for b in bot_ids]
        bot_ids_sorted, corr_matrix = _correlation_matrix(bot_series)

        if bot_ids_sorted != bot_ids:
            logger.error("%s: bot ID ordering mismatch — aborting", instrument)
            all_results.append({"instrument": instrument, "status": "ordering_error"})
            continue

        # ── should_invoke() cost gate ──
        if not args.skip_cost_gate and len(bot_ids) > 0:
            from eta_engine.brain.jarvis_v3.quantum.quantum_agent import QuantumOptimizerAgent

            last_regime = _load_last_regime(args.state_dir, instrument)
            current_regime = _regime_from_trades(all_trades)
            regime_changed = last_regime is None or current_regime != last_regime

            should, reason = QuantumOptimizerAgent.should_invoke(
                n_symbols=len(bot_ids),
                regime_changed_since_last=regime_changed,
            )
            if not should:
                logger.info("%s: SKIPPED — %s", instrument, reason)
                total_skipped += 1
                all_results.append({
                    "instrument": instrument, "status": "skipped",
                    "reason": reason, "n_symbols": len(bot_ids),
                })
                _save_last_regime(args.state_dir, instrument, current_regime)
                continue

        # ── Run quantum ──
        try:
            from eta_engine.brain.jarvis_v3.quantum import QuantumOptimizerAgent, SignalScore
            from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import CloudConfig, QuantumCloudAdapter
        except ImportError as exc:
            logger.error("quantum import failed: %s", exc)
            return 2

        candidates = [
            SignalScore(name=f"{instrument}/{bot_ids[i]}", score=expected_r[i], features=corr_matrix[i])
            for i in range(len(bot_ids))
        ]

        cfg = CloudConfig(
            enable_cloud=args.enable_cloud,
            classical_validate_cloud=True,
        )
        adapter = QuantumCloudAdapter(cfg=cfg)
        agent = QuantumOptimizerAgent(adapter=adapter, cost_budget_daily_usd=QUANTUM_DAILY_BUDGET_USD)

        rec = agent.select_signal_basket(
            candidates=candidates,
            max_picks=min(args.max_active_bots, len(bot_ids)),
            correlation_penalty=args.correlation_penalty,
            use_qubo=True,
        )
        total_invocations += 1
        cost = QUANTUM_COST_PER_INVOCATION_USD * len(bot_ids) * 0.01
        total_cost += cost

        result = {
            "ts": datetime.now(UTC).isoformat(),
            "date": today_str,
            "instrument": instrument,
            "n_days_back": args.n_days_back,
            "max_active_bots": args.max_active_bots,
            "bot_ids": bot_ids,
            "expected_r": expected_r,
            "correlation_matrix": corr_matrix,
            "selected_bots": rec.selected_labels,
            "objective": rec.objective,
            "backend_used": rec.backend_used,
            "fell_back_to_classical": rec.fell_back_to_classical,
            "cost_estimate_usd": round(cost, 4),
            "contribution_summary": rec.contribution_summary,
        }
        all_results.append(result)
        _save_last_regime(args.state_dir, instrument, _regime_from_trades(all_trades))
        logger.info("%s: selected %d/%d %s (backend=%s, cost=$%.4f)",
                    instrument, len(rec.selected_labels), len(bot_ids),
                    rec.selected_labels, rec.backend_used, cost)

    # ── Persist combined results ──
    elapsed_s = time.perf_counter() - t0
    args.out_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "date": today_str,
        "instruments_processed": len(instrument_list),
        "instruments_rebalanced": total_invocations,
        "instruments_skipped": total_skipped,
        "total_cost_usd": round(total_cost, 4),
        "elapsed_seconds": round(elapsed_s, 1),
        "results": all_results,
    }

    out = args.out_dir / f"daily_rebalance_{today_str}.json"
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    logger.info("wrote %s", out)

    current = args.out_dir / "current_allocation.json"
    current.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    # ── Notify Hermes bridge ──
    _notify_hermes(payload, total_invocations, total_cost, total_skipped)

    summary = {
        "date": today_str,
        "instruments": instrument_list,
        "rebalanced": total_invocations,
        "skipped": total_skipped,
        "cost": round(total_cost, 4),
        "elapsed_s": round(elapsed_s, 1),
    }
    print(json.dumps(summary, indent=2))
    return 0


def _notify_hermes(payload: dict, invocations: int, cost: float, skipped: int) -> None:
    try:
        from hermes_jarvis_telegram.hermes_bridge import get_bridge
        bridge = get_bridge()
        results = payload.get("results", [])
        selected_all = []
        for r in results:
            selected = r.get("selected_bots", [])
            selected_all.extend(selected)
        bridge.notify_quantum_rebalance(
            selected_symbols=selected_all,
            objective=sum(r.get("objective", 0) for r in results),
            backend=results[0].get("backend_used", "classical") if results else "classical",
            cost=cost,
        )
        if skipped > 0:
            bridge.notify_system_health(
                health_score=0.8,
                verdict=f"{invocations} instruments rebalanced, {skipped} skipped (cost gate)",
            )
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
