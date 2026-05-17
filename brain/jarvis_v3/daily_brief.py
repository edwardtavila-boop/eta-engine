"""Daily brief generator (Wave-14, 2026-04-27).

End-of-day operator summary that pulls from every JARVIS layer
into one readable page.

Sections:
  * HEADLINE       -- one-line status (overall_status from health_check)
  * VERDICTS       -- counts by final_verdict + avg confidence
  * TRADES         -- realized P&L stats + win rate
  * THESES BROKEN  -- early-exit invalidations from thesis_tracker
  * POSTMORTEMS    -- new postmortems generated today
  * DRIFT          -- self_drift_monitor signals
  * SKILL HEALTH   -- degraded/unavailable external deps
  * TOP ANALOGS    -- regime stats from memory

Plain-text + markdown rendering. No external deps.

Operator pattern:
    cron 21:00 UTC -> generate_daily_brief() -> email/Slack via
    alert_dispatcher.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eta_engine.scripts import workspace_roots

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = workspace_roots.ETA_JARVIS_INTEL_STATE_DIR
DEFAULT_BRIEF_DIR = workspace_roots.ETA_JARVIS_DAILY_BRIEF_DIR


@dataclass
class DailyBrief:
    """Aggregated end-of-day operator brief."""

    date_iso: str
    headline: str
    health_summary: str
    n_verdicts: int
    avg_confidence: float
    verdict_breakdown: dict[str, int] = field(default_factory=dict)
    n_trades: int = 0
    avg_realized_r: float = 0.0
    win_rate: float = 0.0
    n_theses_broken: int = 0
    theses_broken_summary: list[str] = field(default_factory=list)
    n_new_postmortems: int = 0
    new_postmortems: list[str] = field(default_factory=list)
    drift_status: str = "OK"
    drift_signals: list[str] = field(default_factory=list)
    degraded_skills: list[str] = field(default_factory=list)
    top_regimes: list[dict] = field(default_factory=list)
    sage_composite_bias: str = ""
    sage_conviction: float = 0.0
    sage_schools_healthy: int = 0
    sage_schools_degraded: int = 0
    sage_top_schools: list[str] = field(default_factory=list)
    sage_disagreement_summary: str = ""
    # Wave-18: Shadow pipeline status
    shadow_fills: int = 0
    shadow_win_rate: float = 0.0
    shadow_sharpe: float = 0.0
    shadow_pipeline_enabled: bool = False
    shadow_promotions: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_markdown(self) -> str:
        lines: list[str] = []
        lines.append(f"# JARVIS Daily Brief — {self.date_iso}")
        lines.append("")
        lines.append(f"**{self.headline}**")
        lines.append("")
        lines.append(f"## Status: {self.health_summary}")
        lines.append("")

        lines.append("## Decisions")
        lines.append(f"- Total verdicts: {self.n_verdicts}")
        lines.append(f"- Avg confidence: {self.avg_confidence:.2f}")
        if self.verdict_breakdown:
            for k, v in sorted(self.verdict_breakdown.items()):
                lines.append(f"  - {k}: {v}")
        lines.append("")

        lines.append("## Trades")
        lines.append(f"- Closed: {self.n_trades}")
        lines.append(f"- Avg realized R: {self.avg_realized_r:+.2f}")
        lines.append(f"- Win rate: {self.win_rate:.0%}")
        lines.append("")

        if self.n_theses_broken > 0:
            lines.append("## Theses Broken")
            for t in self.theses_broken_summary:
                lines.append(f"- {t}")
            lines.append("")

        if self.n_new_postmortems > 0:
            lines.append("## New Postmortems")
            for p in self.new_postmortems:
                lines.append(f"- {p}")
            lines.append("")

        if self.drift_signals:
            lines.append(f"## Self-Drift: {self.drift_status}")
            for s in self.drift_signals:
                lines.append(f"- {s}")
            lines.append("")

        if self.degraded_skills:
            lines.append("## Skill Health Issues")
            for s in self.degraded_skills:
                lines.append(f"- {s}")
            lines.append("")

        if self.top_regimes:
            lines.append("## Memory Top Regimes")
            for r in self.top_regimes[:5]:
                lines.append(
                    f"- {r['regime']}: {r['n_episodes']} episodes, {r['win_rate']:.0%} wr, {r['avg_r']:+.2f}R avg",
                )
            lines.append("")

        if self.notes:
            lines.append("## Notes")
            for n in self.notes:
                lines.append(f"- {n}")
            lines.append("")

        if self.shadow_pipeline_enabled or self.shadow_fills > 0:
            lines.append("## Shadow Pipeline")
            lines.append(f"- Enabled: {self.shadow_pipeline_enabled}")
            lines.append(f"- Fills: {self.shadow_fills}")
            lines.append(f"- Win rate: {self.shadow_win_rate:.1%}")
            lines.append(f"- Sharpe: {self.shadow_sharpe:.2f}")
            if self.shadow_promotions:
                for p in self.shadow_promotions:
                    lines.append(
                        f"- {p.get('strategy_id', '?')} [{p.get('regime', '?')}]: "
                        f"{p.get('action', '?')} "
                        f"(sharpe={p.get('sharpe', 0):.2f}, "
                        f"wr={p.get('win_rate', 0):.0%})"
                    )
            lines.append("")

        if self.sage_composite_bias:
            lines.append("## Sage & Schools")
            lines.append(f"- Composite bias: **{self.sage_composite_bias.upper()}**")
            lines.append(f"- Conviction: {self.sage_conviction:.2f}")
            lines.append(f"- Schools healthy: {self.sage_schools_healthy}, degraded: {self.sage_schools_degraded}")
            if self.sage_top_schools:
                lines.append(f"- Top schools: {', '.join(self.sage_top_schools)}")
            if self.sage_disagreement_summary:
                lines.append(f"- Disagreement: {self.sage_disagreement_summary}")
            lines.append("")

        return "\n".join(lines)


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _within_window(record_ts: str | None, since: datetime) -> bool:
    if not record_ts:
        return False
    try:
        dt = datetime.fromisoformat(str(record_ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt >= since
    except (TypeError, ValueError):
        return False


def generate_daily_brief(
    *,
    n_hours_back: float = 24,
    output_dir: Path = DEFAULT_BRIEF_DIR,
    state_dir: Path | None = None,
    auto_persist: bool = True,
) -> DailyBrief:
    """Build the daily brief by aggregating every JARVIS state source."""
    cutoff = datetime.now(UTC) - timedelta(hours=n_hours_back)
    today = datetime.now(UTC).date().isoformat()
    state_root = Path(state_dir) if state_dir is not None else DEFAULT_STATE_DIR

    # --- Verdicts ---
    verdict_log = state_root / "verdicts.jsonl"
    verdicts = [v for v in _read_jsonl(verdict_log) if _within_window(v.get("ts"), cutoff)]
    n_verdicts = len(verdicts)
    avg_confidence = sum(float(v.get("confidence", 0.0)) for v in verdicts) / n_verdicts if n_verdicts > 0 else 0.0
    breakdown: dict[str, int] = {}
    for v in verdicts:
        k = str(v.get("final_verdict", "UNKNOWN"))
        breakdown[k] = breakdown.get(k, 0) + 1

    # --- Trades ---
    trade_log = state_root / "trade_closes.jsonl"
    trades = [t for t in _read_jsonl(trade_log) if _within_window(t.get("ts"), cutoff)]
    n_trades = len(trades)
    if n_trades > 0:
        rs = [float(t.get("realized_r", 0.0)) for t in trades]
        avg_r = sum(rs) / len(rs)
        win_rate = sum(1 for r in rs if r > 0) / len(rs)
    else:
        avg_r = 0.0
        win_rate = 0.0

    # --- Thesis breaches ---
    breach_log = state_root / "thesis_breaches.jsonl"
    breaches = [b for b in _read_jsonl(breach_log) if _within_window(b.get("ts"), cutoff)]
    breach_summary = [f"{b.get('signal_id', '')}: {b.get('rule_description', '')}" for b in breaches[:10]]

    # --- Postmortems ---
    pm_dir = state_root / "postmortems"
    new_pms: list[str] = []
    if pm_dir.exists():
        for f in pm_dir.glob("*.json"):
            try:
                age_hours = (datetime.now(UTC).timestamp() - f.stat().st_mtime) / 3600.0
                if age_hours <= n_hours_back:
                    new_pms.append(f.stem)
            except OSError:
                continue

    # --- Drift ---
    drift_status = "OK"
    drift_signals: list[str] = []
    try:
        from eta_engine.brain.jarvis_v3.self_drift_monitor import (
            detect_self_drift,
        )

        drift = detect_self_drift(recent_window_hours=n_hours_back)
        drift_status = drift.overall_status
        drift_signals = [s.note for s in drift.signals[:5]]
    except Exception as exc:  # noqa: BLE001
        logger.debug("daily_brief: drift check failed (%s)", exc)

    # --- Skill health ---
    degraded: list[str] = []
    try:
        from eta_engine.brain.jarvis_v3.skill_health_registry import (
            SkillRegistry,
        )

        reg = SkillRegistry.default()
        for h in reg.degraded_or_unavailable():
            degraded.append(
                f"{h.name} ({h.kind}): {h.status.value}, err={h.error_rate:.0%}, p95={h.p95_latency_ms:.0f}ms",
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("daily_brief: skill health failed (%s)", exc)

    # --- Memory regime stats ---
    top_regimes: list[dict] = []
    try:
        from eta_engine.brain.jarvis_v3.admin_query import (
            memory_regime_stats,
        )

        stats = memory_regime_stats()
        top_regimes = [
            {
                "regime": s.regime,
                "n_episodes": s.n_episodes,
                "win_rate": s.win_rate,
                "avg_r": s.avg_r,
            }
            for s in stats[:5]
        ]
    except Exception as exc:  # noqa: BLE001
        logger.debug("daily_brief: regime stats failed (%s)", exc)

    # --- Sage & Schools summary ---
    sage_bias, sage_conv, sage_healthy, sage_degraded = "", 0.0, 0, 0
    sage_top: list[str] = []
    sage_disagree = ""
    try:
        from eta_engine.brain.jarvis_v3.sage.edge_tracker import default_tracker

        tracker = default_tracker()
        edges = tracker.snapshot()
        if edges:
            by_expectancy = sorted(
                edges.items(),
                key=lambda x: x[1].get("expectancy", 0),
                reverse=True,
            )
            sage_top = [f"{name} ({snap['expectancy']:+.2f}R)" for name, snap in by_expectancy[:5]]
        from eta_engine.brain.jarvis_v3.sage.health import default_monitor

        monitor = default_monitor()
        issues = monitor.check_health()
        sage_healthy = len(edges) - len(issues)
        sage_degraded = len(issues)
    except Exception:  # noqa: BLE001
        pass
    try:
        from eta_engine.brain.jarvis_v3.sage.last_report_cache import cache_size as sage_cache_sz

        if sage_cache_sz() > 0:
            # Best-effort: if a report is cached, read its summary
            pass  # pop_last_any would destroy the cache; just note it's alive
    except Exception:  # noqa: BLE001
        pass

    # --- Health summary ---
    health_summary = "OK"
    try:
        from eta_engine.brain.jarvis_v3.health_check import jarvis_health

        h = jarvis_health()
        health_summary = h.overall_status
    except Exception as exc:  # noqa: BLE001
        logger.debug("daily_brief: health check failed (%s)", exc)
        health_summary = "DEGRADED"

    # --- Headline ---
    if drift_status == "CRITICAL" or health_summary == "CRITICAL":
        headline = "CRITICAL: operator action required"
    elif drift_status == "WARNING" or degraded:
        headline = f"DEGRADED: {n_trades} trades, {avg_r:+.2f}R avg, drift={drift_status}"
    else:
        headline = f"OK: {n_trades} trades, {avg_r:+.2f}R avg, win-rate {win_rate:.0%}"

    # --- Shadow pipeline ---
    shadow_fills = 0
    shadow_win_rate = 0.0
    shadow_sharpe = 0.0
    shadow_enabled = False
    shadow_promotions: list[dict] = []
    try:
        from eta_engine.brain.jarvis_v3.shadow_pipeline import ShadowPipeline

        pipe = ShadowPipeline.default()
        pipe.load_fills()
        shadow_fills = pipe.total_fills
        shadow_win_rate = pipe.win_rate
        shadow_sharpe = pipe.sharpe
        shadow_enabled = pipe.enabled
    except Exception as exc:
        logger.debug("daily_brief: shadow pipeline failed (%s)", exc)

    # --- Notes ---
    notes: list[str] = []
    if n_verdicts == 0:
        notes.append("No JARVIS consultations in window")
    if n_trades == 0 and n_verdicts > 0:
        notes.append("Verdicts logged but no trades closed")
    if n_new_postmortems := len(new_pms):
        notes.append(f"{n_new_postmortems} new postmortem(s) generated today")
    if shadow_fills > 0:
        notes.append(f"Shadow pipeline: {shadow_fills} fills, wr={shadow_win_rate:.0%}, sharpe={shadow_sharpe:.2f}")

    brief = DailyBrief(
        date_iso=today,
        headline=headline,
        health_summary=health_summary,
        n_verdicts=n_verdicts,
        avg_confidence=round(avg_confidence, 3),
        verdict_breakdown=breakdown,
        n_trades=n_trades,
        avg_realized_r=round(avg_r, 4),
        win_rate=round(win_rate, 3),
        n_theses_broken=len(breaches),
        theses_broken_summary=breach_summary,
        n_new_postmortems=len(new_pms),
        new_postmortems=new_pms,
        drift_status=drift_status,
        drift_signals=drift_signals,
        degraded_skills=degraded,
        top_regimes=top_regimes,
        sage_composite_bias=sage_bias,
        sage_conviction=sage_conv,
        sage_schools_healthy=sage_healthy,
        sage_schools_degraded=sage_degraded,
        sage_top_schools=sage_top,
        sage_disagreement_summary=sage_disagree,
        shadow_fills=shadow_fills,
        shadow_win_rate=round(shadow_win_rate, 4),
        shadow_sharpe=round(shadow_sharpe, 4),
        shadow_pipeline_enabled=shadow_enabled,
        shadow_promotions=shadow_promotions,
        notes=notes,
    )

    if auto_persist:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / f"{today}.md").write_text(
                brief.to_markdown(),
                encoding="utf-8",
            )
            (output_dir / f"{today}.json").write_text(
                json.dumps(brief.to_dict(), indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("daily_brief: persist failed (%s)", exc)

    return brief
