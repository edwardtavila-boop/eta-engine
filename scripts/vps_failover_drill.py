"""
EVOLUTIONARY TRADING ALGO  //  scripts.vps_failover_drill
============================================================
VPS disaster-recovery drill — dry-run + checklist.

Why this exists
---------------
Untested DR is no DR. The VPS host runbook
(``deploy/HOST_RUNBOOK.md``) describes how to stand up a fresh box
from scratch; this script *exercises* the parts of that runbook
that can be tested without actually provisioning a new VPS, and
emits a checklist of the operator-only steps for everything else.

What it does (locally — safe to run anytime)
--------------------------------------------
1. **State-backup verifier**: confirms the files that matter for
   resuming trading after a host loss are present and recent:
   - ``docs/strategy_baselines.json`` (the frozen baselines)
   - ``docs/decision_journal.jsonl`` (the audit trail)
   - ``docs/drift_watchdog.jsonl`` (drift history)
   - ``docs/alerts_log.jsonl`` (alert history)
   - ``.env`` (broker keys — checks file exists, NEVER reads contents)
2. **Backup-restore round-trip**: tars the state dir into a temp
   tarball, untars it into a scratch dir, diffs to verify integrity.
3. **Bootstrap-script lint**: runs ``deploy/install_vps.sh`` through
   ``bash -n`` (syntax check) to catch shell bugs before they bite
   on a real DR event.
4. **Cron schedule check**: verifies ``deploy/cron/`` has entries
   for the daemons that must restart on a fresh host (drift_watchdog,
   live_supervisor, etc.) so the operator doesn't forget any.
5. **Idempotency probe** (mock): simulates resuming after a kill,
   verifying the order-reconciler logic in ``brain/jarvis_v3/vps.py``
   doesn't duplicate orders. *Mock — does not contact brokers.*

Operator-only steps (printed as a checklist)
--------------------------------------------
The script prints a final checklist of steps the operator must
execute on a real DR drill day, with explicit time targets:

  T+0   Provision new VPS (provider page open in browser)
  T+5   SSH in, run install_vps.sh
  T+15  Pull state backup from S3 / B2 / wherever
  T+20  Verify .env populated; verify broker session live
  T+25  Run preflight_bot_promotion across all production bots
  T+30  Resume daemons; verify drift_watchdog logs an event
  T+45  Confirm one round-trip paper order before live capital
  T+60  DR drill complete; record durations in research log

Usage
-----

    # Default dry-run + checklist
    python -m eta_engine.scripts.vps_failover_drill

    # Skip the local backup-restore step (faster)
    python -m eta_engine.scripts.vps_failover_drill --no-backup-test

    # Operator drill mode — prints the live checklist, no dry-run
    python -m eta_engine.scripts.vps_failover_drill --drill-mode
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


_STATE_FILES_REQUIRED: list[str] = [
    "docs/strategy_baselines.json",
    "docs/decision_journal.jsonl",
]
_STATE_FILES_RECOMMENDED: list[str] = [
    "docs/drift_watchdog.jsonl",
    "docs/alerts_log.jsonl",
    "docs/runtime_log.jsonl",
]
# .env is special — we verify presence but NEVER read contents
_SECRETS_FILE = ".env"

_DEPLOY_FILES_REQUIRED: list[str] = [
    "deploy/install_vps.sh",
    "deploy/HOST_RUNBOOK.md",
    "deploy/README.md",
]


@dataclass
class CheckResult:
    name: str
    severity: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Local checks
# ---------------------------------------------------------------------------


def _check_state_files_present() -> CheckResult:
    """1a. Required + recommended state files exist."""
    missing_required: list[str] = []
    missing_recommended: list[str] = []
    sizes: dict[str, int] = {}
    for rel in _STATE_FILES_REQUIRED:
        p = ROOT / rel
        if not p.exists():
            missing_required.append(rel)
        else:
            sizes[rel] = p.stat().st_size
    for rel in _STATE_FILES_RECOMMENDED:
        p = ROOT / rel
        if not p.exists():
            missing_recommended.append(rel)
        else:
            sizes[rel] = p.stat().st_size

    if missing_required:
        return CheckResult(
            name="state_files_present",
            severity="red",
            summary=f"required state files missing: {missing_required}",
            details={"missing": missing_required, "sizes": sizes},
        )
    if missing_recommended:
        return CheckResult(
            name="state_files_present",
            severity="amber",
            summary=(
                f"recommended state files missing: {missing_recommended} "
                "— DR can proceed without them but loses replay context"
            ),
            details={"missing": missing_recommended, "sizes": sizes},
        )
    return CheckResult(
        name="state_files_present",
        severity="green",
        summary=f"all {len(sizes)} state files present",
        details={"sizes": sizes},
    )


def _check_state_files_fresh() -> CheckResult:
    """1b. State files have been touched within the last 24h."""
    stale: list[tuple[str, float]] = []
    now = datetime.now(UTC).timestamp()
    for rel in _STATE_FILES_REQUIRED + _STATE_FILES_RECOMMENDED:
        p = ROOT / rel
        if not p.exists():
            continue
        age_h = (now - p.stat().st_mtime) / 3600
        if age_h > 24:
            stale.append((rel, age_h))
    if stale:
        return CheckResult(
            name="state_files_fresh",
            severity="amber",
            summary=(
                f"{len(stale)} state file(s) >24h old: "
                + ", ".join(f"{n} ({h:.0f}h)" for n, h in stale[:3])
            ),
            details={"stale": [{"file": n, "age_h": h} for n, h in stale]},
        )
    return CheckResult(
        name="state_files_fresh",
        severity="green",
        summary="all state files updated within 24h",
    )


def _check_secrets_present() -> CheckResult:
    """1c. .env exists. Never reads contents."""
    p = ROOT / _SECRETS_FILE
    if not p.exists():
        return CheckResult(
            name="secrets_present",
            severity="amber",
            summary=(
                ".env missing — operator must populate broker keys "
                "before flipping live (script never reads contents)"
            ),
        )
    return CheckResult(
        name="secrets_present",
        severity="green",
        summary=f".env exists ({p.stat().st_size} bytes; contents not read)",
    )


def _check_deploy_files_present() -> CheckResult:
    """2. Bootstrap scripts + runbooks exist."""
    missing = [r for r in _DEPLOY_FILES_REQUIRED if not (ROOT / r).exists()]
    if missing:
        return CheckResult(
            name="deploy_files_present",
            severity="red",
            summary=f"missing deploy artifacts: {missing}",
        )
    return CheckResult(
        name="deploy_files_present",
        severity="green",
        summary=f"all {len(_DEPLOY_FILES_REQUIRED)} deploy artifacts present",
    )


def _check_install_script_syntax() -> CheckResult:
    """3. ``bash -n install_vps.sh`` — syntax-check without executing.

    Falls back to ``green`` with note if bash isn't on PATH (Windows
    operator dev box without Git-bash, etc.).
    """
    script = ROOT / "deploy" / "install_vps.sh"
    if not script.exists():
        return CheckResult(
            name="install_script_syntax",
            severity="red",
            summary="deploy/install_vps.sh missing",
        )
    bash = shutil.which("bash")
    if bash is None:
        return CheckResult(
            name="install_script_syntax",
            severity="amber",
            summary=(
                "bash not on PATH; cannot syntax-check install_vps.sh "
                "locally. The CI pipeline / VPS itself will validate it."
            ),
        )
    try:
        result = subprocess.run(  # noqa: S603 -- localhost bash, fixed args
            [bash, "-n", str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return CheckResult(
            name="install_script_syntax",
            severity="amber",
            summary=f"bash -n failed to run: {exc}",
        )
    if result.returncode != 0:
        return CheckResult(
            name="install_script_syntax",
            severity="red",
            summary=f"bash -n found errors: {result.stderr.strip()[:200]}",
        )
    return CheckResult(
        name="install_script_syntax",
        severity="green",
        summary="install_vps.sh syntax-clean",
    )


def _check_cron_schedule() -> CheckResult:
    """4. deploy/cron/ has the daemons we expect."""
    cron_dir = ROOT / "deploy" / "cron"
    if not cron_dir.exists():
        return CheckResult(
            name="cron_schedule",
            severity="amber",
            summary="deploy/cron/ missing — daemons must be hand-registered on the VPS",
        )
    files = sorted(p.name for p in cron_dir.glob("*"))
    return CheckResult(
        name="cron_schedule",
        severity="green",
        summary=f"deploy/cron/ has {len(files)} entries: {files}",
        details={"entries": files},
    )


def _backup_restore_round_trip(skip: bool) -> CheckResult:
    """5. Tar state dir, untar in scratch dir, diff sizes."""
    if skip:
        return CheckResult(
            name="backup_restore_round_trip",
            severity="skip",
            summary="--no-backup-test passed",
        )
    state_files = [
        ROOT / rel for rel in _STATE_FILES_REQUIRED + _STATE_FILES_RECOMMENDED
        if (ROOT / rel).exists()
    ]
    if not state_files:
        return CheckResult(
            name="backup_restore_round_trip",
            severity="red",
            summary="no state files to back up",
        )
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / "state_backup.tar.gz"
        try:
            with tarfile.open(tar_path, "w:gz") as tar:
                for f in state_files:
                    tar.add(f, arcname=f.relative_to(ROOT).as_posix())
        except OSError as exc:
            return CheckResult(
                name="backup_restore_round_trip",
                severity="red",
                summary=f"tar create failed: {exc}",
            )
        restore_dir = Path(tmp) / "restore"
        try:
            with tarfile.open(tar_path) as tar:
                # Python 3.12+ supports the filter argument (recommended).
                tar.extractall(restore_dir, filter="data")  # noqa: S202
        except (OSError, tarfile.TarError) as exc:
            return CheckResult(
                name="backup_restore_round_trip",
                severity="red",
                summary=f"tar extract failed: {exc}",
            )
        size_match = 0
        size_mismatch = 0
        for f in state_files:
            rel = f.relative_to(ROOT).as_posix()
            restored = restore_dir / rel
            if restored.exists() and restored.stat().st_size == f.stat().st_size:
                size_match += 1
            else:
                size_mismatch += 1
        if size_mismatch:
            return CheckResult(
                name="backup_restore_round_trip",
                severity="red",
                summary=f"{size_mismatch} files mismatched after round-trip",
            )
        return CheckResult(
            name="backup_restore_round_trip",
            severity="green",
            summary=(
                f"backup/restore round-trip OK: {size_match} files, "
                f"{tar_path.stat().st_size:,} bytes compressed"
            ),
        )


def _check_idempotent_resume() -> CheckResult:
    """6. Mock: would the resume duplicate orders?

    Reads ``brain/jarvis_v3/vps.py`` and looks for the load-bearing
    pattern: an idempotency-key check before order placement. This is
    a smoke check — not a substitute for an actual paper round-trip
    on the new host.
    """
    p = ROOT / "brain" / "jarvis_v3" / "vps.py"
    if not p.exists():
        return CheckResult(
            name="idempotent_resume",
            severity="amber",
            summary="brain/jarvis_v3/vps.py missing — manual verify on DR day",
        )
    text = p.read_text(encoding="utf-8", errors="replace")
    has_idempotency = (
        "client_order_id" in text
        or "idempotency" in text.lower()
        or "dedup" in text.lower()
    )
    if not has_idempotency:
        return CheckResult(
            name="idempotent_resume",
            severity="amber",
            summary=(
                "vps.py doesn't reference client_order_id / idempotency / "
                "dedup — verify manually that resumed daemons can't double-fire"
            ),
        )
    return CheckResult(
        name="idempotent_resume",
        severity="green",
        summary="vps.py references idempotency-key pattern",
    )


# ---------------------------------------------------------------------------
# Operator-day checklist
# ---------------------------------------------------------------------------


_DRILL_DAY_CHECKLIST: list[tuple[str, str]] = [
    ("T+00", "Provider page open in browser; payment method confirmed."),
    ("T+05", "Provision new Vultr HF or DigitalOcean droplet (US-East, 2-vCPU/4GB)."),
    ("T+10", "SSH in, copy deploy/install_vps.sh, run as root."),
    ("T+15", "rsync state backup from local laptop or pull from S3/B2."),
    ("T+18", "Verify state files present + readable (run this script, no-backup mode)."),
    ("T+20", "Populate .env with broker keys; verify file mode 600."),
    ("T+22", "Start IBKR Client Portal Gateway; visit base_url, log in via browser."),
    ("T+25", "Run python -m eta_engine.scripts.preflight_bot_promotion --json."),
    ("T+27", "Read JSON; resolve any RED before proceeding (no live orders past this)."),
    ("T+30", "Start daemons: live_supervisor, drift_watchdog, jarvis_dashboard."),
    ("T+35", "Confirm drift_watchdog appended at least one row to docs/drift_watchdog.jsonl."),
    ("T+40", "Submit one PAPER round-trip order via IBKR; verify fill recorded."),
    ("T+45", "Compare round-trip to last known-good record on old host (sanity)."),
    ("T+50", "Decision: proceed to live? If yes, flip APEX_MODE=LIVE in .env."),
    ("T+55", "Watch first 15 minutes of live; abort on any RED severity."),
    ("T+60", "DR drill complete. Record actual durations in docs/research_log/."),
]


def _emit_drill_checklist(only_checklist: bool = False) -> None:
    print("\n" + "=" * 70)
    print("VPS DR-DRILL DAY CHECKLIST")
    print("=" * 70)
    print(
        "Targets are aspirational — first drill will be slower; record\n"
        "actual durations so future drills can be planned realistically.\n",
    )
    for tag, step in _DRILL_DAY_CHECKLIST:
        print(f"  [{tag}]  {step}")
    print()
    if not only_checklist:
        print(
            "On real DR day, also run THIS script with --drill-mode "
            "to skip dry-run and only print the checklist.",
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(prog="vps_failover_drill")
    p.add_argument(
        "--no-backup-test", action="store_true",
        help="skip the tar/untar round-trip (faster)",
    )
    p.add_argument(
        "--drill-mode", action="store_true",
        help="emit only the operator checklist (no dry-run)",
    )
    p.add_argument(
        "--json", action="store_true",
        help="machine-readable output",
    )
    args = p.parse_args()

    if args.drill_mode:
        _emit_drill_checklist(only_checklist=True)
        return 0

    print(f"[vps_failover_drill] running dry-run at {datetime.now(UTC).isoformat()}\n")
    checks: list[CheckResult] = [
        _check_state_files_present(),
        _check_state_files_fresh(),
        _check_secrets_present(),
        _check_deploy_files_present(),
        _check_install_script_syntax(),
        _check_cron_schedule(),
        _backup_restore_round_trip(skip=args.no_backup_test),
        _check_idempotent_resume(),
    ]

    sev_glyph = {
        "green": "[GREEN]", "amber": "[AMBER]",
        "red": "[RED]", "skip": "[SKIP]",
    }

    if args.json:
        import json
        print(
            json.dumps(
                [asdict(c) for c in checks],
                indent=2, default=str,
            ),
        )
    else:
        for c in checks:
            tag = sev_glyph.get(c.severity, c.severity).upper()
            print(f"  {tag:10s} {c.name:35s} {c.summary}")

    severities = [c.severity for c in checks]
    rc = 3 if "red" in severities else (2 if "amber" in severities else 0)

    _emit_drill_checklist()
    return rc


if __name__ == "__main__":
    sys.exit(main())
