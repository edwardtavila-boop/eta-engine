"""Collect strategy-supercharge retest evidence into one JSON surface."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402,I001

_REPORT_GLOB = "research_grid_*.md"
_TABLE_HEADER = (
    "| Config | Sym/TF | Scorer | Thr | Gate | W | +OOS | IS Sh | OOS Sh | "
    "Deg% | DSR med | DSR pass% | Verdict | Note |"
)


def _float_cell(value: str) -> float:
    try:
        return float(value.strip().replace("%", ""))
    except ValueError:
        return 0.0


def _int_cell(value: str) -> int:
    try:
        return int(float(value.strip()))
    except ValueError:
        return 0


def _artifact_class(text: str) -> str:
    match = re.search(r"Artifact class:\s*`([^`]+)`", text)
    return match.group(1) if match else "unknown"


def _parse_report(path: Path) -> list[dict[str, object]]:
    """Parse the compact markdown table emitted by run_research_grid."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    artifact_class = _artifact_class(text)
    rows: list[dict[str, object]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("| "):
            continue
        if line == _TABLE_HEADER or line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 14:
            continue
        bot_id = cells[0]
        if not bot_id or bot_id.lower() == "config":
            continue
        verdict = cells[12].upper()
        dsr_percent = _float_cell(cells[11])
        rows.append(
            {
                "bot_id": bot_id,
                "symbol_timeframe": cells[1],
                "scorer": cells[2],
                "windows": _int_cell(cells[5]),
                "positive_oos_windows": _int_cell(cells[6]),
                "is_sharpe": _float_cell(cells[7]),
                "oos_sharpe": _float_cell(cells[8]),
                "degradation_pct": _float_cell(cells[9]),
                "dsr_median": _float_cell(cells[10]),
                "dsr_pass_fraction": dsr_percent / 100.0,
                "verdict": verdict,
                "result_status": "pass" if verdict == "PASS" else "fail",
                "note": cells[13],
                "artifact_class": artifact_class,
                "report_path": str(path),
                "report_mtime": path.stat().st_mtime,
            },
        )
    return rows


def _latest_reports_by_bot(report_dir: Path) -> dict[str, dict[str, object]]:
    latest: dict[str, dict[str, object]] = {}
    if not report_dir.exists():
        return latest
    for path in sorted(report_dir.glob(_REPORT_GLOB)):
        for row in _parse_report(path):
            bot_id = str(row.get("bot_id") or "")
            if not bot_id:
                continue
            previous = latest.get(bot_id)
            if previous is None or float(row.get("report_mtime") or 0.0) >= float(previous.get("report_mtime") or 0.0):
                latest[bot_id] = row
    return latest


def _cutoff_mtime(manifest: dict[str, object]) -> float:
    generated_at = manifest.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        return 0.0
    try:
        return datetime.fromisoformat(generated_at).timestamp()
    except ValueError:
        return 0.0


def _manifest_rows(manifest: dict[str, object]) -> list[dict[str, object]]:
    rows = manifest.get("next_batch")
    if isinstance(rows, list) and rows:
        return [dict(row) for row in rows if isinstance(row, dict)]
    fallback = manifest.get("rows")
    if isinstance(fallback, list):
        return [dict(row) for row in fallback if isinstance(row, dict)]
    return []


def _load_manifest_snapshot(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def build_results(
    *,
    manifest: dict[str, object] | None = None,
    manifest_path: Path = workspace_roots.ETA_STRATEGY_SUPERCHARGE_MANIFEST_PATH,
    report_dir: Path = workspace_roots.ETA_RESEARCH_GRID_RUNTIME_DIR,
    generated_at: str | None = None,
) -> dict[str, object]:
    """Return latest pass/fail/pending retest status for manifest targets."""
    if manifest is None:
        manifest = _load_manifest_snapshot(manifest_path)
        if manifest is None:
            from eta_engine.scripts.strategy_supercharge_manifest import build_manifest

            manifest = build_manifest()

    report_rows = _latest_reports_by_bot(report_dir)
    min_report_mtime = _cutoff_mtime(manifest)
    rows: list[dict[str, object]] = []
    for order, target in enumerate(_manifest_rows(manifest)):
        bot_id = str(target.get("bot_id") or "").strip()
        if not bot_id:
            continue
        evidence = report_rows.get(bot_id)
        stale_evidence = (
            evidence
            if evidence is not None and float(evidence.get("report_mtime") or 0.0) < min_report_mtime
            else None
        )
        if evidence is None or stale_evidence is not None:
            row = {
                **target,
                "result_order": order,
                "result_status": "pending",
                "verdict": "",
                "report_path": "",
                "stale_report_path": str(stale_evidence.get("report_path") or "") if stale_evidence else "",
                "safe_to_mutate_live": False,
                "writes_live_routing": False,
            }
        else:
            row = {
                **target,
                **evidence,
                "result_order": order,
                "safe_to_mutate_live": False,
                "writes_live_routing": False,
            }
        rows.append(row)
    passed = [row for row in rows if row.get("result_status") == "pass"]
    failed = [row for row in rows if row.get("result_status") == "fail"]
    pending = [row for row in rows if row.get("result_status") == "pending"]
    rows_by_bot = {
        str(row["bot_id"]): row
        for row in rows
        if row.get("bot_id")
    }
    return {
        "schema_version": 1,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "source": "strategy_supercharge_results",
        "status": "ready",
        "manifest_summary": manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {},
        "summary": {
            "total_targets": len(rows),
            "tested": len(passed) + len(failed),
            "passed": len(passed),
            "failed": len(failed),
            "pending": len(pending),
            "next_pending_bot": str(pending[0].get("bot_id") or "") if pending else "",
            "first_failed_bot": str(failed[0].get("bot_id") or "") if failed else "",
        },
        "rows": rows,
        "rows_by_bot": rows_by_bot,
        "tested": passed + failed,
        "passed": passed,
        "failed": failed,
        "pending": pending,
    }


def write_results(
    results: dict[str, object],
    path: Path = workspace_roots.ETA_STRATEGY_SUPERCHARGE_RESULTS_PATH,
) -> Path:
    """Atomically write the results snapshot and return the target path."""
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(results, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="strategy_supercharge_results")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--no-write", action="store_true", help="build without writing the canonical snapshot")
    parser.add_argument("--report-dir", type=Path, default=workspace_roots.ETA_RESEARCH_GRID_RUNTIME_DIR)
    parser.add_argument("--out", type=Path, default=workspace_roots.ETA_STRATEGY_SUPERCHARGE_RESULTS_PATH)
    args = parser.parse_args(argv)

    results = build_results(report_dir=args.report_dir)
    written = None if args.no_write else write_results(results, args.out)
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True, default=str))
    else:
        target = f" -> {written}" if written is not None else " (no-write)"
        summary = results["summary"] if isinstance(results.get("summary"), dict) else {}
        print(
            "strategy_supercharge_results "
            f"tested={summary.get('tested', 0)} "
            f"passed={summary.get('passed', 0)} "
            f"pending={summary.get('pending', 0)}{target}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
