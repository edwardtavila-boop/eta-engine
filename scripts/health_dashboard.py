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


def _age_str(ts_iso: str | float | int | None) -> str:
    """Render the age of an ISO-string OR unix-epoch timestamp as 's/m/h/d'.

    Tolerates both shapes because older alert writers emit float epoch_s
    in the `ts` field while newer ones emit ISO-8601 in `timestamp_utc`."""
    if ts_iso is None:
        return "?"
    dt: datetime | None = None
    if isinstance(ts_iso, str):
        try:
            dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        except ValueError:
            return "?"
    elif isinstance(ts_iso, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(ts_iso), UTC)
        except (OSError, OverflowError, ValueError):
            return "?"
    else:
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

    Tolerates mixed-shape records — older alert writers used unix-epoch
    floats in the `ts` field; newer ones use ISO-8601 in `timestamp_utc`.
    Both decode cleanly here."""
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
                    ts = rec.get("timestamp_utc") or rec.get("ts")
                    if ts is None:
                        continue
                    dt: datetime | None = None
                    if isinstance(ts, str):
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        except ValueError:
                            dt = None
                    elif isinstance(ts, (int, float)):
                        try:
                            dt = datetime.fromtimestamp(float(ts), UTC)
                        except (OSError, OverflowError, ValueError):
                            dt = None
                    if dt is not None and dt >= cutoff:
                        out.append(rec)
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
    except OSError:
        return []
    return out


def _verdict_emoji(level: str) -> str:
    return {"GREEN": "[OK]", "PASS": "[OK]", "FRESH": "[OK]",
            "YELLOW": "[??]", "WARN": "[??]",
            "RED": "[!!]", "FAIL": "[!!]", "STALE": "[!!]",
            "CRITICAL": "[XX]", "ERROR": "[XX]",
            "MISSING": "[--]", "NEVER_RUN": "[--]"}.get(level, "[?]")


def _row(label: str, status: str, age: str, detail: str) -> str:
    return f"  {_verdict_emoji(status)} {status:<10s}  {label:<22s}  age={age:<6s}  {detail}"


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
        out["sections"]["ibkr_subscriptions"] = {
            "status": "PASS" if sub.get("all_realtime") else "FAIL",
            "ts": sub.get("ts"),
            "results": [{"exchange": r["exchange"], "verdict": r.get("verdict")}
                        for r in sub.get("results", [])],
        }
    else:
        out["sections"]["ibkr_subscriptions"] = {"status": "NEVER_RUN", "ts": None}

    # 3. Capture health
    cap = _last_jsonl_record(SOURCES["capture_health"])
    if cap:
        out["sections"]["capture_health"] = {
            "status": cap.get("verdict", "?"),
            "ts": cap.get("ts"),
            "n_symbols": cap.get("n_symbols", 0),
            "issues": cap.get("issues", []),
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
        out["sections"]["capture_rotation"] = {
            "status": "GREEN" if rot.get("apply") else "DRY-RUN",
            "ts": rot.get("ts"),
            "n_compressed": rot.get("totals", {}).get("n_compressed", 0),
            "n_cold_archived": rot.get("totals", {}).get("n_cold_archived", 0),
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
        out["sections"]["jarvis_recent"] = {"status": "NEVER_RUN", "n_recent": 0}

    # 8. Recent alerts
    alerts = _read_recent_alerts(SOURCES["alerts"], alert_hours)
    out["recent_alerts"] = alerts
    out["recent_alerts_count"] = len(alerts)

    # Overall verdict (worst across sections)
    rank = {"GREEN": 0, "PASS": 0, "FRESH": 0, "DRY-RUN": 0,
            "YELLOW": 1, "WARN": 1,
            "RED": 2, "FAIL": 2, "STALE": 2,
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
    lines.append(_row("ibkr subscriptions",
                       s["ibkr_subscriptions"].get("status", "?"),
                       _age_str(s["ibkr_subscriptions"].get("ts")),
                       ", ".join(f"{r['exchange']}:{r.get('verdict', '?')}"
                                 for r in s["ibkr_subscriptions"].get("results", [])) or "n/a"))
    lines.append(_row("capture health",
                       s["capture_health"].get("status", "?"),
                       _age_str(s["capture_health"].get("ts")),
                       f"{s['capture_health'].get('n_symbols', 0)} symbols, "
                       f"{len(s['capture_health'].get('issues', []))} issues"))
    lines.append(_row("disk space",
                       s["disk_space"].get("status", "?"),
                       _age_str(s["disk_space"].get("ts")),
                       s["disk_space"].get("summary", "n/a")))
    lines.append(_row("capture rotation",
                       s["capture_rotation"].get("status", "?"),
                       _age_str(s["capture_rotation"].get("ts")),
                       f"compressed={s['capture_rotation'].get('n_compressed', 0)} "
                       f"cold={s['capture_rotation'].get('n_cold_archived', 0)}"))
    fv = s["fleet_verdicts"]
    lines.append(_row("fleet verdicts",
                       fv.get("status", "?"),
                       "—",
                       f"GREEN={fv.get('n_green', 0)} YELLOW={fv.get('n_yellow', 0)} "
                       f"RED={fv.get('n_red', 0)} (total {fv.get('n_total', 0)})"))
    jr = s["jarvis_recent"]
    lines.append(_row("jarvis arbitration",
                       jr.get("status", "?"),
                       "—",
                       f"recent={jr.get('n_recent', 0)}"))
    lines.append("")
    lines.append(f"-- recent alerts (last {alert_hours}h, {d['recent_alerts_count']} total) --")
    if not d["recent_alerts"]:
        lines.append("  (none)")
    else:
        for a in d["recent_alerts"][-10:]:
            lvl = a.get("level", "?")
            src = a.get("source", "?")
            msg = a.get("message", "")[:60]
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
    return {"GREEN": 0, "PASS": 0, "FRESH": 0, "DRY-RUN": 0,
            "YELLOW": 1, "WARN": 1, "MISSING": 1, "NEVER_RUN": 1,
            "RED": 2, "FAIL": 2, "STALE": 2, "PARSE_ERROR": 2,
            "CRITICAL": 3, "ERROR": 3}.get(d["overall"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
