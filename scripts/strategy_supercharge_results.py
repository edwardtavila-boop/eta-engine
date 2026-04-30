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
_RETUNE_OUT_DIR = workspace_roots.ETA_RUNTIME_STATE_DIR / "strategy_supercharge_retunes"
_STYLE_PLAYBOOKS: dict[str, dict[str, object]] = {
    "compression_breakout": {
        "candidate_families": ["compression_breakout"],
        "primary_knobs": [
            "bb_width_max_percentile",
            "breakout_lookback",
            "min_volume_z",
            "atr_stop_mult",
            "rr_target",
        ],
        "focus": "Retune compression sensitivity, breakout confirmation, and stop width before promotion.",
    },
    "crypto_macro_confluence": {
        "candidate_families": ["crypto_macro_confluence", "crypto_regime_trend"],
        "primary_knobs": [
            "vol_band_lookback",
            "min_macro_score",
            "require_eth_alignment",
            "extreme_funding_threshold",
        ],
        "focus": "Retune macro filters against the base regime edge so filters help instead of over-vetoing.",
    },
    "crypto_orb": {
        "candidate_families": ["crypto_orb", "crypto_trend"],
        "primary_knobs": ["range_minutes", "atr_stop_mult", "rr_target"],
        "focus": "Retune the UTC opening range, ATR stop width, and reward target for this crypto tape.",
    },
    "crypto_regime_trend": {
        "candidate_families": ["crypto_regime_trend", "crypto_orb"],
        "primary_knobs": [
            "regime_ema",
            "pullback_ema",
            "pullback_tolerance_pct",
            "atr_stop_mult",
            "rr_target",
        ],
        "focus": "Retune regime/pullback EMAs and stop-target geometry for persistent crypto trends.",
    },
    "drb": {
        "candidate_families": ["drb"],
        "primary_knobs": ["lookback_days", "atr_stop_mult", "rr_target", "min_range_pts"],
        "focus": "Retune daily range lookback and ATR stop/target width on the NQ daily tape.",
    },
    "ensemble_voting": {
        "candidate_families": ["ensemble_voting", "crypto_orb", "crypto_regime_trend"],
        "primary_knobs": [
            "voters",
            "min_agreement_count",
            "use_confidence_weighting",
            "max_gap_to_atr",
        ],
        "focus": "Retune voter mix and agreement threshold before trusting the ensemble composite.",
    },
    "orb": {
        "candidate_families": ["orb", "orb_sage_gated"],
        "primary_knobs": ["range_minutes", "atr_stop_mult", "rr_target", "ema_bias_period"],
        "focus": "Retune RTH opening range, ATR stop width, and trend bias for index-futures tape.",
    },
    "orb_sage_gated": {
        "candidate_families": ["orb_sage_gated", "orb"],
        "primary_knobs": [
            "range_minutes",
            "atr_stop_mult",
            "rr_target",
            "min_conviction",
            "min_alignment",
        ],
        "focus": "Retune ORB geometry and the sage overlay threshold as an ablation pair.",
    },
    "sage_consensus": {
        "candidate_families": ["sage_consensus", "crypto_macro_confluence"],
        "primary_knobs": [
            "min_conviction",
            "min_consensus",
            "min_alignment",
            "sage_lookback_bars",
        ],
        "focus": "Retune sage agreement thresholds and lookback so consensus is selective but not silent.",
    },
    "sage_daily_gated": {
        "candidate_families": ["sage_daily_gated", "crypto_macro_confluence"],
        "primary_knobs": [
            "min_daily_conviction",
            "strict_mode",
            "vol_band_lookback",
            "min_macro_score",
        ],
        "focus": "Retune daily sage conviction and macro filter strictness around the intraday executor.",
    },
}


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


def _near_miss_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    candidates = [
        row
        for row in rows
        if row.get("result_status") == "fail"
        and int(row.get("windows") or 0) > 0
        and float(row.get("oos_sharpe") or 0.0) > 0.0
    ]
    return sorted(
        candidates,
        key=lambda row: (
            -float(row.get("dsr_pass_fraction") or 0.0),
            -float(row.get("oos_sharpe") or 0.0),
            -int(row.get("windows") or 0),
            str(row.get("bot_id") or ""),
        ),
    )


def _symbol_for(row: dict[str, object]) -> str:
    symbol = str(row.get("symbol") or "").strip()
    if symbol:
        return symbol
    sym_tf = str(row.get("symbol_timeframe") or "").strip()
    if "/" in sym_tf:
        return sym_tf.split("/", 1)[0].strip()
    return "UNKNOWN"


def _timeframe_for(row: dict[str, object]) -> str:
    timeframe = str(row.get("timeframe") or "").strip()
    if timeframe:
        return timeframe
    sym_tf = str(row.get("symbol_timeframe") or "").strip()
    if "/" in sym_tf:
        return sym_tf.split("/", 1)[1].strip()
    return ""


def _strategy_kind_for(row: dict[str, object]) -> str:
    return str(row.get("strategy_kind") or "unknown").strip() or "unknown"


def _with_scope_fields(row: dict[str, object]) -> dict[str, object]:
    return {
        **row,
        "symbol": _symbol_for(row),
        "timeframe": _timeframe_for(row),
        "strategy_kind": _strategy_kind_for(row),
    }


def _style_playbook(row: dict[str, object]) -> dict[str, object]:
    strategy_kind = _strategy_kind_for(row)
    playbook = _STYLE_PLAYBOOKS.get(strategy_kind)
    if playbook is not None:
        return dict(playbook)
    return {
        "candidate_families": [strategy_kind],
        "primary_knobs": ["window_days", "step_days", "min_trades_per_window"],
        "focus": "Retune this strategy family with conservative walk-forward settings before promotion.",
    }


def _optimizer_command(bot_id: str) -> list[str]:
    return [
        "python",
        "-m",
        "eta_engine.scripts.fleet_strategy_optimizer",
        "--only-bot",
        bot_id,
        "--out-dir",
        str(_RETUNE_OUT_DIR),
    ]


def _retune_issue_code(row: dict[str, object]) -> str:
    status = str(row.get("result_status") or "")
    if status == "pass":
        return "pass_ready_for_soak_review"
    if status == "pending":
        return "pending_retest"
    windows = int(row.get("windows") or 0)
    if windows <= 0:
        return "insufficient_walk_forward_windows"
    oos_sharpe = float(row.get("oos_sharpe") or 0.0)
    dsr_pass_fraction = float(row.get("dsr_pass_fraction") or 0.0)
    positive_oos = int(row.get("positive_oos_windows") or 0)
    if oos_sharpe > 0.0 and dsr_pass_fraction >= 0.5:
        return "strict_gate_near_miss"
    if oos_sharpe > 0.0:
        return "positive_oos_unstable"
    if positive_oos > 0:
        return "mixed_oos_decay"
    return "negative_oos_edge"


def _retune_priority_score(row: dict[str, object], issue_code: str) -> float:
    oos_sharpe = float(row.get("oos_sharpe") or 0.0)
    dsr_pass_fraction = float(row.get("dsr_pass_fraction") or 0.0)
    windows = int(row.get("windows") or 0)
    positive_oos = int(row.get("positive_oos_windows") or 0)
    base_by_issue = {
        "strict_gate_near_miss": 1000.0,
        "positive_oos_unstable": 900.0,
        "insufficient_walk_forward_windows": 800.0,
        "mixed_oos_decay": 650.0,
        "negative_oos_edge": 500.0,
        "pending_retest": 300.0,
        "pass_ready_for_soak_review": 100.0,
    }
    base = base_by_issue.get(issue_code, 250.0)
    return round(
        base
        + (dsr_pass_fraction * 100.0)
        + (min(max(oos_sharpe, 0.0), 10.0) * 10.0)
        + (positive_oos * 2.0)
        + min(windows, 20),
        3,
    )


def _next_step(issue_code: str) -> str:
    if issue_code == "pass_ready_for_soak_review":
        return "Hold live routing; review paper-soak and promotion gates before any registry or broker change."
    if issue_code == "pending_retest":
        return "Run the manifest smoke command first so retuning is based on current-batch evidence."
    if issue_code == "insufficient_walk_forward_windows":
        return "Repair data coverage or widen the smoke window before ranking strategy quality."
    if issue_code == "strict_gate_near_miss":
        return "Run the fleet optimizer for this bot and compare challenger configs against the registered anchor."
    if issue_code == "positive_oos_unstable":
        return "Retune around the current family, prioritizing stability across walk-forward folds over raw OOS."
    if issue_code == "mixed_oos_decay":
        return "Test tighter filters and alternate stop-target geometry because some windows work but decay dominates."
    return "Treat the registered strategy as weak for this slice; test alternate candidate families before promotion."


def _retune_plan(row: dict[str, object]) -> dict[str, object]:
    bot_id = str(row.get("bot_id") or "")
    issue_code = _retune_issue_code(row)
    playbook = _style_playbook(row)
    command: list[str] = []
    if issue_code not in {"pass_ready_for_soak_review", "pending_retest"} and bot_id:
        command = _optimizer_command(bot_id)
    return {
        "issue_code": issue_code,
        "priority_score": _retune_priority_score(row, issue_code),
        "primary_focus": playbook["focus"],
        "next_step": _next_step(issue_code),
        "candidate_families": playbook["candidate_families"],
        "primary_knobs": playbook["primary_knobs"],
        "optimizer_command": command,
        "safe_to_mutate_live": False,
        "writes_live_routing": False,
    }


def _counts(rows: list[dict[str, object]]) -> dict[str, int]:
    return {
        "total_targets": len(rows),
        "tested": sum(1 for row in rows if row.get("result_status") in {"pass", "fail"}),
        "passed": sum(1 for row in rows if row.get("result_status") == "pass"),
        "failed": sum(1 for row in rows if row.get("result_status") == "fail"),
        "pending": sum(1 for row in rows if row.get("result_status") == "pending"),
    }


def _group_payload(rows: list[dict[str, object]], key: str) -> dict[str, dict[str, object]]:
    groups: dict[str, dict[str, object]] = {}
    for value in sorted({str(row.get(key) or "UNKNOWN") for row in rows}):
        grouped = [row for row in rows if str(row.get(key) or "UNKNOWN") == value]
        near_misses = _near_miss_rows(grouped)
        groups[value] = {
            **_counts(grouped),
            "symbols": sorted({_symbol_for(row) for row in grouped}),
            "strategy_kinds": sorted({_strategy_kind_for(row) for row in grouped}),
            "best_near_miss_bot": str(near_misses[0].get("bot_id") or "") if near_misses else "",
        }
    return groups


def _scope_label(symbols: list[str], strategy_kinds: list[str]) -> str:
    if len(symbols) > 1 and len(strategy_kinds) > 1:
        return "cross_asset_multi_style"
    if len(symbols) > 1:
        return "cross_asset_single_style"
    if len(strategy_kinds) > 1:
        return "single_asset_multi_style"
    if symbols and strategy_kinds:
        return f"{symbols[0]}_{strategy_kinds[0]}"
    if symbols:
        return symbols[0]
    if strategy_kinds:
        return strategy_kinds[0]
    return "unknown"


def _scope_payload(rows: list[dict[str, object]]) -> dict[str, object]:
    symbols = sorted({_symbol_for(row) for row in rows})
    strategy_kinds = sorted({_strategy_kind_for(row) for row in rows})
    return {
        "label": _scope_label(symbols, strategy_kinds),
        "symbols": symbols,
        "timeframes": sorted({_timeframe_for(row) for row in rows if _timeframe_for(row)}),
        "strategy_kinds": strategy_kinds,
        "note": "Scope is derived from manifest target symbols and strategy kinds; this is not an MNQ-only surface.",
    }


def _retune_queue(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    queue: list[dict[str, object]] = []
    for row in rows:
        plan = row.get("retune_plan")
        if not isinstance(plan, dict):
            continue
        if plan.get("issue_code") == "pass_ready_for_soak_review":
            continue
        queue.append(
            {
                "bot_id": str(row.get("bot_id") or ""),
                "symbol": _symbol_for(row),
                "timeframe": _timeframe_for(row),
                "strategy_kind": _strategy_kind_for(row),
                "result_status": str(row.get("result_status") or ""),
                "issue_code": str(plan.get("issue_code") or ""),
                "priority_score": float(plan.get("priority_score") or 0.0),
                "primary_focus": str(plan.get("primary_focus") or ""),
                "next_step": str(plan.get("next_step") or ""),
                "optimizer_command": (
                    plan.get("optimizer_command")
                    if isinstance(plan.get("optimizer_command"), list)
                    else []
                ),
                "primary_knobs": (
                    plan.get("primary_knobs")
                    if isinstance(plan.get("primary_knobs"), list)
                    else []
                ),
                "safe_to_mutate_live": False,
                "writes_live_routing": False,
            },
        )
    return sorted(
        queue,
        key=lambda item: (
            -float(item.get("priority_score") or 0.0),
            str(item.get("bot_id") or ""),
        ),
    )


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
        scoped_row = _with_scope_fields(row)
        rows.append({**scoped_row, "retune_plan": _retune_plan(scoped_row)})
    passed = [row for row in rows if row.get("result_status") == "pass"]
    failed = [row for row in rows if row.get("result_status") == "fail"]
    pending = [row for row in rows if row.get("result_status") == "pending"]
    near_misses = _near_miss_rows(rows)
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
        "scope": _scope_payload(rows),
        "manifest_summary": manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {},
        "summary": {
            "total_targets": len(rows),
            "tested": len(passed) + len(failed),
            "passed": len(passed),
            "failed": len(failed),
            "pending": len(pending),
            "next_pending_bot": str(pending[0].get("bot_id") or "") if pending else "",
            "first_failed_bot": str(failed[0].get("bot_id") or "") if failed else "",
            "best_near_miss_bot": str(near_misses[0].get("bot_id") or "") if near_misses else "",
        },
        "rows": rows,
        "rows_by_bot": rows_by_bot,
        "groups": {
            "by_symbol": _group_payload(rows, "symbol"),
            "by_strategy_kind": _group_payload(rows, "strategy_kind"),
        },
        "retune_queue": _retune_queue(rows),
        "tested": passed + failed,
        "passed": passed,
        "failed": failed,
        "near_misses": near_misses,
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
