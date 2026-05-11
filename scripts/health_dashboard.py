"""
EVOLUTIONARY TRADING ALGO  //  scripts.health_dashboard
=======================================================
Single-command unified status across every monitoring surface in
the supercharge stack.  No more grep-ing 6 different JSONL files
by hand.

Pulls from:
  - logs/eta_engine/supercharge_runs.jsonl       (orchestrator)
  - logs/eta_engine/verdict_cache.json           (per-bot last verdict)
  - logs/eta_engine/jarvis_recommendations.jsonl (policy decisions)
  - logs/eta_engine/ibkr_subscription_status.jsonl (sub audit)
  - logs/eta_engine/capture_health.jsonl         (capture monitor)
  - logs/eta_engine/disk_space.jsonl             (disk monitor)
  - logs/eta_engine/capture_rotation.jsonl       (rotation cron)
  - logs/eta_engine/alerts_log.jsonl             (cross-routine alerts)

Renders a single summary table + recent-alerts feed.

Run
---
::

    python -m eta_engine.scripts.health_dashboard

    # JSON output (machine-readable, for the dashboard server):
    python -m eta_engine.scripts.health_dashboard --json

    # Show alerts from the last N hours (default 24):
    python -m eta_engine.scripts.health_dashboard --alert-hours 48
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

LOG_DIR = ROOT.parent / "logs" / "eta_engine"

SOURCES = {
    "supercharge_runs":     LOG_DIR / "supercharge_runs.jsonl",
    "verdict_cache":        LOG_DIR / "verdict_cache.json",
    "jarvis_recs":          LOG_DIR / "jarvis_recommendations.jsonl",
    "ibkr_sub_status":      LOG_DIR / "ibkr_subscription_status.jsonl",
    "capture_health":       LOG_DIR / "capture_health.jsonl",
    "disk_space":           LOG_DIR / "disk_space.jsonl",
    "capture_rotation":     LOG_DIR / "capture_rotation.jsonl",
    "alerts":               LOG_DIR / "alerts_log.jsonl",
}


def _last_jsonl_record(path: Path) -> dict | None:
    """Read the last non-empty line of a JSONL file as JSON."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return None


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _parse_ts(ts: str | float | int | None) -> datetime | None:
    """Parse a timestamp value to a UTC datetime.  Tolerates ISO-8601
    string, unix epoch number, or None.  Returns None on failure.

    Centralized helper extracted from _age_str + _read_recent_alerts
    duplication (D4 fix 2026-05-11)."""
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), UTC)
        except (OSError, OverflowError, ValueError):
            return None
    return None


def _age_str(ts_iso: str | float | int | None) -> str:
    """Render the age of an ISO-string OR unix-epoch timestamp as 's/m/h/d'.

    Tolerates both shapes because older alert writers emit float epoch_s
    in the `ts` field while newer ones emit ISO-8601 in `timestamp_utc`."""
    dt = _parse_ts(ts_iso)
    if dt is None:
        return "?"
    age = datetime.now(UTC) - dt
    total = age.total_seconds()
    if total < 60:
        return f"{int(total)}s"
    if total < 3600:
        return f"{int(total / 60)}m"
    if total < 86400:
        return f"{total / 3600:.1f}h"
    return f"{total / 86400:.1f}d"


def _read_recent_alerts(path: Path, hours: int) -> list[dict]:
    """Read alerts.jsonl and return records from the last N hours.

    Tolerates mixed-shape records via the centralized _parse_ts
    helper — older alert writers used unix-epoch floats in the `ts`
    field; newer ones use ISO-8601 in `timestamp_utc`."""
    if not path.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    dt = _parse_ts(rec.get("timestamp_utc") or rec.get("ts"))
                    if dt is not None and dt >= cutoff:
                        out.append(rec)
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
    except OSError:
        return []
    return out


def _verdict_emoji(level: str) -> str:
    level = str(level).upper()
    return {"GREEN": "[OK]", "PASS": "[OK]", "FRESH": "[OK]", "IDLE": "[OK]",
            "YELLOW": "[??]", "WARN": "[??]", "WARNING": "[??]",
            "INFO": "[OK]",
            "RED": "[!!]", "FAIL": "[!!]", "STALE": "[!!]", "BLOCKED": "[!!]",
            "CRITICAL": "[XX]", "ERROR": "[XX]",
            "MISSING": "[--]", "NEVER_RUN": "[--]"}.get(level, "[?]")


def _row(label: str, status: str, age: str, detail: str) -> str:
    return f"  {_verdict_emoji(status)} {status:<10s}  {label:<22s}  age={age:<6s}  {detail}"


def _alert_level(alert: dict) -> str:
    level = alert.get("level") or alert.get("severity") or "UNKNOWN"
    return str(level).upper()


def _alert_source(alert: dict) -> str:
    source = alert.get("source") or alert.get("event") or alert.get("title") or "unknown"
    return str(source)


def _alert_message(alert: dict) -> str:
    for key in ("message", "body", "reason", "title"):
        value = alert.get(key)
        if value:
            return str(value)
    payload = alert.get("payload")
    if isinstance(payload, dict):
        for key in ("reason", "action", "status"):
            value = payload.get(key)
            if value:
                return str(value)
        verdict = payload.get("verdict")
        if isinstance(verdict, dict):
            action = verdict.get("action")
            reason = verdict.get("reason")
            if action and reason:
                return f"{action}: {reason}"
            if reason:
                return str(reason)
            if action:
                return str(action)
        payload_parts = []
        for key in ("active_bots", "live", "bars", "scope"):
            if key in payload:
                payload_parts.append(f"{key}={payload[key]}")
        if payload_parts:
            return " ".join(payload_parts)
    if alert.get("event"):
        return str(alert["event"])
    return "no alert detail"


def build_dashboard(*, alert_hours: int = 24) -> dict:
    out: dict = {"ts": datetime.now(UTC).isoformat(), "sections": {}}

    # 1. Supercharge orchestrator
    last_run = _last_jsonl_record(SOURCES["supercharge_runs"])
    if last_run:
        p2 = last_run.get("phase2", {})
        p3 = last_run.get("phase3", {})
        out["sections"]["supercharge"] = {
            "status": "GREEN",
            "ts": last_run.get("ts"),
            "tier": last_run.get("tier", "?"),
            "n_bots": p2.get("n_bots", 0),
            "n_verdicts": p2.get("n_verdicts", 0),
            "n_skipped_cached": p2.get("n_skipped_cached", 0),
            "sage_agree": len(p3.get("agreements", [])) if isinstance(p3.get("agreements"), list)
                            else p3.get("n_agreements", 0),
        }
    else:
        out["sections"]["supercharge"] = {"status": "NEVER_RUN", "ts": None}

    # 2. IBKR subscription audit
    sub = _last_jsonl_record(SOURCES["ibkr_sub_status"])
    if sub:
        if sub.get("setup_status") == "BLOCKED":
            sub_status = "BLOCKED"
        else:
            all_depth_ok = sub.get("all_depth_ok")
            sub_status = "PASS" if sub.get("all_realtime") and all_depth_ok is not False else "FAIL"
        out["sections"]["ibkr_subscriptions"] = {
            "status": sub_status,
            "ts": sub.get("ts"),
            "setup_status": sub.get("setup_status"),
            "setup_error_code": sub.get("setup_error_code"),
            "operator_action": sub.get("operator_action"),
            "results": [{"exchange": r.get("exchange", "?"), "verdict": r.get("verdict")}
                        for r in sub.get("results", []) if isinstance(r, dict)],
            "depth_results": [{"exchange": r.get("exchange", "?"), "verdict": r.get("verdict")}
                              for r in sub.get("depth_results", []) if isinstance(r, dict)],
        }
    else:
        out["sections"]["ibkr_subscriptions"] = {"status": "NEVER_RUN", "ts": None}

    # 3. Capture health
    cap = _last_jsonl_record(SOURCES["capture_health"])
    if cap:
        cap_status = cap.get("verdict", "?")
        cap_issues = cap.get("issues", [])
        ibkr_sub = out["sections"].get("ibkr_subscriptions", {})
        blocked_by: str | None = None
        blocked_reason: str | None = None
        if (
            ibkr_sub.get("status") == "BLOCKED"
            and str(cap_status).upper() in {"RED", "FAIL", "STALE"}
        ):
            blocked_by = "ibkr_subscriptions"
            blocked_reason = ibkr_sub.get("operator_action") or ibkr_sub.get("setup_error_code")
            cap_status = "BLOCKED"
        out["sections"]["capture_health"] = {
            "status": cap_status,
            "raw_status": cap.get("verdict", "?"),
            "ts": cap.get("ts"),
            "n_symbols": cap.get("n_symbols", 0),
            "issues": cap_issues,
            "blocked_by": blocked_by,
            "blocked_reason": blocked_reason,
        }
    else:
        out["sections"]["capture_health"] = {"status": "NEVER_RUN", "ts": None}

    # 4. Disk space
    disk = _last_jsonl_record(SOURCES["disk_space"])
    if disk:
        worst = disk.get("verdict", "?")
        check_summary = []
        for c in disk.get("checks", []):
            check_summary.append(f"{c.get('label', '?')}={c.get('free_gb', '?')}GB")
        out["sections"]["disk_space"] = {
            "status": worst,
            "ts": disk.get("ts"),
            "summary": " | ".join(check_summary),
        }
    else:
        out["sections"]["disk_space"] = {"status": "NEVER_RUN", "ts": None}

    # 5. Rotation
    rot = _last_jsonl_record(SOURCES["capture_rotation"])
    if rot:
        # D3: DRY-RUN with pending work is a YELLOW alert — operator
        # forgot to schedule the --apply variant; disk will fill silently.
        # GREEN only when --apply ran successfully.  DRY-RUN with zero
        # pending work is harmless (no rotation needed yet).
        applied = bool(rot.get("apply"))
        ticks = rot.get("ticks", {})
        depth = rot.get("depth", {})
        # In APPLY mode, n_compressed/n_cold_archived count completed
        # work.  In DRY-RUN mode, the new n_would_compress /
        # n_would_cold_archived fields count pending work (was missing
        # entirely in v1 of this digest).
        pending = (ticks.get("n_would_compress", 0)
                    + ticks.get("n_would_cold_archived", 0)
                    + depth.get("n_would_compress", 0)
                    + depth.get("n_would_cold_archived", 0))
        notes = []
        for label, rec in (("ticks", ticks), ("depth", depth)):
            note = rec.get("note") if isinstance(rec, dict) else None
            if note:
                notes.append(f"{label}: {note}")
        if applied:
            status = "GREEN"
        elif pending > 0:
            status = "YELLOW"
        else:
            status = "DRY-RUN"
        out["sections"]["capture_rotation"] = {
            "status": status,
            "ts": rot.get("ts"),
            "applied": applied,
            "n_compressed": rot.get("totals", {}).get("n_compressed", 0),
            "n_cold_archived": rot.get("totals", {}).get("n_cold_archived", 0),
            "n_pending": pending if not applied else 0,
            "notes": notes,
        }
    else:
        out["sections"]["capture_rotation"] = {"status": "NEVER_RUN", "ts": None}

    # 6. Verdict cache (per-bot)
    cache = _load_json(SOURCES["verdict_cache"])
    if cache:
        n_green = sum(1 for v in cache.values() if isinstance(v, dict) and v.get("verdict") == "GREEN")
        n_yellow = sum(1 for v in cache.values() if isinstance(v, dict) and v.get("verdict") == "YELLOW")
        n_red = sum(1 for v in cache.values() if isinstance(v, dict) and v.get("verdict") == "RED")
        out["sections"]["fleet_verdicts"] = {
            "status": "GREEN" if n_red == 0 else ("YELLOW" if n_red < 3 else "RED"),
            "n_total": len(cache),
            "n_green": n_green,
            "n_yellow": n_yellow,
            "n_red": n_red,
        }
    else:
        out["sections"]["fleet_verdicts"] = {"status": "NEVER_RUN", "n_total": 0}

    # 7. Jarvis recommendations (last 5)
    if SOURCES["jarvis_recs"].exists():
        try:
            with SOURCES["jarvis_recs"].open("r", encoding="utf-8") as f:
                lines = [ln for ln in f if ln.strip()][-5:]
            recs = [json.loads(ln) for ln in lines]
            out["sections"]["jarvis_recent"] = {
                "status": "GREEN", "n_recent": len(recs),
                "recent": [{"bot": r.get("bot_id"),
                            "size_cap": r.get("size_cap_mult"),
                            "ts": r.get("ts")}
                           for r in recs],
            }
        except (OSError, json.JSONDecodeError):
            out["sections"]["jarvis_recent"] = {"status": "PARSE_ERROR"}
    else:
        phase4 = last_run.get("phase4", {}) if isinstance(last_run, dict) else {}
        phase3 = last_run.get("phase3", {}) if isinstance(last_run, dict) else {}
        if isinstance(phase4, dict) and "n_arbitrated" in phase4:
            agreements = phase3.get("n_agreements")
            if agreements is None and isinstance(phase3.get("agreements"), list):
                agreements = len(phase3.get("agreements", []))
            n_agreements = int(agreements or 0)
            if n_agreements == 0:
                status = "IDLE"
                reason = "no sage-approved GREEN bots to arbitrate"
            else:
                status = "YELLOW"
                reason = "sage agreements exist but no Jarvis recommendations were logged"
            out["sections"]["jarvis_recent"] = {
                "status": status,
                "ts": last_run.get("ts"),
                "n_recent": 0,
                "n_arbitrated": phase4.get("n_arbitrated", 0),
                "n_sage_agreements": n_agreements,
                "reason": reason,
            }
        else:
            out["sections"]["jarvis_recent"] = {"status": "NEVER_RUN", "n_recent": 0}

    # 8. Recent alerts
    alerts = _read_recent_alerts(SOURCES["alerts"], alert_hours)
    out["recent_alerts"] = alerts
    out["recent_alerts_count"] = len(alerts)

    # Overall verdict (worst across sections)
    rank = {"GREEN": 0, "PASS": 0, "FRESH": 0, "DRY-RUN": 0, "IDLE": 0,
            "YELLOW": 1, "WARN": 1,
            "RED": 2, "FAIL": 2, "STALE": 2, "BLOCKED": 2,
            "CRITICAL": 3, "ERROR": 3,
            "MISSING": 1, "NEVER_RUN": 1, "PARSE_ERROR": 2}
    worst_rank = -1
    worst_label = "GREEN"
    for sec in out["sections"].values():
        st = sec.get("status", "?")
        r = rank.get(st, -1)
        if r > worst_rank:
            worst_rank = r
            worst_label = st
    out["overall"] = worst_label
    return out


def render_text(d: dict, *, alert_hours: int = 24) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append(f"================ ETA HEALTH DASHBOARD ({d['ts']}) ================")
    lines.append(f"  OVERALL: {_verdict_emoji(d['overall'])} {d['overall']}")
    lines.append("")
    s = d["sections"]
    lines.append(_row("supercharge orchestrator",
                       s["supercharge"].get("status", "?"),
                       _age_str(s["supercharge"].get("ts")),
                       f"tier={s['supercharge'].get('tier', '?')} "
                       f"verdicts={s['supercharge'].get('n_verdicts', 0)}/{s['supercharge'].get('n_bots', 0)} "
                       f"sage_agree={s['supercharge'].get('sage_agree', 0)}"))
    ibkr_sub = s["ibkr_subscriptions"]
    ibkr_detail = ibkr_sub.get("operator_action")
    if not ibkr_detail:
        tick_detail = ", ".join(f"{r['exchange']}:{r.get('verdict', '?')}"
                                for r in ibkr_sub.get("results", []))
        depth_detail = ", ".join(f"{r['exchange']} depth:{r.get('verdict', '?')}"
                                 for r in ibkr_sub.get("depth_results", []))
        ibkr_detail = " | ".join(part for part in (tick_detail, depth_detail) if part) or "n/a"
    lines.append(_row("ibkr subscriptions",
                       ibkr_sub.get("status", "?"),
                       _age_str(ibkr_sub.get("ts")),
                       ibkr_detail))
    cap_health = s["capture_health"]
    cap_detail = (
        f"{cap_health.get('n_symbols', 0)} symbols, "
        f"{len(cap_health.get('issues', []))} issues"
    )
    if cap_health.get("blocked_by"):
        cap_detail = (
            f"blocked by {cap_health.get('blocked_by')}; "
            f"{len(cap_health.get('issues', []))} downstream issue(s); "
            f"{cap_health.get('blocked_reason') or 'clear upstream readiness first'}"
        )
    lines.append(_row("capture health",
                       cap_health.get("status", "?"),
                       _age_str(cap_health.get("ts")),
                       cap_detail))
    lines.append(_row("disk space",
                       s["disk_space"].get("status", "?"),
                       _age_str(s["disk_space"].get("ts")),
                       s["disk_space"].get("summary", "n/a")))
    rotation = s["capture_rotation"]
    rotation_detail = (
        f"compressed={rotation.get('n_compressed', 0)} "
        f"cold={rotation.get('n_cold_archived', 0)}"
    )
    if rotation.get("n_pending", 0):
        rotation_detail += f" pending={rotation.get('n_pending', 0)}"
    if rotation.get("notes"):
        rotation_detail += "; " + "; ".join(rotation.get("notes", []))
    lines.append(_row("capture rotation",
                       rotation.get("status", "?"),
                       _age_str(rotation.get("ts")),
                       rotation_detail))
    fv = s["fleet_verdicts"]
    lines.append(_row("fleet verdicts",
                       fv.get("status", "?"),
                       "—",
                       f"GREEN={fv.get('n_green', 0)} YELLOW={fv.get('n_yellow', 0)} "
                       f"RED={fv.get('n_red', 0)} (total {fv.get('n_total', 0)})"))
    jr = s["jarvis_recent"]
    jarvis_detail = f"recent={jr.get('n_recent', 0)}"
    if jr.get("reason"):
        jarvis_detail += f"; {jr.get('reason')}"
    lines.append(_row("jarvis arbitration",
                       jr.get("status", "?"),
                       _age_str(jr.get("ts")),
                       jarvis_detail))
    lines.append("")
    lines.append(f"-- recent alerts (last {alert_hours}h, {d['recent_alerts_count']} total) --")
    if not d["recent_alerts"]:
        lines.append("  (none)")
    else:
        for a in d["recent_alerts"][-10:]:
            lvl = _alert_level(a)
            src = _alert_source(a)[:26]
            msg = _alert_message(a)[:60]
            ts_age = _age_str(a.get("timestamp_utc") or a.get("ts"))
            lines.append(f"  {_verdict_emoji(lvl)} {lvl:<8s} [{src:<26s}] {ts_age:<6s} {msg}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    ap.add_argument("--alert-hours", type=int, default=24,
                    help="Show alerts from the last N hours (default 24)")
    args = ap.parse_args()

    d = build_dashboard(alert_hours=args.alert_hours)
    if args.json:
        print(json.dumps(d, indent=2))
    else:
        print(render_text(d, alert_hours=args.alert_hours))

    # Exit-code mapping for cron / shell:
    return {"GREEN": 0, "PASS": 0, "FRESH": 0, "DRY-RUN": 0, "IDLE": 0,
            "YELLOW": 1, "WARN": 1, "MISSING": 1, "NEVER_RUN": 1,
            "RED": 2, "FAIL": 2, "STALE": 2, "BLOCKED": 2, "PARSE_ERROR": 2,
            "CRITICAL": 3, "ERROR": 3}.get(d["overall"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
