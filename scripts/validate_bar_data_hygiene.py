"""Bar data hygiene validator — detects rollover/splice artifacts and corrupt bars.

The 2026-05-07 fleet audit found NG1_1h.csv had 65 adjacent-close jumps >5% and
CL1_1h.csv had 14 — these are continuous-front-month rollover artifacts (or
yfinance hygiene issues) that fabricate sweep/breakout signals and invalidate
any backtest run on the affected file.

Checks performed per CSV:

  1. Adjacent-bar return jumps (|log return| > threshold; flagged as rollover
     candidate when a futures continuous-front-month file rolls near a known
     contract roll date).
  2. OHLC sanity (low > high, low above open/close, high below open/close,
     non-finite fields).
  3. Volume sanity (negative or NaN).
  4. Duplicate timestamps.
  5. Out-of-order timestamps.
  6. Gaps > N expected intervals (weekend windows excluded for futures, never
     for crypto which trades 24/7).

The script never modifies any input CSV. Outputs a JSON report grouped by file
plus an overall PASS / WARN / FAIL verdict.

Usage
-----
    python -m eta_engine.scripts.validate_bar_data_hygiene \\
        --files mnq_data/history/NG1_1h.csv mnq_data/history/CL1_1h.csv \\
        --threshold-pct 5.0 \\
        --output reports/bar_data_hygiene/2026-05-07.json

Or scan everything under the canonical history roots:

    python -m eta_engine.scripts.validate_bar_data_hygiene --all-csvs

Exit codes: 0 = PASS, 1 = WARN, 2 = FAIL.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

# Default per-asset-class threshold for the absolute log return between adjacent
# bars. Anything above this is suspect.
FUTURES_THRESHOLD_PCT = 5.0
CRYPTO_THRESHOLD_PCT = 10.0
TREASURY_THRESHOLD_PCT = 3.0

# Map timeframe suffix to seconds-per-bar.
TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "8h": 28800,
    "D": 86400,
    "1d": 86400,
}

# Default acceptable gap (in expected-bar intervals) before flagging.
DEFAULT_GAP_INTERVALS = 3

# Symbols treated as crypto (no weekend gap logic, default 10% threshold).
CRYPTO_SYMBOL_PREFIXES = ("BTC", "ETH", "SOL", "XRP", "DOGE", "LTC", "BCH", "MBT", "MET")

# Symbols treated as treasury / rates futures (tighter 3% threshold).
TREASURY_SYMBOL_PREFIXES = ("ZN", "ZT", "ZB", "ZF", "TN", "FV", "TY", "US")

# Weekend window for futures: Saturday 00:00 UTC through Sunday 22:00 UTC.
# CME equity index futures resume Sunday 22:00 UTC; energies/grains differ
# slightly but using 22:00 keeps false-positive rate low without missing real
# Mon-morning gaps that almost never start before 23:00 UTC anyway.
WEEKEND_OPEN_WEEKDAY = 5      # Saturday
WEEKEND_CLOSE_WEEKDAY = 6     # Sunday
WEEKEND_CLOSE_HOUR = 22       # 22:00 UTC


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    """One detected hygiene problem."""

    row: int
    type: str
    detail: str
    ts: int | None = None
    rollover_candidate: bool = False
    magnitude_pct: float | None = None
    prev_close: float | None = None
    close: float | None = None


@dataclass
class FileReport:
    path: str
    rows: int
    issues: list[Issue] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class Report:
    scanned_at: str
    files: list[FileReport] = field(default_factory=list)
    overall: str = "PASS"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _classify_symbol(filename: str) -> str:
    """Return one of "crypto" | "treasury" | "futures" | "other"."""

    stem = Path(filename).stem.upper()
    base = stem.split("_", 1)[0]
    if any(base.startswith(p) for p in CRYPTO_SYMBOL_PREFIXES):
        return "crypto"
    if any(base.startswith(p) for p in TREASURY_SYMBOL_PREFIXES):
        return "treasury"
    return "futures"


def _is_continuous_front_month(filename: str) -> bool:
    """A continuous-front-month file is e.g. NG1_1h.csv, CL1_1h.csv, ES1_5m.csv.

    Heuristic: stem starts with letters then a digit "1" before the first
    underscore (e.g. NG1, CL1, ES1, 6E1). Crypto pairs like BTC_1h.csv have
    no trailing digit before the underscore, so they correctly fall through.
    """

    stem = Path(filename).stem
    head = stem.split("_", 1)[0]
    return len(head) >= 2 and head[-1] == "1" and head[:-1].isalpha()


def _timeframe_seconds(filename: str) -> int | None:
    """Parse the timeframe component of a path stem (e.g. NG1_1h -> 3600)."""

    stem = Path(filename).stem
    if "_" not in stem:
        return None
    suffix = stem.rsplit("_", 1)[1]
    return TIMEFRAME_SECONDS.get(suffix)


def _default_threshold(filename: str) -> float:
    cls = _classify_symbol(filename)
    if cls == "crypto":
        return CRYPTO_THRESHOLD_PCT
    if cls == "treasury":
        return TREASURY_THRESHOLD_PCT
    return FUTURES_THRESHOLD_PCT


def _is_weekend_gap(prev_ts: int, ts: int) -> bool:
    """True if the gap from prev_ts -> ts brackets a futures weekend close.

    Uses two complementary tests:

      - either endpoint sits inside the weekend window
        (Sat 00:00 UTC <= t < Sun 22:00 UTC), or
      - the interval ``[prev_ts, ts]`` spans the Friday close (~21:00 UTC)
        through the Sunday re-open (~22:00 UTC).
    """

    prev_dt = datetime.fromtimestamp(prev_ts, tz=UTC)
    cur_dt = datetime.fromtimestamp(ts, tz=UTC)

    def inside(d: datetime) -> bool:
        if d.weekday() == WEEKEND_OPEN_WEEKDAY:
            return True
        return d.weekday() == WEEKEND_CLOSE_WEEKDAY and d.hour < WEEKEND_CLOSE_HOUR

    if inside(prev_dt) or inside(cur_dt):
        return True

    # Walk forward day-by-day from prev_dt and check whether we cross any
    # Saturday between the two timestamps; that is a weekend gap.
    delta = cur_dt - prev_dt
    if delta <= timedelta(0):
        return False
    if delta > timedelta(days=4):
        return False  # too long to be a normal weekend close
    cursor = prev_dt
    while cursor <= cur_dt:
        if cursor.weekday() == WEEKEND_OPEN_WEEKDAY:
            return True
        cursor += timedelta(hours=1)
    return False


def _is_third_friday(d: date) -> bool:
    """3rd Friday helper used for index/equity-future quarterly rolls."""

    if d.weekday() != 4:
        return False
    # Day-of-month for the 3rd Friday is between 15 and 21 inclusive.
    return 15 <= d.day <= 21


def _is_rollover_candidate(filename: str, ts: int) -> bool:
    """Best-effort heuristic for whether ``ts`` falls near a known roll date.

    Uses asset-class roll calendars at month-level granularity. Anything in the
    last 5 calendar days of the prior month or the first 5 days of the
    delivery month is flagged. Index futures additionally flag the 3rd-Friday
    week of quarter months.
    """

    if not _is_continuous_front_month(filename):
        return False
    cls = _classify_symbol(filename)
    if cls == "crypto":
        return False

    head = Path(filename).stem.split("_", 1)[0].upper()
    dt = datetime.fromtimestamp(ts, tz=UTC).date()

    # Index futures (ES1, MES1, NQ1, MNQ1, M2K1, RTY1, YM1, MYM1, 6E1, 6B1, 6J1):
    # quarterly roll on 3rd Friday of Mar/Jun/Sep/Dec — the week leading up
    # counts as a candidate window.
    index_heads = {"ES1", "MES1", "NQ1", "MNQ1", "M2K1", "RTY1", "YM1", "MYM1", "6E1", "6B1", "6J1"}
    if head in index_heads:
        if dt.month in (3, 6, 9, 12):
            for offset in range(-7, 1):
                check = dt + timedelta(days=offset)
                if check.month == dt.month and _is_third_friday(check):
                    return True
        return False

    # Energies (CL1, NG1, RB1, HO1): roll late-month every month.
    if head in {"CL1", "NG1", "RB1", "HO1", "BZ1"}:
        if dt.day >= 22:
            return True
        return dt.day <= 5

    # Metals (GC1, SI1, HG1, PL1, PA1) and grains (ZC1, ZS1, ZW1, ZL1, ZM1):
    # roll near month-end of contract delivery months. Use "last 5 / first 5"
    # window unconditionally since we don't ship the full per-symbol calendar.
    if head in {"GC1", "SI1", "HG1", "PL1", "PA1", "ZC1", "ZS1", "ZW1", "ZL1", "ZM1"}:
        return dt.day >= 22 or dt.day <= 5

    # Default for any other future-with-1: treat last/first 3 days as rollover-ish.
    return dt.day >= 25 or dt.day <= 3


# ---------------------------------------------------------------------------
# Core scanning logic
# ---------------------------------------------------------------------------


def _parse_float(raw: str) -> float:
    """Tolerant float parser; returns NaN for empty / unparseable values."""

    raw = raw.strip()
    if not raw:
        return float("nan")
    try:
        return float(raw)
    except ValueError:
        return float("nan")


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader)


def scan_file(
    path: Path,
    *,
    threshold_pct: float | None = None,
    gap_intervals: int = DEFAULT_GAP_INTERVALS,
) -> FileReport:
    """Scan a single bar CSV and return a :class:`FileReport`."""

    report = FileReport(path=str(path), rows=0)

    if not path.is_file():
        report.error = f"file not found: {path}"
        report.summary = {"total_issues": 0, "by_type": {}, "rollover_candidates": 0}
        return report

    try:
        rows = _read_rows(path)
    except Exception as exc:
        report.error = f"read error: {exc}"
        report.summary = {"total_issues": 0, "by_type": {}, "rollover_candidates": 0}
        return report

    report.rows = len(rows)
    if not rows:
        report.summary = {"total_issues": 0, "by_type": {}, "rollover_candidates": 0}
        return report

    needed = {"time", "open", "high", "low", "close", "volume"}
    if not needed.issubset(rows[0].keys()):
        report.error = f"missing required columns; have {sorted(rows[0].keys())}"
        report.summary = {"total_issues": 0, "by_type": {}, "rollover_candidates": 0}
        return report

    threshold = threshold_pct if threshold_pct is not None else _default_threshold(path.name)
    threshold_log = math.log(1.0 + threshold / 100.0)
    cls = _classify_symbol(path.name)
    bar_seconds = _timeframe_seconds(path.name)
    is_crypto = cls == "crypto"

    seen_ts: dict[int, int] = {}  # ts -> row index of first occurrence
    prev_ts: int | None = None
    prev_close: float | None = None

    for idx, row in enumerate(rows, start=2):  # +2 for header + 1-based row id
        # --- timestamp parsing ------------------------------------------------
        try:
            ts = int(float(row["time"]))
        except (KeyError, ValueError, TypeError):
            report.issues.append(Issue(
                row=idx,
                type="bad_timestamp",
                detail=f"unparseable time={row.get('time')!r}",
            ))
            continue

        # --- duplicate / out-of-order ---------------------------------------
        if ts in seen_ts:
            report.issues.append(Issue(
                row=idx,
                type="duplicate_timestamp",
                detail=f"ts={ts} also at row {seen_ts[ts]}",
                ts=ts,
            ))
        else:
            seen_ts[ts] = idx

        if prev_ts is not None and ts < prev_ts:
            report.issues.append(Issue(
                row=idx,
                type="out_of_order_timestamp",
                detail=f"ts={ts} < prev_ts={prev_ts}",
                ts=ts,
            ))

        # --- field parsing ---------------------------------------------------
        o = _parse_float(row["open"])
        h = _parse_float(row["high"])
        low_ = _parse_float(row["low"])
        c = _parse_float(row["close"])
        v = _parse_float(row["volume"])

        # --- non-finite -----------------------------------------------------
        for name, val in (("open", o), ("high", h), ("low", low_), ("close", c)):
            if not math.isfinite(val):
                report.issues.append(Issue(
                    row=idx,
                    type="non_finite_ohlc",
                    detail=f"{name}={val}",
                    ts=ts,
                ))

        # --- ohlc invariants -------------------------------------------------
        if math.isfinite(low_) and math.isfinite(h) and low_ > h:
            report.issues.append(Issue(
                row=idx,
                type="ohlc_invalid",
                detail=f"low {low_} > high {h}",
                ts=ts,
            ))
        if math.isfinite(low_) and math.isfinite(o) and math.isfinite(c) and low_ > min(o, c):
            report.issues.append(Issue(
                row=idx,
                type="ohlc_invalid",
                detail=f"low {low_} > min(open={o}, close={c})",
                ts=ts,
            ))
        if math.isfinite(h) and math.isfinite(o) and math.isfinite(c) and h < max(o, c):
            report.issues.append(Issue(
                row=idx,
                type="ohlc_invalid",
                detail=f"high {h} < max(open={o}, close={c})",
                ts=ts,
            ))

        # Intra-bar range anomaly — catches yfinance "low=31.75" while close=3875
        # patterns that don't violate ordering invariants but are clearly wrong.
        # Range > 50% of close is not a real bar at any timeframe.
        if (
            math.isfinite(h)
            and math.isfinite(low_)
            and math.isfinite(c)
            and c > 0
            and (h - low_) > 0.5 * c
        ):
            report.issues.append(Issue(
                row=idx,
                type="ohlc_invalid",
                detail=(
                    f"intra-bar range {h - low_:.4f} > 50% of close {c:.4f} "
                    f"(high={h}, low={low_})"
                ),
                ts=ts,
            ))

        # --- volume ----------------------------------------------------------
        if not math.isfinite(v):
            report.issues.append(Issue(
                row=idx,
                type="volume_invalid",
                detail=f"volume={v}",
                ts=ts,
            ))
        elif v < 0:
            report.issues.append(Issue(
                row=idx,
                type="volume_invalid",
                detail=f"volume={v} < 0",
                ts=ts,
            ))

        # --- gap detection --------------------------------------------------
        if (
            bar_seconds is not None
            and prev_ts is not None
            and ts > prev_ts
        ):
            gap = ts - prev_ts
            if gap > bar_seconds * gap_intervals and not (
                not is_crypto and _is_weekend_gap(prev_ts, ts)
            ):
                report.issues.append(Issue(
                    row=idx,
                    type="gap",
                    detail=(
                        f"{gap}s gap = {gap / bar_seconds:.1f} bar intervals "
                        f"(threshold={gap_intervals})"
                    ),
                    ts=ts,
                ))

        # --- adjacent jump --------------------------------------------------
        if (
            prev_close is not None
            and math.isfinite(prev_close)
            and math.isfinite(c)
            and prev_close > 0
            and c > 0
        ):
            log_ret = math.log(c / prev_close)
            if abs(log_ret) > threshold_log:
                pct = (math.exp(log_ret) - 1.0) * 100.0
                rollover = _is_rollover_candidate(path.name, ts)
                report.issues.append(Issue(
                    row=idx,
                    type="adjacent_jump",
                    detail=f"log_return={log_ret:.4f} ({pct:+.2f}%) threshold={threshold:.2f}%",
                    ts=ts,
                    rollover_candidate=rollover,
                    magnitude_pct=round(pct, 4),
                    prev_close=prev_close,
                    close=c,
                ))

        prev_ts = ts
        if math.isfinite(c):
            prev_close = c

    # --- summary ------------------------------------------------------------
    by_type: dict[str, int] = {}
    rollover_candidates = 0
    for issue in report.issues:
        by_type[issue.type] = by_type.get(issue.type, 0) + 1
        if issue.rollover_candidate:
            rollover_candidates += 1
    report.summary = {
        "total_issues": len(report.issues),
        "by_type": by_type,
        "rollover_candidates": rollover_candidates,
        "asset_class": cls,
        "threshold_pct": threshold,
    }
    return report


# ---------------------------------------------------------------------------
# Discovery / orchestration
# ---------------------------------------------------------------------------


def discover_csvs(roots: list[Path]) -> list[Path]:
    """Return all *.csv files under any of ``roots`` (deduplicated, sorted)."""

    found: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        if root.is_file() and root.suffix.lower() == ".csv":
            found.add(root.resolve())
            continue
        for p in root.rglob("*.csv"):
            if p.is_file():
                found.add(p.resolve())
    return sorted(found)


def overall_status(reports: list[FileReport]) -> str:
    """Aggregate verdict.

    FAIL — any file has an OHLC, duplicate, out-of-order, or non-finite issue,
           OR > 50 adjacent-jump issues that are NOT all rollover candidates.
    WARN — any other issues found.
    PASS — clean.
    """

    fail_types = {
        "ohlc_invalid",
        "non_finite_ohlc",
        "duplicate_timestamp",
        "out_of_order_timestamp",
        "bad_timestamp",
    }
    has_fail = False
    has_warn = False
    for r in reports:
        if r.error:
            has_fail = True
            continue
        for issue in r.issues:
            if issue.type in fail_types:
                has_fail = True
            else:
                has_warn = True
        non_rollover_jumps = sum(
            1 for i in r.issues if i.type == "adjacent_jump" and not i.rollover_candidate
        )
        if non_rollover_jumps > 50:
            has_fail = True
    if has_fail:
        return "FAIL"
    if has_warn:
        return "WARN"
    return "PASS"


def build_report(
    files: list[Path],
    *,
    threshold_pct: float | None = None,
    gap_intervals: int = DEFAULT_GAP_INTERVALS,
) -> Report:
    file_reports = [
        scan_file(p, threshold_pct=threshold_pct, gap_intervals=gap_intervals)
        for p in files
    ]
    return Report(
        scanned_at=datetime.now(UTC).isoformat(),
        files=file_reports,
        overall=overall_status(file_reports),
    )


def report_to_dict(report: Report) -> dict[str, Any]:
    return {
        "scanned_at": report.scanned_at,
        "overall": report.overall,
        "files": [
            {
                "path": fr.path,
                "rows": fr.rows,
                "error": fr.error,
                "summary": fr.summary,
                "issues": [asdict(i) for i in fr.issues],
            }
            for fr in report.files
        ],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


_CANONICAL_HISTORY_ROOTS = (
    Path("mnq_data") / "history",
    Path("data") / "crypto" / "history",
    Path("data") / "crypto" / "ibkr" / "history",
)


def _resolve_canonical_roots(workspace: Path) -> list[Path]:
    return [workspace / r for r in _CANONICAL_HISTORY_ROOTS]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="validate_bar_data_hygiene",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--files",
        nargs="*",
        default=[],
        help="Specific CSV files to scan (relative or absolute).",
    )
    p.add_argument(
        "--all-csvs",
        action="store_true",
        help="Scan every *.csv under canonical history roots.",
    )
    p.add_argument(
        "--workspace",
        default=str(Path("C:/EvolutionaryTradingAlgo")),
        help="Workspace root (used to resolve canonical history roots).",
    )
    p.add_argument(
        "--threshold-pct",
        type=float,
        default=None,
        help="Override per-asset-class default jump threshold (in percent).",
    )
    p.add_argument(
        "--gap-intervals",
        type=int,
        default=DEFAULT_GAP_INTERVALS,
        help="Flag gaps > N expected bar intervals (default 3).",
    )
    p.add_argument(
        "--output",
        default=None,
        help="Write JSON report to this path (parent dirs created).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-file console summary.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    targets: list[Path] = []
    if args.all_csvs:
        targets.extend(discover_csvs(_resolve_canonical_roots(Path(args.workspace))))
    for f in args.files:
        p = Path(f)
        if not p.is_absolute():
            p = (Path(args.workspace) / p).resolve()
        targets.append(p)

    # Deduplicate while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in targets:
        rp = p.resolve() if p.exists() else p
        if rp in seen:
            continue
        seen.add(rp)
        unique.append(p)

    if not unique:
        print("no files to scan; pass --files or --all-csvs", file=sys.stderr)
        return 2

    report = build_report(
        unique,
        threshold_pct=args.threshold_pct,
        gap_intervals=args.gap_intervals,
    )
    payload = report_to_dict(report)

    if args.output:
        out = Path(args.output)
        if not out.is_absolute():
            out = (Path(args.workspace) / out).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if not args.quiet:
        for fr in report.files:
            if fr.error:
                print(f"[ERROR] {fr.path}: {fr.error}")
                continue
            if fr.summary.get("total_issues", 0) == 0:
                print(f"[OK]    {fr.path}: {fr.rows} rows clean")
            else:
                by_type = ", ".join(
                    f"{k}={v}" for k, v in sorted(fr.summary["by_type"].items())
                )
                print(
                    f"[WARN]  {fr.path}: {fr.rows} rows; "
                    f"{fr.summary['total_issues']} issues ({by_type}); "
                    f"rollover_candidates={fr.summary['rollover_candidates']}"
                )
        print(f"OVERALL: {report.overall}")

    return {"PASS": 0, "WARN": 1, "FAIL": 2}[report.overall]


if __name__ == "__main__":
    raise SystemExit(main())
