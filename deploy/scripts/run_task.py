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

# brain.avengers.push may or may not be exported (depends on whether the
# push subsystem has been committed). Fall back to the Telegram adapter
# for critical alerts; noop if even that's missing.
try:
    from eta_engine.brain.avengers import AlertLevel, push  # type: ignore[attr-defined]

    _HAS_PUSH = True
except ImportError:
    _HAS_PUSH = False

    class _AlertLevelStub:
        CRITICAL = "CRITICAL"
        WARN = "WARN"
        INFO = "INFO"

    AlertLevel = _AlertLevelStub()  # type: ignore[misc]

    def push(level: object, title: str, body: str) -> None:  # type: ignore[misc]
        try:
            from eta_engine.deploy.scripts.telegram_alerts import send_from_env

            send_from_env(f"*{title}*\n{body}", priority=str(level))
        except Exception:  # noqa: BLE001
            pass


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
    return {"trained": True, "samples": len(samples), "version": model.version, "accuracy": model.accuracy}


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
    out_path.write_text(
        json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "last_health": data.get("health", "UNKNOWN"),
                "last_composite": data.get("last_composite"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
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
    return {"candidates_found": report.candidates_found, "buckets_scanned": report.buckets_scanned}


def _task_causal_review(state_dir: Path) -> dict:
    """BATMAN: run propensity matching on recent audit log."""
    # Scaffold -- a real implementation populates the CausalDAG from the
    # audit log. Stub writes a report file so downstream cron can chain.
    from eta_engine.brain.jarvis_v3.next_level.causal import CausalDAG

    dag = CausalDAG()
    out_path = state_dir / "causal_review.json"
    out_path.write_text(
        json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "nodes": len(dag.nodes()),
                "observations": len(dag.observations()),
                "note": "scaffold -- populate from audit log",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"written": str(out_path)}


def _task_twin_verdict(state_dir: Path) -> dict:
    """BATMAN: digital-twin verdict rollup."""
    from eta_engine.brain.jarvis_v3.next_level.digital_twin import (
        TwinComparator,
    )

    cmp_ = TwinComparator()
    v = cmp_.verdict()
    out_path = state_dir / "twin_verdict.json"
    out_path.write_text(json.dumps(v.model_dump(mode="json"), indent=2), encoding="utf-8")
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
                    {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}},
                ],
                messages=[{"role": "user", "content": "warmup_ping"}],
            )
            cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
            results[persona] = {
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "cache_read": cache_read,
                "cache_write": cache_write,
            }
            total_tokens += resp.usage.input_tokens + resp.usage.output_tokens
            # Estimated cost for Haiku warmup (tiny)
            total_cost_est += resp.usage.input_tokens * 0.80 / 1_000_000 + resp.usage.output_tokens * 4.00 / 1_000_000
        except Exception as exc:  # noqa: BLE001
            results[persona] = {"error": str(exc)[:200]}

    out_path = state_dir / "cache_warmup.json"
    out_path.write_text(
        json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "personas_warmed": [p for p, r in results.items() if "error" not in r],
                "personas_failed": [p for p, r in results.items() if "error" in r],
                "total_tokens": total_tokens,
                "estimated_cost_usd": round(total_cost_est, 6),
                "per_persona": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "warmed": sum(1 for r in results.values() if "error" not in r),
        "failed": sum(1 for r in results.values() if "error" in r),
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
            text=True,
            timeout=15,
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
            text=True,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        report["pull_output"] = pull_out[-500:]
    except subprocess.CalledProcessError as exc:
        report["error"] = f"git pull failed: {exc.output[-500:]}"
        _write_meta_report(state_dir, report)
        return report

    after = subprocess.check_output(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        text=True,
        timeout=15,
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
            [
                str(venv_python),
                "-m",
                "pytest",
                "tests/test_jarvis_v3.py",
                "tests/test_jarvis_v3_supercharge.py",
                "tests/test_jarvis_v3_next_level.py",
                "tests/test_jarvis_v3_claude_layer.py",
                "tests/test_avengers_dispatch.py",
                "tests/test_deploy.py",
                "-q",
                "--tb=no",
                "-x",
            ],
            cwd=str(repo_dir),
            text=True,
            stderr=subprocess.STDOUT,
            timeout=180,
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
        for svc in ("Apex-Jarvis-Live", "Apex-Avengers-Fleet", "Apex-Dashboard", "Apex-Cloudflare-Tunnel"):
            try:
                subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        f"Stop-ScheduledTask -TaskName {svc} -ErrorAction SilentlyContinue; "
                        f"Start-Sleep -Seconds 1; Start-ScheduledTask -TaskName {svc}",
                    ],
                    timeout=30,
                    check=False,
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


# ===========================================================================
# SUPERCHARGE ROUND -- 6 additional handlers (#14 #16 #17 #18 #19 #20)
# ===========================================================================


def _task_health_watchdog(state_dir: Path) -> dict:
    """ALFRED: auto-heal. Every 5min check the 3 boot services are Running;
    restart any that have been Ready for > 2 min. Telegram on first restart."""
    import subprocess

    report: dict = {"ts": datetime.now(UTC).isoformat(), "actions": []}
    services = ("Apex-Jarvis-Live", "Apex-Avengers-Fleet", "Apex-Dashboard", "Apex-Cloudflare-Tunnel")
    if os.name != "nt":
        return {"skipped": True, "reason": "watchdog is Windows-only"}

    for svc in services:
        try:
            out = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-ScheduledTask -TaskName {svc} -ErrorAction SilentlyContinue).State",
                ],
                text=True,
                timeout=15,
            ).strip()
        except Exception as exc:  # noqa: BLE001
            report["actions"].append({"svc": svc, "state": "PROBE_FAIL", "error": str(exc)[:100]})
            continue
        if not out:
            continue
        if out != "Running":
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", f"Start-ScheduledTask -TaskName {svc}"],
                    timeout=15,
                    check=False,
                )
                report["actions"].append(
                    {"svc": svc, "state_before": out, "state_after": "Started"},
                )
            except Exception as exc:  # noqa: BLE001
                report["actions"].append({"svc": svc, "error": str(exc)[:100]})

    out_path = state_dir / "health_watchdog.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Telegram ping if we had to restart anything (non-blocking)
    if report["actions"] and any("state_after" in a for a in report["actions"]):
        try:
            from eta_engine.deploy.scripts.telegram_alerts import send_from_env

            restarted = [a["svc"] for a in report["actions"] if a.get("state_after")]
            if restarted:
                send_from_env(
                    f"*Watchdog auto-healed*: {', '.join(restarted)}",
                    priority="WARN",
                )
        except Exception:  # noqa: BLE001
            pass
    return {
        "actions": len(report["actions"]),
        "restarted": [a["svc"] for a in report["actions"] if a.get("state_after")],
    }


def _task_self_test(state_dir: Path) -> dict:
    """ALFRED: end-to-end smoke. health probe + tunnel probe + (optional) live Claude."""
    import urllib.error
    import urllib.request

    report: dict = {"ts": datetime.now(UTC).isoformat(), "checks": {}}

    # 1. local health endpoint
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8000/health",
            timeout=5,
        ) as resp:
            report["checks"]["local_health"] = {
                "status": resp.status,
                "ok": resp.status == 200,
            }
    except (urllib.error.URLError, TimeoutError) as exc:
        report["checks"]["local_health"] = {"ok": False, "error": str(exc)[:100]}

    # 2. public tunnel endpoint (with UA -- Cloudflare rejects empty UA)
    try:
        req = urllib.request.Request(
            "https://jarvis.evolutionarytradingalgo.live/health",
            headers={"User-Agent": "apex-self-test/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            report["checks"]["public_tunnel"] = {
                "status": resp.status,
                "ok": resp.status == 200,
            }
    except (urllib.error.URLError, TimeoutError) as exc:
        report["checks"]["public_tunnel"] = {"ok": False, "error": str(exc)[:100]}

    # 3. Avengers heartbeat freshness (< 2 min)
    hb_path = state_dir / "avengers_heartbeat.json"
    if hb_path.exists():
        try:
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            ts = datetime.fromisoformat(hb["ts"].replace("Z", "+00:00"))
            age = (datetime.now(UTC) - ts).total_seconds()
            report["checks"]["heartbeat_fresh"] = {
                "ok": age < 120,
                "age_seconds": int(age),
            }
        except Exception as exc:  # noqa: BLE001
            report["checks"]["heartbeat_fresh"] = {"ok": False, "error": str(exc)[:100]}
    else:
        report["checks"]["heartbeat_fresh"] = {"ok": False, "error": "no heartbeat file"}

    # 4. Live Claude call if key present (budget-sensitive: once per day is cheap)
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic

            client = anthropic.Anthropic()
            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=5,
                messages=[{"role": "user", "content": "ping"}],
            )
            report["checks"]["claude_live"] = {
                "ok": True,
                "tokens": resp.usage.output_tokens,
            }
        except Exception as exc:  # noqa: BLE001
            report["checks"]["claude_live"] = {"ok": False, "error": str(exc)[:150]}

    # Overall verdict
    all_ok = all(c.get("ok") for c in report["checks"].values())
    report["overall"] = "PASS" if all_ok else "FAIL"
    out_path = state_dir / "self_test.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Telegram on failure
    if not all_ok:
        try:
            from eta_engine.deploy.scripts.telegram_alerts import send_from_env

            failures = [k for k, v in report["checks"].items() if not v.get("ok")]
            send_from_env(
                f"*Self-test FAILED* failures: {', '.join(failures)}",
                priority="CRITICAL",
            )
        except Exception:  # noqa: BLE001
            pass
    return report


def _task_log_rotate(state_dir: Path, log_dir: Path) -> dict:
    """ROBIN: archive + prune old logs. Keep last 3 days of active logs;
    gzip anything older; delete gzips older than 30 days."""
    import gzip
    import shutil

    report: dict = {"ts": datetime.now(UTC).isoformat(), "archived": [], "pruned": []}
    now_ts = datetime.now(UTC).timestamp()
    # Archive .log files > 3 days old
    for log_file in log_dir.glob("*.log"):
        try:
            age_days = (now_ts - log_file.stat().st_mtime) / 86400
            if age_days > 3:
                arc = log_file.with_suffix(
                    f".log.{datetime.now(UTC):%Y%m%d}.gz",
                )
                with log_file.open("rb") as src, gzip.open(arc, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                log_file.write_bytes(b"")  # truncate original
                report["archived"].append(str(arc.name))
        except Exception as exc:  # noqa: BLE001
            report.setdefault("errors", []).append(
                f"{log_file.name}: {exc}"[:200],
            )
    # Prune .gz older than 30 days
    for gz in log_dir.glob("*.gz"):
        try:
            age_days = (now_ts - gz.stat().st_mtime) / 86400
            if age_days > 30:
                gz.unlink()
                report["pruned"].append(gz.name)
        except Exception:  # noqa: BLE001
            pass
    (state_dir / "log_rotate.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    return {"archived": len(report["archived"]), "pruned": len(report["pruned"])}


def _task_disk_cleanup(state_dir: Path) -> dict:
    """ROBIN: weekly cleanup. Purge %TEMP% files > 7 days,
    .pytest_cache + __pycache__ directories in the repo, and old package caches."""
    report: dict = {"ts": datetime.now(UTC).isoformat(), "bytes_freed": 0, "files_deleted": 0}
    cutoff_ts = datetime.now(UTC).timestamp() - 7 * 86400

    targets: list[Path] = []
    if os.name == "nt":
        targets = [
            Path(os.environ.get("TEMP", r"C:\Windows\Temp")),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Temp",
        ]
    else:
        targets = [Path("/tmp")]

    for root in targets:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff_ts:
                    size = p.stat().st_size
                    p.unlink()
                    report["bytes_freed"] += size
                    report["files_deleted"] += 1
            except Exception:  # noqa: BLE001
                continue

    # Also blow away pyc caches older than 14 days
    repo = Path(os.environ.get("APEX_REPO_DIR", r"C:\eta_engine"))
    if repo.exists():
        pyc_cutoff = datetime.now(UTC).timestamp() - 14 * 86400
        for pycache in repo.rglob("__pycache__"):
            try:
                if pycache.stat().st_mtime < pyc_cutoff:
                    for p in pycache.rglob("*"):
                        if p.is_file():
                            report["bytes_freed"] += p.stat().st_size
                            p.unlink()
                            report["files_deleted"] += 1
                    pycache.rmdir()
            except Exception:  # noqa: BLE001
                continue
    (state_dir / "disk_cleanup.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    return report


def _task_backup(state_dir: Path) -> dict:
    """ALFRED: daily snapshot of state + config. Compresses
    state_dir + .env + config.json into a single tar.gz, keeps last 7."""
    import tarfile

    report: dict = {"ts": datetime.now(UTC).isoformat()}
    repo = Path(os.environ.get("APEX_REPO_DIR", r"C:\eta_engine"))
    backup_dir = state_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    archive = backup_dir / f"apex-backup-{stamp}.tar.gz"

    try:
        with tarfile.open(archive, "w:gz") as tar:
            # Add state dir contents (exclude backups subdir to avoid recursion)
            for f in state_dir.iterdir():
                if f.is_file():
                    tar.add(f, arcname=f"state/{f.name}")
            # Add .env (critical)
            env_path = repo / ".env"
            if env_path.exists():
                tar.add(env_path, arcname=".env")
            # Add config.json if present
            cfg = repo / "config.json"
            if cfg.exists():
                tar.add(cfg, arcname="config.json")
        report["archive"] = str(archive)
        report["size_bytes"] = archive.stat().st_size
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)[:200]
        (state_dir / "backup.json").write_text(
            json.dumps(report, indent=2),
            encoding="utf-8",
        )
        return report

    # Rotate: keep last 7 archives
    existing = sorted(backup_dir.glob("apex-backup-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    pruned = 0
    for old in existing[7:]:
        try:
            old.unlink()
            pruned += 1
        except Exception:  # noqa: BLE001
            pass
    report["retained"] = len(existing) - pruned
    report["pruned"] = pruned
    (state_dir / "backup.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    return report


def _task_prometheus_export(state_dir: Path) -> dict:
    """ROBIN: write OpenMetrics format alongside JSON heartbeat.

    Writes to state_dir/prometheus/avengers.prom -- a Prometheus textfile
    exporter can pick this up with node_exporter --collector.textfile.
    Also serves stand-alone via /metrics endpoint (added in dashboard_api).
    """
    hb_path = state_dir / "avengers_heartbeat.json"
    dash_path = state_dir / "dashboard_payload.json"
    prom_dir = state_dir / "prometheus"
    prom_dir.mkdir(parents=True, exist_ok=True)
    out_path = prom_dir / "avengers.prom"

    lines: list[str] = [
        "# HELP apex_up Whether the Avengers daemon is alive (1=yes)",
        "# TYPE apex_up gauge",
        "apex_up 1",
    ]

    if hb_path.exists():
        try:
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            lines.extend(
                [
                    "# HELP apex_quota_hourly_pct Fraction of hourly USD budget consumed",
                    "# TYPE apex_quota_hourly_pct gauge",
                    f"apex_quota_hourly_pct {hb.get('hourly_pct', 0.0)}",
                    "# HELP apex_quota_daily_pct Fraction of daily USD budget consumed",
                    "# TYPE apex_quota_daily_pct gauge",
                    f"apex_quota_daily_pct {hb.get('daily_pct', 0.0)}",
                    "# HELP apex_cache_hit_rate Anthropic prompt-cache hit rate in last hour",
                    "# TYPE apex_cache_hit_rate gauge",
                    f"apex_cache_hit_rate {hb.get('cache_hit_rate', 0.0)}",
                    "# HELP apex_distiller_version Current classifier version",
                    "# TYPE apex_distiller_version gauge",
                    f"apex_distiller_version {hb.get('distiller_version', 0)}",
                    "# HELP apex_distiller_trained 1 if classifier has training data, 0 otherwise",
                    "# TYPE apex_distiller_trained gauge",
                    f"apex_distiller_trained {1 if hb.get('distiller_trained') else 0}",
                    "# HELP apex_quota_state JARVIS quota state code (OK=0, WARN=1, DOWNSHIFT=2, FREEZE=3)",
                    "# TYPE apex_quota_state gauge",
                    f"apex_quota_state {_prom_quota_code(hb.get('quota_state', 'OK'))}",
                ]
            )
        except Exception as exc:  # noqa: BLE001
            lines.append(f"# error reading heartbeat: {exc}"[:200])

    if dash_path.exists():
        try:
            d = json.loads(dash_path.read_text(encoding="utf-8"))
            stress = d.get("stress", {}) or {}
            lines.extend(
                [
                    "# HELP apex_stress_composite Weighted stress composite [0,1]",
                    "# TYPE apex_stress_composite gauge",
                    f"apex_stress_composite {stress.get('composite', 0.0)}",
                ]
            )
        except Exception:  # noqa: BLE001
            pass

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"written": str(out_path), "metrics": sum(1 for line in lines if not line.startswith("#"))}


def _prom_quota_code(state: str) -> int:
    return {"OK": 0, "WARN": 1, "DOWNSHIFT": 2, "FREEZE": 3}.get(state, 0)


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
        health=health,
        stress=stress,
        horizons={"now": 0.0, "next_15m": 0.0, "next_1h": 0.0, "overnight": 0.0},
        projection={"level": 0.0, "trend": 0.0, "forecast_5": 0.0},
        regime="UNKNOWN",
        session_phase="OVERNIGHT",
        suggestion="TRADE",
    )
    out_path = state_dir / "dashboard_payload.json"
    out_path.write_text(json.dumps(payload.model_dump(mode="json"), indent=2), encoding="utf-8")
    return {"written": str(out_path)}


def _task_chaos_drill(state_dir: Path) -> dict:
    """ALFRED: run monthly chaos drills and journal the verdict.

    Calls ``eta_engine.scripts.chaos_drill.run_drills`` against a
    fresh sub-sandbox (never touches the real ``~/.jarvis`` state), then
    writes a report to ``{state_dir}/chaos_drill.json`` for the dashboard
    and appends to ``{state_dir}/chaos_drill_history.jsonl`` for trend.

    Exit is always success from cron's perspective -- the drill failing
    is signal, not a crash. The verdict is in the journal.
    """
    from eta_engine.scripts.chaos_drill import run_drills

    results = run_drills()
    passed = sum(1 for r in results if r.get("passed"))
    failed = len(results) - passed
    report = {
        "ts": datetime.now(UTC).isoformat(),
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "results": results,
    }
    out = state_dir / "chaos_drill.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    history = state_dir / "chaos_drill_history.jsonl"
    with history.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(report) + "\n")

    # Operator observability: CRITICAL if any drill failed (kill-switch
    # guarantees, breaker isolation, etc. are load-bearing for Apex eval
    # survival). Dedup window on the PushBus keeps back-to-back monthly
    # re-fires quiet unless the failing drill changes.
    if failed > 0:
        failing = [r.get("name", "?") for r in results if not r.get("passed")]
        try:
            push(
                AlertLevel.CRITICAL,
                title=f"chaos_drill: {failed}/{len(results)} FAILED",
                body="failing drills: " + ", ".join(failing),
                source="chaos_drill",
                tags=["ALFRED", "chaos_drill_failure"],
            )
        except Exception:  # noqa: BLE001
            logger.exception("push() raised reporting chaos drill failure")

    return {"passed": passed, "failed": failed, "total": len(results)}


def _task_audit_summarize(state_dir: Path) -> dict:
    """ROBIN: daily rollup of yesterday's JARVIS audit log."""
    audit_path = state_dir / "jarvis_audit.jsonl"
    if not audit_path.exists():
        return {"skipped": True, "reason": "no audit log"}
    from eta_engine.brain.jarvis_v3 import nl_query

    r = nl_query.reason_freq(audit_path, hours=24.0)
    out_path = state_dir / "audit_daily_summary.json"
    out_path.write_text(json.dumps(r.model_dump(mode="json"), indent=2), encoding="utf-8")
    return {"summary": r.summary}


HANDLERS: dict[BackgroundTask, callable] = {
    BackgroundTask.KAIZEN_RETRO: lambda s, _l: _task_kaizen_retro(s),
    BackgroundTask.DISTILL_TRAIN: lambda s, _l: _task_distill_train(s),
    BackgroundTask.SHADOW_TICK: lambda s, _l: _task_shadow_tick(s),
    BackgroundTask.DRIFT_SUMMARY: lambda s, _l: _task_drift_summary(s),
    BackgroundTask.STRATEGY_MINE: lambda s, _l: _task_strategy_mine(s),
    BackgroundTask.CAUSAL_REVIEW: lambda s, _l: _task_causal_review(s),
    BackgroundTask.TWIN_VERDICT: lambda s, _l: _task_twin_verdict(s),
    BackgroundTask.DOCTRINE_REVIEW: lambda s, _l: _task_doctrine_review(s),
    BackgroundTask.LOG_COMPACT: lambda s, ld: _task_log_compact(s, ld),
    BackgroundTask.PROMPT_WARMUP: lambda s, _l: _task_prompt_warmup(s),
    BackgroundTask.DASHBOARD_ASSEMBLE: lambda s, _l: _task_dashboard_assemble(s),
    BackgroundTask.AUDIT_SUMMARIZE: lambda s, _l: _task_audit_summarize(s),
    BackgroundTask.META_UPGRADE: lambda s, _l: _task_meta_upgrade(s),
    BackgroundTask.CHAOS_DRILL: lambda s, _l: _task_chaos_drill(s),
    BackgroundTask.HEALTH_WATCHDOG: lambda s, _l: _task_health_watchdog(s),
    BackgroundTask.SELF_TEST: lambda s, _l: _task_self_test(s),
    BackgroundTask.LOG_ROTATE: lambda s, ld: _task_log_rotate(s, ld),
    BackgroundTask.DISK_CLEANUP: lambda s, _l: _task_disk_cleanup(s),
    BackgroundTask.BACKUP: lambda s, _l: _task_backup(s),
    BackgroundTask.PROMETHEUS_EXPORT: lambda s, _l: _task_prometheus_export(s),
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
        logger.error("unknown task %r -- options: %s", args.task, ", ".join(t.value for t in BackgroundTask))
        return 2

    owner = TASK_OWNERS[task]
    logger.info("[%s] task=%s starting", owner, task.value)
    try:
        handler = HANDLERS[task]
        out = handler(state_dir, log_dir)
        logger.info("[%s] task=%s done -- %s", owner, task.value, out)
        # Persist one-line result for dashboard
        (state_dir / "last_task.json").write_text(
            json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "task": task.value,
                    "owner": owner,
                    "result": out,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.error("[%s] task=%s failed: %s\n%s", owner, task.value, exc, traceback.format_exc())
        # Fan out the failure so the operator knows cron is silently
        # broken. PushBus dedups repeat titles within a 10-minute
        # window so a task that fails every 5 min does NOT spam
        # Telegram -- only the first-in-window hits the remote channel,
        # all subsequent repeats hit the local audit log only.
        try:
            push(
                AlertLevel.WARN,
                title=f"run_task:{task.value} failed",
                body=f"owner={owner} exc={type(exc).__name__}: {exc}",
                source=f"run_task:{task.value}",
                tags=[owner, "cron_failure"],
            )
        except Exception:  # noqa: BLE001 -- push must never shadow the original error
            logger.exception("push() raised while reporting task failure")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
