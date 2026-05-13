"""
EVOLUTIONARY TRADING ALGO  //  scripts.export_tax_ledger
=========================================================
Tax-friendly CSV ledger of realized trades.

Why this exists
---------------
At year-end, your CPA needs a CSV that maps to the IRS Form 8949 +
Form 6781 schedules. Generic backtest output isn't shaped for that.
This script generates the right columns from any Trade source:

* **`--source backtest`**: re-runs a registered bot's strategy
  over the full available data and exports the resulting trades.
  Useful for paper-mode taxes and for reconstructing a tax view
  of historical research.
* **`--source paper-journal`** *(future)*: reads the live paper-
  fill journal once it exists. Same exporter — single source of
  trade truth.

Sections covered
----------------
* **Form 8949** — for spot crypto positions held in a taxable
  account. Short-term (≤1 year holding period) vs long-term
  (>1 year) rows. The exporter computes ``holding_period`` and
  the boolean ``is_long_term`` per trade.

* **Form 6781** — for **Section 1256 contracts**: regulated
  futures contracts (CME E-mini / Micro E-mini, /MNQ, /NQ, /ES,
  /ZN, etc.) get the 60/40 split (60pct long-term, 40pct
  short-term) regardless of holding period. The exporter sets
  ``is_section_1256 = True`` for any registered futures bot.

* **Wash-sale flagging** — a heuristic that marks any losing
  trade where the same symbol & side reopened within 30
  calendar days. Real wash-sale enforcement is the CPA's job;
  the flag is a "look at this row" hint, not authoritative.

Outputs
-------
* `<out>/<bot_id>_tax_ledger_<year>.csv` — one row per closed
  trade, with the columns below.
* `<out>/<bot_id>_tax_summary_<year>.json` — yearly aggregate
  (gross gain, gross loss, net, by-section breakdown).

CSV columns
-----------
1.  ``trade_id``                — deterministic hash, audit-trail link
2.  ``bot_id``                   — registered bot
3.  ``strategy_id``              — strategy version (e.g., btc_corb_v3)
4.  ``symbol``                   — instrument
5.  ``asset_class``              — "futures" | "crypto" | "equity"
6.  ``section``                  — "8949_short" | "8949_long" | "6781"
7.  ``side``                     — BUY | SELL
8.  ``qty``                      — contracts / coins
9.  ``acquired_date``            — entry date (UTC)
10. ``acquired_time``            — entry time (UTC, ISO)
11. ``disposed_date``            — exit date (UTC)
12. ``disposed_time``            — exit time (UTC, ISO)
13. ``cost_basis_usd``           — entry_price * qty
14. ``proceeds_usd``             — exit_price * qty
15. ``gross_pnl_usd``            — proceeds - cost_basis (sign-corrected for SELL-short)
16. ``holding_period_days``      — calendar days
17. ``is_long_term``             — True if holding > 365d (Form 8949 only)
18. ``is_section_1256``          — True for regulated futures (60/40 split applies)
19. ``wash_sale_flag``           — heuristic; True if losing + symbol/side reopened ≤30d
20. ``exit_reason``              — engine label: target/stop/eod/etc
21. ``regime``                   — regime at entry (audit context)
22. ``confluence_score``         — engine score at entry (audit context)
23. ``notes``                    — free-form (e.g., "paper-warmup half-size")

Usage
-----

    # Last year's tax ledger for a single promoted bot
    python -m eta_engine.scripts.export_tax_ledger \\
        --bot-id mnq_futures --year 2025

    # Whole fleet (default)
    python -m eta_engine.scripts.export_tax_ledger --year 2025

    # Backtest reconstruction with a custom date window
    python -m eta_engine.scripts.export_tax_ledger \\
        --bot-id btc_hybrid --start 2024-01-01 --end 2024-12-31

Notes on the sample ledger you'll see today
--------------------------------------------
Until live fills exist, the exporter reconstructs trades by
re-running the bot's promoted strategy over the available data.
That means the **dates are real backtest dates**, not real fill
dates — useful for previewing the schema and for paper-mode tax
work, not yet for actual filing. When the live blotter starts
producing JSONL fill records, the same exporter switches sources
without changing column shape.

Section 1256 contract list (preliminary)
----------------------------------------
The exporter marks the following symbols as Section 1256:
* **/MNQ, /NQ, /ES, /MES** — CME E-mini and Micro E-mini Nasdaq /
  S&P futures. Regulated futures contracts.
* **MNQ1, NQ1, ES1** — TradingView continuous-contract aliases
  for the above.
* **/MBT, /BTC, /MET, /ETH** — CME crypto futures (cash-settled).

Spot crypto (BTC, ETH, SOL) is NOT Section 1256 — it's
Form 8949 (short-term cap gains for ≤1y holding).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

if TYPE_CHECKING:
    from eta_engine.backtest.models import Trade

# Section 1256 contract universe — regulated futures.
_SECTION_1256_SYMBOLS: frozenset[str] = frozenset(
    {
        # Index futures (CME E-mini / Micro)
        "MNQ",
        "MNQ1",
        "/MNQ",
        "NQ",
        "NQ1",
        "/NQ",
        "ES",
        "ES1",
        "/ES",
        "MES",
        "/MES",
        "RTY",
        "RTY1",
        "/RTY",
        "M2K",
        "/M2K",
        "YM",
        "YM1",
        "/YM",
        "MYM",
        "/MYM",
        # CME crypto futures (cash-settled, 1256-eligible)
        "MBT",
        "/MBT",
        "BTC1",
        "MET",
        "/MET",
        "ETH1",
        "/BTC",
        "/ETH",
    }
)

# Spot-crypto symbols — Form 8949, not Section 1256.
_SPOT_CRYPTO_SYMBOLS: frozenset[str] = frozenset(
    {
        "BTC",
        "BTC-USD",
        "BTC/USD",
        "ETH",
        "ETH-USD",
        "ETH/USD",
        "SOL",
        "SOL-USD",
        "SOL/USD",
        "XRP",
        "XRP-USD",
        "XRP/USD",
    }
)

# Wash-sale window per IRS §1091. Calendar days, both sides.
_WASH_SALE_WINDOW_DAYS = 30


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TaxRow:
    """One CSV row, fully classified."""

    trade_id: str
    bot_id: str
    strategy_id: str
    symbol: str
    asset_class: str
    section: str
    side: str
    qty: float
    acquired_date: str
    acquired_time: str
    disposed_date: str
    disposed_time: str
    cost_basis_usd: float
    proceeds_usd: float
    gross_pnl_usd: float
    holding_period_days: float
    is_long_term: bool
    is_section_1256: bool
    wash_sale_flag: bool
    exit_reason: str
    regime: str
    confluence_score: float
    notes: str


def _asset_class_for(symbol: str) -> str:
    s = symbol.upper().replace(" ", "")
    if s in _SECTION_1256_SYMBOLS:
        return "futures"
    if s in _SPOT_CRYPTO_SYMBOLS or s.startswith(("BTC", "ETH", "SOL", "XRP")):
        return "crypto"
    return "equity"


def _is_section_1256(symbol: str) -> bool:
    return symbol.upper().replace(" ", "") in _SECTION_1256_SYMBOLS


def _section_for(*, asset_class: str, is_long_term: bool, is_1256: bool) -> str:
    if is_1256:
        return "6781"
    return "8949_long" if is_long_term else "8949_short"


def _trade_id_for(*, bot_id: str, strategy_id: str, t: Trade) -> str:
    """Deterministic short hash from immutable trade fields."""
    raw = (
        f"{bot_id}|{strategy_id}|{t.symbol}|{t.side}|{t.qty}|"
        f"{t.entry_time.isoformat()}|{t.exit_time.isoformat()}|"
        f"{t.entry_price}|{t.exit_price}"
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]  # noqa: S324


def _wash_sale_flags(rows: list[_TaxRow]) -> list[bool]:
    """Heuristic: a losing trade is flagged if the SAME symbol+side
    is re-opened within 30 calendar days of the loss exit. The IRS
    rule is more nuanced (substantially identical, replacement
    purchase, holding-period adjustments) — this is a hint for the
    CPA, not authoritative."""
    flags = [False] * len(rows)
    # Build a quick (symbol, side) -> sorted entry_time list.
    by_key: dict[tuple[str, str], list[tuple[float, int]]] = {}
    for i, r in enumerate(rows):
        key = (r.symbol, r.side)
        ts = datetime.fromisoformat(r.acquired_time).timestamp()
        by_key.setdefault(key, []).append((ts, i))
    for arr in by_key.values():
        arr.sort()

    for i, r in enumerate(rows):
        if r.gross_pnl_usd >= 0.0:
            continue
        exit_ts = datetime.fromisoformat(r.disposed_time).timestamp()
        window = exit_ts + _WASH_SALE_WINDOW_DAYS * 86400.0
        for ts, j in by_key.get((r.symbol, r.side), []):
            if j == i:
                continue
            if exit_ts <= ts <= window:
                flags[i] = True
                break
    return flags


def _row_for_trade(  # noqa: PLR0913
    *,
    t: Trade,
    bot_id: str,
    strategy_id: str,
    notes: str,
) -> _TaxRow:
    asset_class = _asset_class_for(t.symbol)
    is_1256 = _is_section_1256(t.symbol)
    holding_secs = (t.exit_time - t.entry_time).total_seconds()
    holding_days = holding_secs / 86400.0
    is_long_term = holding_days > 365.0 and not is_1256

    cost_basis = t.entry_price * t.qty
    proceeds = t.exit_price * t.qty
    # Sign of pnl_usd is already correct in the engine for both
    # long and short sides; we keep gross_pnl_usd consistent with it.
    # (Short-sale "proceeds first, cost-to-cover later" semantics
    # are reflected in the engine's pnl_usd, so no flip needed here.)
    gross_pnl = float(t.pnl_usd)

    return _TaxRow(
        trade_id=_trade_id_for(bot_id=bot_id, strategy_id=strategy_id, t=t),
        bot_id=bot_id,
        strategy_id=strategy_id,
        symbol=t.symbol,
        asset_class=asset_class,
        section=_section_for(
            asset_class=asset_class,
            is_long_term=is_long_term,
            is_1256=is_1256,
        ),
        side=t.side,
        qty=round(float(t.qty), 8),
        acquired_date=t.entry_time.date().isoformat(),
        acquired_time=t.entry_time.isoformat(),
        disposed_date=t.exit_time.date().isoformat(),
        disposed_time=t.exit_time.isoformat(),
        cost_basis_usd=round(cost_basis, 2),
        proceeds_usd=round(proceeds, 2),
        gross_pnl_usd=round(gross_pnl, 2),
        holding_period_days=round(holding_days, 4),
        is_long_term=is_long_term,
        is_section_1256=is_1256,
        wash_sale_flag=False,  # filled in after collection
        exit_reason=t.exit_reason or "",
        regime=t.regime or "",
        confluence_score=round(float(t.confluence_score), 3),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Backtest source
# ---------------------------------------------------------------------------


def _trades_from_backtest(  # type: ignore[no-untyped-def]  # noqa: ANN202
    *,
    bot_id: str,
    start: datetime | None,
    end: datetime | None,
):
    """Run the bot's registered strategy over its data + return Trade list."""
    from eta_engine.backtest import BacktestConfig, BacktestEngine
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline
    from eta_engine.scripts.run_drift_watchdog import _build_strategy
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(bot_id)
    if a is None:
        return None, None
    ds = default_library().get(symbol=a.symbol, timeframe=a.timeframe)
    if ds is None:
        return None, a
    bars = default_library().load_bars(ds, require_positive_prices=True)
    if start is not None:
        bars = [b for b in bars if b.timestamp >= start]
    if end is not None:
        bars = [b for b in bars if b.timestamp < end]
    if not bars:
        return None, a
    cfg = BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol=a.symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=a.confluence_threshold,
        max_trades_per_day=10,
    )
    strat = _build_strategy(a)
    res = BacktestEngine(
        pipeline=FeaturePipeline.default(),
        config=cfg,
        strategy=strat,
    ).run(bars)
    return res.trades, a


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def _write_csv(out_path: Path, rows: list[_TaxRow]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "trade_id",
        "bot_id",
        "strategy_id",
        "symbol",
        "asset_class",
        "section",
        "side",
        "qty",
        "acquired_date",
        "acquired_time",
        "disposed_date",
        "disposed_time",
        "cost_basis_usd",
        "proceeds_usd",
        "gross_pnl_usd",
        "holding_period_days",
        "is_long_term",
        "is_section_1256",
        "wash_sale_flag",
        "exit_reason",
        "regime",
        "confluence_score",
        "notes",
    ]
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(
                {k: getattr(r, k) for k in fieldnames},
            )


def _write_summary(out_path: Path, rows: list[_TaxRow], year: int) -> None:
    summary: dict[str, Any] = {
        "year": year,
        "n_trades": len(rows),
        "by_section": {},
        "by_bot": {},
        "totals": {
            "gross_gain_usd": 0.0,
            "gross_loss_usd": 0.0,
            "net_pnl_usd": 0.0,
        },
        "wash_sale_flagged": 0,
        "_notes": (
            "Form 8949 columns are split into 8949_short (≤1y holding) "
            "and 8949_long (>1y). Form 6781 captures Section 1256 "
            "contracts (regulated futures) with the 60/40 LT/ST rule "
            "applied at the form level — see your CPA. wash_sale_flag "
            "is heuristic only."
        ),
    }
    by_section: dict[str, dict[str, float]] = {}
    by_bot: dict[str, dict[str, float]] = {}
    for r in rows:
        sec = by_section.setdefault(
            r.section,
            {"n_trades": 0, "gross_gain_usd": 0.0, "gross_loss_usd": 0.0, "net_usd": 0.0},
        )
        sec["n_trades"] += 1
        if r.gross_pnl_usd >= 0:
            sec["gross_gain_usd"] += r.gross_pnl_usd
            summary["totals"]["gross_gain_usd"] += r.gross_pnl_usd
        else:
            sec["gross_loss_usd"] += r.gross_pnl_usd
            summary["totals"]["gross_loss_usd"] += r.gross_pnl_usd
        sec["net_usd"] += r.gross_pnl_usd
        summary["totals"]["net_pnl_usd"] += r.gross_pnl_usd
        if r.wash_sale_flag:
            summary["wash_sale_flagged"] += 1

        b = by_bot.setdefault(
            r.bot_id,
            {"n_trades": 0, "net_usd": 0.0},
        )
        b["n_trades"] += 1
        b["net_usd"] += r.gross_pnl_usd

    # Round everything for readability.
    for sec in by_section.values():
        for k in ("gross_gain_usd", "gross_loss_usd", "net_usd"):
            sec[k] = round(sec[k], 2)
    for b in by_bot.values():
        b["net_usd"] = round(b["net_usd"], 2)
    for k in ("gross_gain_usd", "gross_loss_usd", "net_pnl_usd"):
        summary["totals"][k] = round(summary["totals"][k], 2)

    summary["by_section"] = by_section
    summary["by_bot"] = by_bot

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(prog="export_tax_ledger")
    p.add_argument(
        "--source",
        default="backtest",
        choices=["backtest", "paper-journal"],
        help="trade-source mode (paper-journal not yet wired)",
    )
    p.add_argument(
        "--bot-id",
        default=None,
        help="single bot; default = all production bots",
    )
    p.add_argument(
        "--year",
        type=int,
        default=datetime.now(UTC).year - 1,
        help="tax year (default: previous calendar year)",
    )
    p.add_argument(
        "--start",
        help="ISO date YYYY-MM-DD (overrides --year)",
    )
    p.add_argument(
        "--end",
        help="ISO date YYYY-MM-DD (overrides --year)",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "docs" / "tax_ledger",
    )
    args = p.parse_args()

    if args.source == "paper-journal":
        print("[export_tax_ledger] paper-journal source not yet wired")
        print("  When live fills land in the blotter JSONL, this branch")
        print("  will read from there. Fall back to --source backtest")
        print("  for paper-mode previews and historical reconstructions.")
        return 1

    if args.start:
        start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    else:
        start = datetime(args.year, 1, 1, tzinfo=UTC)
    if args.end:
        end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    else:
        end = datetime(args.year + 1, 1, 1, tzinfo=UTC)

    if args.bot_id:
        bot_ids = [args.bot_id]
    else:
        # Pull production-promoted bots from the registry.
        from eta_engine.strategies.per_bot_registry import all_assignments

        bot_ids = []
        for a in all_assignments():
            # Skip explicitly deactivated bots.
            if a.extras.get("deactivated"):
                continue
            bot_ids.append(a.bot_id)

    print(
        f"[export_tax_ledger] year={args.year} "
        f"window={start.date()} -> {end.date()} "
        f"bots={len(bot_ids)} -> {args.out_dir}",
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    grand_rows: list[_TaxRow] = []
    for bot_id in bot_ids:
        trades, a = _trades_from_backtest(
            bot_id=bot_id,
            start=start,
            end=end,
        )
        if trades is None or a is None or not trades:
            print(f"  - {bot_id}: SKIP (no data / no trades)")
            continue

        # Filter to in-window (entry inside the year)
        in_year = [t for t in trades if start <= t.entry_time < end]
        if not in_year:
            print(f"  - {bot_id}: SKIP (no trades in year)")
            continue

        notes = ""
        warmup = a.extras.get("warmup_policy") or {}
        if isinstance(warmup, dict) and warmup.get("warmup_days"):
            notes = f"warmup-policy: half-size first {warmup['warmup_days']}d post-promotion"

        rows: list[_TaxRow] = [
            _row_for_trade(
                t=t,
                bot_id=bot_id,
                strategy_id=a.strategy_id,
                notes=notes,
            )
            for t in in_year
        ]
        # Wash-sale pass
        flags = _wash_sale_flags(rows)
        rows = [_TaxRow(**{**vars(r), "wash_sale_flag": f}) for r, f in zip(rows, flags, strict=False)]
        out_csv = args.out_dir / f"{bot_id}_tax_ledger_{args.year}.csv"
        out_json = args.out_dir / f"{bot_id}_tax_summary_{args.year}.json"
        _write_csv(out_csv, rows)
        _write_summary(out_json, rows, year=args.year)
        net = sum(r.gross_pnl_usd for r in rows)
        print(
            f"  - {bot_id}: {len(rows)} trades  net=${net:+,.2f}  -> {out_csv.name}",
        )
        grand_rows.extend(rows)

    if grand_rows:
        out_csv = args.out_dir / f"FLEET_tax_ledger_{args.year}.csv"
        out_json = args.out_dir / f"FLEET_tax_summary_{args.year}.json"
        _write_csv(out_csv, grand_rows)
        _write_summary(out_json, grand_rows, year=args.year)
        net = sum(r.gross_pnl_usd for r in grand_rows)
        print(
            f"\n[export_tax_ledger] fleet total: {len(grand_rows)} trades, net ${net:+,.2f}",
        )
        print(f"  fleet csv:     {out_csv}")
        print(f"  fleet summary: {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
