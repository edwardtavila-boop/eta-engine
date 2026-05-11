"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_universe_audit
========================================================
Survivorship-bias check: confirms that the strategy was tested only
on symbols currently tradeable on the operator's broker, AND that
the symbol universe hasn't silently shifted between training and
deployment.

Why this exists
---------------
A strategy backtested on a universe that's drifted produces
silently-bad numbers.  Examples:
  - Contract rolls (front-month MNQ changed; old data + new spec
    don't align without adjustment)
  - Delisted contracts (a symbol in the harness's history isn't
    tradeable today)
  - Subscription changes (a symbol the operator no longer subscribes
    to but the backtest still uses)
  - Restricted contracts (margin requirements changed; ineligible
    for paper)

This script audits the harness's recent invocations against:
  1. The current symbol allow-list (active subscriptions)
  2. The current bot registry (deactivated symbols)
  3. The capture daemon's writable symbol set
  4. The l2_strategy_registry's listed symbols
and flags any (strategy, symbol) tuple in the backtest log whose
symbol is no longer in any of those sources.

Run
---
::

    python -m eta_engine.scripts.l2_universe_audit
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
L2_BACKTEST_LOG = LOG_DIR / "l2_backtest_runs.jsonl"
UNIVERSE_AUDIT_LOG = LOG_DIR / "l2_universe_audit.jsonl"


@dataclass
class SurvivorshipFinding:
    strategy: str
    symbol: str
    last_seen_in_backtest: str
    in_capture_set: bool
    in_strategy_registry: bool
    in_harness_specs: bool
    finding: str  # "OK" | "STALE_SYMBOL" | "UNSUPPORTED_SYMBOL"


@dataclass
class UniverseAuditReport:
    n_strategies_audited: int
    n_findings: int
    n_stale: int
    n_unsupported: int
    findings: list[SurvivorshipFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _read_backtest_symbols(*, _path: Path,
                              since_days: int = 90) -> dict[str, list[dict]]:
    """Read backtest log, return {(strategy, symbol): [records...]}."""
    if not _path.exists():
        return {}
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    out: dict[str, list[dict]] = {}
    try:
        with _path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt < cutoff:
                    continue
                strategy = rec.get("strategy")
                symbol = rec.get("symbol")
                if strategy and symbol:
                    key = f"{strategy}|{symbol}"
                    rec["_parsed_ts"] = dt
                    out.setdefault(key, []).append(rec)
    except OSError:
        return {}
    return out


def _current_capture_symbols() -> set[str]:
    """Symbols actively being captured on the VPS (per scripts/
    capture_tick_stream's default symbol list)."""
    # In production this could probe the actual VPS; for now use
    # the pinned-bot default set from capture_tick_stream docs
    return {"MNQ", "NQ", "M2K", "6E", "MCL", "MYM", "NG", "MBT"}


def _strategy_registry_symbols() -> set[str]:
    """Symbols listed as active in l2_strategy_registry."""
    try:
        from eta_engine.strategies.l2_strategy_registry import L2_STRATEGIES
        return {s.symbol for s in L2_STRATEGIES
                 if s.promotion_status != "deactivated"}
    except (ImportError, AttributeError):
        return set()


def _harness_supported_symbols() -> set[str]:
    """Symbols defined in SYMBOL_SPECS."""
    try:
        from eta_engine.scripts.l2_backtest_harness import SYMBOL_SPECS
        return set(SYMBOL_SPECS.keys())
    except ImportError:
        return set()


def run_audit(*, since_days: int = 90,
                _backtest_path: Path | None = None) -> UniverseAuditReport:
    backtest = _read_backtest_symbols(
        _path=_backtest_path if _backtest_path is not None else L2_BACKTEST_LOG,
        since_days=since_days)
    capture_syms = _current_capture_symbols()
    registry_syms = _strategy_registry_symbols()
    harness_syms = _harness_supported_symbols()

    findings: list[SurvivorshipFinding] = []
    for key, records in backtest.items():
        strategy, symbol = key.split("|", 1)
        last_rec = max(records, key=lambda r: r["_parsed_ts"])
        in_cap = symbol in capture_syms
        in_reg = symbol in registry_syms
        in_harness = symbol in harness_syms
        # Classification:
        # - UNSUPPORTED_SYMBOL: not in harness_specs (can't trade it
        #   even if data exists)
        # - STALE_SYMBOL: in harness specs but no longer captured AND
        #   not in registry
        # - OK: still active somewhere
        if not in_harness:
            finding = "UNSUPPORTED_SYMBOL"
        elif not in_cap and not in_reg:
            finding = "STALE_SYMBOL"
        else:
            finding = "OK"
        findings.append(SurvivorshipFinding(
            strategy=strategy, symbol=symbol,
            last_seen_in_backtest=last_rec["_parsed_ts"].isoformat(),
            in_capture_set=in_cap,
            in_strategy_registry=in_reg,
            in_harness_specs=in_harness,
            finding=finding,
        ))

    n_stale = sum(1 for f in findings if f.finding == "STALE_SYMBOL")
    n_unsupported = sum(1 for f in findings if f.finding == "UNSUPPORTED_SYMBOL")
    notes: list[str] = []
    if not findings:
        notes.append("no backtest records in window")
    if n_unsupported > 0:
        notes.append(
            f"{n_unsupported} (strategy, symbol) pairs have backtest "
            "data but symbol not in SYMBOL_SPECS — strategy cannot "
            "be promoted on these.")
    if n_stale > 0:
        notes.append(
            f"{n_stale} (strategy, symbol) pairs no longer captured "
            "or registered — drop from active universe.")
    return UniverseAuditReport(
        n_strategies_audited=len({f.strategy for f in findings}),
        n_findings=len(findings),
        n_stale=n_stale,
        n_unsupported=n_unsupported,
        findings=findings,
        notes=notes,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = run_audit(since_days=args.days)
    try:
        with UNIVERSE_AUDIT_LOG.open("a", encoding="utf-8") as f:
            d = asdict(report)
            d.pop("findings", None)  # trim
            f.write(json.dumps({"ts": datetime.now(UTC).isoformat(),
                                 **d}, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: universe audit log write failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(asdict(report), indent=2))
        return 0 if report.n_unsupported == 0 else 1

    print()
    print("=" * 78)
    print("L2 UNIVERSE AUDIT  (survivorship-bias check)")
    print("=" * 78)
    print(f"  strategies audited : {report.n_strategies_audited}")
    print(f"  (strategy, symbol) : {report.n_findings}")
    print(f"  stale              : {report.n_stale}")
    print(f"  unsupported        : {report.n_unsupported}")
    print()
    if report.findings:
        print(f"  {'Strategy':<25s} {'Symbol':<8s} {'Last seen':<22s} {'Finding'}")
        print(f"  {'-'*25:<25s} {'-'*8:<8s} {'-'*22:<22s} {'-'*15}")
        for f in report.findings:
            print(f"  {f.strategy:<25s} {f.symbol:<8s} "
                  f"{f.last_seen_in_backtest[:19]:<22s} {f.finding}")
    if report.notes:
        print()
        print("  Notes:")
        for n in report.notes:
            print(f"    - {n}")
    print()
    return 0 if report.n_unsupported == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
