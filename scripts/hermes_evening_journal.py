"""
Hermes evening journal — cron entrypoint at 22:30 ET (02:30 UTC next day).

End-of-day memory consolidation. Once per weekday, after futures
settlement + the daily debrief, this cron asks Hermes to review the
day and write 1–3 DURABLE patterns to the holographic memory store
via fact_store. The fact log builds a week-over-week pattern library
Hermes can recall on subsequent days.

What we want preserved (not just session noise):
  * Which bots performed and which underperformed
  * Regime classification + how it influenced outcomes
  * Anomalies that fired and their resolution (or lack of)
  * Override decisions taken and outcomes
  * Operator interaction patterns (what they asked, what concerned them)

What we do NOT preserve:
  * Raw chat transcripts (memory provider already has those if relevant)
  * Per-trade numerics (already in trade_closes.jsonl)
  * Anomaly raw hits (already in anomaly_watcher.jsonl)

Output: Hermes writes facts directly via the memory_store tool. The
cron just spawns him with a clear prompt and audits the result.

Cron schedule: 02:30 UTC weekdays (10:30 PM ET) — 1 hour after the
daily_debrief, after session close + settlement, when the operator
has likely seen the day's data but isn't actively trading.

Never raises. Always exits 0.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.scripts.hermes_evening_journal")

_WORKSPACE = Path(r"C:\EvolutionaryTradingAlgo")
_VAR_ROOT = _WORKSPACE / "var"
_AUDIT_PATH = _VAR_ROOT / "hermes_evening_journal.jsonl"

_HERMES_EXE = r"C:\Users\Administrator\.hermes\hermes-agent\.venv\Scripts\hermes.exe"
_HERMES_TIMEOUT_S = 180
_HERMES_SESSION = "evening-journal"

_JOURNAL_PROMPT = """\
EVENING JOURNAL (auto-triggered 22:30 ET, post-settlement).

Review today's fleet activity and consolidate 1-3 DURABLE patterns into
memory via the fact_store tool. Be selective — only patterns that will
matter NEXT WEEK or LATER, not transient noise.

Tool chain to use:
  1. jarvis_pnl_summary(window_hours=24) — today's PnL summary
  2. jarvis_pnl_multi_window — compare to 7d + 30d
  3. jarvis_anomaly_recent(since_hours=24) — what fired today
  4. jarvis_prop_firm_status — account headroom changes
  5. jarvis_current_regime — regime classification
  6. jarvis_attribution_cube — which schools earned the PnL
  7. fact_store (memory tool) — to save the patterns

Output 1-3 facts in the format:
  category: <single-word>
  fact: <one-sentence durable pattern, no numbers from today, focus on
        BEHAVIOR that will recur>

Example good facts (preserve these patterns):
  category=regime
  fact=mnq_futures_sage performs best in transition regime; struggles
       in high-vol expansion.
  category=anomaly_pattern
  fact=suspicious_win on mnq bots above 7R usually correlates with
       overnight gap fills — verify with fill audit, not always real edge.

Example BAD facts (don't preserve these):
  - "fleet was up +6R today"  (transient, not durable)
  - "rsi_mr_mnq_v2 lost 6.17R today" (single-day noise)
  - "tested operator's /pause command" (not a trading pattern)

Reply with EXACTLY this JSON envelope:
{
  "facts_saved": [
    {"category": "...", "fact": "..."},
    ...
  ],
  "summary": "one-line summary of today"
}

Be terse. Be durable. Build the pattern library."""


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _append_audit(record: dict[str, Any]) -> None:
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        logger.warning("audit write failed: %s", exc)


def run_journal(*, dry_run: bool = False) -> dict[str, Any]:
    """Spawn Hermes for the evening journal pass. Returns the result envelope."""
    asof = _now_iso()
    hermes = os.environ.get("ETA_HERMES_CLI", _HERMES_EXE).strip()
    if not os.path.exists(hermes):
        record = {
            "asof": asof,
            "ok": False,
            "error": f"hermes exe not found: {hermes}",
        }
        _append_audit(record)
        return record

    if dry_run:
        record = {
            "asof": asof,
            "ok": True,
            "dry_run": True,
            "prompt_preview": _JOURNAL_PROMPT[:300],
            "would_run_cmd": [hermes, "chat", "-q", "<prompt>", "-Q", "--source", "tool"],
        }
        _append_audit(record)
        return record

    cmd = [
        hermes,
        "chat",
        "-q",
        _JOURNAL_PROMPT,
        "-Q",
        "--source",
        "tool",
        "--continue",
        _HERMES_SESSION,
    ]
    if _env_truthy("ETA_HERMES_JOURNAL_ACCEPT_HOOKS"):
        cmd.append("--accept-hooks")
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=_HERMES_TIMEOUT_S,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        record = {"asof": asof, "ok": False, "error": "hermes timeout"}
        _append_audit(record)
        return record
    except Exception as exc:  # noqa: BLE001
        logger.exception("hermes subprocess crashed: %s", exc)
        record = {"asof": asof, "ok": False, "error": str(exc)[:200]}
        _append_audit(record)
        return record

    if proc.returncode != 0:
        record = {
            "asof": asof,
            "ok": False,
            "returncode": proc.returncode,
            "stderr_preview": (proc.stderr or "")[:300],
        }
        _append_audit(record)
        return record

    out = (proc.stdout or "").strip()
    parsed: dict[str, Any] | None = None
    # Try to extract the JSON envelope from Hermes's reply
    try:
        # Find the first '{' and last '}' to handle markdown fencing
        start = out.find("{")
        end = out.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(out[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        parsed = None

    facts_saved = (parsed or {}).get("facts_saved") if isinstance(parsed, dict) else None
    if not isinstance(facts_saved, list):
        facts_saved = []

    record = {
        "asof": asof,
        "ok": True,
        "n_facts_saved": len(facts_saved),
        "facts_saved": facts_saved,
        "summary": (parsed or {}).get("summary") if isinstance(parsed, dict) else None,
        "raw_output_preview": out[:500],
    }
    _append_audit(record)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Hermes evening journal. Once per weekday at 22:30 ET, Hermes "
            "reviews the day's fleet activity and writes 1-3 durable patterns "
            "to memory via fact_store. Builds the week-over-week pattern library."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Audit the prompt without invoking Hermes",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    result = run_journal(dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, default=str, indent=2))
    else:
        if result.get("ok"):
            print(
                f"[hermes_evening_journal] {result.get('asof')} "
                f"facts_saved={result.get('n_facts_saved', 0)} "
                f"summary={(result.get('summary') or '')[:120]}"
            )
        else:
            print(
                f"[hermes_evening_journal] {result.get('asof')} "
                f"FAILED: {result.get('error') or result.get('stderr_preview', '?')}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
