"""
Hermes proactive auto-investigator — cron entrypoint.

Bridges the anomaly_watcher's hit log to Hermes's diagnosis skill. Every
5 minutes:

  1. Scan ``var/anomaly_watcher.jsonl`` for hits that appeared since the
     last cycle (cursor stored in ``var/hermes_proactive_cursor.json``)
  2. For each NEW hit that warrants investigation (warn or critical), call
     ``hermes chat -q <prompt> --source tool --continue auto-investigator``
     where the prompt asks Hermes to use the ``jarvis-anomaly-investigator``
     skill on this specific hit.
  3. Capture Hermes's diagnosis output, format as Telegram message,
     send via ``send_from_env``.

Why a cron instead of a real event subscriber: the anomaly_watcher writes
to a JSONL log, not an event stream Hermes natively subscribes to. The
cron is the simplest reliable bridge — and at 5-minute granularity, the
operator sees diagnoses within minutes of an anomaly firing, faster than
manually typing /investigate.

The cron honors the operator's /silence command (same as anomaly_pulse)
and the dedup window so the same anomaly's diagnosis only fires once.

Run manually for smoke test:
    python -m eta_engine.scripts.hermes_proactive_investigator --dry-run

Cron schedule:
    eta_engine/deploy/hermes_proactive_task.xml (every 5 min)

Never raises — cron exit code is always 0 on a successful pass.
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

logger = logging.getLogger("eta_engine.scripts.hermes_proactive_investigator")

_WORKSPACE = Path(r"C:\EvolutionaryTradingAlgo")
_VAR_ROOT = _WORKSPACE / "var"
_HITS_LOG = _VAR_ROOT / "anomaly_watcher.jsonl"
_CURSOR_PATH = _VAR_ROOT / "hermes_proactive_cursor.json"
_AUDIT_PATH = _VAR_ROOT / "hermes_proactive_audit.jsonl"

_HERMES_EXE = r"C:\Users\Administrator\.hermes\hermes-agent\.venv\Scripts\hermes.exe"
_HERMES_TIMEOUT_S = 120
_HERMES_SESSION = "auto-investigator"

# Severities that auto-fire a diagnosis. info-level (win_streak / fleet_hot_day)
# is positive news, doesn't need investigation. warn + critical do.
_INVESTIGATE_SEVERITIES = frozenset({"warn", "critical"})

# Patterns where we suppress auto-investigation because the operator
# already has dedicated infrastructure handling them (e.g. fleet
# drawdown gets the dedicated drawdown_response skill and a Telegram
# pulse already, plus operator review).
_SKIP_PATTERNS = frozenset({"fleet_drawdown"})


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _append_audit(record: dict[str, Any]) -> None:
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        logger.warning("audit write failed: %s", exc)


def _load_cursor() -> dict[str, Any]:
    return _read_json(_CURSOR_PATH) or {"seen_keys": [], "last_run_at": None}


def _save_cursor(seen_keys: list[str]) -> None:
    # Keep the last 200 keys so the file doesn't grow unbounded
    trimmed = seen_keys[-200:]
    _write_json(_CURSOR_PATH, {"seen_keys": trimmed, "last_run_at": _now_iso()})


def _read_new_hits(seen_keys: set[str]) -> list[dict[str, Any]]:
    """Read anomaly_watcher hits not yet seen by this cron."""
    if not _HITS_LOG.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with _HITS_LOG.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = str(rec.get("key") or "")
                if not key or key in seen_keys:
                    continue
                out.append(rec)
    except OSError as exc:
        logger.warning("hits log read failed: %s", exc)
    return out


def _is_silenced() -> bool:
    try:
        from eta_engine.scripts import telegram_inbound_bot

        return telegram_inbound_bot.is_silenced()
    except Exception:  # noqa: BLE001
        return False


def _ask_hermes_to_investigate(hit: dict[str, Any]) -> str:
    """Spawn hermes chat to run jarvis-anomaly-investigator on one hit."""
    hermes = os.environ.get("ETA_HERMES_CLI", _HERMES_EXE).strip()
    if not os.path.exists(hermes):
        return f"_hermes CLI not found at_ `{hermes}`"

    bot_id = hit.get("bot_id", "?")
    pattern = hit.get("pattern", "?")
    severity = hit.get("severity", "?")
    detail = hit.get("detail", "")
    suggested = hit.get("suggested_skill", "jarvis-anomaly-investigator")

    prompt = (
        f"PROACTIVE INVESTIGATION (auto-triggered by anomaly_watcher cron).\n\n"
        f"Anomaly fired:\n"
        f"  pattern:  {pattern}\n"
        f"  bot_id:   {bot_id}\n"
        f"  severity: {severity}\n"
        f"  detail:   {detail}\n\n"
        f"Activate the {suggested} skill on this hit. Run the diagnosis "
        f"tool chain, classify the cause, and output a Markdown-formatted "
        f"Telegram message following the skill's output format. Keep the "
        f"total reply under 1500 characters. Do NOT call any destructive "
        f"tool (kill_switch, retire_strategy, killall) — recommend only."
    )

    cmd = [
        hermes,
        "chat",
        "-q",
        prompt,
        "-Q",
        "--source",
        "tool",
        "--continue",
        _HERMES_SESSION,
        "-s",
        suggested,  # preload the suggested skill so Hermes uses it
    ]
    if _env_truthy("ETA_HERMES_PROACTIVE_ACCEPT_HOOKS"):
        cmd.append("--accept-hooks")
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv
            cmd,
            capture_output=True,
            text=True,
            timeout=_HERMES_TIMEOUT_S,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"_hermes investigation timed out (>{_HERMES_TIMEOUT_S}s)_"
    except Exception as exc:  # noqa: BLE001
        logger.exception("hermes subprocess crashed: %s", exc)
        return f"_hermes invocation failed_: `{str(exc)[:200]}`"

    if proc.returncode != 0:
        return f"_hermes exit {proc.returncode}_: `{(proc.stderr or '').strip()[:200]}`"
    out = (proc.stdout or "").strip()
    if not out:
        return "_hermes returned empty output_"
    if len(out) > 1800:
        out = out[:1800].rstrip() + "\n\n_...(truncated)_"
    return out


def run_once(*, dry_run: bool = False) -> dict[str, Any]:
    """One pass of the proactive investigator. Returns summary dict."""
    asof = _now_iso()
    cursor = _load_cursor()
    seen_keys = set(cursor.get("seen_keys") or [])

    new_hits = _read_new_hits(seen_keys)
    if not new_hits:
        record = {"asof": asof, "n_new": 0, "n_investigated": 0, "reason": "no_new_hits"}
        _append_audit(record)
        return record

    # Filter to investigate-worthy hits
    candidates = [
        h
        for h in new_hits
        if str(h.get("severity") or "").lower() in _INVESTIGATE_SEVERITIES
        and str(h.get("pattern") or "") not in _SKIP_PATTERNS
    ]

    silenced = _is_silenced()

    # ALWAYS update the cursor with all new hits we observed — even skipped
    # ones — so we don't re-process them on the next tick.
    new_keys = [str(h.get("key") or "") for h in new_hits if h.get("key")]
    if not dry_run:
        _save_cursor(sorted(seen_keys | set(new_keys)))

    if not candidates:
        record = {
            "asof": asof,
            "n_new": len(new_hits),
            "n_investigated": 0,
            "reason": "no_investigate_worthy_hits",
        }
        _append_audit(record)
        return record

    if silenced and not dry_run:
        record = {
            "asof": asof,
            "n_new": len(new_hits),
            "n_candidates": len(candidates),
            "n_investigated": 0,
            "reason": "silenced_by_operator",
        }
        _append_audit(record)
        return record

    # Run investigations sequentially (Hermes is single-threaded under the hood;
    # parallel subprocess spawns can step on each other's --continue session).
    sent_count = 0
    diagnoses: list[dict[str, Any]] = []
    for hit in candidates[:5]:  # cap to 5 per cycle to bound runtime
        diagnosis = _ask_hermes_to_investigate(hit)
        diagnoses.append({"hit_key": hit.get("key"), "diagnosis_preview": diagnosis[:200]})
        if dry_run:
            continue
        # Send to Telegram
        try:
            from eta_engine.deploy.scripts.telegram_alerts import send_from_env

            body = f"🔍 *Auto-investigation: {hit.get('pattern')} / {hit.get('bot_id')}*\n\n" + diagnosis
            send_result = send_from_env(body, priority="WARN")
            if send_result.get("ok"):
                sent_count += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("telegram send failed: %s", exc)

    record = {
        "asof": asof,
        "n_new": len(new_hits),
        "n_candidates": len(candidates),
        "n_investigated": len(diagnoses),
        "n_sent": sent_count,
        "diagnoses": diagnoses,
        "dry_run": dry_run,
    }
    _append_audit(record)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Hermes proactive auto-investigator. Scans the anomaly_watcher hit "
            "log for new entries and auto-runs the jarvis-anomaly-investigator "
            "skill on each, posting diagnoses to Telegram."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run investigations + record diagnoses but skip the Telegram send",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    result = run_once(dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, default=str, indent=2))
    else:
        print(
            f"[hermes_proactive] {result.get('asof')} "
            f"new={result.get('n_new', 0)} "
            f"investigated={result.get('n_investigated', 0)} "
            f"sent={result.get('n_sent', 0)} "
            f"reason={result.get('reason', 'ok')}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
