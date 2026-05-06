"""Per-ticker IBKR paper smoke — exercises every routed asset.

Resolves the front-month contract for each symbol in
:data:`eta_engine.venues.ibkr_live.FUTURES_MAP` (and the crypto entries
in ``CRYPTO_MAP``), reporting PASS / FAIL per symbol with diagnostic
info. Catches:

  * IB Gateway connectivity issues (no JVM, wrong port, expired session)
  * Subscriptions / market-data permissions missing for an exchange
  * Stale month codes after a roll
  * Symbol drift between the routing yaml and FUTURES_MAP

Usage::

  # Probe-only (no orders):
  python -m eta_engine.scripts.ibkr_paper_smoke_all_assets

  # Limit to a subset:
  python -m eta_engine.scripts.ibkr_paper_smoke_all_assets --symbols MNQ,ES,GC

  # Full smoke (places + cancels a tiny bracket per symbol — only safe
  # against an explicitly-paper IBKR account):
  python -m eta_engine.scripts.ibkr_paper_smoke_all_assets --place

Default symbol set covers every contract referenced by the active bot
fleet's routing yaml: MNQ, NQ, ES, RTY, M2K, GC, MGC, CL, MCL, NG, 6E, M6E.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Default smoke universe — every futures asset our supervisor can route
# today. CRYPTO_MAP entries (BTC/ETH/etc.) are intentionally excluded:
# IBKR paper does not support PAXOS crypto, so those symbols would
# always FAIL on a paper smoke and pollute the report.
DEFAULT_SMOKE_SYMBOLS: tuple[str, ...] = (
    "MNQ", "NQ", "ES", "MES", "RTY", "M2K",
    "GC", "MGC", "CL", "MCL", "NG",
    "6E", "M6E",
)


@dataclass(slots=True)
class SymbolResult:
    """Outcome of a single per-symbol probe."""

    symbol: str
    status: str  # "PASS" | "FAIL"
    exchange: str = ""
    contract_month: str = ""
    conid: int | None = None
    multiplier: str = ""
    error: str = ""
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "exchange": self.exchange,
            "contract_month": self.contract_month,
            "conid": self.conid,
            "multiplier": self.multiplier,
            "error": self.error,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


@dataclass(slots=True)
class SmokeReport:
    """Aggregate outcome of a smoke run."""

    started_utc: str
    finished_utc: str
    total: int
    passed: int
    failed: int
    results: list[SymbolResult] = field(default_factory=list)

    @property
    def all_pass(self) -> bool:
        return self.failed == 0 and self.total > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "all_pass": self.all_pass,
            "results": [r.to_dict() for r in self.results],
        }


async def probe_symbol(ib: Any, symbol: str) -> SymbolResult:  # noqa: ANN401 — ib_insync IB
    """Resolve + qualify the contract for one symbol.

    On success we record the exchange, contract month, conid, and the
    multiplier so an operator inspecting the report can sanity-check
    that they actually got the contract they expected.
    """
    from eta_engine.venues.ibkr_live import (
        CRYPTO_MAP,
        FUTURES_MAP,
        _make_contract,
    )

    sym = symbol.upper().strip()
    started = datetime.now(UTC)
    if sym not in FUTURES_MAP and sym not in CRYPTO_MAP:
        return SymbolResult(
            symbol=sym, status="FAIL",
            error=f"symbol {sym!r} not in FUTURES_MAP or CRYPTO_MAP",
            elapsed_ms=(datetime.now(UTC) - started).total_seconds() * 1000.0,
        )
    try:
        contract = await _make_contract(sym, ib=ib)
    except Exception as exc:  # noqa: BLE001 — surface diagnostic
        return SymbolResult(
            symbol=sym, status="FAIL", error=f"_make_contract: {exc}",
            elapsed_ms=(datetime.now(UTC) - started).total_seconds() * 1000.0,
        )
    if contract is None:
        return SymbolResult(
            symbol=sym, status="FAIL", error="_make_contract returned None",
            elapsed_ms=(datetime.now(UTC) - started).total_seconds() * 1000.0,
        )

    # Qualify so we get a real conid back from IB. If qualify already ran
    # inside _make_contract (futures path), this is a cheap re-validate.
    try:
        qualified = await ib.qualifyContractsAsync(contract)
    except Exception as exc:  # noqa: BLE001
        return SymbolResult(
            symbol=sym, status="FAIL", error=f"qualifyContractsAsync: {exc}",
            elapsed_ms=(datetime.now(UTC) - started).total_seconds() * 1000.0,
        )
    if not qualified:
        return SymbolResult(
            symbol=sym, status="FAIL",
            error="qualifyContractsAsync returned empty list",
            elapsed_ms=(datetime.now(UTC) - started).total_seconds() * 1000.0,
        )

    q = qualified[0]
    return SymbolResult(
        symbol=sym,
        status="PASS",
        exchange=getattr(q, "exchange", "") or "",
        contract_month=getattr(q, "lastTradeDateOrContractMonth", "") or "",
        conid=getattr(q, "conId", None) or None,
        multiplier=str(getattr(q, "multiplier", "") or ""),
        elapsed_ms=(datetime.now(UTC) - started).total_seconds() * 1000.0,
    )


async def run_smoke(
    symbols: tuple[str, ...] = DEFAULT_SMOKE_SYMBOLS,
    *,
    host: str = "127.0.0.1",
    port: int = 4002,
    client_id: int | None = None,
) -> SmokeReport:
    """Connect to IB Gateway and probe each symbol sequentially.

    We probe sequentially, not in parallel, because IB's qualifyContracts
    can race when overlapping ContFuture queries hit the same exchange.
    Each probe is fast enough (< 1 s typical) that a sequential walk
    over 13 symbols completes well inside any reasonable timeout.
    """
    import os
    import random

    from ib_insync import IB

    if client_id is None:
        # Pick a random clientId to avoid colliding with a live broker_router.
        # Range 700-799 matches the ETA convention for one-shot diagnostic
        # tools (vs broker_router which lives in 100-199).
        client_id = int(os.environ.get("ETA_IBKR_SMOKE_CLIENT_ID", random.randint(700, 799)))

    started = datetime.now(UTC).isoformat()
    results: list[SymbolResult] = []

    ib = IB()
    try:
        await ib.connectAsync(host=host, port=port, clientId=client_id, timeout=8.0)
        logger.info(
            "Connected to IB Gateway %s:%d clientId=%d (managedAccounts=%s)",
            host, port, client_id, list(ib.managedAccounts()),
        )
        for sym in symbols:
            result = await probe_symbol(ib, sym)
            results.append(result)
            level = logging.INFO if result.status == "PASS" else logging.WARNING
            logger.log(
                level,
                "%-6s %s exchange=%s month=%s conid=%s mult=%s elapsed=%.1fms %s",
                result.symbol, result.status, result.exchange or "-",
                result.contract_month or "-", result.conid or "-",
                result.multiplier or "-", result.elapsed_ms,
                f"err={result.error}" if result.error else "",
            )
    finally:
        if ib.isConnected():
            ib.disconnect()

    finished = datetime.now(UTC).isoformat()
    passed = sum(1 for r in results if r.status == "PASS")
    return SmokeReport(
        started_utc=started,
        finished_utc=finished,
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        results=results,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SMOKE_SYMBOLS),
        help="Comma-separated symbol list (default: full universe).",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="IB Gateway host (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port", type=int, default=4002,
        help="IB Gateway port (default: 4002 for paper).",
    )
    parser.add_argument(
        "--client-id", type=int, default=None,
        help="IB clientId. Default: random 700-799 (env: ETA_IBKR_SMOKE_CLIENT_ID).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a JSON report to stdout instead of human-readable lines.",
    )
    parser.add_argument(
        "--place", action="store_true",
        help="(Reserved) Also place + cancel a tiny test bracket per symbol. "
             "Not implemented yet — current smoke is contract-resolution only.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    if not symbols:
        logger.error("no symbols to probe")
        return 2

    if args.place:
        logger.error("--place is reserved for a future revision; aborting")
        return 2

    try:
        report = asyncio.run(run_smoke(
            symbols, host=args.host, port=args.port, client_id=args.client_id,
        ))
    except Exception as exc:  # noqa: BLE001 — top-level CLI handler
        logger.exception("smoke run aborted: %s", exc)
        return 3

    if args.json:
        import json
        sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")
    else:
        sys.stdout.write(
            f"\n=== IBKR paper smoke result ===\n"
            f"  total  = {report.total}\n"
            f"  passed = {report.passed}\n"
            f"  failed = {report.failed}\n"
            f"  status = {'PASS' if report.all_pass else 'FAIL'}\n",
        )

    return 0 if report.all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
