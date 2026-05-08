"""Build a machine-readable bot strategy/data readiness matrix.

This is an operator-facing companion to ``paper_live_launch_check``: it keeps
strategy assignment, promotion status, baseline presence, and critical data
coverage in one compact row per bot.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402

if TYPE_CHECKING:
    from eta_engine.data.library import DataLibrary
    from eta_engine.strategies.per_bot_registry import StrategyAssignment


@dataclass(frozen=True)
class PriorityMetadata:
    """Capital-priority metadata for readiness rows and operator surfaces."""

    asset_class: str
    priority_bucket: str
    priority_rank: int
    preferred_broker_stack: tuple[str, ...]
    edge_thesis: str
    primary_edges: tuple[str, ...]
    exit_playbook: str
    risk_playbook: str
    daily_focus: str


@dataclass(frozen=True)
class ReadinessRow:
    """One bot's current strategy and launch-readiness posture."""

    bot_id: str
    strategy_id: str
    strategy_kind: str
    symbol: str
    timeframe: str
    active: bool
    promotion_status: str
    baseline_status: str
    data_status: str
    launch_lane: str
    can_paper_trade: bool
    can_live_trade: bool
    missing_critical: tuple[str, ...]
    missing_optional: tuple[str, ...]
    next_action: str
    asset_class: str = "unknown"
    priority_bucket: str = "other"
    priority_rank: int = 999
    preferred_broker_stack: tuple[str, ...] = ()
    capital_priority: int = 999_999
    edge_thesis: str = ""
    primary_edges: tuple[str, ...] = ()
    exit_playbook: str = ""
    risk_playbook: str = ""
    daily_focus: str = ""


_EQUITY_INDEX_FUTURES = frozenset({"MNQ", "NQ", "MES", "ES", "M2K", "RTY", "MYM", "YM"})
_COMMODITY_FUTURES = frozenset({"MCL", "CL", "MGC", "GC", "NG", "SI", "HG"})
_RATES_FX_FUTURES = frozenset({"6E", "M6E", "ZN", "ZB", "ZF", "ZT"})
_CME_CRYPTO_FUTURES = frozenset({"MBT", "MET"})
_SPOT_CRYPTO = frozenset({"BTC", "ETH", "SOL", "XRP", "AVAX", "LINK", "DOGE"})
_KNOWN_SYMBOL_ROOTS_BY_LENGTH = tuple(sorted(
    _EQUITY_INDEX_FUTURES
    | _COMMODITY_FUTURES
    | _RATES_FX_FUTURES
    | _CME_CRYPTO_FUTURES
    | _SPOT_CRYPTO,
    key=len,
    reverse=True,
))
_FUTURES_MONTH_CODES = "FGHJKMNQUVXZ"

_FUTURES_BROKER_STACK = ("ibkr", "tradovate_when_enabled", "tastytrade")
_IBKR_FUTURES_BROKER_STACK = ("ibkr", "tradovate_when_enabled")
_SPOT_CRYPTO_BROKER_STACK = ("alpaca", "ibkr_when_crypto_live_enabled")

_BUCKET_ORDER = (
    "equity_index_futures",
    "commodities",
    "rates_fx",
    "cme_crypto_futures",
    "other",
    "spot_crypto",
)
_BROKER_PRIORITY = ("ibkr", "tradovate_when_enabled", "tastytrade", "alpaca")
_BOT_PRIORITY_TIEBREAK = {
    "volume_profile_mnq": 0,
    "volume_profile_nq": 1,
    "mnq_futures_sage": 2,
    "rsi_mr_mnq_v2": 3,
}


def _symbol_root(symbol: str) -> str:
    """Normalize supervisor/front-month symbols into routing class roots."""
    raw = str(symbol or "").strip().upper()
    if "/" in raw:
        raw = raw.split("/", 1)[0]
    cleaned = "".join(ch for ch in raw if ch.isalnum())
    for suffix in ("USDT", "USD"):
        if cleaned.endswith(suffix):
            candidate = cleaned[: -len(suffix)]
            if candidate in _SPOT_CRYPTO:
                cleaned = candidate
                break
    continuous_root = cleaned.rstrip("0123456789") or cleaned
    if continuous_root in _KNOWN_SYMBOL_ROOTS_BY_LENGTH:
        return continuous_root
    for known_root in _KNOWN_SYMBOL_ROOTS_BY_LENGTH:
        if len(cleaned) <= len(known_root):
            continue
        if cleaned.startswith(known_root):
            suffix = cleaned[len(known_root):]
            if len(suffix) in {2, 3} and suffix[0] in _FUTURES_MONTH_CODES and suffix[1:].isdigit():
                return known_root
    return continuous_root


def _priority_metadata(symbol: str) -> PriorityMetadata:
    root = _symbol_root(symbol)
    if root in _EQUITY_INDEX_FUTURES:
        return PriorityMetadata(
            asset_class="futures",
            priority_bucket="equity_index_futures",
            priority_rank=10,
            preferred_broker_stack=_FUTURES_BROKER_STACK,
            edge_thesis=(
                "Index futures are the funded lead lane: exploit session structure, "
                "liquidity sweeps, value-area reversion, and NQ/MNQ leader-follower behavior."
            ),
            primary_edges=(
                "volume_profile_value_area",
                "opening_range_breakout_sage_gate",
                "anchor_sweep_reclaim",
                "rsi_mean_reversion_v2",
            ),
            exit_playbook=(
                "Broker OCO on live entries; paper-local watcher must tighten after SLA, "
                "scale around 1.5R, and protect runners before max-hold."
            ),
            risk_playbook=(
                "Smallest funded contracts first, max correlated MNQ/NQ exposure, "
                "stand aside during volatility/news spikes, and enforce stale-position SLAs."
            ),
            daily_focus=(
                "Perfect MNQ/NQ execution quality before expanding size or adding "
                "adjacent equity-index symbols."
            ),
        )
    if root in _COMMODITY_FUTURES:
        return PriorityMetadata(
            asset_class="futures",
            priority_bucket="commodities",
            priority_rank=20,
            preferred_broker_stack=_FUTURES_BROKER_STACK,
            edge_thesis=(
                "Commodities trade inventory, rollover, and session/event imbalance; "
                "the edge must be asset-specific instead of generic confluence."
            ),
            primary_edges=(
                "event_aware_sweep_reclaim",
                "rollover_adjusted_value_reversion",
                "energy_inventory_gap_filter",
                "metals_session_impulse_filter",
            ),
            exit_playbook=(
                "ATR/native tick brackets with hard event-window flatten or no-new-entry guards; "
                "never let CL/NG paper trades drift through known report windows unchecked."
            ),
            risk_playbook=(
                "Lower size until 5m commodity data, rollover adjustment, and event calendar "
                "gates are verified; NG/CL jumps are blocker-class until proven clean."
            ),
            daily_focus="Separate CL/NG/GC behavior, validate data quality, and only graduate event-aware variants.",
        )
    if root in _RATES_FX_FUTURES:
        return PriorityMetadata(
            asset_class="futures",
            priority_bucket="rates_fx",
            priority_rank=30,
            preferred_broker_stack=_IBKR_FUTURES_BROKER_STACK,
            edge_thesis=(
                "Rates and FX are macro-timing lanes: edge comes from regime, session, "
                "and scheduled catalyst alignment, not high-frequency generic signals."
            ),
            primary_edges=(
                "macro_session_reclaim",
                "fomc_nfp_standaside",
                "trend_pullback_after_catalyst",
            ),
            exit_playbook=(
                "Wider time-based brackets, strict catalyst blackout, and forced review "
                "before holding through major macro releases."
            ),
            risk_playbook=(
                "IBKR-only until venue parity is verified; keep risk low because macro slippage "
                "can invalidate clean backtest assumptions."
            ),
            daily_focus="Treat 6E/ZN as macro specialist lanes and require calendar-aware promotion evidence.",
        )
    if root in _CME_CRYPTO_FUTURES:
        return PriorityMetadata(
            asset_class="futures",
            priority_bucket="cme_crypto_futures",
            priority_rank=40,
            preferred_broker_stack=_IBKR_FUTURES_BROKER_STACK,
            edge_thesis=(
                "CME crypto futures are the regulated crypto lane: prioritize basis, "
                "RTH/overnight structure, and futures microstructure over spot-perp noise."
            ),
            primary_edges=(
                "funding_basis_divergence",
                "rth_opening_range",
                "overnight_gap_reversion",
                "z_score_fade",
            ),
            exit_playbook=(
                "Micro contract brackets with time stops; require 540d MBT/MET data before "
                "trusting small-sample Sharpe."
            ),
            risk_playbook=(
                "Keep research/paper-only until data depth and CME-specific slippage pass; "
                "do not substitute offshore perp assumptions."
            ),
            daily_focus="Build MBT/MET as regulated crypto futures specialists, not clones of spot crypto bots.",
        )
    if root in _SPOT_CRYPTO:
        return PriorityMetadata(
            asset_class="spot_crypto",
            priority_bucket="spot_crypto",
            priority_rank=90,
            preferred_broker_stack=_SPOT_CRYPTO_BROKER_STACK,
            edge_thesis=(
                "Spot crypto is a secondary lane for personal/paper routing: only trade "
                "regime-stable NY-session edges with real feature coverage."
            ),
            primary_edges=(
                "ny_session_momentum_reclaim",
                "funding_sentiment_filter",
                "macro_regime_filter",
            ),
            exit_playbook=(
                "Reduce-only paper exits with local target/stop watcher; disable duplicate "
                "bots that produce identical trades."
            ),
            risk_playbook=(
                "Alpaca paper first; no live spot expansion until duplicate clusters are retired "
                "and missing macro/onchain features are real."
            ),
            daily_focus="Keep only distinct, feature-backed spot crypto edges and avoid cloning futures logic.",
        )
    return PriorityMetadata(
        asset_class="other",
        priority_bucket="other",
        priority_rank=80,
        preferred_broker_stack=("manual_review",),
        edge_thesis="Unclassified symbol: hold outside promotion until a real asset-class playbook is assigned.",
        primary_edges=("manual_review_required",),
        exit_playbook="Manual review before any paper/live routing.",
        risk_playbook="No capital allocation until symbol, venue, and data contract are classified.",
        daily_focus="Classify or retire this lane.",
    )


def _promotion_rank(row: ReadinessRow) -> int:
    status_rank = {
        "production": 0,
        "production_candidate": 1,
        "paper_soak": 2,
        "research_candidate": 4,
        "unbaselined": 5,
        "shadow_benchmark": 6,
        "non_edge_strategy": 7,
        "deprecated": 8,
        "deactivated": 9,
    }
    return status_rank.get(str(row.promotion_status or ""), 5)


def _launch_lane_rank(row: ReadinessRow) -> int:
    lane_rank = {
        "live_preflight": 0,
        "paper_soak": 1,
        "research": 2,
        "blocked_data": 3,
        "shadow_only": 4,
        "non_edge": 5,
        "deactivated": 6,
    }
    return lane_rank.get(str(row.launch_lane or ""), 7)


def _with_priority(row: ReadinessRow) -> ReadinessRow:
    meta = _priority_metadata(row.symbol)
    capital_priority = (meta.priority_rank * 100) + (_promotion_rank(row) * 10) + _launch_lane_rank(row)
    return replace(
        row,
        asset_class=meta.asset_class,
        priority_bucket=meta.priority_bucket,
        priority_rank=meta.priority_rank,
        preferred_broker_stack=meta.preferred_broker_stack,
        capital_priority=capital_priority,
        edge_thesis=meta.edge_thesis,
        primary_edges=meta.primary_edges,
        exit_playbook=meta.exit_playbook,
        risk_playbook=meta.risk_playbook,
        daily_focus=meta.daily_focus,
    )


def _readiness_sort_key(row: ReadinessRow) -> tuple[int, int, int, int, str]:
    prioritized = _with_priority(row)
    return (
        prioritized.capital_priority,
        _BOT_PRIORITY_TIEBREAK.get(prioritized.bot_id, 999),
        prioritized.priority_rank,
        _launch_lane_rank(prioritized),
        prioritized.bot_id,
    )


def prioritize_readiness_rows(rows: list[ReadinessRow] | tuple[ReadinessRow, ...]) -> list[ReadinessRow]:
    """Return rows in the project capital-priority order.

    Futures/index work is the primary funded lane, commodities come next,
    then rates/FX, CME crypto futures, and spot crypto stays last until the
    operator funds and explicitly enables those live broker paths.
    """
    return sorted((_with_priority(row) for row in rows), key=_readiness_sort_key)


def _requirement_label(req: Any) -> str:  # noqa: ANN401 - small duck-typed helper for DataRequirement-like rows.
    return f"{req.kind}:{req.symbol}/{req.timeframe or '-'}"


def _baseline_entry(bot_id: str, strategy_id: str) -> dict[str, object] | None:
    from eta_engine.scripts.paper_live_launch_check import _load_baseline_entry

    return _load_baseline_entry(bot_id, strategy_id)


def _baseline_status(bot_id: str, strategy_id: str) -> str:
    return "baseline_present" if _baseline_entry(bot_id, strategy_id) is not None else "baseline_missing"


def _promotion_status(assignment: StrategyAssignment, *, active: bool) -> str:
    if not active:
        return "deactivated"
    explicit = assignment.extras.get("promotion_status")
    if isinstance(explicit, str) and explicit:
        return explicit
    baseline = _baseline_entry(assignment.bot_id, assignment.strategy_id)
    baseline_status = baseline.get("_promotion_status") if baseline else None
    if isinstance(baseline_status, str) and baseline_status:
        return baseline_status
    return "production" if baseline is not None else "unbaselined"


def _row_for_assignment(assignment: StrategyAssignment, *, library: DataLibrary) -> ReadinessRow:
    from eta_engine.data.audit import audit_bot
    from eta_engine.strategies.per_bot_registry import is_active

    active = is_active(assignment)
    promotion_status = _promotion_status(assignment, active=active)
    baseline_status = _baseline_status(assignment.bot_id, assignment.strategy_id)

    if not active:
        return ReadinessRow(
            bot_id=assignment.bot_id,
            strategy_id=assignment.strategy_id,
            strategy_kind=assignment.strategy_kind,
            symbol=assignment.symbol,
            timeframe=assignment.timeframe,
            active=False,
            promotion_status=promotion_status,
            baseline_status=baseline_status,
            data_status="deactivated",
            launch_lane="deactivated",
            can_paper_trade=False,
            can_live_trade=False,
            missing_critical=(),
            missing_optional=(),
            next_action="No action: bot is explicitly deactivated.",
        )

    audit = audit_bot(assignment.bot_id, library=library)
    missing_critical = tuple(_requirement_label(req) for req in (audit.missing_critical if audit else ()))
    missing_optional = tuple(_requirement_label(req) for req in (audit.missing_optional if audit else ()))

    if missing_critical:
        data_status = "blocked"
        launch_lane = "blocked_data"
        can_paper_trade = False
        next_action = "Fetch missing critical data: " + ", ".join(missing_critical)
    elif promotion_status in {"shadow_benchmark", "deprecated"}:
        data_status = "ready"
        launch_lane = "shadow_only"
        can_paper_trade = False
        next_action = "Keep as diagnostics only; do not paper-trade this lane."
    elif promotion_status == "non_edge_strategy":
        data_status = "ready"
        launch_lane = "non_edge"
        can_paper_trade = False
        next_action = "Keep separate from promotion-gated trading edges."
    elif promotion_status == "research_candidate":
        data_status = "ready"
        launch_lane = "research"
        can_paper_trade = False
        next_action = "Continue research retest; do not promote until strict gate and soak pass."
    elif promotion_status == "production":
        data_status = "ready"
        launch_lane = "live_preflight"
        can_paper_trade = True
        next_action = "Run per-bot promotion preflight before live routing."
    else:
        data_status = "ready"
        launch_lane = "paper_soak"
        can_paper_trade = True
        next_action = "Run paper-soak and broker drift checks before live routing."

    return ReadinessRow(
        bot_id=assignment.bot_id,
        strategy_id=assignment.strategy_id,
        strategy_kind=assignment.strategy_kind,
        symbol=assignment.symbol,
        timeframe=assignment.timeframe,
        active=True,
        promotion_status=promotion_status,
        baseline_status=baseline_status,
        data_status=data_status,
        launch_lane=launch_lane,
        can_paper_trade=can_paper_trade,
        can_live_trade=False,
        missing_critical=missing_critical,
        missing_optional=missing_optional,
        next_action=next_action,
    )


def build_readiness_matrix(
    *,
    library: DataLibrary | None = None,
    bot_ids: list[str] | tuple[str, ...] | None = None,
) -> list[ReadinessRow]:
    """Return strategy/data readiness rows for selected bots or all bots."""
    from eta_engine.data.library import default_library
    from eta_engine.strategies.per_bot_registry import all_assignments, get_for_bot

    lib = library or default_library()
    if bot_ids is None:
        assignments = all_assignments()
    else:
        assignments = []
        for bot_id in bot_ids:
            assignment = get_for_bot(bot_id)
            if assignment is not None:
                assignments.append(assignment)
    return prioritize_readiness_rows([_row_for_assignment(assignment, library=lib) for assignment in assignments])


def supervisor_pinned_bot_ids() -> tuple[str, ...]:
    """Return the Windows supervisor runner pin in stable display order."""
    from eta_engine.scripts.paper_live_launch_check import _supervisor_pinned_bot_ids
    from eta_engine.strategies.per_bot_registry import get_for_bot

    def sort_key(bot_id: str) -> tuple[int, int, str]:
        assignment = get_for_bot(bot_id)
        if assignment is None:
            return (999_999, 999, bot_id)
        meta = _priority_metadata(assignment.symbol)
        promo = str(assignment.extras.get("promotion_status") or "")
        promotion = {
            "production": 0,
            "production_candidate": 1,
            "paper_soak": 2,
            "research_candidate": 4,
        }.get(promo, 5)
        return (
            (meta.priority_rank * 100) + (promotion * 10),
            _BOT_PRIORITY_TIEBREAK.get(bot_id, 999),
            bot_id,
        )

    return tuple(sorted(_supervisor_pinned_bot_ids(), key=sort_key))


def build_snapshot(
    rows: list[ReadinessRow],
    *,
    generated_at: str | None = None,
    scope: str = "all",
    supervisor_pinned: tuple[str, ...] = (),
) -> dict[str, object]:
    """Return a canonical snapshot payload for dashboards and automation."""
    rows = prioritize_readiness_rows(rows)
    lane_counts: dict[str, int] = {}
    bucket_counts: dict[str, int] = {}
    for row in rows:
        lane_counts[row.launch_lane] = lane_counts.get(row.launch_lane, 0) + 1
        bucket_counts[row.priority_bucket] = bucket_counts.get(row.priority_bucket, 0) + 1
    ordered_bucket_counts = {
        bucket: bucket_counts[bucket]
        for bucket in _BUCKET_ORDER
        if bucket_counts.get(bucket)
    }
    return {
        "schema_version": 1,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "source": "bot_strategy_readiness",
        "scope": scope,
        "supervisor_pinned": list(supervisor_pinned),
        "summary": {
            "total_bots": len(rows),
            "blocked_data": lane_counts.get("blocked_data", 0),
            "can_live_any": any(row.can_live_trade for row in rows),
            "can_paper_trade": sum(row.can_paper_trade for row in rows),
            "launch_lanes": dict(sorted(lane_counts.items())),
            "priority_focus": "futures_and_commodities_first",
            "priority_buckets": ordered_bucket_counts,
            "broker_priority": list(_BROKER_PRIORITY),
            "top_priority_bots": [row.bot_id for row in rows[: min(10, len(rows))]],
        },
        "rows": [asdict(row) for row in rows],
    }


def write_snapshot(
    snapshot: dict[str, object],
    path: Path = workspace_roots.ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH,
) -> Path:
    """Atomically write the readiness snapshot and return the target path."""
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot_strategy_readiness")
    parser.add_argument("--bot-id", action="append", default=[], help="bot id to include; repeatable")
    parser.add_argument(
        "--scope",
        choices=("all", "supervisor_pinned"),
        default="all",
        help="assignment scope when --bot-id is omitted",
    )
    parser.add_argument("--root", action="append", default=[], help="data library root; repeatable")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON rows")
    parser.add_argument("--snapshot", action="store_true", help="emit/write canonical snapshot payload")
    parser.add_argument("--no-write", action="store_true", help="build snapshot without writing the artifact")
    parser.add_argument("--out", type=Path, default=workspace_roots.ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH)
    args = parser.parse_args(argv)

    library = None
    if args.root:
        from eta_engine.data.library import DataLibrary

        library = DataLibrary(roots=[Path(root) for root in args.root])

    supervisor_pinned: tuple[str, ...] = ()
    bot_ids = args.bot_id or None
    if bot_ids is None and args.scope == "supervisor_pinned":
        supervisor_pinned = supervisor_pinned_bot_ids()
        bot_ids = list(supervisor_pinned)

    rows = build_readiness_matrix(library=library, bot_ids=bot_ids)
    if args.snapshot:
        snapshot = build_snapshot(
            rows,
            scope="explicit_bots" if args.bot_id else args.scope,
            supervisor_pinned=supervisor_pinned,
        )
        written = None if args.no_write else write_snapshot(snapshot, args.out)
        if args.json:
            print(json.dumps(snapshot, indent=2, sort_keys=True, default=str))
        else:
            target = f" -> {written}" if written is not None else " (no-write)"
            print(
                "bot_strategy_readiness snapshot "
                f"rows={snapshot['summary']['total_bots']} "
                f"lanes={snapshot['summary']['launch_lanes']}{target}"
            )
    elif args.json:
        print(json.dumps([asdict(row) for row in rows], indent=2, sort_keys=True))
    else:
        for row in rows:
            print(
                f"{row.launch_lane:<18} {row.bot_id:<24} {row.strategy_id:<28} "
                f"data={row.data_status} baseline={row.baseline_status}"
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
