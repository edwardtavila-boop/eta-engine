"""
JARVIS v3 // trade_narrator (T10)

Turns one consult trace record (or a sequence of records) into a single
operator-readable paragraph and appends it to a daily journal file. The
narrator is intentionally lightweight: NO LLM calls, NO external state,
just a deterministic template renderer fed by the existing trace record
shape.

Why deterministic and not LLM-narrated:

  * Cost: a consult fires ~hundreds of times per day across the fleet.
    Running every one through DeepSeek for a paragraph would burn $$$.
  * Latency: the consult hot-path can't wait on a 2-3s LLM call.
  * Determinism: the operator wants the same record → same paragraph,
    diff-able across days. LLM narration breaks that.

The LLM IS used at the END of the week: ``synthesize_week()`` reads the
daily journal files and asks Hermes to compress them into a 1-page
narrative. That's the high-leverage AI step — synthesis across many
days, not paragraph-per-trade.

Public interface:
  * ``narrate(record) -> str``     — one record → one paragraph.
  * ``append_to_journal(record)``  — narrate + append to today's file.
  * ``read_day(date_str) -> str``  — read one journal file.
  * ``week_files(end_date)`` — list of the 7 most recent journal files.
  * ``EXPECTED_HOOKS`` — wiring-audit declaration.

Storage: ``var/eta_engine/state/trade_journal/YYYY-MM-DD.md`` per
CLAUDE.md hard rule #1. One file per day, append-only, plain markdown.
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.brain.jarvis_v3.trade_narrator")

DEFAULT_JOURNAL_DIR = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\trade_journal",
)

EXPECTED_HOOKS = (
    "narrate",
    "append_to_journal",
    "read_day",
    "week_files",
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _today_utc() -> date:
    return datetime.now(UTC).date()


def _journal_path(d: date, dir_: Path | None = None) -> Path:
    base = dir_ or DEFAULT_JOURNAL_DIR
    return base / f"{d.isoformat()}.md"


def _safe_get(rec: Any, key: str, default: Any = None) -> Any:  # noqa: ANN401
    if isinstance(rec, dict):
        return rec.get(key, default)
    return default


def _fmt_size_modifier(rec: dict) -> str:
    """Render the size_modifier as a percent string (e.g. '70%')."""
    verdict = _safe_get(rec, "verdict", {})
    size = verdict.get("final_size_multiplier") if isinstance(verdict, dict) else None
    if size is None:
        size = _safe_get(rec, "final_size", 1.0)
    try:
        return f"{int(round(float(size) * 100))}%"
    except (TypeError, ValueError):
        return "?%"


def _fmt_verdict(rec: dict) -> str:
    """Pluck final_verdict from nested verdict dict, fall back to action."""
    verdict = _safe_get(rec, "verdict", {})
    if isinstance(verdict, dict):
        fv = verdict.get("final_verdict")
        if fv:
            return str(fv).upper()
    return str(_safe_get(rec, "action", "UNKNOWN")).upper()


def _fmt_dissent(rec: dict) -> str:
    """List schools that dissented from the majority verdict."""
    dissent = _safe_get(rec, "dissent", [])
    if not isinstance(dissent, (list, tuple)) or not dissent:
        return ""
    names = []
    for d in dissent:
        if isinstance(d, dict):
            names.append(str(d.get("school", "?")))
        else:
            names.append(str(d))
    if not names:
        return ""
    return f" Dissent: {', '.join(names)}."


def _fmt_block_reason(rec: dict) -> str:
    """Render the block_reason if present (e.g. 'fleet_kill_active')."""
    br = _safe_get(rec, "block_reason")
    if not br:
        return ""
    return f" BLOCKED: {br}."


def _fmt_hermes_calls(rec: dict) -> str:
    """Summarise any Hermes interactions during the consult."""
    hc = _safe_get(rec, "hermes_calls", {})
    if not isinstance(hc, dict) or not hc:
        return ""
    sites = [k for k, v in hc.items() if v]
    if not sites:
        return ""
    return f" Hermes touched: {', '.join(sites)}."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def narrate(record: dict) -> str:
    """Render one trace record as a one-paragraph operator-readable summary.

    The template is intentionally short and parseable so a week's worth
    of paragraphs can be skimmed in 60 seconds. Format:

        [HH:MM:SS] {BOT}  {VERDICT} @ {SIZE}%  consult={ID8}  {dissent} {hermes_calls}

    NEVER raises. Bad input → returns a degraded-but-safe placeholder
    paragraph so the journal append path keeps moving.
    """
    if not isinstance(record, dict):
        return "[??:??:??] ???  UNKNOWN consult (record not a dict)"

    ts_raw = _safe_get(record, "ts", "")
    # Trim ISO timestamp to HH:MM:SS for readability
    hhmmss = "??:??:??"
    if isinstance(ts_raw, str) and len(ts_raw) >= 19:
        # Pull the time portion regardless of timezone suffix
        try:
            hhmmss = ts_raw[11:19]
        except Exception:  # noqa: BLE001
            hhmmss = "??:??:??"

    bot = str(_safe_get(record, "bot_id", "?"))
    verdict = _fmt_verdict(record)
    size_pct = _fmt_size_modifier(record)
    consult_id = str(_safe_get(record, "consult_id", "?"))[:8]
    dissent = _fmt_dissent(record)
    block_reason = _fmt_block_reason(record)
    hermes = _fmt_hermes_calls(record)

    return (f"[{hhmmss}] {bot}  {verdict} @ {size_pct}  consult={consult_id}.{block_reason}{dissent}{hermes}").rstrip()


def _atomic_append(path: Path, text: str) -> bool:
    """Append `text` to `path` durably (open-append-close-fsync).

    Returns True on success, False on failure (logged, not raised).
    True append-atomicity isn't a thing on Windows for shared files —
    but a single ``write()`` call of a short paragraph is well below the
    pipe-buffer size, so concurrent writers won't interleave bytes.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
            fh.flush()
            with contextlib.suppress(OSError, AttributeError):
                os.fsync(fh.fileno())
        return True
    except OSError as exc:
        logger.warning("trade_narrator append failed: %s", exc)
        return False


def append_to_journal(
    record: dict,
    journal_dir: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """Narrate the record and append the paragraph to today's journal file.

    The file is created if it doesn't exist with a small header line.
    Returns True on success, False on failure (best-effort, never raises).
    """
    today = (now or datetime.now(UTC)).date()
    target = _journal_path(today, journal_dir)

    # First-write header to make the file self-documenting
    header_needed = not target.exists()
    line = narrate(record) + "\n"

    if header_needed:
        header = (
            f"# JARVIS Trade Journal — {today.isoformat()}\n"
            f"\n"
            f"_One line per consult. Format: [time] bot_id  VERDICT @ size%  "
            f"consult=id8.  (dissent / block_reason / hermes_calls if any)_\n\n"
        )
        line = header + line
    return _atomic_append(target, line)


def read_day(d: date | str, journal_dir: Path | None = None) -> str:
    """Return the full contents of one day's journal, or "" if missing.

    NEVER raises. Operator-facing read path used by the
    jarvis-trade-narrator skill's synthesis prompt.
    """
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d)
        except ValueError:
            return ""
    target = _journal_path(d, journal_dir)
    if not target.exists():
        return ""
    try:
        return target.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("trade_narrator.read_day failed: %s", exc)
        return ""


def week_files(end_date: date | None = None, journal_dir: Path | None = None) -> list[Path]:
    """Return up to 7 most-recent journal files ending on ``end_date``.

    The list is newest-first. Missing days are skipped (not included as
    empty entries). Used by ``synthesize_week()`` to feed Hermes the
    week's worth of paragraphs in one prompt.
    """
    end = end_date or _today_utc()
    base = journal_dir or DEFAULT_JOURNAL_DIR
    files: list[Path] = []
    for delta in range(0, 7):
        d = end - timedelta(days=delta)
        candidate = _journal_path(d, base)
        if candidate.exists():
            files.append(candidate)
    return files
