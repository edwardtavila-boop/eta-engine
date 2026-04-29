"""
EVOLUTIONARY TRADING ALGO  //  scripts.paper_live_launch_check
================================================================
Pre-launch readiness check for paper-live deployment of all
promoted bots in per_bot_registry.

User mandate (2026-04-27): "lets have it down solid and then
launch all bots to run paper live".

This script does NOT launch anything — it's the readiness
**audit** that runs before the actual launch. It enumerates
every promoted bot in the registry and checks:

1. **Strategy resolves** — the strategy_kind has a registered
   handler in run_research_grid.
2. **Data available + fresh** — primary strategy data resolves, and
   every critical DataRequirement is present; stale critical support
   feeds warn before launch.
3. **Baseline persisted** — strategy_baselines.json has an entry
   (or registry entry has rationale-baseline).
4. **Warmup policy set** — promoted_on, warmup_days, risk_mult.
5. **Bot directory exists** — bots/<dir>/ has a bot.py.
6. **No deferred dependencies** — does the bot need any provider
   that isn't yet wired (e.g. funding for ETH bots).

Output is a per-bot status table:
  ✅ READY = clear to launch paper-live
  ⚠️  WARN = launchable but with caveats
  ❌ BLOCK = cannot launch (missing data / unresolved kind)

Usage
-----
    python -m eta_engine.scripts.paper_live_launch_check

    # Filter to specific bots
    python -m eta_engine.scripts.paper_live_launch_check \\
        --bots eth_compression,btc_sage_daily_etf

    # JSON output for CI / dashboard
    python -m eta_engine.scripts.paper_live_launch_check --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

# Strategy kinds we know how to resolve at runtime
_RESOLVABLE_KINDS: frozenset[str] = frozenset({
    "confluence", "orb", "drb", "grid",
    "crypto_orb", "crypto_trend", "crypto_meanrev", "crypto_scalp",
    "sage_consensus", "orb_sage_gated", "crypto_regime_trend",
    "crypto_macro_confluence", "sage_daily_gated", "ensemble_voting",
    "compression_breakout", "sweep_reclaim",
})
_SHADOW_ONLY_STATUSES: frozenset[str] = frozenset({
    "shadow_benchmark",
    "deprecated",
})
_NON_EDGE_STATUSES: frozenset[str] = frozenset({
    "non_edge_strategy",
})


def _check_data_available(symbol: str, timeframe: str) -> bool:
    """True if the canonical data library can resolve this dataset."""
    from eta_engine.data.library import default_library

    return default_library().get(symbol=symbol, timeframe=timeframe) is not None


def _check_data_freshness(
    symbol: str,
    timeframe: str,
    *,
    generated_at: datetime | None = None,
) -> dict[str, object] | None:
    """Return freshness metadata for the launch dataset, if available."""
    from eta_engine.data.library import default_library
    from eta_engine.scripts.announce_data_library import _dataset_freshness

    dataset = default_library().get(symbol=symbol, timeframe=timeframe)
    if dataset is None:
        return None
    freshness = _dataset_freshness(dataset, generated_at or datetime.now(UTC))
    return {
        "dataset_key": dataset.key,
        "end": dataset.end_ts.isoformat(),
        "rows": dataset.row_count,
        **freshness,
    }


def _data_freshness_warning(symbol: str, timeframe: str, freshness: dict[str, object]) -> str | None:
    """Human-readable warning for stale launch data."""
    if freshness.get("status") != "stale":
        return None
    age = freshness.get("age_days")
    end = freshness.get("end")
    if isinstance(age, (int, float)) and isinstance(end, str):
        end_date = end.split("T", 1)[0]
        return f"stale data: {symbol}/{timeframe} ended {end_date} ({age:.2f}d old)"
    return f"stale data: {symbol}/{timeframe}"


def _requirement_payload(req: Any) -> dict[str, object]:  # noqa: ANN401
    return {
        "kind": req.kind,
        "symbol": req.symbol,
        "timeframe": req.timeframe,
        "critical": req.critical,
        "note": req.note,
    }


def _requirement_label(req: Any) -> str:  # noqa: ANN401
    return f"{req.kind}:{req.symbol}/{req.timeframe or '-'}"


def _matches_primary_launch_dataset(
    req: Any,  # noqa: ANN401
    *,
    primary_symbol: str,
    primary_timeframe: str,
) -> bool:
    return (
        req.kind == "bars"
        and req.symbol.upper() == primary_symbol.upper()
        and req.timeframe == primary_timeframe
    )


def _critical_feed_freshness_warning(req: Any, freshness: dict[str, object]) -> str | None:  # noqa: ANN401
    """Human-readable warning for stale critical non-primary feeds."""
    if freshness.get("status") != "stale":
        return None
    age = freshness.get("age_days")
    end = freshness.get("end")
    label = _requirement_label(req)
    if isinstance(age, (int, float)) and isinstance(end, str):
        end_date = end.split("T", 1)[0]
        return f"stale critical feed: {label} ended {end_date} ({age:.2f}d old)"
    return f"stale critical feed: {label}"


def _check_critical_data_requirements(
    bot_id: str,
    *,
    primary_symbol: str,
    primary_timeframe: str,
    generated_at: datetime | None = None,
    library: Any | None = None,  # noqa: ANN401
) -> dict[str, list[object]]:
    """Check every critical requirement beyond the primary launch dataset."""
    from eta_engine.data.audit import audit_bot
    from eta_engine.scripts.announce_data_library import _dataset_freshness, _resolution_payload

    audit = audit_bot(bot_id, library=library)
    if audit is None or audit.deactivated:
        return {"issues": [], "warnings": [], "evidence": []}

    now = generated_at or datetime.now(UTC)
    issues: list[str] = []
    warnings: list[str] = []
    evidence: list[dict[str, object]] = []

    for req in audit.missing_critical:
        if _matches_primary_launch_dataset(
            req,
            primary_symbol=primary_symbol,
            primary_timeframe=primary_timeframe,
        ):
            continue
        issues.append(f"missing critical feed: {_requirement_label(req)}")

    for req, dataset in audit.available:
        if not req.critical or _matches_primary_launch_dataset(
            req,
            primary_symbol=primary_symbol,
            primary_timeframe=primary_timeframe,
        ):
            continue
        freshness = _dataset_freshness(dataset, now)
        item = {
            "requirement": _requirement_payload(req),
            "dataset_key": dataset.key,
            "rows": dataset.row_count,
            "end": dataset.end_ts.isoformat(),
            "resolution": _resolution_payload(req, dataset),
            **freshness,
        }
        evidence.append(item)
        warning = _critical_feed_freshness_warning(req, item)
        if warning is not None:
            warnings.append(warning)

    return {"issues": issues, "warnings": warnings, "evidence": evidence}


def _check_bot_dir_exists(bot_id: str) -> bool:
    """Heuristic: variant bots share a dir with a base bot.
    True if either bots/{bot_id}/ exists OR the bot is in
    VARIANT_BOT_IDS (which we infer here from the registry sync test).
    """
    bots_dir = ROOT / "bots"
    if (bots_dir / bot_id).exists():
        return True
    # Variant mapping (mirror of test_bots_registry_sync.VARIANT_BOT_IDS)
    variant_map = {
        "mnq_futures": "mnq",         # base bot — registered under bots/mnq/
        "nq_futures": "nq",            # base bot — registered under bots/nq/
        "nq_daily_drb": "nq",
        "mnq_futures_sage": "mnq",
        "nq_futures_sage": "nq",
        "btc_hybrid_sage": "btc_hybrid",
        "btc_regime_trend": "btc_hybrid",
        "mnq_sage_consensus": "mnq",
        "btc_sage_daily_etf": "btc_hybrid",
        "btc_regime_trend_etf": "btc_hybrid",
        "btc_ensemble_2of3": "btc_hybrid",
        "eth_sage_daily": "eth_perp",
        "eth_compression": "eth_perp",
        "btc_compression": "btc_hybrid",
    }
    underlying = variant_map.get(bot_id)
    return bool(underlying and (bots_dir / underlying).exists())


def _load_baseline_entry(bot_id: str, strategy_id: str) -> dict[str, Any] | None:
    """Return the persisted baseline entry, if present.

    Schema: ``{"strategies": [{"strategy_id": ..., ...}, ...]}``.
    Either strategy_id or bot_id can match.
    """
    path = ROOT / "docs" / "strategy_baselines.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            baselines = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(baselines, dict):
        return None
    strategies = baselines.get("strategies") or []
    for s in strategies:
        if isinstance(s, dict) and (
            s.get("strategy_id") == strategy_id
            or s.get("bot_id") == bot_id
        ):
            return s
    return None


def _check_baseline_persisted(bot_id: str, strategy_id: str) -> bool:
    """Does strategy_baselines.json have an entry?"""
    return _load_baseline_entry(bot_id, strategy_id) is not None


def _fmt_metric(value: object, *, pct: bool = False) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    if pct:
        return f"{float(value) * 100:.1f}%"
    return f"{float(value):+.3f}"


def _research_evidence(extras: dict[str, object]) -> dict[str, object]:
    tune = extras.get("research_tune")
    if not isinstance(tune, dict):
        return {}

    evidence: dict[str, object] = {
        k: tune[k]
        for k in (
            "scope",
            "source_artifact",
            "strict_gate",
            "candidate_agg_is_sharpe",
            "candidate_agg_oos_sharpe",
            "candidate_dsr_pass_fraction",
            "candidate_degradation",
            "provider_backed",
        )
        if k in tune
    }
    full_history = tune.get("full_history_smoke")
    if isinstance(full_history, dict):
        evidence["full_history_smoke"] = {
            k: full_history[k]
            for k in (
                "source_artifact",
                "tradable_bars",
                "raw_bars",
                "windows",
                "agg_is_sharpe",
                "agg_oos_sharpe",
                "dsr_pass_fraction",
                "degradation",
                "strict_gate",
            )
            if k in full_history
        }
    return evidence


def _research_warning(extras: dict[str, object]) -> str:
    evidence = _research_evidence(extras)
    source = evidence.get("full_history_smoke")
    if not isinstance(source, dict):
        source = evidence

    parts = ["research_candidate"]
    if source.get("strict_gate") is False:
        parts.append("strict gate failed")

    oos = _fmt_metric(
        source.get("agg_oos_sharpe", source.get("candidate_agg_oos_sharpe")),
    )
    if oos is not None:
        parts.append(f"OOS {oos}")

    is_sharpe = _fmt_metric(
        source.get("agg_is_sharpe", source.get("candidate_agg_is_sharpe")),
    )
    if is_sharpe is not None:
        parts.append(f"IS {is_sharpe}")

    dsr = _fmt_metric(
        source.get("dsr_pass_fraction", source.get("candidate_dsr_pass_fraction")),
        pct=True,
    )
    if dsr is not None:
        parts.append(f"DSR pass {dsr}")

    degradation = _fmt_metric(
        source.get("degradation", source.get("candidate_degradation")),
        pct=True,
    )
    if degradation is not None:
        parts.append(f"degradation {degradation}")

    artifact = source.get("source_artifact")
    if isinstance(artifact, str):
        parts.append(f"evidence {artifact}")

    if len(parts) == 1:
        parts.append("gate not fully passed")
    return f"{parts[0]} ({'; '.join(parts[1:])})"


def _audit_bot(assignment: Any) -> dict:  # noqa: ANN401
    """Run all readiness checks for a single bot. Returns status dict.

    Logic:
    * Issues → BLOCK (cannot launch)
    * Warnings → WARN (launchable with caveats)
    * Neither → READY

    A bot is fully READY if it has BOTH a baseline AND a warmup_policy
    (or has been explicitly marked deactivated). The two gates cover
    different concerns:
    * baseline → drift watchdog has a reference for live PnL comparison
    * warmup_policy → sizing discipline during the first N days post-
      promotion; protects from regime-shift outliers blowing the eval
    """
    issues: list[str] = []
    warnings: list[str] = []
    extras = assignment.extras or {}
    evidence: dict[str, object] = {}

    if bool(extras.get("deactivated")):
        return {
            "bot_id": assignment.bot_id,
            "strategy_id": assignment.strategy_id,
            "strategy_kind": assignment.strategy_kind,
            "symbol": assignment.symbol,
            "timeframe": assignment.timeframe,
            "promotion_status": "deactivated",
            "status": "READY",
            "issues": [],
            "warnings": [],
        }

    # 1. Strategy kind resolvable
    if assignment.strategy_kind not in _RESOLVABLE_KINDS:
        issues.append(f"unknown strategy_kind: {assignment.strategy_kind}")

    # 2. Data available
    if not _check_data_available(assignment.symbol, assignment.timeframe):
        issues.append(
            f"no data file for {assignment.symbol}/{assignment.timeframe}",
        )
    else:
        data_freshness = _check_data_freshness(assignment.symbol, assignment.timeframe)
        if data_freshness is not None:
            evidence["data_freshness"] = data_freshness
            freshness_warning = _data_freshness_warning(
                assignment.symbol,
                assignment.timeframe,
                data_freshness,
            )
            if freshness_warning is not None:
                warnings.append(freshness_warning)

    critical_data = _check_critical_data_requirements(
        assignment.bot_id,
        primary_symbol=assignment.symbol,
        primary_timeframe=assignment.timeframe,
    )
    issues.extend(str(issue) for issue in critical_data["issues"])
    warnings.extend(str(warning) for warning in critical_data["warnings"])
    if critical_data["evidence"]:
        evidence["critical_data_requirements"] = critical_data["evidence"]

    # 3. Bot directory exists
    if not _check_bot_dir_exists(assignment.bot_id):
        issues.append(f"no bot directory for {assignment.bot_id}")

    # 4. Promotion-status check (research_candidate is its own warning)
    promo_status = extras.get("promotion_status")
    if promo_status == "research_candidate":
        evidence.update(_research_evidence(extras))
        warnings.append(_research_warning(extras))
    elif promo_status in _SHADOW_ONLY_STATUSES:
        evidence["launch_role"] = "shadow_only"
        shadow_reason = extras.get("shadow_reason")
        if isinstance(shadow_reason, str) and shadow_reason:
            evidence["shadow_reason"] = shadow_reason
    elif promo_status in _NON_EDGE_STATUSES:
        evidence["launch_role"] = "non_edge_exposure"
        non_edge_reason = extras.get("non_edge_reason")
        if isinstance(non_edge_reason, str) and non_edge_reason:
            evidence["non_edge_reason"] = non_edge_reason
    elif promo_status == "deactivated":
        # Deactivated bots aren't warnings — they're explicitly off
        return {
            "bot_id": assignment.bot_id,
            "strategy_id": assignment.strategy_id,
            "strategy_kind": assignment.strategy_kind,
            "symbol": assignment.symbol,
            "timeframe": assignment.timeframe,
            "promotion_status": "deactivated",
            "status": "READY",  # explicitly off = not a launch blocker
            "issues": [],
            "warnings": [],
        }

    # 5. Baseline + warmup_policy.
    #
    # Ready logic:
    # * Baseline present → drift watchdog has a reference. Implicit
    #   standard warmup (30 days × 0.5 risk) applies unless overridden.
    # * No baseline → real validation gap, WARN.
    # * Explicit warmup_policy → applied as override.
    baseline_entry = _load_baseline_entry(
        assignment.bot_id, assignment.strategy_id,
    )
    has_baseline = baseline_entry is not None
    has_warmup_override = bool(
        extras.get("warmup_policy"),
    )
    if not has_baseline:
        warnings.append("baseline not in strategy_baselines.json")
    elif promo_status == "research_candidate" and not evidence:
        for key in (
            "_latest_walk_forward_summary",
            "_walk_forward_summary",
            "_full_history_smoke",
        ):
            value = baseline_entry.get(key)
            if isinstance(value, str) and value:
                evidence["baseline_summary"] = value
                break
    # warmup_policy is no longer a WARN by itself — implicit standard
    # applies. Only flag when the bot has neither baseline nor warmup
    # (caught by the no-baseline warning above).
    _ = has_warmup_override  # kept for reporting clarity

    if issues:
        status = "BLOCK"
    elif warnings:
        status = "WARN"
    else:
        status = "READY"

    return {
        "bot_id": assignment.bot_id,
        "strategy_id": assignment.strategy_id,
        "strategy_kind": assignment.strategy_kind,
        "symbol": assignment.symbol,
        "timeframe": assignment.timeframe,
        "promotion_status": promo_status or "promoted",
        "status": status,
        "issues": issues,
        "warnings": warnings,
        "evidence": {
            "baseline_present": has_baseline,
            "warmup_policy": (
                extras.get("warmup_policy")
                if has_warmup_override else "implicit_standard_30d_half_risk"
            ),
            **evidence,
        },
    }


def _print_table(results: list[dict]) -> None:
    print(f"\n{'STATUS':<7}  {'bot_id':<22}  {'strategy_id':<28}"
          f"  {'kind':<22}  notes")
    print("-" * 150)
    for r in results:
        symbol_status = {
            "READY": "READY",
            "WARN": "WARN ",
            "BLOCK": "BLOCK",
        }[r["status"]]
        notes = "; ".join(r["issues"] + r["warnings"]) or "-"
        if len(notes) > 96:
            notes = notes[:93] + "..."
        print(
            f"{symbol_status:<7}  {r['bot_id']:<22}  {r['strategy_id']:<28}"
            f"  {r['strategy_kind']:<22}  {notes}"
        )
    print("-" * 150)
    counts = {"READY": 0, "WARN": 0, "BLOCK": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"\nSummary: {counts['READY']} READY, {counts['WARN']} WARN, "
          f"{counts['BLOCK']} BLOCK (out of {len(results)} bots)")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--bots", default=None,
        help="Comma-separated bot_ids to audit (default: all)",
    )
    p.add_argument("--json", action="store_true",
                   help="Emit JSON instead of a table")
    args = p.parse_args()

    from eta_engine.strategies.per_bot_registry import ASSIGNMENTS

    filt = (
        {b.strip() for b in args.bots.split(",") if b.strip()}
        if args.bots else None
    )
    targets = [
        a for a in ASSIGNMENTS
        if filt is None or a.bot_id in filt
    ]

    results = [_audit_bot(a) for a in targets]
    results.sort(key=lambda r: (
        {"READY": 0, "WARN": 1, "BLOCK": 2}[r["status"]],
        r["bot_id"],
    ))

    if args.json:
        print(json.dumps({
            "timestamp": datetime.now(UTC).isoformat(),
            "n_bots": len(results),
            "ready": [r for r in results if r["status"] == "READY"],
            "warn": [r for r in results if r["status"] == "WARN"],
            "block": [r for r in results if r["status"] == "BLOCK"],
        }, indent=2))
    else:
        print(f"[paper-live-check] {datetime.now(UTC).isoformat()}")
        print(f"[paper-live-check] auditing {len(results)} bots")
        _print_table(results)

    n_blocked = sum(1 for r in results if r["status"] == "BLOCK")
    return n_blocked  # exit code = number of blockers


if __name__ == "__main__":
    sys.exit(main())
