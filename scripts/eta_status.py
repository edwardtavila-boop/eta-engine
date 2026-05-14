"""ETA status — one-shot operator dashboard.

Consolidates every key signal from across the fleet into a single
operator-readable surface:

  * Supervisor heartbeat (n_bots, tick, mode, last write)
  * FM cache stats (hits, misses, hit rate)
  * FM daily-spend breaker (spent today, cap, headroom)
  * Diamond leaderboard (n_diamonds, PROP_READY designations)
  * Launch readiness verdict (R1-R7 gate states)
  * Latest 1000-bootstrap kaizen pass (bootstraps, applied count)
  * Quantum 6h rebalance (last run + hedge picks)
  * Recent eta_events (last 10 entries from the alert dispatcher)

Designed for `python -m eta_engine.scripts.eta_status` (text) or
`--json` for piping to dashboards.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eta_engine.scripts.supervisor_heartbeat_check import build_supervisor_heartbeat_report

# wave-25q post-review: derive STATE_DIR from __file__ rather than
# hardcoding an absolute Windows path. The previous hardcode broke on
# the VPS (different drive root in some deploys) and on dev machines
# where the workspace lives under a non-C: path. ETA_STATE_DIR env-var
# override is the canonical "I know better" escape hatch.
import os  # noqa: E402,PLC0415,I001
_STATE_DIR_ENV = os.environ.get("ETA_STATE_DIR")
if _STATE_DIR_ENV:
    STATE_DIR = Path(_STATE_DIR_ENV)
else:
    _SCRIPT_ROOT = Path(__file__).resolve()
    # scripts/<file> -> eta_engine -> workspace_root
    _WORKSPACE_ROOT = _SCRIPT_ROOT.parents[2]
    STATE_DIR = _WORKSPACE_ROOT / "var" / "eta_engine" / "state"
HEARTBEAT_PATH = STATE_DIR / "jarvis_intel" / "supervisor" / "heartbeat.json"
LEADERBOARD = STATE_DIR / "diamond_leaderboard_latest.json"
LAUNCH_READINESS = STATE_DIR / "diamond_prop_launch_readiness_latest.json"
KAIZEN_LATEST = STATE_DIR / "kaizen_latest.json"
EVENTS_LOG = STATE_DIR / "eta_events.jsonl"
QUANTUM_DIR = STATE_DIR / "quantum"
LAUNCH_READINESS_MAX_AGE_SECONDS = 30 * 60


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _tail_jsonl(path: Path, n: int) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw in lines[-n:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def _latest_quantum_rebalance() -> dict[str, Any]:
    if not QUANTUM_DIR.is_dir():
        return {}
    candidates = sorted(QUANTUM_DIR.glob("daily_rebalance_*.json"))
    if not candidates:
        return {}
    return _load_json(candidates[-1])


def _parse_iso_ts(ts: object) -> datetime | None:
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _age_seconds(ts: object) -> float | None:
    dt = _parse_iso_ts(ts)
    if dt is None:
        return None
    return round((datetime.now(UTC) - dt).total_seconds(), 1)


def _state_root_for_heartbeat(path: Path) -> Path:
    parts = path.parts
    if len(parts) >= 4 and parts[-3:] == ("jarvis_intel", "supervisor", "heartbeat.json"):
        return path.parents[2]
    return path.parent


def _supervisor_health_summary() -> dict[str, Any]:
    try:
        report = build_supervisor_heartbeat_report(state_root=_state_root_for_heartbeat(HEARTBEAT_PATH))
    except Exception:
        return {}
    return {
        "healthy": bool(report.get("healthy")),
        "status": report.get("status"),
        "diagnosis": report.get("diagnosis"),
        "canonical_age_seconds": report.get("canonical_age_seconds"),
        "action_items": report.get("action_items") or [],
    }


def _gate_details(gates: list[Any], status: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for gate in gates:
        if not isinstance(gate, dict):
            continue
        if status is not None and gate.get("status") != status:
            continue
        detail = gate.get("detail")
        out.append(
            {
                "name": gate.get("name"),
                "status": gate.get("status"),
                "rationale": gate.get("rationale"),
                "detail": detail if isinstance(detail, dict) else {},
            },
        )
    return out


def gather() -> dict[str, Any]:
    hb = _load_json(HEARTBEAT_PATH)
    supervisor_health = _supervisor_health_summary()
    lb = _load_json(LEADERBOARD)
    lr = _load_json(LAUNCH_READINESS)
    kz = _load_json(KAIZEN_LATEST)
    qm = _latest_quantum_rebalance()

    fm_cache = hb.get("fm_cache") or {}
    hits = int(fm_cache.get("hits", 0) or 0)
    misses = int(fm_cache.get("misses", 0) or 0)
    total = hits + misses
    hit_rate_pct = round(hits / total * 100, 1) if total else 0.0

    fm_breaker = hb.get("fm_breaker") or {}
    spent = float(fm_breaker.get("spent_today_usd", 0.0) or 0.0)
    cap = float(fm_breaker.get("cap_usd", 0.0) or 0.0)
    headroom_pct = round((1 - spent / cap) * 100, 1) if cap > 0 else 0.0

    qm_results = qm.get("results") or []
    qm_with_signals = [r for r in qm_results if r.get("selected_bots")]
    qm_with_hedges = [r for r in qm_with_signals if (r.get("hedge_recommendation") or {}).get("hedges_selected")]
    lr_gates = lr.get("gates") or []
    lr_age = _age_seconds(lr.get("ts"))
    lr_stale = lr_age is None or lr_age > LAUNCH_READINESS_MAX_AGE_SECONDS

    return {
        "ts": datetime.now(UTC).isoformat(),
        "supervisor": {
            "ts": hb.get("ts"),
            "tick_count": hb.get("tick_count"),
            "n_bots": hb.get("n_bots"),
            "mode": hb.get("mode"),
            "health": supervisor_health,
        },
        "fm_cache": {
            "size": fm_cache.get("size"),
            "hits": hits,
            "misses": misses,
            "hit_rate_pct": hit_rate_pct,
            "ttl_seconds": fm_cache.get("ttl_seconds"),
        },
        "fm_breaker": {
            "spent_today_usd": spent,
            "cap_usd": cap,
            "headroom_pct": headroom_pct,
            "tripped": bool(fm_breaker.get("tripped", False)),
        },
        "diamond_leaderboard": {
            "n_diamonds": lb.get("n_diamonds"),
            "n_prop_ready": lb.get("n_prop_ready"),
            "prop_ready_bots": lb.get("prop_ready_bots"),
        },
        "launch_readiness": {
            "ts": lr.get("ts"),
            "age_seconds": lr_age,
            "stale": lr_stale,
            "max_age_seconds": LAUNCH_READINESS_MAX_AGE_SECONDS,
            "verdict": lr.get("overall_verdict"),
            "summary": lr.get("summary"),
            "gates": _gate_details(lr_gates),
            "failing_gates": [g["name"] for g in lr_gates if isinstance(g, dict) and g.get("status") == "NO_GO"],
            "warning_gates": [
                g["name"]
                for g in lr_gates
                if isinstance(g, dict) and g.get("status") not in ("GO", "NO_GO")
            ],
            "failing_gate_details": _gate_details(lr_gates, "NO_GO"),
            "warning_gate_details": [
                g
                for g in _gate_details(lr_gates)
                if g.get("status") not in ("GO", "NO_GO")
            ],
            "launch_date": lr.get("launch_date"),
            "days_until_launch": lr.get("days_until_launch"),
        },
        "kaizen": {
            "ts": kz.get("started_at"),
            "bootstraps": kz.get("bootstraps"),
            "n_bots": kz.get("n_bots"),
            "applied_count": kz.get("applied_count"),
            "tier_counts": kz.get("tier_counts"),
            "action_counts": kz.get("action_counts"),
        },
        "quantum_6h": {
            "ts": qm.get("ts"),
            "rebalanced": qm.get("instruments_rebalanced") or qm.get("rebalanced"),
            "skipped": qm.get("instruments_skipped") or qm.get("skipped"),
            "total_cost_usd": float(qm.get("total_cost_usd") or qm.get("cost") or 0.0),
            "instruments_with_signals": [r["instrument"] for r in qm_with_signals],
            "instruments_with_hedges": [r["instrument"] for r in qm_with_hedges],
        },
        "recent_events": _tail_jsonl(EVENTS_LOG, 10),
    }


def render_text(state: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"ETA STATUS — {state['ts']}")
    lines.append("=" * 72)

    s = state["supervisor"]
    s_health = s.get("health") if isinstance(s.get("health"), dict) else {}
    lines.append(
        f"\nSupervisor      : tick={s.get('tick_count')}  n_bots={s.get('n_bots')}  "
        f"mode={s.get('mode')}  last={s.get('ts')}"
    )
    if s_health.get("status"):
        health_text = "healthy" if s_health.get("healthy") else str(s_health.get("status"))
        lines.append(f"  health       : {health_text}  {s_health.get('diagnosis')}")
        action_items = s_health.get("action_items") if isinstance(s_health.get("action_items"), list) else []
        if action_items:
            lines.append(f"  action       : {action_items[0]}")

    c = state["fm_cache"]
    lines.append(
        f"FM cache        : {c.get('hits')} hits / {c.get('misses')} misses "
        f"= {c.get('hit_rate_pct')}%  (size={c.get('size')}, ttl={c.get('ttl_seconds')}s)"
    )

    b = state["fm_breaker"]
    lines.append(
        f"FM breaker      : ${b.get('spent_today_usd'):.4f} / ${b.get('cap_usd'):.2f} "
        f"= {b.get('headroom_pct')}% headroom  tripped={b.get('tripped')}"
    )

    d = state["diamond_leaderboard"]
    lines.append(
        f"Diamonds        : {d.get('n_diamonds')} in fleet, "
        f"{d.get('n_prop_ready')} PROP_READY: {d.get('prop_ready_bots') or '(none)'}"
    )

    launch = state["launch_readiness"]
    lines.append(
        f"Launch verdict  : {launch.get('verdict')}  "
        f"({launch.get('days_until_launch')}d to {launch.get('launch_date')})"
    )
    lr_age = launch.get("age_seconds")
    if lr_age is not None:
        stale_tag = "STALE" if launch.get("stale") else "fresh"
        lines.append(f"  freshness    : {stale_tag}  age={lr_age:.0f}s  receipt={launch.get('ts')}")
    elif launch.get("stale"):
        lines.append("  freshness    : STALE  receipt timestamp missing/unparseable")
    if launch.get("failing_gates"):
        lines.append(f"  NO_GO gates   : {', '.join(launch['failing_gates'])}")
        for gate in launch.get("failing_gate_details") or []:
            lines.append(f"    - {gate.get('name')}: {gate.get('rationale')}")
    if launch.get("warning_gates"):
        lines.append(f"  WARN gates    : {', '.join(launch['warning_gates'])}")
        for gate in launch.get("warning_gate_details") or []:
            lines.append(f"    - {gate.get('name')}: {gate.get('rationale')}")
    if launch.get("summary"):
        lines.append(f"  summary       : {launch['summary']}")

    k = state["kaizen"]
    lines.append(
        f"Kaizen latest   : bootstraps={k.get('bootstraps')}  "
        f"n_bots={k.get('n_bots')}  applied={k.get('applied_count')}  "
        f"ts={k.get('ts')}"
    )
    if k.get("action_counts"):
        ac = k["action_counts"]
        parts = [f"{kk}={vv}" for kk, vv in ac.items()]
        lines.append(f"  actions       : {', '.join(parts)}")

    q = state["quantum_6h"]
    lines.append(
        f"Quantum (6h)    : rebalanced={q.get('rebalanced')}  skipped={q.get('skipped')}  "
        f"cost=${q.get('total_cost_usd'):.4f}  last={q.get('ts')}"
    )
    if q.get("instruments_with_signals"):
        lines.append(f"  signals       : {', '.join(q['instruments_with_signals'])}")
    if q.get("instruments_with_hedges"):
        lines.append(f"  with hedges   : {', '.join(q['instruments_with_hedges'])}")

    re = state["recent_events"]
    if re:
        lines.append("\nRecent events (last 10):")
        for e in re:
            kind = e.get("kind", "?")
            ts = e.get("ts", "")
            detail = {k: v for k, v in e.items() if k not in ("kind", "ts")}
            d_str = json.dumps(detail, default=str) if detail else ""
            lines.append(f"  {ts}  {kind}  {d_str}")
    else:
        lines.append("\nRecent events : (none)")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = p.parse_args(argv)

    state = gather()
    if args.json:
        print(json.dumps(state, indent=2, default=str))
    else:
        print(render_text(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
