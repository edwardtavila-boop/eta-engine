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
   - ``var/eta_engine/state/decision_journal.jsonl`` (the audit trail)
   - ``var/eta_engine/state/drift_watchdog.jsonl`` (drift history)
   - ``logs/eta_engine/alerts_log.jsonl`` (alert history)
   - ``logs/eta_engine/runtime_log.jsonl`` (runtime history)
   - ``.env`` (broker keys — checks required key presence without emitting values)
2. **Backup-restore round-trip**: tars the state dir into a temp
   tarball, untars it into a scratch dir, diffs to verify integrity.
3. **Bootstrap-script lint**: runs ``deploy/install_vps.sh`` through
   ``bash -n`` (syntax check) to catch shell bugs before they bite
   on a real DR event.
4. **Cron schedule check**: verifies ``deploy/cron/`` has entries
   for the daemons that must restart on a fresh host (drift_watchdog,
   live_supervisor, etc.) so the operator doesn't forget any.
5. **Idempotency probe** (mock): verifies the live deterministic
   ``client_order_id`` router and the required preflight gate are both
   present. *Mock — does not contact brokers.*

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

from eta_engine.scripts import workspace_roots  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


_STATE_FILES_REQUIRED: list[str] = [
    "docs/strategy_baselines.json",
    "var/eta_engine/state/decision_journal.jsonl",
]
_STATE_FILES_FRESHNESS_EXEMPT: set[str] = {
    "docs/strategy_baselines.json",
}
_STATIC_STATE_FILES_RECOMMENDED: list[str] = []
# .env is special: inspect only key names / non-empty status and never emit values.
_SECRETS_FILE = ".env"

_DEPLOY_FILES_REQUIRED: list[str] = [
    "deploy/install_vps.sh",
    "deploy/HOST_RUNBOOK.md",
    "deploy/README.md",
]

_ENV_EXAMPLE_FILE = ".env.example"
_ENV_COPY_COMMANDS = [
    "python -m eta_engine.scripts.operator_env_bootstrap --create --json",
    "Copy-Item -LiteralPath .env.example -Destination .env",
    'icacls .env /inheritance:r /grant:r "${env:USERNAME}:(R,W)"',
    "notepad .env",
    "cp .env.example .env && chmod 600 .env",
    "$EDITOR .env",
]
_ENV_RUNTIME_REQUIRED_GROUP = {
    "runtime_mode": ["ETA_MODE=PAPER"],
    "jarvis_budget": [
        "ANTHROPIC_API_KEY",
        "JARVIS_HOURLY_USD_BUDGET",
        "JARVIS_DAILY_USD_BUDGET",
    ],
}
_ENV_RUNTIME_REQUIRED_OPTIONS: dict[str, tuple[tuple[str, ...], ...]] = {
    "runtime_mode": (("ETA_MODE",),),
    "jarvis_budget": (
        ("ANTHROPIC_API_KEY",),
        ("JARVIS_HOURLY_USD_BUDGET",),
        ("JARVIS_DAILY_USD_BUDGET",),
    ),
}
_ENV_IBKR_DIRECT_REQUIRED_GROUP = {
    "ibkr_primary": ["IBKR_VENUE_TYPE=paper"],
}
_ENV_IBKR_DIRECT_REQUIRED_OPTIONS: dict[str, tuple[tuple[str, ...], ...]] = {
    "ibkr_primary": (("IBKR_VENUE_TYPE",),),
}
_ENV_IBKR_CLIENT_PORTAL_GROUP = {
    "ibkr_client_portal_sidecars": [
        "IBKR_CP_BASE_URL",
        "IBKR_ACCOUNT_ID",
        "IBKR_SYMBOL_CONID_MAP or IBKR_CONID_<SYMBOL>",
    ],
}
_ENV_IBKR_CLIENT_PORTAL_OPTIONS: dict[str, tuple[tuple[str, ...], ...]] = {
    "ibkr_client_portal_sidecars": (
        ("IBKR_CP_BASE_URL",),
        ("IBKR_ACCOUNT_ID",),
        ("IBKR_SYMBOL_CONID_MAP", "IBKR_CONID_"),
    ),
}
_ENV_IBKR_FULL_REQUIRED_GROUP = {
    "ibkr_primary": [
        "IBKR_VENUE_TYPE=paper",
        "IBKR_CP_BASE_URL",
        "IBKR_ACCOUNT_ID",
        "IBKR_SYMBOL_CONID_MAP or IBKR_CONID_<SYMBOL>",
    ],
}
_ENV_IBKR_FULL_REQUIRED_OPTIONS: dict[str, tuple[tuple[str, ...], ...]] = {
    "ibkr_primary": (
        ("IBKR_VENUE_TYPE",),
        ("IBKR_CP_BASE_URL",),
        ("IBKR_ACCOUNT_ID",),
        ("IBKR_SYMBOL_CONID_MAP", "IBKR_CONID_"),
    ),
}
_ENV_TASTY_RECOMMENDED_GROUP = {
    "tastytrade_fallback": [
        "TASTY_VENUE_TYPE=paper",
        "TASTY_API_BASE_URL",
        "TASTY_ACCOUNT_NUMBER",
        "TASTY_SESSION_TOKEN",
    ],
}
_ENV_TASTY_RECOMMENDED_OPTIONS: dict[str, tuple[tuple[str, ...], ...]] = {
    "tastytrade_fallback": (
        ("TASTY_VENUE_TYPE",),
        ("TASTY_API_BASE_URL",),
        ("TASTY_ACCOUNT_NUMBER",),
        ("TASTY_SESSION_TOKEN",),
    ),
}
_DIRECT_IBKR_ROUTES = {"direct_ibkr", "direct", "ibkr", ""}
_ENV_TEMPLATE_REQUIRED_TOKENS = [
    "ETA_MODE",
    "ANTHROPIC_API_KEY",
    "JARVIS_HOURLY_USD_BUDGET",
    "JARVIS_DAILY_USD_BUDGET",
    "IBKR_VENUE_TYPE",
    "IBKR_CP_BASE_URL",
    "IBKR_ACCOUNT_ID",
    "IBKR_SYMBOL_CONID_MAP",
    "TASTY_VENUE_TYPE",
    "TASTY_API_BASE_URL",
    "TASTY_ACCOUNT_NUMBER",
    "TASTY_SESSION_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]
_VPS_BASH_VALIDATION_COMMANDS = [
    "cd ~/eta_engine && bash -n deploy/install_vps.sh",
    "cd ~/eta_engine && .venv/bin/python -m eta_engine.scripts.vps_failover_drill --no-backup-test --json",
]
_WINDOWS_BASH_FALLBACKS = (
    Path("C:/Program Files/Git/bin/bash.exe"),
    Path("C:/Program Files/Git/usr/bin/bash.exe"),
    Path("C:/Program Files (x86)/Git/bin/bash.exe"),
    Path("C:/Program Files (x86)/Git/usr/bin/bash.exe"),
    Path("C:/msys64/usr/bin/bash.exe"),
)

_IDEMPOTENCY_EVIDENCE_FILES: list[tuple[str, Path, tuple[str, ...]]] = [
    (
        "deterministic_router",
        ROOT / "scripts" / "live_supervisor.py",
        (
            "_ensure_client_order_id",
            "client_order_id",
            "idempotent_order_id",
            "hashlib.sha256",
        ),
    ),
    (
        "required_preflight_gate",
        ROOT / "scripts" / "live_tiny_preflight_dryrun.py",
        (
            "_gate_idempotent_order_id",
            "JarvisAwareRouter._ensure_client_order_id",
            "client_order_id",
            "same coid",
        ),
    ),
]


def _display_path(path: Path) -> str:
    """Return a stable workspace-relative display path when possible."""
    workspace_root = ROOT.parent
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return str(path)


def _state_file_paths() -> tuple[list[tuple[str, Path]], list[tuple[str, Path]]]:
    """Return required/recommended DR state files as (label, path) pairs."""
    required: list[tuple[str, Path]] = []
    for rel in _STATE_FILES_REQUIRED:
        if rel == "var/eta_engine/state/decision_journal.jsonl":
            required.append(
                (
                    _display_path(workspace_roots.ETA_RUNTIME_DECISION_JOURNAL_PATH),
                    workspace_roots.ETA_RUNTIME_DECISION_JOURNAL_PATH,
                )
            )
        else:
            required.append((rel, ROOT / rel))
    recommended = [(rel, ROOT / rel) for rel in _STATIC_STATE_FILES_RECOMMENDED]
    recommended.extend(
        [
            (
                _display_path(workspace_roots.ETA_DRIFT_WATCHDOG_LOG_PATH),
                workspace_roots.ETA_DRIFT_WATCHDOG_LOG_PATH,
            ),
            (
                _display_path(workspace_roots.default_alerts_log_path()),
                workspace_roots.default_alerts_log_path(),
            ),
            (_display_path(workspace_roots.ETA_RUNTIME_LOG_PATH), workspace_roots.ETA_RUNTIME_LOG_PATH),
        ]
    )
    return required, recommended


def _archive_name(path: Path) -> str:
    """Archive state files relative to the canonical workspace root."""
    workspace_root = ROOT.parent
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return path.name


def _env_readiness_details() -> dict[str, Any]:
    """Return operator guidance for populating .env without exposing secrets."""
    route = "direct_ibkr"
    required_groups, recommended_groups = _env_readiness_groups(route)
    example_path = ROOT / _ENV_EXAMPLE_FILE
    return {
        "env_path": _display_path(ROOT / _SECRETS_FILE),
        "template": _display_path(example_path),
        "template_exists": example_path.exists(),
        "copy_command": f"cp {_ENV_EXAMPLE_FILE} .env && chmod 600 .env",
        "copy_commands": list(_ENV_COPY_COMMANDS),
        "active_brokers": ["IBKR", "Tastytrade"],
        "dormant_brokers": ["Tradovate"],
        "paper_live_route": route,
        "required_groups": required_groups,
        "recommended_groups": recommended_groups,
        "note": "populate real values only; the DR drill inspects key names/non-empty status and never emits values",
    }


def _read_env_values(path: Path) -> dict[str, str]:
    """Return env key -> cleaned raw value without exposing secrets elsewhere."""
    env_values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        env_values[key] = _clean_env_value(value)
    return env_values


def _env_key_state(env_values: dict[str, str]) -> dict[str, bool]:
    """Return env key -> has non-empty value without exposing values."""
    return {key: bool(value) for key, value in env_values.items()}


def _clean_env_value(value: str) -> str:
    """Normalize an env-file value for presence checks without exposing it."""
    cleaned = value.strip()
    if cleaned.startswith('"') and cleaned.endswith('"') and len(cleaned) >= 2:
        return cleaned[1:-1].strip()
    if cleaned.startswith("'") and cleaned.endswith("'") and len(cleaned) >= 2:
        return cleaned[1:-1].strip()
    if "#" in cleaned:
        cleaned = cleaned.split("#", 1)[0].rstrip()
    return cleaned.strip()


def _env_option_satisfied(key_state: dict[str, bool], options: tuple[str, ...]) -> bool:
    """Return True when any exact key or prefix option is populated."""
    for option in options:
        if option.endswith("_"):
            if any(key.startswith(option) and has_value for key, has_value in key_state.items()):
                return True
        elif key_state.get(option):
            return True
    return False


def _missing_env_requirements(
    key_state: dict[str, bool],
    groups: dict[str, tuple[tuple[str, ...], ...]],
) -> dict[str, list[str]]:
    """Return display-safe missing key requirements by group."""
    missing: dict[str, list[str]] = {}
    for group, requirements in groups.items():
        group_missing = [
            " or ".join(options)
            for options in requirements
            if not _env_option_satisfied(key_state, options)
        ]
        if group_missing:
            missing[group] = group_missing
    return missing


def _paper_live_route(env_values: dict[str, str]) -> str:
    """Return the supervisor paper-live route implied by .env contents."""
    route = str(env_values.get("ETA_PAPER_LIVE_ORDER_ROUTE") or "direct_ibkr").strip().lower()
    return route or "direct_ibkr"


def _env_readiness_groups(route: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return display-safe required/recommended groups for ``route``."""
    required = dict(_ENV_RUNTIME_REQUIRED_GROUP)
    recommended = dict(_ENV_TASTY_RECOMMENDED_GROUP)
    if route in _DIRECT_IBKR_ROUTES:
        required.update(_ENV_IBKR_DIRECT_REQUIRED_GROUP)
        recommended.update(_ENV_IBKR_CLIENT_PORTAL_GROUP)
    else:
        required.update(_ENV_IBKR_FULL_REQUIRED_GROUP)
    return required, recommended


def _env_option_groups(
    route: str,
) -> tuple[dict[str, tuple[tuple[str, ...], ...]], dict[str, tuple[tuple[str, ...], ...]]]:
    """Return required/recommended key-option groups for ``route``."""
    required = dict(_ENV_RUNTIME_REQUIRED_OPTIONS)
    recommended = dict(_ENV_TASTY_RECOMMENDED_OPTIONS)
    if route in _DIRECT_IBKR_ROUTES:
        required.update(_ENV_IBKR_DIRECT_REQUIRED_OPTIONS)
        recommended.update(_ENV_IBKR_CLIENT_PORTAL_OPTIONS)
    else:
        required.update(_ENV_IBKR_FULL_REQUIRED_OPTIONS)
    return required, recommended


def _vps_bash_validation_details(
    *,
    reason: str | None = None,
    local_shell_path: str | None = None,
    launcher_failures: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Return the exact remote validation commands for install_vps.sh."""
    details: dict[str, Any] = {
        "script": "deploy/install_vps.sh",
        "vps_commands": list(_VPS_BASH_VALIDATION_COMMANDS),
        "local_shell": "bash",
    }
    if local_shell_path:
        details["local_shell_path"] = local_shell_path
    if launcher_failures:
        details["launcher_failures"] = launcher_failures
    if reason:
        details["reason"] = reason
    return details


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
    required, recommended = _state_file_paths()
    for label, p in required:
        if not p.exists():
            missing_required.append(label)
        else:
            sizes[label] = p.stat().st_size
    for label, p in recommended:
        if not p.exists():
            missing_recommended.append(label)
        else:
            sizes[label] = p.stat().st_size

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
    """1b. Dynamic state files have been touched within the last 24h."""
    stale: list[tuple[str, float]] = []
    now = datetime.now(UTC).timestamp()
    required, recommended = _state_file_paths()
    for label, p in required + recommended:
        if label in _STATE_FILES_FRESHNESS_EXEMPT:
            continue
        if not p.exists():
            continue
        age_h = (now - p.stat().st_mtime) / 3600
        if age_h > 24:
            stale.append((label, age_h))
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
    """1c. .env exists and required launch keys are populated."""
    p = ROOT / _SECRETS_FILE
    if not p.exists():
        return CheckResult(
            name="secrets_present",
            severity="amber",
            summary=(
                ".env missing — operator must populate broker keys "
                "before flipping live"
            ),
            details=_env_readiness_details(),
        )
    details = _env_readiness_details()
    try:
        env_values = _read_env_values(p)
        key_state = _env_key_state(env_values)
    except OSError as exc:
        return CheckResult(
            name="secrets_present",
            severity="amber",
            summary=f".env exists but could not be inspected safely: {exc}",
            details={**details, "inspection_error": type(exc).__name__},
        )
    route = _paper_live_route(env_values)
    required_groups, recommended_groups = _env_readiness_groups(route)
    required_options, recommended_options = _env_option_groups(route)
    missing_required = _missing_env_requirements(key_state, required_options)
    missing_recommended = _missing_env_requirements(key_state, recommended_options)
    inspected_details = {
        **details,
        "paper_live_route": route,
        "required_groups": required_groups,
        "recommended_groups": recommended_groups,
        "required_missing": missing_required,
        "recommended_missing": missing_recommended,
        "inspected_keys_only": True,
        "values_emitted": False,
    }
    if missing_required:
        missing_count = sum(len(values) for values in missing_required.values())
        return CheckResult(
            name="secrets_present",
            severity="amber",
            summary=(
                f".env exists but {missing_count} required launch key(s) are "
                "missing or empty (values not emitted)"
            ),
            details=inspected_details,
        )
    return CheckResult(
        name="secrets_present",
        severity="green",
        summary=f".env required launch keys are populated ({p.stat().st_size} bytes; values not emitted)",
        details=inspected_details,
    )


def _check_env_template_complete() -> CheckResult:
    """1d. .env.example names every key required by failover guidance."""
    p = ROOT / _ENV_EXAMPLE_FILE
    if not p.exists():
        return CheckResult(
            name="env_template_complete",
            severity="red",
            summary=".env.example missing",
            details={"template": _display_path(p)},
        )
    text = p.read_text(encoding="utf-8", errors="replace")
    missing = [token for token in _ENV_TEMPLATE_REQUIRED_TOKENS if token not in text]
    if missing:
        return CheckResult(
            name="env_template_complete",
            severity="red",
            summary=f".env.example missing failover key(s): {missing}",
            details={"missing": missing, "template": _display_path(p)},
        )
    return CheckResult(
        name="env_template_complete",
        severity="green",
        summary=f".env.example covers {len(_ENV_TEMPLATE_REQUIRED_TOKENS)} failover keys",
        details={"required_tokens": list(_ENV_TEMPLATE_REQUIRED_TOKENS)},
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
    bash_candidates = _bash_candidates()
    if not bash_candidates:
        return CheckResult(
            name="install_script_syntax",
            severity="amber",
            summary=(
                "bash not on PATH; cannot syntax-check install_vps.sh "
                "locally. The CI pipeline / VPS itself will validate it."
            ),
            details=_vps_bash_validation_details(reason="bash_not_on_path"),
        )
    launcher_failures: list[dict[str, str]] = []
    script_arg = "deploy/install_vps.sh"
    for bash in bash_candidates:
        try:
            result = subprocess.run(  # noqa: S603 -- localhost bash, fixed args
                [bash, "-n", script_arg],
                check=False,
                capture_output=True,
                cwd=ROOT,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            launcher_failures.append({"path": bash, "reason": type(exc).__name__})
            continue
        output = _clean_process_output(result.stdout, result.stderr)
        if result.returncode != 0 and _is_bash_launcher_unavailable(output):
            launcher_failures.append(
                {"path": bash, "reason": "local_bash_launcher_unavailable"},
            )
            continue
        if result.returncode != 0:
            return CheckResult(
                name="install_script_syntax",
                severity="red",
                summary=f"bash -n found errors: {output[:200]}",
                details={
                    **_vps_bash_validation_details(
                        reason="syntax_error",
                        local_shell_path=bash,
                        launcher_failures=launcher_failures,
                    ),
                    "output": output[:500],
                },
            )
        return CheckResult(
            name="install_script_syntax",
            severity="green",
            summary="install_vps.sh syntax-clean",
            details=_vps_bash_validation_details(
                local_shell_path=bash,
                launcher_failures=launcher_failures,
            ),
        )
    return CheckResult(
        name="install_script_syntax",
        severity="amber",
        summary=(
            "bash launchers were found but none could run scripts locally. "
            "Validate deploy/install_vps.sh on the VPS or a shell with bash installed."
        ),
        details=_vps_bash_validation_details(
            reason="local_bash_launcher_unavailable",
            launcher_failures=launcher_failures,
        ),
    )


def _bash_candidates() -> list[str]:
    """Return unique bash executables, preferring PATH then Git Bash fallbacks."""
    candidates: list[str] = []
    path_bash = shutil.which("bash")
    if path_bash:
        candidates.append(path_bash)
    candidates.extend(str(path) for path in _WINDOWS_BASH_FALLBACKS if path.exists())
    return list(dict.fromkeys(candidates))


def _clean_process_output(*chunks: str) -> str:
    """Normalize subprocess output for concise operator messages."""
    text = "\n".join(chunk for chunk in chunks if chunk)
    return text.replace("\x00", "").strip()


def _is_bash_launcher_unavailable(output: str) -> bool:
    """Detect Windows bash launchers that never reached shell parsing."""
    lowered = output.lower()
    return (
        "windows subsystem for linux has no installed distributions" in lowered
        or "wsl.exe --install" in lowered
        or "install a distribution" in lowered
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
    required, recommended = _state_file_paths()
    state_files = [path for _, path in required + recommended if path.exists()]
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
                    tar.add(f, arcname=_archive_name(f))
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
            rel = _archive_name(f)
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


def _idempotency_evidence() -> tuple[list[dict[str, Any]], list[str]]:
    """Return proof files that cover restart-safe order dedup evidence."""
    evidence: list[dict[str, Any]] = []
    missing: list[str] = []
    for label, path, tokens in _IDEMPOTENCY_EVIDENCE_FILES:
        if not path.exists():
            missing.append(f"{label}: missing {_display_path(path)}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        missing_tokens = [token for token in tokens if token not in text]
        if missing_tokens:
            missing.append(f"{label}: missing token(s) {', '.join(missing_tokens)}")
            continue
        evidence.append(
            {
                "label": label,
                "path": _display_path(path),
                "tokens": list(tokens),
            }
        )
    return evidence, missing


def _check_idempotent_resume() -> CheckResult:
    """6. Mock: would the resume duplicate orders?

    Static smoke check: verifies the live deterministic client-order-id router
    and the required preflight gate are both present. This is not a substitute
    for an actual paper round-trip on the new host.
    """
    # The live order path owns idempotency; JARVIS VPS admin actions do not
    # place orders, so the DR drill checks the order router plus preflight gate.
    evidence, missing = _idempotency_evidence()
    if missing:
        return CheckResult(
            name="idempotent_resume",
            severity="amber",
            summary=(
                "idempotent resume evidence incomplete -- verify manually that "
                "resumed daemons cannot double-fire"
            ),
            details={"evidence": evidence, "missing": missing},
        )
    return CheckResult(
        name="idempotent_resume",
        severity="green",
        summary=(
            "idempotent resume covered by live deterministic order-id router "
            "+ required preflight gate"
        ),
        details={"evidence": evidence},
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
    (
        "T+35",
        "Confirm drift_watchdog appended at least one row to "
        "var/eta_engine/state/drift_watchdog.jsonl.",
    ),
    ("T+40", "Submit one PAPER round-trip order via IBKR; verify fill recorded."),
    ("T+45", "Compare round-trip to last known-good record on old host (sanity)."),
    ("T+50", "Decision: proceed to live? If yes, flip ETA_MODE=LIVE in .env."),
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


def collect_checks(*, skip_backup_test: bool = False) -> list[CheckResult]:
    """Run the local DR checks without printing operator text."""
    return [
        _check_state_files_present(),
        _check_state_files_fresh(),
        _check_secrets_present(),
        _check_env_template_complete(),
        _check_deploy_files_present(),
        _check_install_script_syntax(),
        _check_cron_schedule(),
        _backup_restore_round_trip(skip=skip_backup_test),
        _check_idempotent_resume(),
    ]


def exit_code_for_checks(checks: list[CheckResult]) -> int:
    """Return the process exit code implied by the check severities."""
    severities = [c.severity for c in checks]
    return 3 if "red" in severities else (2 if "amber" in severities else 0)


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
    checks = collect_checks(skip_backup_test=args.no_backup_test)

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

    rc = exit_code_for_checks(checks)

    _emit_drill_checklist()
    return rc


if __name__ == "__main__":
    sys.exit(main())
