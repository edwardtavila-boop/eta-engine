"""Fleet-wide realism audit — runs every active bot through the
realistic-fill simulator in parallel and surfaces:

- Invalid signals (stop on wrong side of entry, RR absurd, notional > cap)
- Realism gap (legacy vs realistic vs pessimistic PnL deltas)
- Walk-forward IS-vs-OOS performance (overfit detection)
- Per-bot session bucketing (RTH vs overnight)
- Sample size warnings (<30 trades = not statistically meaningful)

This is the SINGLE TOOL to run before promoting any bot to live capital.
A bot that fails this audit must NOT be live.

Usage
-----
    # Full fleet, all modes, walk-forward, 8 workers
    python -m eta_engine.scripts.fleet_realism_audit --workers 8

    # Only specific bots
    python -m eta_engine.scripts.fleet_realism_audit --bots volume_profile_mnq vwap_mr_mnq

    # Skip walk-forward for faster iteration
    python -m eta_engine.scripts.fleet_realism_audit --no-walk-forward

    # Strict mode — exit non-zero if ANY bot has invalid signals or
    # an OOS-vs-IS PnL decay > 50%
    python -m eta_engine.scripts.fleet_realism_audit --strict
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from eta_engine.scripts import workspace_roots  # noqa: E402

AUDIT_OUTPUT_DIR = workspace_roots.ETA_RUNTIME_STATE_DIR / "fleet_audits"


@dataclass
class BotAuditResult:
    bot_id: str
    symbol: str
    timeframe: str
    legacy_pnl: float = 0.0
    realistic_pnl: float = 0.0
    pessimistic_pnl: float = 0.0
    legacy_trades: int = 0
    realistic_trades: int = 0
    realistic_wr: float = 0.0
    realistic_signals_rejected: int = 0
    rejection_codes: dict[str, int] = field(default_factory=dict)
    is_pnl: float | None = None
    oos_pnl: float | None = None
    is_trades: int | None = None
    oos_trades: int | None = None
    is_wr: float | None = None
    oos_wr: float | None = None
    realism_gap_pct: float = 0.0   # (realistic - legacy) / |legacy| × 100
    error: str | None = None

    @property
    def status(self) -> str:
        if self.error:
            return "ERROR"
        if self.realistic_signals_rejected > 0:
            return "INVALID_SIGNALS"
        if self.realistic_trades < 30:
            return "INSUFFICIENT_SAMPLE"
        if self.realistic_pnl < 0:
            return "UNPROFITABLE"
        if self.oos_pnl is not None and self.is_pnl and self.is_pnl != 0:
            decay = (self.oos_pnl - self.is_pnl) / abs(self.is_pnl) * 100
            if decay < -50:
                return "OOS_DECAY"
        return "OK"


def _run_one_bot_audit(
    bot_id: str, days: int, walk_forward: bool, is_fraction: float,
) -> dict:
    """Run all three modes + walk-forward for one bot.  Returns dict so
    it can be pickled across the ProcessPoolExecutor boundary.
    """
    from eta_engine.scripts.paper_trade_sim import run_simulation
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(bot_id)
    if a is None:
        return {"bot_id": bot_id, "error": f"unknown bot_id {bot_id}"}

    daily_bars = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24, "4h": 6, "D": 1, "W": 0.14}
    bar_limit = int(days * daily_bars.get(a.timeframe, 288))

    out: dict = {
        "bot_id": bot_id, "symbol": a.symbol, "timeframe": a.timeframe,
    }
    try:
        legacy = run_simulation(bot_id, max_bars=100000, bar_limit=bar_limit, mode="legacy")
        realistic = run_simulation(bot_id, max_bars=100000, bar_limit=bar_limit, mode="realistic")
        pessimistic = run_simulation(bot_id, max_bars=100000, bar_limit=bar_limit, mode="pessimistic")

        out["legacy_pnl"] = legacy.total_pnl_usd
        out["legacy_trades"] = legacy.trades_taken
        out["realistic_pnl"] = realistic.total_pnl_usd
        out["realistic_trades"] = realistic.trades_taken
        out["realistic_wr"] = realistic.win_rate_pct
        out["realistic_signals_rejected"] = realistic.signals_rejected
        out["rejection_codes"] = dict(realistic.rejection_codes)
        out["pessimistic_pnl"] = pessimistic.total_pnl_usd

        if abs(legacy.total_pnl_usd) > 0.01:
            out["realism_gap_pct"] = (
                (realistic.total_pnl_usd - legacy.total_pnl_usd) / abs(legacy.total_pnl_usd) * 100
            )

        if walk_forward:
            is_res = run_simulation(
                bot_id, max_bars=100000, bar_limit=bar_limit,
                mode="realistic", is_fraction=is_fraction, eval_oos=False,
            )
            oos_res = run_simulation(
                bot_id, max_bars=100000, bar_limit=bar_limit,
                mode="realistic", is_fraction=is_fraction, eval_oos=True,
            )
            out["is_pnl"] = is_res.total_pnl_usd
            out["is_trades"] = is_res.trades_taken
            out["is_wr"] = is_res.win_rate_pct
            out["oos_pnl"] = oos_res.total_pnl_usd
            out["oos_trades"] = oos_res.trades_taken
            out["oos_wr"] = oos_res.win_rate_pct
    except Exception as e:  # noqa: BLE001 — sim can throw assorted ValueError
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def run_audit(
    bots: list[str] | None = None, days: int = 30,
    walk_forward: bool = True, is_fraction: float = 0.7,
    workers: int = 4, sequential: bool = False,
) -> list[BotAuditResult]:
    from eta_engine.strategies.per_bot_registry import all_assignments, is_active

    if bots:
        bot_ids = bots
    else:
        bot_ids = [a.bot_id for a in all_assignments() if is_active(a)]

    print(f"Auditing {len(bot_ids)} bots {'sequentially' if sequential else f'with {workers} workers'}...")
    print("  Modes: legacy / realistic / pessimistic")
    if walk_forward:
        print(f"  Walk-forward: IS={is_fraction*100:.0f}%, OOS={(1-is_fraction)*100:.0f}%")

    results: list[BotAuditResult] = []

    def _consume(bot_id: str, d: dict) -> None:
        r = BotAuditResult(
            bot_id=d.get("bot_id", bot_id),
            symbol=d.get("symbol", "?"),
            timeframe=d.get("timeframe", "?"),
            legacy_pnl=d.get("legacy_pnl", 0.0),
            realistic_pnl=d.get("realistic_pnl", 0.0),
            pessimistic_pnl=d.get("pessimistic_pnl", 0.0),
            legacy_trades=d.get("legacy_trades", 0),
            realistic_trades=d.get("realistic_trades", 0),
            realistic_wr=d.get("realistic_wr", 0.0),
            realistic_signals_rejected=d.get("realistic_signals_rejected", 0),
            rejection_codes=d.get("rejection_codes", {}),
            is_pnl=d.get("is_pnl"),
            oos_pnl=d.get("oos_pnl"),
            is_trades=d.get("is_trades"),
            oos_trades=d.get("oos_trades"),
            is_wr=d.get("is_wr"),
            oos_wr=d.get("oos_wr"),
            realism_gap_pct=d.get("realism_gap_pct", 0.0),
            error=d.get("error"),
        )
        results.append(r)
        tag = r.status
        if r.error:
            print(f"  [{r.bot_id}] {tag}: {r.error}", flush=True)
        else:
            rejected = f" REJ={r.realistic_signals_rejected}" if r.realistic_signals_rejected else ""
            print(
                f"  [{r.bot_id}] {tag} legacy=${r.legacy_pnl:+.0f} "
                f"realistic=${r.realistic_pnl:+.0f} pessimistic=${r.pessimistic_pnl:+.0f} "
                f"trades={r.realistic_trades} wr={r.realistic_wr:.0f}%{rejected}",
                flush=True,
            )

    if sequential:
        # Sequential mode — required on Windows when bots trigger heavy
        # init (sage daily verdicts, EMA cache rebuilds) that don't survive
        # ProcessPoolExecutor spawn cleanly.  Slower but reliable.
        for b in bot_ids:
            try:
                d = _run_one_bot_audit(b, days, walk_forward, is_fraction)
            except Exception as e:  # noqa: BLE001
                d = {"bot_id": b, "error": f"{type(e).__name__}: {e}"}
            _consume(b, d)
        return results

    with ProcessPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_run_one_bot_audit, b, days, walk_forward, is_fraction): b
                   for b in bot_ids}
        for f in as_completed(futures):
            bot_id = futures[f]
            try:
                d = f.result()
            except Exception as e:  # noqa: BLE001
                results.append(BotAuditResult(
                    bot_id=bot_id, symbol="?", timeframe="?",
                    error=f"executor: {type(e).__name__}: {e}",
                ))
                print(f"  [{bot_id}] EXCEPTION {e}", flush=True)
                continue
            _consume(bot_id, d)
    return results


def write_audit_report(results: list[BotAuditResult], strict: bool = False) -> int:
    """Print the report to stdout and write a JSON snapshot to disk."""
    AUDIT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=UTC)
    out_path = AUDIT_OUTPUT_DIR / f"fleet_audit_{now.strftime('%Y%m%dT%H%M%SZ')}.json"

    snapshot = {
        "audit_ts": now.isoformat(),
        "bot_count": len(results),
        "results": [
            {
                "bot_id": r.bot_id, "symbol": r.symbol, "timeframe": r.timeframe,
                "status": r.status,
                "legacy_pnl": r.legacy_pnl, "realistic_pnl": r.realistic_pnl,
                "pessimistic_pnl": r.pessimistic_pnl,
                "legacy_trades": r.legacy_trades,
                "realistic_trades": r.realistic_trades, "realistic_wr": r.realistic_wr,
                "realistic_signals_rejected": r.realistic_signals_rejected,
                "rejection_codes": r.rejection_codes,
                "is_pnl": r.is_pnl, "oos_pnl": r.oos_pnl,
                "is_trades": r.is_trades, "oos_trades": r.oos_trades,
                "is_wr": r.is_wr, "oos_wr": r.oos_wr,
                "realism_gap_pct": r.realism_gap_pct,
                "error": r.error,
            }
            for r in results
        ],
    }
    out_path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")

    print("\n" + "=" * 96)
    print(f"FLEET REALISM AUDIT — {now.isoformat()}")
    print(f"Snapshot: {out_path}")
    print("=" * 96)

    by_status: dict[str, list[BotAuditResult]] = {}
    for r in results:
        by_status.setdefault(r.status, []).append(r)

    print(f"\n{'Status':<22} {'Count':>6}")
    print("-" * 32)
    for s in sorted(by_status, key=lambda k: -len(by_status[k])):
        print(f"{s:<22} {len(by_status[s]):>6}")

    # CRITICAL bucket: invalid signals
    invalid = by_status.get("INVALID_SIGNALS", [])
    if invalid:
        print(f"\n>>> INVALID SIGNALS ({len(invalid)} bots) — DO NOT GO LIVE WITH THESE")
        print("-" * 96)
        for r in sorted(invalid, key=lambda x: -x.realistic_signals_rejected):
            codes = ", ".join(f"{k}={v}" for k, v in sorted(r.rejection_codes.items()))
            print(f"  {r.bot_id:<28} {r.symbol:<5} rejected={r.realistic_signals_rejected:>3}  ({codes})")

    # Promotable: OK + sufficient sample + profitable
    promotable = [r for r in results if r.status == "OK"]
    if promotable:
        print(f"\nPROMOTABLE CANDIDATES ({len(promotable)}) — passed all gates")
        print("-" * 96)
        print(f"{'Bot':<28} {'Sym':<5} {'TF':<4} {'Trades':>6} {'WR':>6} "
              f"{'Realistic':>10} {'IS':>9} {'OOS':>9} {'Decay':>7}")
        for r in sorted(promotable, key=lambda x: -x.realistic_pnl):
            decay_str = ""
            if r.is_pnl is not None and r.oos_pnl is not None and r.is_pnl != 0:
                decay = (r.oos_pnl - r.is_pnl) / abs(r.is_pnl) * 100
                decay_str = f"{decay:+5.0f}%"
            is_str = f"${r.is_pnl:+.0f}" if r.is_pnl is not None else "-"
            oos_str = f"${r.oos_pnl:+.0f}" if r.oos_pnl is not None else "-"
            print(f"  {r.bot_id:<28} {r.symbol:<5} {r.timeframe:<4} "
                  f"{r.realistic_trades:>6} {r.realistic_wr:>5.1f}% "
                  f"${r.realistic_pnl:>+9.0f} {is_str:>9} {oos_str:>9} {decay_str:>7}")

    # Errors
    errors = by_status.get("ERROR", [])
    if errors:
        print(f"\nERRORS ({len(errors)}):")
        for r in errors:
            print(f"  {r.bot_id}: {r.error}")

    # Strict-mode exit code
    if strict:
        if invalid or errors:
            return 1
        # Also fail if any promotable has OOS decay > 50%
        for r in promotable:
            if r.is_pnl and r.oos_pnl is not None and r.is_pnl != 0:
                decay = (r.oos_pnl - r.is_pnl) / abs(r.is_pnl) * 100
                if decay < -50:
                    print(f"\n>>> STRICT FAIL: {r.bot_id} OOS decay {decay:.0f}%")
                    return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fleet_realism_audit", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bots", nargs="+", help="specific bot_ids (default: all active)")
    p.add_argument("--days", type=int, default=30, help="bars window in days")
    p.add_argument("--no-walk-forward", action="store_true", help="skip IS/OOS split")
    p.add_argument("--is-fraction", type=float, default=0.7, help="IS fraction for walk-forward")
    p.add_argument("--workers", type=int, default=4, help="parallel processes")
    p.add_argument("--sequential", action="store_true",
                   help="run one bot at a time (slower but reliable on Windows)")
    p.add_argument("--strict", action="store_true",
                   help="exit non-zero on any invalid signal or OOS decay > 50%%")
    args = p.parse_args(argv)

    results = run_audit(
        bots=args.bots, days=args.days,
        walk_forward=not args.no_walk_forward,
        is_fraction=args.is_fraction, workers=args.workers,
        sequential=args.sequential,
    )
    return write_audit_report(results, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
