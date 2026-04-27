"""Daily quantum portfolio rebalance (Wave-9/11 production hook).

Runs once per day off the trade-decision hot path. Asks the
QuantumOptimizerAgent to reallocate the active bot fleet given:

  * Each bot's current expected R (from recent journal performance)
  * Pairwise correlation between bots' realized P&L
  * Cardinality constraint (operator-tunable max_active_bots)

Output: ``state/quantum/daily_rebalance_<date>.json`` with the
recommended weights. Bots consult this file at startup to apply the
recommended size-multiplier.

Designed to be invoked by a scheduled task. The shell command:

    schtasks /Create /TN "ETA Quantum Daily Rebalance"
      /TR "<install_root>/.venv/Scripts/python.exe <install_root>/scripts/quantum_daily_rebalance.py"
      /SC DAILY /ST 21:00

(Where ``<install_root>`` is ``C:\\EvolutionaryTradingAlgo\\eta_engine`` on
the production VPS.)

Idempotent. Safe to re-run.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

logger = logging.getLogger("quantum_daily_rebalance")


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


def _compute_bot_stats(
    *, n_days_back: float, log_path: Path,
) -> dict[str, list[float]]:
    """Group realized R per bot for the last N days. Returns
    {bot_id: [r1, r2, ...]}."""
    cutoff = datetime.now(UTC) - timedelta(days=n_days_back)
    grouped: dict[str, list[float]] = defaultdict(list)
    for t in _read_jsonl(log_path):
        try:
            dt = datetime.fromisoformat(
                str(t.get("ts", "")).replace("Z", "+00:00"),
            )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            if dt < cutoff:
                continue
            bot = str(t.get("bot_id", ""))
            if not bot:
                continue
            grouped[bot].append(float(t.get("realized_r", 0.0)))
        except (TypeError, ValueError):
            continue
    return dict(grouped)


def _correlation_matrix(
    series_by_bot: dict[str, list[float]],
) -> tuple[list[str], list[list[float]]]:
    """Compute pairwise correlation between bot return streams.

    Aligns by index (assumes streams are time-ordered). Pads shorter
    streams with zeros at the start.
    """
    bots = sorted(series_by_bot.keys())
    if len(bots) < 2:
        return bots, [[1.0]] if bots else ([], [])
    n = max(len(series_by_bot[b]) for b in bots)
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

    matrix = [[1.0 if i == j else 0.0 for j in range(len(bots))]
              for i in range(len(bots))]
    for i in range(len(bots)):
        for j in range(i + 1, len(bots)):
            c = _corr(aligned[i], aligned[j])
            matrix[i][j] = c
            matrix[j][i] = c
    return bots, matrix


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n-days-back", type=float, default=30,
                   help="Window of trade history to consider")
    p.add_argument("--max-active-bots", type=int, default=4,
                   help="Cardinality cap (max bots picked)")
    p.add_argument("--correlation-penalty", type=float, default=0.5,
                   help="QUBO redundancy penalty coefficient")
    p.add_argument("--enable-cloud", action="store_true",
                   help="Allow real cloud quantum backends (D-Wave / IBM)")
    p.add_argument("--trade-log", type=Path,
                   default=ROOT / "state" / "jarvis_intel" / "trade_closes.jsonl")
    p.add_argument("--out-dir", type=Path,
                   default=ROOT / "state" / "quantum")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    today = datetime.now(UTC).date().isoformat()
    logger.info("quantum daily rebalance: %s", today)

    # 1. Pull per-bot trade history
    series = _compute_bot_stats(
        n_days_back=args.n_days_back, log_path=args.trade_log,
    )
    if not series:
        logger.warning("no trade history -- skipping rebalance")
        return 0
    logger.info("bots with history: %d", len(series))

    # 2. Compute expected R + correlations
    bot_ids = sorted(series.keys())
    expected_r = [
        sum(series[b]) / max(len(series[b]), 1) for b in bot_ids
    ]
    bot_ids_sorted, corr_matrix = _correlation_matrix(series)
    if bot_ids_sorted != bot_ids:
        logger.error("bot ID ordering mismatch -- aborting")
        return 2

    # 3. Build SignalScore objects + run quantum agent
    from eta_engine.brain.jarvis_v3.quantum import (
        QuantumOptimizerAgent,
        SignalScore,
    )
    from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import (
        CloudConfig, QuantumCloudAdapter,
    )
    candidates = [
        SignalScore(
            name=bot_ids[i],
            score=expected_r[i],
            features=corr_matrix[i],
        )
        for i in range(len(bot_ids))
    ]
    cfg = CloudConfig(
        enable_cloud=args.enable_cloud,
        classical_validate_cloud=True,  # always cross-check
    )
    adapter = QuantumCloudAdapter(cfg=cfg)
    agent = QuantumOptimizerAgent(adapter=adapter)
    rec = agent.select_signal_basket(
        candidates=candidates,
        max_picks=args.max_active_bots,
        correlation_penalty=args.correlation_penalty,
        use_qubo=True,
    )

    # 4. Persist recommendation
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"daily_rebalance_{today}.json"
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "date": today,
        "n_days_back": args.n_days_back,
        "max_active_bots": args.max_active_bots,
        "correlation_penalty": args.correlation_penalty,
        "enable_cloud": args.enable_cloud,
        "bot_ids": bot_ids,
        "expected_r": expected_r,
        "correlation_matrix": corr_matrix,
        "recommendation": asdict(rec),
        "selected_bots": rec.selected_labels,
        "objective": rec.objective,
        "backend_used": rec.backend_used,
        "fell_back_to_classical": rec.fell_back_to_classical,
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("wrote %s", out)

    # 5. Update "current_allocation.json" symlink-ish (just copy)
    current = args.out_dir / "current_allocation.json"
    current.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # 6. Print summary
    print(json.dumps({
        "date": today,
        "selected_bots": rec.selected_labels,
        "n_total": len(bot_ids),
        "n_picked": len(rec.selected_labels),
        "objective": rec.objective,
        "backend": rec.backend_used,
        "fell_back": rec.fell_back_to_classical,
        "summary": rec.contribution_summary,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
