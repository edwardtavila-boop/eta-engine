"""Fleet status dashboard - single command, full operator view.

Combines every visibility surface we built into one terminal screen:

  - Supervisor health (PID, uptime, mode, feed)
  - Open positions (broker truth + supervisor claim, divergence flagged)
  - Today's realized P&L vs killswitch limit
  - TWS gateway status (from watchdog)
  - Cutover readiness (from auto_cutover_watcher)
  - Top performers + bottom performers (from scoreboard)
  - Recent v3 events (last N WARN/CRITICAL)
  - Feed health snapshot (from heartbeat)

Usage:
    python -m eta_engine.scripts.fleet_status
    python -m eta_engine.scripts.fleet_status --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402

_HEARTBEAT = workspace_roots.ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH
_RECONCILE = workspace_roots.ETA_JARVIS_SUPERVISOR_RECONCILE_PATH
_TWS_STATUS = workspace_roots.ETA_RUNTIME_STATE_DIR / "tws_watchdog.json"
_CUTOVER_STATUS = workspace_roots.ETA_RUNTIME_STATE_DIR / "cutover_status.json"
_V3_EVENTS = workspace_roots.ETA_RUNTIME_STATE_DIR / "jarvis_v3_events.jsonl"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _section_supervisor(hb: dict) -> dict:
    if not hb:
        return {"status": "no heartbeat - supervisor not running?"}
    age_s = None
    try:
        ts = datetime.fromisoformat(hb["ts"].replace("Z", "+00:00"))
        age_s = (datetime.now(UTC) - ts.astimezone(UTC)).total_seconds()
    except (KeyError, ValueError, TypeError):
        pass
    return {
        "ts": hb.get("ts"),
        "age_s": round(age_s, 1) if age_s is not None else None,
        "mode": hb.get("mode"),
        "feed": hb.get("feed"),
        "n_bots": hb.get("n_bots"),
        "tick_count": hb.get("tick_count"),
        "live_money_enabled": hb.get("live_money_enabled"),
        "stale": (age_s is not None and age_s > 180),
    }


def _section_positions(hb: dict, reconcile: dict) -> dict:
    open_positions = []
    for bot in hb.get("bots", []):
        p = bot.get("open_position")
        if p:
            open_positions.append(
                {
                    "bot_id": bot.get("bot_id"),
                    "symbol": bot.get("symbol"),
                    "side": p.get("side"),
                    "qty": p.get("qty"),
                    "entry": p.get("entry_price"),
                    "broker_bracket": bool(p.get("broker_bracket")),
                }
            )
    return {
        "open_count": len(open_positions),
        "open_positions": open_positions,
        "reconcile_checked_at": reconcile.get("checked_at"),
        "broker_only": reconcile.get("broker_only", []),
        "supervisor_only": reconcile.get("supervisor_only", []),
        "divergent": reconcile.get("divergent", []),
    }


def _section_killswitch() -> dict:
    try:
        from eta_engine.scripts.daily_loss_killswitch import killswitch_status

        return killswitch_status()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _section_tws(tws: dict) -> dict:
    if not tws:
        return {"status": "no watchdog data"}
    return {
        "healthy": tws.get("healthy"),
        "checked_at": tws.get("checked_at"),
        "consecutive_failures": tws.get("consecutive_failures"),
        "last_healthy_at": tws.get("last_healthy_at"),
    }


def _section_cutover(cut: dict) -> dict:
    if not cut:
        return {"status": "no cutover data"}
    return {
        "checked_at": cut.get("checked_at"),
        "perms_active": cut.get("perms_active"),
        "env_active": cut.get("env_active"),
        "auto_opted_in": cut.get("auto_opted_in"),
        "action": cut.get("action"),
    }


def _section_scoreboard(hb: dict) -> dict:
    """Top + bottom 3 bots by realized_pnl."""
    try:
        from eta_engine.scripts.bot_scoreboard import _bot_metrics, _load_closes

        closes = _load_closes()
        rows = [_bot_metrics(b, closes) for b in hb.get("bots", [])]
        rows = [r for r in rows if r["closes"] > 0]
        rows.sort(key=lambda r: r["realized_pnl"], reverse=True)
        top = rows[:3]
        bottom = rows[-3:] if len(rows) > 3 else []
        total_pnl = sum(r["realized_pnl"] for r in rows)
        total_closes = sum(r["closes"] for r in rows)
        return {
            "n_bots_with_closes": len(rows),
            "total_realized_pnl_usd": round(total_pnl, 2),
            "total_closes": total_closes,
            "top": [{k: r[k] for k in ("bot_id", "realized_pnl", "win_rate", "closes")} for r in top],
            "bottom": [{k: r[k] for k in ("bot_id", "realized_pnl", "win_rate", "closes")} for r in bottom],
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _section_v3_events(n: int = 5) -> dict:
    if not _V3_EVENTS.exists():
        return {"recent": [], "warn_count_24h": 0, "critical_count_24h": 0}
    try:
        with _V3_EVENTS.open(encoding="utf-8") as fh:
            tail = fh.readlines()[-200:]
    except OSError:
        return {"recent": [], "error": "read failed"}
    recent: list[dict] = []
    warn_24h = 0
    critical_24h = 0
    cutoff_ts = datetime.now(UTC).timestamp() - 86400
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        sev = rec.get("severity", "INFO")
        try:
            rec_ts = datetime.fromisoformat(
                rec.get("ts", "").replace("Z", "+00:00"),
            ).timestamp()
        except (ValueError, TypeError):
            rec_ts = 0
        if sev in ("WARN", "WARNING"):
            if rec_ts >= cutoff_ts:
                warn_24h += 1
            recent.append(rec)
        elif sev in ("CRITICAL", "ERROR"):
            if rec_ts >= cutoff_ts:
                critical_24h += 1
            recent.append(rec)
    return {
        "warn_count_24h": warn_24h,
        "critical_count_24h": critical_24h,
        "recent": [
            {
                "ts": r.get("ts"),
                "layer": r.get("layer"),
                "event": r.get("event"),
                "bot_id": r.get("bot_id"),
                "severity": r.get("severity"),
            }
            for r in recent[-n:]
        ],
    }


def _section_feed_health(hb: dict) -> dict:
    fh = hb.get("feed_health") or {}
    summary = []
    for key, counts in fh.items():
        ok = counts.get("ok", 0)
        empty = counts.get("empty", 0)
        total = ok + empty
        rate = empty / total if total else 0.0
        summary.append(
            {
                "key": key,
                "ok": ok,
                "empty": empty,
                "empty_rate": round(rate, 4),
            }
        )
    return {"feeds": summary}


def gather() -> dict:
    hb = _load_json(_HEARTBEAT)
    reconcile = _load_json(_RECONCILE)
    tws = _load_json(_TWS_STATUS)
    cutover = _load_json(_CUTOVER_STATUS)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "supervisor": _section_supervisor(hb),
        "positions": _section_positions(hb, reconcile),
        "killswitch": _section_killswitch(),
        "tws": _section_tws(tws),
        "cutover": _section_cutover(cutover),
        "scoreboard": _section_scoreboard(hb),
        "v3_events": _section_v3_events(),
        "feed_health": _section_feed_health(hb),
    }


def _print_text(snap: dict) -> None:
    sup = snap["supervisor"]
    pos = snap["positions"]
    ks = snap["killswitch"]
    tws = snap["tws"]
    cut = snap["cutover"]
    sb = snap["scoreboard"]
    ev = snap["v3_events"]
    fh = snap["feed_health"]

    print("=" * 78)
    print(f" FLEET STATUS - {snap['generated_at']}")
    print("=" * 78)

    # Supervisor
    print("\n* SUPERVISOR")
    if sup.get("status"):
        print(f"  ! {sup['status']}")
    else:
        stale = " (STALE)" if sup.get("stale") else ""
        print(
            f"  mode={sup['mode']} feed={sup['feed']} bots={sup['n_bots']} "
            f"tick={sup['tick_count']} hb_age={sup['age_s']}s{stale}"
        )

    # Positions
    print(f"\n* POSITIONS - {pos['open_count']} open")
    for p in pos["open_positions"][:8]:
        bb = "Ybracket" if p["broker_bracket"] else "naked"
        print(f"  {p['bot_id']:<28} {p['symbol']:<6} {p['side']} {p['qty']:.4f} @ {p['entry']:.2f}  {bb}")
    if pos["broker_only"] or pos["supervisor_only"] or pos["divergent"]:
        print("  ! reconcile divergence:")
        if pos["broker_only"]:
            print(f"    broker_only: {pos['broker_only']}")
        if pos["supervisor_only"]:
            print(f"    supervisor_only: {pos['supervisor_only']}")
        if pos["divergent"]:
            print(f"    divergent: {pos['divergent']}")

    # Killswitch
    print("\n* DAILY KILL SWITCH")
    if ks.get("error"):
        print(f"  error: {ks['error']}")
    else:
        trip = "[!!] TRIPPED" if ks.get("tripped") else "[OK] armed"
        print(
            f"  {trip}  day_pnl=${ks.get('today_pnl_usd', 0):+.2f}  "
            f"limit=${ks.get('limit_usd', 0):+.2f}  date={ks.get('date')}"
        )

    # TWS
    print("\n* TWS GATEWAY")
    if tws.get("status"):
        print(f"  {tws['status']}")
    else:
        marker = "[OK]" if tws.get("healthy") else "[!!]"
        print(
            f"  {marker} healthy={tws.get('healthy')} "
            f"failures={tws.get('consecutive_failures')} "
            f"last_healthy={tws.get('last_healthy_at')}"
        )

    # Cutover
    print("\n* LIVE-CRYPTO CUTOVER READINESS")
    if cut.get("status"):
        print(f"  {cut['status']}")
    else:
        print(
            f"  perms_active={cut.get('perms_active')} "
            f"env_active={cut.get('env_active')} "
            f"auto_opted_in={cut.get('auto_opted_in')} "
            f"action={cut.get('action')}"
        )

    # Scoreboard
    print("\n* PERFORMERS")
    if sb.get("error"):
        print(f"  error: {sb['error']}")
    else:
        print(
            f"  fleet pnl=${sb['total_realized_pnl_usd']:+.2f} "
            f"closes={sb['total_closes']} bots_w_closes={sb['n_bots_with_closes']}"
        )
        if sb.get("top"):
            print("  TOP:")
            for r in sb["top"]:
                print(f"    {r['bot_id']:<28} pnl=${r['realized_pnl']:>+9.2f} wr={r['win_rate']:.1%} cls={r['closes']}")
        if sb.get("bottom"):
            print("  BOTTOM:")
            for r in sb["bottom"]:
                print(f"    {r['bot_id']:<28} pnl=${r['realized_pnl']:>+9.2f} wr={r['win_rate']:.1%} cls={r['closes']}")

    # v3 events
    print(f"\n* V3 EVENTS - {ev['warn_count_24h']} WARN / {ev['critical_count_24h']} CRITICAL in last 24h")
    for r in ev["recent"][-5:]:
        print(f"  {r['ts'][:19]} {r['severity']:<8} {r['layer']}/{r['event']} {r.get('bot_id', '')}")

    # Feed health
    print("\n* FEED HEALTH")
    for f in fh["feeds"]:
        marker = "[OK]" if f["empty_rate"] < 0.10 else ("[--]" if f["empty_rate"] < 0.30 else "[!!]")
        print(f"  {marker} {f['key']:<24} ok={f['ok']:>4} empty={f['empty']:>3} rate={f['empty_rate']:.2%}")

    print("\n" + "=" * 78)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true", help="Emit JSON")
    args = p.parse_args(argv)
    snap = gather()
    if args.json:
        print(json.dumps(snap, indent=2, default=str))
    else:
        _print_text(snap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
