"""
EVOLUTIONARY TRADING ALGO  //  data.audit
==========================================
Cross-reference ``data.library`` (what we have) with
``data.requirements`` (what each bot needs) to produce a coverage
report. JARVIS reads this to know which bots are blocked on data
fetches.

Output structure
----------------
``audit_bot(bot_id)`` returns a ``BotAudit`` with three lists:
``available`` (have it), ``missing_critical`` (bot is blocked
without these), ``missing_optional`` (would improve the strategy
but isn't blocking).

Per-feed matching rules:

* ``kind == "bars"``: look up ``library.get(symbol, timeframe)``.
* ``kind == "correlation"``: same lookup; correlation feed is just
  another bar series.
* ``kind == "funding" / "onchain" / "sentiment" / "macro"``: not
  yet covered by the library (which only knows about bar CSVs).
  These are reported as missing until the library learns about
  these data shapes — that's a separate iteration.
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

    @property
    def is_runnable(self) -> bool:
        """True iff the bot has every critical feed available."""
        return not self.missing_critical

    @property
    def critical_coverage_pct(self) -> float:
        """0..100 — fraction of critical reqs that are available."""
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
    """Map a DataRequirement to a library dataset.

    * ``bars`` / ``correlation`` — direct (symbol, timeframe) lookup.
    * ``funding`` — files written as ``<X>FUND_<TF>.csv`` by
      ``scripts/fetch_funding_rates``, looked up under the synthetic
      symbol ``<X>FUND``.
    * ``onchain`` — files written as ``<X>ONCHAIN_<TF>.csv`` by
      ``scripts/fetch_onchain_history``, looked up under the synthetic
      symbol ``<X>ONCHAIN``. Defaults to daily ("D") when ``timeframe``
      is None on the requirement (the canonical Glassnode-style cadence).
    * ``sentiment`` — files written as ``<X>SENT_<TF>.csv`` looked up
      under ``<X>SENT``. Same default-D fallback as onchain.
    * ``macro`` — files written as ``<NAME>MACRO_<TF>.csv`` (e.g.
      ``DXYMACRO_D.csv``). Unlike the others the symbol IS the macro
      ticker (DXY, VIX, FEAR_GREED, etc.) so the synthetic suffix goes
      after the requirement's symbol verbatim.
    """
    if _is_bar_kind(req.kind) and req.timeframe is not None:
        return lib.get(symbol=req.symbol, timeframe=req.timeframe)
    if req.kind == "funding" and req.timeframe is not None:
        return lib.get(symbol=f"{req.symbol}FUND", timeframe=req.timeframe)
    if req.kind == "onchain":
        tf = req.timeframe or "D"
        return lib.get(symbol=f"{req.symbol}ONCHAIN", timeframe=tf)
    if req.kind == "sentiment":
        tf = req.timeframe or "D"
        return lib.get(symbol=f"{req.symbol}SENT", timeframe=tf)
    if req.kind == "macro":
        tf = req.timeframe or "D"
        return lib.get(symbol=f"{req.symbol}MACRO", timeframe=tf)
    return None


def audit_bot(bot_id: str, library: DataLibrary | None = None) -> BotAudit | None:
    """Return coverage for ``bot_id`` or None if no requirements registered."""
    from eta_engine.data.library import default_library
    from eta_engine.data.requirements import get_requirements

    reqs = get_requirements(bot_id)
    if reqs is None:
        return None
    lib = library or default_library()

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
    runnable = [a for a in audits if a.is_runnable]
    blocked = [a for a in audits if not a.is_runnable]

    lines = [
        "# Bot data coverage audit",
        "",
        f"_Runnable: {len(runnable)} / {len(audits)}_  "
        f"_Blocked: {len(blocked)} (missing critical data)_",
        "",
        "| Bot | Critical % | Available | Missing critical | Missing optional |",
        "|---|---:|---|---|---|",
    ]
    for a in audits:
        avail_str = ", ".join(
            f"{r.kind}:{r.symbol}/{r.timeframe or '-'}" for r, _ in a.available
        ) or "—"
        miss_crit = ", ".join(
            f"{r.kind}:{r.symbol}/{r.timeframe or '-'}" for r in a.missing_critical
        ) or "—"
        miss_opt = ", ".join(
            f"{r.kind}:{r.symbol}/{r.timeframe or '-'}" for r in a.missing_optional
        ) or "—"
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
