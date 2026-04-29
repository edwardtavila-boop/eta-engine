"""
EVOLUTIONARY TRADING ALGO  //  scripts.hydrate_canonical_market_data
====================================================================
Hydrate the canonical ETA market-data roots from the best data we can
find locally, then backfill missing crypto history with the framework's
existing public-data fetchers.

Why this exists
---------------
The user asked to "find" the real data and make it "fully accessible by
the framework." In practice the best futures bars were hiding in legacy
``apex_predator`` worktrees under ``.codex`` while the canonical ETA
roots only held a thin subset. Some longer-horizon NQ data also lived in
the current workspace root but in the older "main" schema instead of the
history schema the strategy runners prefer.

This script does three things:

1. Imports the deepest legacy futures bars it can find into
   ``C:\\EvolutionaryTradingAlgo\\mnq_data\\history``.
2. Converts the canonical workspace's existing main-shape futures files
   into the same history schema when they fill gaps.
3. Fetches missing crypto price/funding/supporting series into
   ``C:\\EvolutionaryTradingAlgo\\data\\crypto\\history`` and
   ``C:\\EvolutionaryTradingAlgo\\mnq_data\\history``.

Usage::

    python -m eta_engine.scripts.hydrate_canonical_market_data
    python -m eta_engine.scripts.hydrate_canonical_market_data --skip-crypto
    python -m eta_engine.scripts.hydrate_canonical_market_data --force
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from glob import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.data.library import _parse_filename, _probe  # noqa: E402
from eta_engine.scripts.fetch_btc_bars import _filename as _crypto_filename  # noqa: E402
from eta_engine.scripts.fetch_btc_bars import fetch_bars as fetch_crypto_bars  # noqa: E402
from eta_engine.scripts.fetch_btc_bars import write_csv as write_crypto_bars  # noqa: E402
from eta_engine.scripts.fetch_etf_flows_farside import fetch as fetch_btc_etf_flows  # noqa: E402
from eta_engine.scripts.fetch_fear_greed_alternative import fetch as fetch_btc_fear_greed  # noqa: E402
from eta_engine.scripts.fetch_funding_rates import (  # noqa: E402
    fetch_funding,
)
from eta_engine.scripts.fetch_funding_rates import (  # noqa: E402
    write_csv as write_funding_csv,
)
from eta_engine.scripts.fetch_lth_proxy import compute_and_write as build_btc_lth_proxy  # noqa: E402
from eta_engine.scripts.workspace_roots import (  # noqa: E402
    CRYPTO_HISTORY_ROOT,
    MNQ_DATA_ROOT,
    MNQ_HISTORY_ROOT,
    ensure_dir,
)

_CONTINUOUS_FUTURES = frozenset({"MNQ", "NQ", "ES", "MES", "RTY"})
_VIX_ALIASES = frozenset({"VIX_YF", "VIX"})


@dataclass(frozen=True)
class ImportCandidate:
    source: Path
    target: Path
    source_kind: str  # "history" or "main"
    note: str
    row_count: int


@dataclass(frozen=True)
class CryptoPlan:
    symbol: str
    timeframe: str
    months: int


_CRYPTO_BAR_PLAN: tuple[CryptoPlan, ...] = (
    CryptoPlan("BTC", "1h", 24),
    CryptoPlan("BTC", "D", 60),
    CryptoPlan("ETH", "5m", 6),
    CryptoPlan("ETH", "1h", 24),
    CryptoPlan("ETH", "D", 60),
    CryptoPlan("SOL", "1h", 24),
    CryptoPlan("SOL", "D", 24),
)

_CRYPTO_FUNDING_SYMBOLS: tuple[str, ...] = ("BTC", "ETH", "SOL")


def _normalize_symbol(raw: str) -> str:
    symbol = raw.upper()
    if symbol in _VIX_ALIASES:
        return "VIX"
    if symbol in _CONTINUOUS_FUTURES:
        return f"{symbol}1"
    return symbol


def _normalize_timeframe(raw: str) -> str:
    return raw.upper() if raw.lower() in {"d", "w"} else raw


def _canonical_history_name_from_databento(name: str) -> str | None:
    stem = Path(name).stem.lower()
    if stem == "vix_yf_d":
        return "VIX_D.csv"
    parts = stem.rsplit("_", 1)
    if len(parts) != 2:
        return None
    raw_symbol, raw_tf = parts
    symbol = _normalize_symbol(raw_symbol)
    timeframe = _normalize_timeframe(raw_tf)
    if not timeframe:
        return None
    return f"{symbol}_{timeframe}.csv"


def _canonical_history_name_from_main(name: str) -> str | None:
    parsed = _parse_filename(Path(name))
    if parsed is not None:
        symbol, timeframe, schema_kind = parsed
        if schema_kind != "main":
            return None
        return f"{_normalize_symbol(symbol)}_{timeframe}.csv"
    stem = Path(name).stem.lower()
    parts = stem.rsplit("_", 1)
    if len(parts) != 2:
        return None
    raw_symbol, raw_tf = parts
    symbol = _normalize_symbol(raw_symbol)
    timeframe = _normalize_timeframe(raw_tf)
    return f"{symbol}_{timeframe}.csv"


def _probe_rows(path: Path, source_kind: str) -> int:
    probe = _probe(path, source_kind)
    return probe[0] if probe is not None else 0


def _iter_legacy_databento_dirs() -> list[Path]:
    home = Path.home()
    patterns = (
        str(home / ".codex" / "worktrees" / "*" / "apex_predator" / "data" / "bars" / "databento"),
        str(
            home
            / ".config"
            / "superpowers"
            / "worktrees"
            / "apex_predator"
            / "*"
            / "data"
            / "bars"
            / "databento"
        ),
    )
    out: list[Path] = []
    for pattern in patterns:
        out.extend(Path(match) for match in sorted(glob(pattern)))
    return [p for p in out if p.exists() and p.is_dir()]


def _collect_futures_candidates() -> dict[Path, ImportCandidate]:
    best: dict[Path, ImportCandidate] = {}

    for root in _iter_legacy_databento_dirs():
        for source in sorted(root.glob("*.csv")):
            target_name = _canonical_history_name_from_databento(source.name)
            if target_name is None:
                continue
            row_count = _probe_rows(source, "history")
            if row_count <= 0:
                continue
            target = MNQ_HISTORY_ROOT / target_name
            candidate = ImportCandidate(
                source=source,
                target=target,
                source_kind="history",
                note="legacy_databento",
                row_count=row_count,
            )
            current = best.get(target)
            if current is None or (candidate.row_count, candidate.source.stat().st_mtime) > (
                current.row_count,
                current.source.stat().st_mtime,
            ):
                best[target] = candidate

    for source in sorted(MNQ_DATA_ROOT.glob("*.csv")):
        target_name = _canonical_history_name_from_main(source.name)
        if target_name is None:
            continue
        row_count = _probe_rows(source, "main")
        if row_count <= 0:
            continue
        target = MNQ_HISTORY_ROOT / target_name
        candidate = ImportCandidate(
            source=source,
            target=target,
            source_kind="main",
            note="canonical_main",
            row_count=row_count,
        )
        current = best.get(target)
        if current is None or candidate.row_count > current.row_count:
            best[target] = candidate

    return best


def _convert_main_to_history(source: Path, target: Path) -> int:
    rows = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(f"{target.suffix}.tmp")
    with source.open("r", encoding="utf-8", newline="") as src, tmp.open(
        "w", encoding="utf-8", newline="",
    ) as dst:
        reader = csv.DictReader(src)
        writer = csv.writer(dst)
        writer.writerow(["time", "open", "high", "low", "close", "volume"])
        for row in reader:
            epoch_s = row.get("epoch_s")
            if epoch_s:
                try:
                    ts = int(float(epoch_s))
                except ValueError:
                    ts = 0
            else:
                raw = (row.get("timestamp_utc") or row.get("timestamp") or "").strip()
                if raw.endswith("Z"):
                    raw = raw[:-1] + "+00:00"
                try:
                    dt = datetime.fromisoformat(raw)
                except ValueError:
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                ts = int(dt.timestamp())
            try:
                writer.writerow([
                    ts,
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row.get("volume", 0.0) or 0.0),
                ])
            except (KeyError, ValueError):
                continue
            rows += 1
    tmp.replace(target)
    return rows


def _import_futures(*, force: bool = False) -> tuple[int, int]:
    ensure_dir(MNQ_HISTORY_ROOT)
    imported = 0
    skipped = 0
    for target, candidate in sorted(_collect_futures_candidates().items()):
        existing_rows = _probe_rows(target, "history") if target.exists() else 0
        if not force and existing_rows >= candidate.row_count:
            print(
                f"[hydrate:futures] skip {target.name} "
                f"(existing rows={existing_rows:,} >= source rows={candidate.row_count:,})",
            )
            skipped += 1
            continue
        print(
            f"[hydrate:futures] {candidate.note}: {candidate.source} -> {target} "
            f"({candidate.row_count:,} rows)",
        )
        if candidate.source_kind == "history":
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate.source, target)
        else:
            wrote = _convert_main_to_history(candidate.source, target)
            if wrote <= 0:
                print(f"  [warn] conversion produced zero rows for {candidate.source}")
                skipped += 1
                continue
        imported += 1
    return imported, skipped


def _fetch_crypto_prices(*, force: bool = False) -> tuple[int, int]:
    ensure_dir(CRYPTO_HISTORY_ROOT)
    written = 0
    skipped = 0
    now = datetime.now(UTC)
    for plan in _CRYPTO_BAR_PLAN:
        out_path = CRYPTO_HISTORY_ROOT / _crypto_filename(plan.symbol, plan.timeframe)
        if out_path.exists() and out_path.stat().st_size > 100 and not force:
            print(f"[hydrate:crypto] skip {out_path.name} (already present)")
            skipped += 1
            continue
        start = now - timedelta(days=30 * plan.months)
        print(
            f"[hydrate:crypto] fetching {plan.symbol}/{plan.timeframe} "
            f"{start.date()} -> {now.date()}",
        )
        rows = fetch_crypto_bars(
            symbol=plan.symbol,
            timeframe=plan.timeframe,
            start=start,
            end=now,
        )
        if not rows:
            print(f"  [warn] zero rows fetched for {plan.symbol}/{plan.timeframe}")
            continue
        write_crypto_bars(out_path, rows)
        print(f"  wrote {len(rows):,} rows -> {out_path}")
        written += 1
    return written, skipped


def _fetch_crypto_funding(*, force: bool = False) -> tuple[int, int]:
    ensure_dir(CRYPTO_HISTORY_ROOT)
    written = 0
    skipped = 0
    end = datetime.now(UTC)
    start = end - timedelta(days=90)
    for symbol in _CRYPTO_FUNDING_SYMBOLS:
        out_path = CRYPTO_HISTORY_ROOT / f"{symbol}FUND_8h.csv"
        if out_path.exists() and out_path.stat().st_size > 100 and not force:
            print(f"[hydrate:funding] skip {out_path.name} (already present)")
            skipped += 1
            continue
        print(f"[hydrate:funding] fetching {symbol} {start.date()} -> {end.date()}")
        rows = fetch_funding(symbol=symbol, start=start, end=end)
        if not rows:
            print(f"  [warn] zero funding rows fetched for {symbol}")
            continue
        write_funding_csv(out_path, rows)
        print(f"  wrote {len(rows):,} rows -> {out_path}")
        written += 1
    return written, skipped


def _fetch_supporting_btc_series(*, force: bool = False) -> tuple[int, int]:
    wrote = 0
    skipped = 0

    etf_path = MNQ_HISTORY_ROOT / "BTC_ETF_FLOWS.csv"
    if etf_path.exists() and etf_path.stat().st_size > 100 and not force:
        print(f"[hydrate:support] skip {etf_path.name} (already present)")
        skipped += 1
    else:
        rows = fetch_btc_etf_flows(etf_path, dry_run=False)
        if rows > 0:
            wrote += 1

    fg_path = MNQ_HISTORY_ROOT / "BTC_FEAR_GREED.csv"
    if fg_path.exists() and fg_path.stat().st_size > 100 and not force:
        print(f"[hydrate:support] skip {fg_path.name} (already present)")
        skipped += 1
    else:
        rows = fetch_btc_fear_greed(fg_path, dry_run=False)
        if rows > 0:
            wrote += 1

    lth_path = CRYPTO_HISTORY_ROOT / "BTC_LTH_PROXY.csv"
    btc_daily = CRYPTO_HISTORY_ROOT / "BTC_D.csv"
    if lth_path.exists() and lth_path.stat().st_size > 100 and not force:
        print(f"[hydrate:support] skip {lth_path.name} (already present)")
        skipped += 1
    else:
        rows = build_btc_lth_proxy(btc_daily, lth_path, dry_run=False)
        if rows > 0:
            wrote += 1

    return wrote, skipped


def main() -> int:
    p = argparse.ArgumentParser(prog="hydrate_canonical_market_data")
    p.add_argument("--skip-futures", action="store_true")
    p.add_argument("--skip-crypto", action="store_true")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()

    imported = skipped = 0
    if not args.skip_futures:
        fut_imported, fut_skipped = _import_futures(force=args.force)
        imported += fut_imported
        skipped += fut_skipped

    if not args.skip_crypto:
        price_written, price_skipped = _fetch_crypto_prices(force=args.force)
        fund_written, fund_skipped = _fetch_crypto_funding(force=args.force)
        support_written, support_skipped = _fetch_supporting_btc_series(force=args.force)
        imported += price_written + fund_written + support_written
        skipped += price_skipped + fund_skipped + support_skipped

    print(
        f"[hydrate] complete: imported={imported} skipped={skipped} "
        f"mnq_history={MNQ_HISTORY_ROOT} crypto_history={CRYPTO_HISTORY_ROOT}",
    )
    return 0 if imported > 0 or skipped > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
