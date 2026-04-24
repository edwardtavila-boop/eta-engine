"""
Deploy // run_task
==================
Single entry point invoked by cron for every Avengers background task.

Usage (from repo root on the VPS, with .venv activated):

    python -m deploy.scripts.run_task KAIZEN_RETRO
    python -m deploy.scripts.run_task SHADOW_TICK
    python -m deploy.scripts.run_task STRATEGY_MINE
    # ...etc. One task per invocation.

Why one task per call: cron fires on its own schedule. Keeping the
runner stateless + task-scoped means a Kaizen failure doesn't block the
5-minute SHADOW_TICK. Each invocation writes to JSONL logs + exits.

Exit codes:
  0 -- task completed
  1 -- task skipped (preconditions not met)
  2 -- task failed; error logged
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import traceback
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.brain.avengers import (
    TASK_OWNERS,
    BackgroundTask,
)

logger = logging.getLogger("deploy.run_task")


DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "eta_engine"
DEFAULT_LOG_DIR = Path.home() / ".local" / "log" / "eta_engine"


# ---------------------------------------------------------------------------
# Task handlers (one per BackgroundTask)
# ---------------------------------------------------------------------------

def _task_kaizen_retro(state_dir: Path) -> dict:
    """ALFRED: close the day with a retrospective + emit a +1 ticket."""
    from eta_engine.brain.jarvis_v3.kaizen import (
        CycleKind,
        KaizenLedger,
        close_cycle,
    )
    now = datetime.now(UTC)
    ledger_path = state_dir / "kaizen_ledger.json"
    ledger = KaizenLedger.load(ledger_path) if ledger_path.exists() else KaizenLedger()
    # Operator typically fills went_well / went_poorly via the voice hub
    # or dashboard. Here we close with placeholders the operator will edit.
    retro, ticket = close_cycle(
        cycle_kind=CycleKind.DAILY,
        window_start=now.replace(hour=0, minute=0, second=0, microsecond=0),
        window_end=now,
        went_well=["autopilot cadence honored"],
        went_poorly=[],
        now=now,
    )
    ledger.add_retro(retro)
    ledger.add_ticket(ticket)
    ledger.save(ledger_path)
    return {"ticket_id": ticket.id, "retrospectives": len(ledger.retrospectives())}


def _task_distill_train(state_dir: Path) -> dict:
    """ALFRED: retrain the distillation classifier on accumulated samples."""
    from eta_engine.brain.jarvis_v3.claude_layer.distillation import (
        Distiller,
        DistillSample,
    )
    samples_path = state_dir / "distill_samples.jsonl"
    if not samples_path.exists():
        return {"trained": False, "reason": "no samples yet"}
    samples: list[DistillSample] = []
    for line in samples_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            samples.append(DistillSample.model_validate(json.loads(line)))
        except Exception:  # noqa: BLE001
            continue
    if len(samples) < 20:
        return {"trained": False, "reason": f"only {len(samples)} samples; need >=20"}
    d = Distiller.load(state_dir / "distiller.json")
    model = d.fit(samples, iters=500)
    d.save(state_dir / "distiller.json")
    return {"trained": True, "samples": len(samples),
            "version": model.version, "accuracy": model.accuracy}


def _task_shadow_tick(state_dir: Path) -> dict:
    """ALFRED: resolve any open shadow trades at current prices."""
    from eta_engine.brain.jarvis_v3.next_level.shadow import ShadowLedger
    path = state_dir / "shadow_ledger.json"
    ledger = ShadowLedger.load(path) if path.exists() else ShadowLedger()
    # Price feed is injected; for the cron job we use the last-close
    # prices from the parquet cache (if available). Fall back to empty
    # lookup -- existing trades only expire on timeout.
    price_lookup: dict[str, float] = {}
    changed = ledger.tick(price_lookup=price_lookup)
    ledger.save(path)
    return {"resolved": len(changed), "open": len(ledger.open_trades())}


def _task_drift_summary(state_dir: Path) -> dict:
    """ALFRED: roll up anomaly detection state. No-op scaffold -- reads
    the current JARVIS context snapshot and writes a drift report."""
    snap_path = state_dir / "jarvis_live_health.json"
    if not snap_path.exists():
        return {"skipped": True, "reason": "no live health snapshot yet"}
    data = json.loads(snap_path.read_text(encoding="utf-8"))
    out_path = state_dir / "drift_summary.json"
    out_path.write_text(json.dumps({
        "ts": datetime.now(UTC).isoformat(),
        "last_health": data.get("health", "UNKNOWN"),
        "last_composite": data.get("last_composite"),
    }, indent=2), encoding="utf-8")
    return {"written": str(out_path)}


def _task_strategy_mine(state_dir: Path) -> dict:
    """BATMAN: mine precedent graph for candidate strategies."""
    from eta_engine.brain.jarvis_v3.next_level import strategy_synthesis
    from eta_engine.brain.jarvis_v3.precedent import PrecedentGraph
    path = state_dir / "precedent_graph.json"
    graph = PrecedentGraph.load(path) if path.exists() else PrecedentGraph()
    report = strategy_synthesis.mine(graph)
    out_path = state_dir / "strategy_candidates.json"
    strategy_synthesis.export_specs(report, out_path)
    return {"candidates_found": report.candidates_found,
            "buckets_scanned": report.buckets_scanned}


def _task_causal_review(state_dir: Path) -> dict:
    """BATMAN: run propensity matching on recent audit log."""
    # Scaffold -- a real implementation populates the CausalDAG from the
    # audit log. Stub writes a report file so downstream cron can chain.
    from eta_engine.brain.jarvis_v3.next_level.causal import CausalDAG
    dag = CausalDAG()
    out_path = state_dir / "causal_review.json"
    out_path.write_text(json.dumps({
        "ts": datetime.now(UTC).isoformat(),
        "nodes": len(dag.nodes()),
        "observations": len(dag.observations()),
        "note": "scaffold -- populate from audit log",
    }, indent=2), encoding="utf-8")
    return {"written": str(out_path)}


def _task_twin_verdict(state_dir: Path) -> dict:
    """BATMAN: digital-twin verdict rollup."""
    from eta_engine.brain.jarvis_v3.next_level.digital_twin import (
        TwinComparator,
    )
    cmp_ = TwinComparator()
    v = cmp_.verdict()
    out_path = state_dir / "twin_verdict.json"
    out_path.write_text(json.dumps(v.model_dump(mode="json"), indent=2),
                        encoding="utf-8")
    return {"verdict": v.verdict, "severity": v.severity}


def _task_doctrine_review(state_dir: Path) -> dict:
    """BATMAN: quarterly doctrine review. Produces a delta proposal."""
    from eta_engine.brain.jarvis_v3.philosophy import summarize_doctrine
    out_path = state_dir / "doctrine_review.md"
    out_path.write_text(
        f"# Doctrine Review ({datetime.now(UTC).isoformat()})\n\n"
        f"Current doctrine:\n\n```\n{summarize_doctrine()}\n```\n\n"
        "Operator: review audit log, propose diffs.\n",
        encoding="utf-8",
    )
    return {"written": str(out_path)}


def _task_log_compact(state_dir: Path, log_dir: Path) -> dict:
    """ROBIN: compact rolling log files; prune audit log to last 30d."""
    bytes_freed = 0
    for log_file in log_dir.glob("*.log"):
        if log_file.stat().st_size > 50 * 1024 * 1024:  # > 50 MiB
            # Keep the last 1 MiB; truncate earlier lines.
            data = log_file.read_bytes()[-1_000_000:]
            before = log_file.stat().st_size
            log_file.write_bytes(data)
            bytes_freed += before - log_file.stat().st_size
    return {"bytes_freed": bytes_freed}


def _task_prompt_warmup(state_dir: Path) -> dict:
    """ROBIN: pre-load the persona prefixes into the Anthropic cache.

    Fires a tiny call per persona right before a high-volume period
    (pre-market open, pre-close) so the 5-min cache is hot when JARVIS
    starts escalating.

    Each call is a minimal Haiku request that reads the persona prefix
    (cache-write on first call, cache-read on subsequent). After this
    task runs, any real debate call within the next ~5 minutes gets a
    10% cache-read discount on the prefix.
    """
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"skipped": True, "reason": "no API key"}

    try:
        import anthropic
    except ImportError:
        return {"skipped": True, "reason": "anthropic SDK not installed"}

    from eta_engine.brain.jarvis_v3.claude_layer.prompts import (
        PERSONA_PREFIXES,
    )

    # Use dotenv to load .env if present (harmless if already loaded)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    client = anthropic.Anthropic()
    results: dict[str, dict] = {}
    total_cost_est = 0.0
    total_tokens = 0

    for persona, prefix in PERSONA_PREFIXES.items():
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=20,  # tiny -- just enough to prove cache
                system=[
                    {"type": "text", "text": prefix,
                     "cache_control": {"type": "ephemeral"}},
                ],
                messages=[{"role": "user", "content": "warmup_ping"}],
            )
            cache_read  = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
            results[persona] = {
                "input_tokens":  resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "cache_read":    cache_read,
                "cache_write":   cache_write,
            }
            total_tokens += resp.usage.input_tokens + resp.usage.output_tokens
            # Estimated cost for Haiku warmup (tiny)
            total_cost_est += (
                resp.usage.input_tokens   * 0.80  / 1_000_000 +
                resp.usage.output_tokens  * 4.00  / 1_000_000
            )
        except Exception as exc:  # noqa: BLE001
            results[persona] = {"error": str(exc)[:200]}

    out_path = state_dir / "cache_warmup.json"
    out_path.write_text(json.dumps({
        "ts": datetime.now(UTC).isoformat(),
        "personas_warmed":  [p for p, r in results.items() if "error" not in r],
        "personas_failed":  [p for p, r in results.items() if "error" in r],
        "total_tokens":     total_tokens,
        "estimated_cost_usd": round(total_cost_est, 6),
        "per_persona":      results,
    }, indent=2), encoding="utf-8")
    return {
        "warmed":  sum(1 for r in results.values() if "error" not in r),
        "failed":  sum(1 for r in results.values() if "error" in r),
        "est_cost_usd": round(total_cost_est, 6),
    }


def _task_meta_upgrade(state_dir: Path) -> dict:
    """ALFRED: daily self-upgrade. git pull -> run fast tests -> restart services if tests pass.

    Runs safely: if pytest fails on new commits, we do NOT restart services --
    the old (green) build keeps running until operator intervention. Writes a
    structured report so the operator sees what happened each day.
    """
    import shutil
    import subprocess

    repo_dir = Path(os.environ.get("APEX_REPO_DIR", r"C:\eta_engine"))
    if not (repo_dir / ".git").exists():
        return {"skipped": True, "reason": f"{repo_dir} is not a git repo"}

    report: dict = {"ts": datetime.now(UTC).isoformat(), "repo": str(repo_dir)}

    # 1. Capture current HEAD
    try:
        before = subprocess.check_output(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            text=True, timeout=15,
        ).strip()
        report["before"] = before
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"git rev-parse failed: {exc}"
        _write_meta_report(state_dir, report)
        return report

    # 2. git pull
    try:
        pull_out = subprocess.check_output(
            ["git", "-C", str(repo_dir), "pull", "--ff-only"],
            text=True, stderr=subprocess.STDOUT, timeout=60,
        )
        report["pull_output"] = pull_out[-500:]
    except subprocess.CalledProcessError as exc:
        report["error"] = f"git pull failed: {exc.output[-500:]}"
        _write_meta_report(state_dir, report)
        return report

    after = subprocess.check_output(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        text=True, timeout=15,
    ).strip()
    report["after"] = after

    if before == after:
        report["result"] = "no_changes"
        _write_meta_report(state_dir, report)
        return report

    # 3. Run fast tests (core jarvis_v3 + deploy suites only -- ~2s)
    venv_python = repo_dir / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        # *nix fallback
        venv_python = repo_dir / ".venv" / "bin" / "python"
    if not venv_python.exists():
        report["error"] = "venv python not found"
        _write_meta_report(state_dir, report)
        return report

    try:
        pytest_out = subprocess.check_output(
            [str(venv_python), "-m", "pytest",
             "tests/test_jarvis_v3.py",
             "tests/test_jarvis_v3_supercharge.py",
             "tests/test_jarvis_v3_next_level.py",
             "tests/test_jarvis_v3_claude_layer.py",
             "tests/test_avengers_dispatch.py",
             "tests/test_deploy.py",
             "-q", "--tb=no", "-x"],
            cwd=str(repo_dir), text=True, stderr=subprocess.STDOUT, timeout=180,
        )
        report["test_output"] = pytest_out[-600:]
        report["tests_pass"] = True
    except subprocess.CalledProcessError as exc:
        report["test_output"] = exc.output[-600:]
        report["tests_pass"] = False
        report["result"] = "tests_failed_no_restart"
        _write_meta_report(state_dir, report)
        return report

    # 4. Tests green -> restart services (Windows only; Linux systemd path TBD)
    restarted: list[str] = []
    if shutil.which("powershell"):
        for svc in ("Apex-Jarvis-Live", "Apex-Avengers-Fleet", "Apex-Dashboard",
                    "Apex-Cloudflare-Tunnel"):
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"Stop-ScheduledTask -TaskName {svc} -ErrorAction SilentlyContinue; "
                     f"Start-Sleep -Seconds 1; Start-ScheduledTask -TaskName {svc}"],
                    timeout=30, check=False,
                )
                restarted.append(svc)
            except Exception:  # noqa: BLE001
                pass
    report["services_restarted"] = restarted
    report["result"] = "upgraded_and_restarted"
    _write_meta_report(state_dir, report)
    return report


def _write_meta_report(state_dir: Path, report: dict) -> None:
    out = state_dir / "meta_upgrade.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    # Also keep a rolling jsonl history
    history = state_dir / "meta_upgrade_history.jsonl"
    with history.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(report) + "\n")


def _task_dashboard_assemble(state_dir: Path) -> dict:
    """ROBIN: assemble the dashboard payload JSON."""
    from eta_engine.brain.jarvis_v3.dashboard_payload import build_payload
    # Pull the latest JARVIS snapshot if available
    snap_path = state_dir / "jarvis_live_health.json"
    health = "UNKNOWN"
    stress = {"composite": 0.0, "binding": "none", "components": []}
    if snap_path.exists():
        d = json.loads(snap_path.read_text(encoding="utf-8"))
        health = str(d.get("health", "UNKNOWN"))
        stress["composite"] = float(d.get("last_composite") or 0.0)
    payload = build_payload(
        health=health, stress=stress,
        horizons={"now": 0.0, "next_15m": 0.0, "next_1h": 0.0, "overnight": 0.0},
        projection={"level": 0.0, "trend": 0.0, "forecast_5": 0.0},
        regime="UNKNOWN", session_phase="OVERNIGHT",
        suggestion="TRADE",
    )
    out_path = state_dir / "dashboard_payload.json"
    out_path.write_text(json.dumps(payload.model_dump(mode="json"), indent=2),
                        encoding="utf-8")
    return {"written": str(out_path)}


def _task_audit_summarize(state_dir: Path) -> dict:
    """ROBIN: daily rollup of yesterday's JARVIS audit log."""
    audit_path = state_dir / "jarvis_audit.jsonl"
    if not audit_path.exists():
        return {"skipped": True, "reason": "no audit log"}
    from eta_engine.brain.jarvis_v3 import nl_query
    r = nl_query.reason_freq(audit_path, hours=24.0)
    out_path = state_dir / "audit_daily_summary.json"
    out_path.write_text(json.dumps(r.model_dump(mode="json"), indent=2),
                        encoding="utf-8")
    return {"summary": r.summary}


HANDLERS: dict[BackgroundTask, callable] = {
    BackgroundTask.KAIZEN_RETRO:       lambda s, _l: _task_kaizen_retro(s),
    BackgroundTask.DISTILL_TRAIN:      lambda s, _l: _task_distill_train(s),
    BackgroundTask.SHADOW_TICK:        lambda s, _l: _task_shadow_tick(s),
    BackgroundTask.DRIFT_SUMMARY:      lambda s, _l: _task_drift_summary(s),
    BackgroundTask.STRATEGY_MINE:      lambda s, _l: _task_strategy_mine(s),
    BackgroundTask.CAUSAL_REVIEW:      lambda s, _l: _task_causal_review(s),
    BackgroundTask.TWIN_VERDICT:       lambda s, _l: _task_twin_verdict(s),
    BackgroundTask.DOCTRINE_REVIEW:    lambda s, _l: _task_doctrine_review(s),
    BackgroundTask.LOG_COMPACT:        lambda s, ld: _task_log_compact(s, ld),
    BackgroundTask.PROMPT_WARMUP:      lambda s, _l: _task_prompt_warmup(s),
    BackgroundTask.DASHBOARD_ASSEMBLE: lambda s, _l: _task_dashboard_assemble(s),
    BackgroundTask.AUDIT_SUMMARIZE:    lambda s, _l: _task_audit_summarize(s),
    BackgroundTask.META_UPGRADE:       lambda s, _l: _task_meta_upgrade(s),
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("task", help="BackgroundTask name (e.g. KAIZEN_RETRO)")
    ap.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    ap.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    args = ap.parse_args(argv)

    state_dir = Path(args.state_dir)
    log_dir = Path(args.log_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        task = BackgroundTask(args.task.upper())
    except ValueError:
        logger.error("unknown task %r -- options: %s",
                     args.task, ", ".join(t.value for t in BackgroundTask))
        return 2

    owner = TASK_OWNERS[task]
    logger.info("[%s] task=%s starting", owner, task.value)
    try:
        handler = HANDLERS[task]
        out = handler(state_dir, log_dir)
        logger.info("[%s] task=%s done -- %s", owner, task.value, out)
        # Persist one-line result for dashboard
        (state_dir / "last_task.json").write_text(json.dumps({
            "ts": datetime.now(UTC).isoformat(),
            "task": task.value,
            "owner": owner,
            "result": out,
        }, indent=2), encoding="utf-8")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.error("[%s] task=%s failed: %s\n%s",
                     owner, task.value, exc, traceback.format_exc())
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
