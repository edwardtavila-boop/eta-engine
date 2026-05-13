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
``evolutionary_trading_algo`` worktrees under ``.codex`` while the canonical ETA
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
    python -m eta_engine.scripts.hydrate_canonical_market_data --dry-run
    python -m eta_engine.scripts.hydrate_canonical_market_data --skip-crypto
    python -m eta_engine.scripts.hydrate_canonical_market_data --force
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from collections import defaultdict
from contextlib import suppress
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
from eta_engine.scripts.fetch_onchain_history import (  # noqa: E402
    _COLUMNS_BY_SYMBOL as _ONCHAIN_COLUMNS,
)
from eta_engine.scripts.fetch_onchain_history import (  # noqa: E402
    _btc_daily_series,
    _eth_daily_series,
)
from eta_engine.scripts.fetch_onchain_history import (  # noqa: E402
    _filename as _onchain_filename,
)
from eta_engine.scripts.fetch_onchain_history import (  # noqa: E402
    write_csv as write_onchain_csv,
)
from eta_engine.scripts.workspace_roots import (  # noqa: E402
    CRYPTO_HISTORY_ROOT,
    CRYPTO_ONCHAIN_ROOT,
    MNQ_DATA_ROOT,
    MNQ_HISTORY_ROOT,
    ensure_dir,
    ensure_parent,
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
    CryptoPlan("BTC", "1m", 6),
    CryptoPlan("BTC", "5m", 24),
    CryptoPlan("BTC", "1h", 24),
    CryptoPlan("BTC", "D", 60),
    CryptoPlan("ETH", "5m", 6),
    CryptoPlan("ETH", "1h", 24),
    CryptoPlan("ETH", "D", 60),
    CryptoPlan("SOL", "5m", 12),
    CryptoPlan("SOL", "1h", 24),
    CryptoPlan("SOL", "D", 24),
)

_CRYPTO_FUNDING_SYMBOLS: tuple[str, ...] = ("BTC", "ETH", "SOL")

_ONCHAIN_FETCHERS = {
    "BTC": _btc_daily_series,
    "ETH": _eth_daily_series,
}

_FUTURES_RESAMPLE_PLAN: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("MNQ1", "1h", ("1m", "5m")),
    ("MNQ1", "4h", ("1h", "1m", "5m")),
)


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
        str(home / ".codex" / "worktrees" / "*" / "evolutionary_trading_algo" / "data" / "bars" / "databento"),
        str(
            home
            / ".config"
            / "superpowers"
            / "worktrees"
            / "evolutionary_trading_algo"
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


def _collect_futures_candidates(*, max_legacy_files: int | None = 200) -> dict[Path, ImportCandidate]:
    best: dict[Path, ImportCandidate] = {}
    legacy_seen = 0
    legacy_limit = None if max_legacy_files is None or max_legacy_files <= 0 else max_legacy_files

    for root in _iter_legacy_databento_dirs():
        for source in sorted(root.glob("*.csv")):
            if legacy_limit is not None and legacy_seen >= legacy_limit:
                break
            legacy_seen += 1
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
        if legacy_limit is not None and legacy_seen >= legacy_limit:
            break

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
    with (
        source.open("r", encoding="utf-8", newline="") as src,
        tmp.open(
            "w",
            encoding="utf-8",
            newline="",
        ) as dst,
    ):
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
                writer.writerow(
                    [
                        ts,
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                        float(row.get("volume", 0.0) or 0.0),
                    ]
                )
            except (KeyError, ValueError):
                continue
            rows += 1
    if rows <= 0:
        with suppress(OSError):
            tmp.unlink()
        return 0
    tmp.replace(target)
    return rows


def _read_history_ohlcv(path: Path) -> list[dict[str, float]]:
    if not path.exists():
        return []
    out: list[dict[str, float]] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                out.append(
                    {
                        "time": int(float(row["time"])),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row.get("volume", 0.0) or 0.0),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
    out.sort(key=lambda row: row["time"])
    return out


def _bucket_time(ts: int, timeframe: str) -> int:
    dt = datetime.fromtimestamp(ts, UTC)
    if timeframe == "1h":
        bucket = dt.replace(minute=0, second=0, microsecond=0)
    elif timeframe == "4h":
        bucket = dt.replace(
            hour=(dt.hour // 4) * 4,
            minute=0,
            second=0,
            microsecond=0,
        )
    elif timeframe == "D":
        bucket = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif timeframe == "W":
        bucket = dt.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=dt.weekday())
    else:
        raise ValueError(f"unsupported resample timeframe: {timeframe}")
    return int(bucket.timestamp())


def _resample_rows(rows: list[dict[str, float]], timeframe: str) -> list[dict[str, float]]:
    if not rows:
        return []
    buckets: dict[int, list[dict[str, float]]] = defaultdict(list)
    for row in rows:
        buckets[_bucket_time(int(row["time"]), timeframe)].append(row)
    out: list[dict[str, float]] = []
    for bucket_ts in sorted(buckets):
        bucket_rows = sorted(buckets[bucket_ts], key=lambda row: row["time"])
        out.append(
            {
                "time": bucket_ts,
                "open": bucket_rows[0]["open"],
                "high": max(row["high"] for row in bucket_rows),
                "low": min(row["low"] for row in bucket_rows),
                "close": bucket_rows[-1]["close"],
                "volume": sum(row["volume"] for row in bucket_rows),
            }
        )
    return out


def _write_history_rows(path: Path, rows: list[dict[str, float]]) -> int:
    if not rows:
        return 0
    ensure_parent(path)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["time", "open", "high", "low", "close", "volume"])
        for row in rows:
            writer.writerow(
                [
                    int(row["time"]),
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row["volume"],
                ]
            )
    return len(rows)


def _import_futures(
    *,
    force: bool = False,
    dry_run: bool = False,
    max_legacy_files: int | None = 200,
) -> tuple[int, int]:
    ensure_dir(MNQ_HISTORY_ROOT)
    imported = 0
    skipped = 0
    for target, candidate in sorted(_collect_futures_candidates(max_legacy_files=max_legacy_files).items()):
        existing_rows = _probe_rows(target, "history") if target.exists() else 0
        if not force and existing_rows >= candidate.row_count:
            print(
                f"[hydrate:futures] skip {target.name} "
                f"(existing rows={existing_rows:,} >= source rows={candidate.row_count:,})",
            )
            skipped += 1
            continue
        print(
            f"[hydrate:futures] {candidate.note}: {candidate.source} -> {target} ({candidate.row_count:,} rows)",
        )
        if dry_run:
            skipped += 1
            continue
        if candidate.source_kind == "history":
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate.source, target)
        else:
            wrote = _convert_main_to_history(candidate.source, target)
            if wrote <= 0:
                if target.exists() and _probe_rows(target, "history") <= 0:
                    with suppress(OSError):
                        target.unlink()
                print(f"  [warn] conversion produced zero rows for {candidate.source}")
                skipped += 1
                continue
        imported += 1
    return imported, skipped


def _synthesize_futures_timeframes(
    *,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[int, int]:
    written = 0
    skipped = 0
    for symbol, target_tf, source_candidates in _FUTURES_RESAMPLE_PLAN:
        target = MNQ_HISTORY_ROOT / f"{symbol}_{target_tf}.csv"
        existing_rows = _probe_rows(target, "history") if target.exists() else 0
        if existing_rows > 0 and not force:
            print(
                f"[hydrate:resample] skip {target.name} (existing rows={existing_rows:,})",
            )
            skipped += 1
            continue
        source_path = next(
            (
                MNQ_HISTORY_ROOT / f"{symbol}_{source_tf}.csv"
                for source_tf in source_candidates
                if _probe_rows(MNQ_HISTORY_ROOT / f"{symbol}_{source_tf}.csv", "history") > 0
            ),
            None,
        )
        if source_path is None:
            print(f"[hydrate:resample] warn {symbol}/{target_tf}: no usable source in {source_candidates}")
            skipped += 1
            continue
        print(f"[hydrate:resample] {source_path.name} -> {target.name}")
        if dry_run:
            skipped += 1
            continue
        rows = _read_history_ohlcv(source_path)
        out_rows = _resample_rows(rows, target_tf)
        if not out_rows:
            print(f"  [warn] resample produced zero rows from {source_path.name}")
            skipped += 1
            continue
        count = _write_history_rows(target, out_rows)
        print(f"  wrote {count:,} rows -> {target}")
        written += 1
    return written, skipped


def _fetch_crypto_prices(*, force: bool = False, dry_run: bool = False) -> tuple[int, int]:
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
            f"[hydrate:crypto] fetching {plan.symbol}/{plan.timeframe} {start.date()} -> {now.date()}",
        )
        if dry_run:
            skipped += 1
            continue
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


def _fetch_crypto_funding(*, force: bool = False, dry_run: bool = False) -> tuple[int, int]:
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
        if dry_run:
            skipped += 1
            continue
        rows = fetch_funding(symbol=symbol, start=start, end=end)
        if not rows:
            print(f"  [warn] zero funding rows fetched for {symbol}")
            continue
        write_funding_csv(out_path, rows)
        print(f"  wrote {len(rows):,} rows -> {out_path}")
        written += 1
    return written, skipped


def _fetch_supporting_btc_series(*, force: bool = False, dry_run: bool = False) -> tuple[int, int]:
    wrote = 0
    skipped = 0

    etf_path = MNQ_HISTORY_ROOT / "BTC_ETF_FLOWS.csv"
    if etf_path.exists() and etf_path.stat().st_size > 100 and not force:
        print(f"[hydrate:support] skip {etf_path.name} (already present)")
        skipped += 1
    elif dry_run:
        print(f"[hydrate:support] would fetch {etf_path.name}")
        skipped += 1
    else:
        rows = fetch_btc_etf_flows(etf_path, dry_run=False)
        if rows > 0:
            wrote += 1

    fg_path = MNQ_HISTORY_ROOT / "BTC_FEAR_GREED.csv"
    if fg_path.exists() and fg_path.stat().st_size > 100 and not force:
        print(f"[hydrate:support] skip {fg_path.name} (already present)")
        skipped += 1
    elif dry_run:
        print(f"[hydrate:support] would fetch {fg_path.name}")
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
    elif dry_run:
        print(f"[hydrate:support] would build {lth_path.name}")
        skipped += 1
    else:
        rows = build_btc_lth_proxy(btc_daily, lth_path, dry_run=False)
        if rows > 0:
            wrote += 1

    return wrote, skipped


def _fetch_free_onchain_series(*, force: bool = False, dry_run: bool = False) -> tuple[int, int]:
    ensure_dir(CRYPTO_ONCHAIN_ROOT)
    wrote = 0
    skipped = 0

    cutoff = (datetime.now(UTC) - timedelta(days=365)).date()
    for symbol, fetcher in _ONCHAIN_FETCHERS.items():
        out_path = CRYPTO_ONCHAIN_ROOT / _onchain_filename(symbol)
        if out_path.exists() and out_path.stat().st_size > 100 and not force:
            print(f"[hydrate:onchain] skip {out_path.name} (already present)")
            skipped += 1
            continue

        if dry_run:
            print(f"[hydrate:onchain] would fetch {symbol} daily free on-chain series (365d)")
            skipped += 1
            continue

        print(f"[hydrate:onchain] fetching {symbol} daily free on-chain series (365d)")
        series = fetcher(365)
        series = {day: row for day, row in series.items() if day >= cutoff}
        if not series:
            print(f"  [warn] zero rows fetched for {symbol} on-chain series")
            skipped += 1
            continue

        write_onchain_csv(out_path, series, _ONCHAIN_COLUMNS[symbol])
        print(f"  wrote {len(series):,} rows -> {out_path}")
        wrote += 1
    return wrote, skipped


def main() -> int:
    p = argparse.ArgumentParser(prog="hydrate_canonical_market_data")
    p.add_argument("--skip-futures", action="store_true")
    p.add_argument("--skip-crypto", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument(
        "--max-legacy-files",
        type=int,
        default=200,
        help="maximum legacy databento CSVs to probe before stopping; 0 means unlimited",
    )
    args = p.parse_args()

    imported = skipped = 0
    if not args.skip_futures:
        fut_imported, fut_skipped = _import_futures(
            force=args.force,
            dry_run=args.dry_run,
            max_legacy_files=args.max_legacy_files,
        )
        fut_resampled, fut_resample_skipped = _synthesize_futures_timeframes(
            force=args.force,
            dry_run=args.dry_run,
        )
        imported += fut_imported
        imported += fut_resampled
        skipped += fut_skipped + fut_resample_skipped

    if not args.skip_crypto:
        price_written, price_skipped = _fetch_crypto_prices(force=args.force, dry_run=args.dry_run)
        fund_written, fund_skipped = _fetch_crypto_funding(force=args.force, dry_run=args.dry_run)
        support_written, support_skipped = _fetch_supporting_btc_series(force=args.force, dry_run=args.dry_run)
        onchain_written, onchain_skipped = _fetch_free_onchain_series(
            force=args.force,
            dry_run=args.dry_run,
        )
        imported += price_written + fund_written + support_written + onchain_written
        skipped += price_skipped + fund_skipped + support_skipped + onchain_skipped

    print(
        f"[hydrate] complete: imported={imported} skipped={skipped} "
        f"mnq_history={MNQ_HISTORY_ROOT} crypto_history={CRYPTO_HISTORY_ROOT}",
    )
    return 0 if imported > 0 or skipped > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
