"""
EVOLUTIONARY TRADING ALGO  //  data.audit
=========================================
Cross-reference ``data.library`` (what we have) with
``data.requirements`` (what each bot needs) to produce a coverage
report. JARVIS reads this to know which bots are blocked on data
fetches.

Output structure
----------------
``audit_bot(bot_id)`` returns a ``BotAudit`` with three lists:
``available`` (have it), ``missing_critical`` (bot is blocked
without these), ``missing_optional`` (would improve the strategy
but is not blocking).

Per-feed matching rules:

* ``kind == "bars"``: look up ``library.get(symbol, timeframe)``.
* ``kind == "correlation"``: same lookup; correlation feed is just
  another bar series.
* ``kind == "funding" / "onchain" / "sentiment" / "macro"``:
  resolve through synthetic library symbols such as ``BTCFUND``,
  ``BTCONCHAIN``, or ``FEAR_GREEDMACRO`` so support feeds can live
  in the same canonical catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.data.library import DataLibrary, DatasetMeta
    from eta_engine.data.requirements import DataRequirement


@dataclass
class BotAudit:
    """Per-bot data coverage report."""

    bot_id: str
    available: list[tuple[DataRequirement, DatasetMeta]] = field(default_factory=list)
    missing_critical: list[DataRequirement] = field(default_factory=list)
    missing_optional: list[DataRequirement] = field(default_factory=list)
    sources_hint: tuple[str, ...] = field(default_factory=tuple)
    deactivated: bool = False

    @property
    def is_runnable(self) -> bool:
        """True iff the bot has every critical feed available."""
        return not self.missing_critical

    @property
    def critical_coverage_pct(self) -> float:
        """0..100 - fraction of critical reqs that are available."""
        critical_reqs = [r for r, _ in self.available if r.critical] + self.missing_critical
        if not critical_reqs:
            return 100.0
        return 100.0 * (len(critical_reqs) - len(self.missing_critical)) / len(critical_reqs)


def _is_bar_kind(kind: str) -> bool:
    return kind in {"bars", "correlation"}


def _resolve_library_lookup(
    req: DataRequirement,
    lib: DataLibrary,
) -> DatasetMeta | None:
    """Map a DataRequirement to a library dataset."""
    if _is_bar_kind(req.kind) and req.timeframe is not None:
        return lib.get(symbol=req.symbol, timeframe=req.timeframe)
    if req.kind == "funding" and req.timeframe is not None:
        return lib.get(symbol=f"{req.symbol}FUND", timeframe=req.timeframe)
    if req.kind == "onchain":
        tf = req.timeframe or "D"
        return lib.get(symbol=f"{req.symbol}ONCHAIN", timeframe=tf)
    if req.kind == "sentiment":
        tf = req.timeframe or "D"
        direct = lib.get(symbol=f"{req.symbol}SENT", timeframe=tf)
        if direct is not None:
            return direct
        if tf != "D":
            direct_daily = lib.get(symbol=f"{req.symbol}SENT", timeframe="D")
            if direct_daily is not None:
                return direct_daily
        if req.symbol.upper() in {"BTC", "ETH", "SOL"}:
            # Alternative.me Fear & Greed is a crypto-wide daily proxy. Keep
            # specific paid/provider feeds preferred, but let optional BTC/ETH/SOL
            # sentiment requirements resolve to this honest lower-resolution proxy.
            return lib.get(symbol="FEAR_GREEDMACRO", timeframe="D")
        return None
    if req.kind == "macro":
        tf = req.timeframe or "D"
        return lib.get(symbol=f"{req.symbol}MACRO", timeframe=tf)
    return None


def audit_bot(bot_id: str, library: DataLibrary | None = None) -> BotAudit | None:
    """Return coverage for ``bot_id`` or None if no requirements registered."""
    from eta_engine.data.library import default_library
    from eta_engine.data.requirements import get_requirements
    from eta_engine.strategies.per_bot_registry import get_for_bot, is_active

    reqs = get_requirements(bot_id)
    if reqs is None:
        return None
    lib = library or default_library()

    assignment = get_for_bot(bot_id)
    if assignment is not None and not is_active(assignment):
        return BotAudit(
            bot_id=bot_id,
            sources_hint=reqs.sources_hint,
            deactivated=True,
        )

    out = BotAudit(bot_id=bot_id, sources_hint=reqs.sources_hint)
    for req in reqs.requirements:
        ds = _resolve_library_lookup(req, lib)
        if ds is not None:
            out.available.append((req, ds))
        elif req.critical:
            out.missing_critical.append(req)
        else:
            out.missing_optional.append(req)
    return out


def audit_all(library: DataLibrary | None = None) -> list[BotAudit]:
    """Audit every bot in the requirements registry."""
    from eta_engine.data.requirements import all_requirements

    out: list[BotAudit] = []
    for r in all_requirements():
        a = audit_bot(r.bot_id, library=library)
        if a is not None:
            out.append(a)
    return out


def summary_markdown(audits: list[BotAudit]) -> str:
    """Single-table report. Mark blockers up front so JARVIS can flag."""
    runnable = [a for a in audits if a.is_runnable and not a.deactivated]
    blocked = [a for a in audits if not a.is_runnable]
    deactivated = [a for a in audits if a.deactivated]

    lines = [
        "# Bot data coverage audit",
        "",
        f"_Runnable: {len(runnable)} / {len(audits)}_  "
        f"_Blocked: {len(blocked)} (missing critical data)_  "
        f"_Deactivated: {len(deactivated)}_",
        "",
        "| Bot | Critical % | Available | Missing critical | Missing optional |",
        "|---|---:|---|---|---|",
    ]
    for a in audits:
        if a.deactivated:
            avail_str = "deactivated"
            miss_crit = "-"
            miss_opt = "-"
        else:
            avail_str = ", ".join(
                f"{r.kind}:{r.symbol}/{r.timeframe or '-'}" for r, _ in a.available
            ) or "-"
            miss_crit = ", ".join(
                f"{r.kind}:{r.symbol}/{r.timeframe or '-'}" for r in a.missing_critical
            ) or "-"
            miss_opt = ", ".join(
                f"{r.kind}:{r.symbol}/{r.timeframe or '-'}" for r in a.missing_optional
            ) or "-"
        lines.append(
            f"| {a.bot_id} | {a.critical_coverage_pct:.0f}% | {avail_str} | "
            f"{miss_crit} | {miss_opt} |"
        )

    if blocked:
        lines.append("")
        lines.append("## Suggested data sources for blocked bots")
        lines.append("")
        for a in blocked:
            if a.sources_hint:
                lines.append(f"- **{a.bot_id}**: {'; '.join(a.sources_hint)}")
    return "\n".join(lines)
