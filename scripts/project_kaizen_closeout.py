"""Project kaizen closeout runner.

This script turns the recurring "finish the project" pass into a repeatable
set of gates: repo hygiene, secrets/health validation, optional targeted tests,
and optional live dashboard probes. It writes the result to the canonical ETA
runtime state directory so the operator surface and future agents can start
from the same truth.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

from eta_engine.scripts.retune_advisory_cache import build_retune_advisory, summarize_active_experiment
from eta_engine.scripts import workspace_roots

_CANONICAL_ROOT = workspace_roots.WORKSPACE_ROOT
_DEFAULT_OUTPUT_DIR = workspace_roots.ETA_RUNTIME_STATE_DIR
_DEFAULT_LIVE_URL = "https://ops.evolutionarytradingalgo.com"
_MAX_CAPTURE = 4_000
_RETUNE_TRUTH_CHECK = "diamond_retune_truth_check_latest.json"
_PUBLIC_RETUNE_TRUTH = "public_diamond_retune_truth_latest.json"
_PUBLIC_BROKER_CLOSE_TRUTH = "public_broker_close_truth_latest.json"
_RUNTIME_OPTIONAL_SUBMODULES = frozenset({"mnq_backtest"})


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    stdout_raw: str = ""
    stderr_raw: str = ""


def _clip(value: str, limit: int = _MAX_CAPTURE) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _dict_field(payload: dict[str, Any] | None, key: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    value = payload.get(key)
    if isinstance(value, dict):
        return value
    return {}


def _string_list(payload: dict[str, Any] | None, key: str) -> list[str]:
    if not isinstance(payload, dict):
        return []
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _retune_advisory(output_dir: Path) -> dict[str, Any]:
    return build_retune_advisory(output_dir / "health")


def _summarize_git_status(raw_status: str) -> dict[str, Any]:
    lines = [line.rstrip() for line in raw_status.splitlines() if line.strip()]
    counts = {
        "modified": 0,
        "added": 0,
        "deleted": 0,
        "renamed": 0,
        "copied": 0,
        "untracked": 0,
        "conflicted": 0,
        "other": 0,
    }
    change_type_preview: dict[str, list[str]] = {name: [] for name in counts}
    groups: dict[str, int] = {}

    for line in lines:
        prefix = line[:2]
        path = _git_status_path(line)
        group = _git_status_group(path)
        groups[group] = groups.get(group, 0) + 1
        if prefix == "??":
            counts["untracked"] += 1
            _append_change_preview(change_type_preview, "untracked", path)
            continue
        if "U" in prefix:
            counts["conflicted"] += 1
            _append_change_preview(change_type_preview, "conflicted", path)
            continue
        if "M" in prefix:
            counts["modified"] += 1
            _append_change_preview(change_type_preview, "modified", path)
            continue
        if "A" in prefix:
            counts["added"] += 1
            _append_change_preview(change_type_preview, "added", path)
            continue
        if "D" in prefix:
            counts["deleted"] += 1
            _append_change_preview(change_type_preview, "deleted", path)
            continue
        if "R" in prefix:
            counts["renamed"] += 1
            _append_change_preview(change_type_preview, "renamed", path)
            continue
        if "C" in prefix:
            counts["copied"] += 1
            _append_change_preview(change_type_preview, "copied", path)
            continue
        counts["other"] += 1
        _append_change_preview(change_type_preview, "other", path)

    preview = [_git_status_path(line) for line in lines[:5]]
    nonzero_counts = {name: value for name, value in counts.items() if value}
    detail_bits = [f"{name}={value}" for name, value in nonzero_counts.items()]
    top_groups = [
        {"group": name, "count": count}
        for name, count in sorted(groups.items(), key=lambda item: (-item[1], item[0]))[:8]
    ]
    detail = f"{len(lines)} dirty entries"
    if detail_bits:
        detail += f" ({', '.join(detail_bits)})"
    if preview:
        detail += f"; preview: {', '.join(preview)}"
    if top_groups:
        groups_text = ", ".join(f"{item['group']}={item['count']}" for item in top_groups[:5])
        detail += f"; top_groups: {groups_text}"

    return {
        "entry_count": len(lines),
        "counts": counts,
        "preview": preview,
        "top_groups": top_groups,
        "change_type_preview": {name: paths for name, paths in change_type_preview.items() if paths},
        "review_action": _dirty_review_action(len(lines), counts),
        "detail": detail,
    }


def _git_status_path(line: str) -> str:
    if len(line) > 3 and line[2] == " ":
        return line[3:]
    if len(line) > 2 and line[1] == " ":
        return line[2:]
    return line.strip()


def _git_status_group(path: str) -> str:
    normalized = path.replace("\\", "/")
    if " -> " in normalized:
        normalized = normalized.rsplit(" -> ", 1)[-1]
    return normalized.split("/", 1)[0] or "root"


def _append_change_preview(preview: dict[str, list[str]], name: str, path: str) -> None:
    if len(preview[name]) < 5:
        preview[name].append(path)


def _dirty_review_action(entry_count: int, counts: dict[str, int]) -> str:
    if entry_count == 0:
        return "none"
    if counts.get("conflicted", 0):
        return "resolve_conflicts_before_gitlink_wiring"
    if entry_count > 50:
        return "split_dirty_worktree_by_group_before_gitlink_wiring"
    if counts.get("untracked", 0):
        return "review_untracked_files_before_gitlink_wiring"
    return "review_dirty_files_before_gitlink_wiring"


def _gate(
    name: str,
    status: str,
    detail: str = "",
    *,
    required: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "status": status,
        "required": required,
        "detail": detail,
    }
    if extra:
        payload["extra"] = extra
    return payload


def _run_command(args: Sequence[str], *, cwd: Path, timeout_s: int) -> CommandResult:
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            list(args),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=proc.returncode,
            stdout=_clip(proc.stdout),
            stderr=_clip(proc.stderr),
            duration_s=round(time.perf_counter() - start, 3),
            stdout_raw=proc.stdout,
            stderr_raw=proc.stderr,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=124,
            stdout=_clip(exc.stdout or ""),
            stderr=_clip(f"timeout after {timeout_s}s\n{exc.stderr or ''}"),
            duration_s=round(time.perf_counter() - start, 3),
            stdout_raw=exc.stdout or "",
            stderr_raw=f"timeout after {timeout_s}s\n{exc.stderr or ''}",
        )
    except OSError as exc:
        return CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=127,
            stdout="",
            stderr=str(exc),
            duration_s=round(time.perf_counter() - start, 3),
            stderr_raw=str(exc),
        )


def _classify_command_exit(name: str, exit_code: int, *, strict_secrets: bool = False) -> str:
    if exit_code == 0:
        return "pass"
    if name == "health_check" and exit_code == 1:
        return "warn"
    if name == "secrets_validator" and not strict_secrets and exit_code == 1:
        return "pass"
    if name == "secrets_validator" and not strict_secrets:
        return "warn"
    return "fail"


def _classify_submodule_status(result: CommandResult) -> tuple[str, str]:
    if result.returncode == 0:
        return "pass", f"exit={result.returncode} duration={result.duration_s}s"

    try:
        payload = json.loads(result.stdout_raw or result.stdout)
    except json.JSONDecodeError:
        return "fail", f"exit={result.returncode} duration={result.duration_s}s"

    modules = payload.get("modules") if isinstance(payload.get("modules"), dict) else {}
    dirty_group_detail = _submodule_dirty_groups_detail(modules)
    blocker_sets: list[tuple[str, tuple[str, ...]]] = []
    for name, raw_module in modules.items():
        module = raw_module if isinstance(raw_module, dict) else {}
        blockers = module.get("blockers") if isinstance(module.get("blockers"), list) else []
        normalized = tuple(str(blocker).strip() for blocker in blockers if str(blocker).strip())
        if normalized:
            blocker_sets.append((str(name), normalized))

    if blocker_sets and all(blockers == ("dirty worktree",) for _name, blockers in blocker_sets):
        modules_text = ", ".join(name for name, _blockers in blocker_sets)
        detail = f"dirty child worktree blocks gitlink wiring ({modules_text}){dirty_group_detail}"
        return "warn", detail

    allowed_integration_blockers = {"dirty worktree", "gitlink diverged"}
    allowed_optional_blockers = {"missing submodule checkout", "gitlink uninitialized"}
    optional_only_modules: list[str] = []
    required_blocker_sets: list[tuple[str, set[str]]] = []
    for name, blockers in blocker_sets:
        blocker_set = set(blockers)
        if name in _RUNTIME_OPTIONAL_SUBMODULES and blocker_set.issubset(allowed_optional_blockers):
            optional_only_modules.append(name)
            continue
        required_blocker_sets.append((name, blocker_set))

    if optional_only_modules and not required_blocker_sets:
        modules_text = ", ".join(optional_only_modules)
        detail = f"runtime-optional submodule missing/uninitialized ({modules_text}){dirty_group_detail}"
        return "warn", detail

    if blocker_sets:
        normalized_sets = [blocker_set for _name, blocker_set in required_blocker_sets]
        if required_blocker_sets and all(blocker_set.issubset(allowed_integration_blockers) for blocker_set in normalized_sets) and any(
            "dirty worktree" in blocker_set for blocker_set in normalized_sets
        ):
            modules_text = ", ".join([name for name, _blockers in required_blocker_sets] + optional_only_modules)
            if optional_only_modules:
                detail = f"dirty/diverged child integration plus optional missing submodule checkout blocks gitlink wiring ({modules_text})"
            else:
                detail = f"dirty/diverged child integration blocks gitlink wiring ({modules_text})"
            detail += dirty_group_detail
            return "warn", detail

    return "fail", f"exit={result.returncode} duration={result.duration_s}s"


def _submodule_dirty_groups_detail(modules: dict[str, Any]) -> str:
    parts: list[str] = []
    for name in sorted(modules):
        module = modules.get(name)
        if not isinstance(module, dict):
            continue
        summary = module.get("dirty_summary")
        if not isinstance(summary, dict):
            dirty_entries = module.get("dirty_entries")
            if isinstance(dirty_entries, list) and dirty_entries:
                summary = _summarize_git_status("\n".join(str(entry) for entry in dirty_entries))
            else:
                summary = {}
        top_groups = summary.get("top_groups")
        if not isinstance(top_groups, list) or not top_groups:
            continue
        group_bits: list[str] = []
        for item in top_groups[:3]:
            if not isinstance(item, dict):
                continue
            group = str(item.get("group", "")).strip()
            count = item.get("count")
            if group and isinstance(count, int):
                group_bits.append(f"{group}={count}")
        if not group_bits:
            continue
        action = str(summary.get("review_action") or "").strip()
        action_text = f" ({action})" if action and action != "none" else ""
        parts.append(f"{name}: {', '.join(group_bits)}{action_text}")
    if not parts:
        return ""
    return "; dirty_groups: " + "; ".join(parts[:3])


def _command_gate(
    name: str,
    args: Sequence[str],
    *,
    cwd: Path,
    timeout_s: int,
    strict_secrets: bool = False,
    warn_on_stdout: bool = False,
) -> dict[str, Any]:
    result = _run_command(args, cwd=cwd, timeout_s=timeout_s)
    detail = f"exit={result.returncode} duration={result.duration_s}s"
    summary: dict[str, Any] | None = None
    if name == "submodule_status":
        status, detail = _classify_submodule_status(result)
    else:
        status = _classify_command_exit(name, result.returncode, strict_secrets=strict_secrets)
    if status == "pass" and warn_on_stdout and result.stdout.strip():
        status = "warn"
    if name == "eta_engine_status" and result.returncode == 0 and (result.stdout_raw or result.stdout).strip():
        summary = _summarize_git_status(result.stdout_raw or result.stdout)
        detail = summary["detail"]
    return _gate(
        name,
        status,
        detail,
        extra={
            "args": result.args,
            "cwd": result.cwd,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_s": result.duration_s,
            "returncode": result.returncode,
            "summary": summary,
        },
    )


def _jarvis_memory_migration_gate(py: str, *, cwd: Path, timeout_s: int) -> dict[str, Any]:
    """Audit whether JARVIS second-brain memory is already canonical."""
    result = _run_command(
        [py, "-m", "eta_engine.scripts.jarvis_memory_migration", "--json"],
        cwd=cwd,
        timeout_s=timeout_s,
    )
    detail = f"exit={result.returncode} duration={result.duration_s}s"
    status = "pass" if result.returncode == 0 else "fail"
    summary: dict[str, Any] | None = None
    if result.returncode == 0:
        try:
            payload = json.loads(result.stdout_raw or result.stdout)
        except json.JSONDecodeError:
            status = "fail"
            detail = f"{detail}; invalid JSON"
        else:
            if isinstance(payload, dict):
                summary = {
                    "status": payload.get("status"),
                    "copy_count": payload.get("copy_count"),
                    "missing_source_count": payload.get("missing_source_count"),
                    "canonical_present_count": payload.get("canonical_present_count"),
                    "dry_run": payload.get("dry_run"),
                }
                detail = (
                    f"status={payload.get('status')} copy_count={payload.get('copy_count')} "
                    f"missing_source_count={payload.get('missing_source_count')} "
                    f"canonical_present_count={payload.get('canonical_present_count')}"
                )
                if payload.get("status") == "needs_migration":
                    status = "warn"
            else:
                status = "fail"
                detail = f"{detail}; expected JSON object"
    return _gate(
        "jarvis_memory_migration",
        status,
        detail,
        extra={
            "args": result.args,
            "cwd": result.cwd,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_s": result.duration_s,
            "returncode": result.returncode,
            "summary": summary,
        },
    )


def _dirty_worktree_reconciliation_gate(py: str, *, cwd: Path, output_dir: Path, timeout_s: int) -> dict[str, Any]:
    output_path = output_dir / "dirty_worktree_reconciliation_latest.json"
    result = _run_command(
        [
            py,
            "-m",
            "eta_engine.scripts.dirty_worktree_reconciliation",
            "--output",
            str(output_path),
            "--summary-json",
            "--top",
            "5",
        ],
        cwd=cwd,
        timeout_s=timeout_s,
    )
    detail = f"exit={result.returncode} duration={result.duration_s}s"
    status = "pass" if result.returncode == 0 else "warn" if result.returncode == 1 else "fail"
    summary: dict[str, Any] | None = None
    if result.stdout_raw or result.stdout:
        try:
            payload = json.loads(result.stdout_raw or result.stdout)
        except json.JSONDecodeError:
            status = "fail"
            detail = f"{detail}; invalid JSON"
        else:
            if isinstance(payload, dict):
                dirty_modules = payload.get("dirty_modules")
                blocking_modules = payload.get("blocking_modules")
                summary = {
                    "action": payload.get("action"),
                    "ready": payload.get("ready"),
                    "dirty_modules": dirty_modules,
                    "blocking_modules": blocking_modules,
                    "output_path": payload.get("output_path"),
                }
                dirty_text = ", ".join(str(item) for item in dirty_modules or [])
                blocking_text = ", ".join(str(item) for item in blocking_modules or [])
                detail = f"action={payload.get('action')} dirty_modules={dirty_text or 'none'}"
                if blocking_text:
                    detail += f" blocking_modules={blocking_text}"
                output = payload.get("output_path")
                if output:
                    detail += f" artifact={output}"
            else:
                status = "fail"
                detail = f"{detail}; expected JSON object"
    return _gate(
        "dirty_worktree_reconciliation",
        status,
        detail,
        extra={
            "args": result.args,
            "cwd": result.cwd,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_s": result.duration_s,
            "returncode": result.returncode,
            "summary": summary,
        },
    )


def _canonical_root_gate(root: Path) -> dict[str, Any]:
    resolved = root.resolve()
    expected = str(_CANONICAL_ROOT).casefold()
    actual = str(resolved).casefold()
    if actual == expected:
        return _gate("canonical_root", "pass", str(resolved))
    return _gate(
        "canonical_root",
        "fail",
        f"expected {_CANONICAL_ROOT}; got {resolved}",
    )


def _fetch_json(url: str, *, timeout_s: int) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, headers={"User-Agent": "eta-kaizen-closeout/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as response:
        status = int(response.status)
        body = response.read().decode("utf-8", errors="replace")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return status, data


def _fetch_json_via_curl(url: str, *, timeout_s: int) -> tuple[int, dict[str, Any]]:
    curl_exe = shutil.which("curl.exe") or shutil.which("curl")
    if not curl_exe:
        raise OSError("curl executable not found")

    marker = "__CURL_STATUS__:"
    proc = subprocess.run(
        [
            curl_exe,
            "-sS",
            "-A",
            "eta-kaizen-closeout/1.0",
            "--max-time",
            str(timeout_s),
            "-w",
            f"\\n{marker}%{{http_code}}",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=max(timeout_s + 5, 10),
    )
    if proc.returncode != 0:
        error = proc.stderr.strip() or proc.stdout.strip() or f"curl exit {proc.returncode}"
        raise OSError(error)

    body, separator, status_text = proc.stdout.rpartition(f"\n{marker}")
    if not separator:
        raise ValueError("curl status marker missing")
    status_code = int(status_text.strip())
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return status_code, data


def _bot_fleet_diagnostics_url(url: str) -> str | None:
    marker = "/api/bot-fleet"
    normalized = url.rstrip("/")
    if not normalized.endswith(marker):
        return None
    return normalized[: -len(marker)] + "/api/dashboard/diagnostics"


def _load_bot_fleet_diagnostics_fallback(url: str, *, timeout_s: int) -> tuple[str, int, dict[str, Any]]:
    diagnostics_url = _bot_fleet_diagnostics_url(url)
    if not diagnostics_url:
        raise ValueError("diagnostics fallback unavailable")
    try:
        status_code, payload = _fetch_json_via_curl(diagnostics_url, timeout_s=min(timeout_s, 20))
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError, TimeoutError, ValueError):
        status_code, payload = _fetch_json(diagnostics_url, timeout_s=timeout_s)
    bot_fleet = payload.get("bot_fleet")
    if not isinstance(bot_fleet, dict):
        raise ValueError("diagnostics bot_fleet payload missing")
    return diagnostics_url, status_code, bot_fleet


def _live_endpoint_gate(name: str, url: str, *, timeout_s: int) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        status_code, data = _fetch_json(url, timeout_s=timeout_s)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
        if name.endswith("bot_fleet"):
            try:
                fallback_url, fallback_status_code, fallback_data = _load_bot_fleet_diagnostics_fallback(
                    url,
                    timeout_s=timeout_s,
                )
            except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as fallback_exc:
                return _gate(
                    name,
                    "fail",
                    str(exc),
                    extra={
                        "url": url,
                        "duration_s": round(time.perf_counter() - start, 3),
                        "fallback_error": str(fallback_exc),
                    },
                )

            duration = round(time.perf_counter() - start, 3)
            truth_status = fallback_data.get("truth_status")
            detail = (
                f"primary unavailable ({exc}); fallback http={fallback_status_code} "
                f"via dashboard diagnostics"
            )
            if truth_status not in {"live", "healthy"}:
                detail += f"; truth_status={truth_status}"
            return _gate(
                name,
                "warn",
                detail,
                extra={
                    "url": url,
                    "duration_s": duration,
                    "status_code": fallback_status_code,
                    "primary_error": str(exc),
                    "fallback": {
                        "url": fallback_url,
                        "status_code": fallback_status_code,
                        "source": "dashboard_diagnostics.bot_fleet",
                    },
                    "summary": {
                        "status": fallback_data.get("status"),
                        "critical_ready": fallback_data.get("critical_ready"),
                        "truth_status": truth_status,
                        "truth_summary_line": fallback_data.get("truth_summary_line"),
                    },
                },
            )
        return _gate(
            name,
            "fail",
            str(exc),
            extra={"url": url, "duration_s": round(time.perf_counter() - start, 3)},
        )

    duration = round(time.perf_counter() - start, 3)
    status = "pass"
    detail = f"http={status_code} duration={duration}s"
    if status_code >= 400:
        status = "fail"
    elif name.endswith("transition") and not data.get("critical_ready"):
        status = "fail"
        detail += "; critical_ready=false"
    elif name.endswith("bot_fleet") and data.get("truth_status") not in {"live", "healthy"}:
        status = "warn"
        detail += f"; truth_status={data.get('truth_status')}"

    return _gate(
        name,
        status,
        detail,
        extra={
            "url": url,
            "duration_s": duration,
            "status_code": status_code,
            "summary": {
                "status": data.get("status"),
                "critical_ready": data.get("critical_ready"),
                "truth_status": data.get("truth_status"),
                "truth_summary_line": data.get("truth_summary_line"),
            },
        },
    )


def _overall_status(gates: list[dict[str, Any]]) -> tuple[str, int]:
    required_failures = [gate for gate in gates if gate["required"] and gate["status"] == "fail"]
    if required_failures:
        return "fail", 2
    warnings = [gate for gate in gates if gate["status"] == "warn"]
    if warnings:
        return "warn", 1
    return "pass", 0


def _write_report(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    snapshot = output_dir / f"kaizen_closeout_{stamp}.json"
    latest = output_dir / "kaizen_closeout_latest.json"
    text = json.dumps(report, indent=2, default=str)
    snapshot.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    return {"snapshot": str(snapshot), "latest": str(latest)}


def run_closeout(
    *,
    root: Path | None = None,
    output_dir: Path | None = None,
    python_exe: str | None = None,
    include_live: bool = False,
    live_url: str = _DEFAULT_LIVE_URL,
    run_tests: bool = True,
    strict_secrets: bool = False,
    timeout_s: int = 120,
) -> dict[str, Any]:
    project_root = (root or Path(__file__).resolve().parents[2]).resolve()
    state_dir = output_dir or _DEFAULT_OUTPUT_DIR
    py = python_exe or sys.executable
    eta_engine = project_root / "eta_engine"

    gates: list[dict[str, Any]] = [_canonical_root_gate(project_root)]
    gates.append(
        _command_gate(
            "eta_engine_diff_check",
            ["git", "-C", str(eta_engine), "diff", "--check"],
            cwd=project_root,
            timeout_s=timeout_s,
            strict_secrets=strict_secrets,
        )
    )
    gates.append(
        _command_gate(
            "eta_engine_status",
            ["git", "-C", str(eta_engine), "status", "--short"],
            cwd=project_root,
            timeout_s=timeout_s,
            strict_secrets=strict_secrets,
            warn_on_stdout=True,
        )
    )
    gates.append(
        _command_gate(
            "submodule_status",
            [py, "-m", "eta_engine.scripts.submodule_wiring_preflight", "--root", str(project_root)],
            cwd=project_root,
            timeout_s=timeout_s,
            strict_secrets=strict_secrets,
        )
    )
    if (eta_engine / "scripts" / "dirty_worktree_reconciliation.py").exists():
        gates.append(
            _dirty_worktree_reconciliation_gate(
                py,
                cwd=project_root,
                output_dir=state_dir,
                timeout_s=timeout_s,
            )
        )
    gates.append(
        _command_gate(
            "secrets_validator",
            [py, "-m", "eta_engine.scripts.secrets_validator", "--json"],
            cwd=project_root,
            timeout_s=timeout_s,
            strict_secrets=strict_secrets,
        )
    )
    if (eta_engine / "scripts" / "jarvis_memory_migration.py").exists():
        gates.append(_jarvis_memory_migration_gate(py, cwd=project_root, timeout_s=timeout_s))
    health_args = [
        py,
        "-m",
        "eta_engine.scripts.health_check",
        "--output-dir",
        str(state_dir / "health"),
    ]
    if include_live:
        health_args.append("--allow-remote-supervisor-truth")
        health_args.append("--allow-remote-retune-truth")
    gates.append(
        _command_gate(
            "health_check",
            health_args,
            cwd=project_root,
            timeout_s=timeout_s,
            strict_secrets=strict_secrets,
        )
    )
    if run_tests:
        gates.append(
            _command_gate(
                "targeted_pytest",
                [
                    py,
                    "-m",
                    "pytest",
                    str(eta_engine / "tests" / "test_project_kaizen_closeout.py"),
                    str(
                        eta_engine
                        / "tests"
                        / "test_jarvis_strategy_supervisor.py::test_env_file_loader_tolerates_non_utf8_bytes",
                    ),
                    "-q",
                ],
                cwd=project_root,
                timeout_s=timeout_s,
                strict_secrets=strict_secrets,
            )
        )
    if include_live:
        base = live_url.rstrip("/")
        gates.extend(
            [
                _live_endpoint_gate("live_health", f"{base}/health", timeout_s=min(timeout_s, 30)),
                _live_endpoint_gate(
                    "live_paper_transition",
                    f"{base}/api/jarvis/paper_live_transition",
                    timeout_s=min(timeout_s, 30),
                ),
                _live_endpoint_gate(
                    "live_bot_fleet",
                    f"{base}/api/bot-fleet",
                    timeout_s=min(timeout_s, 10),
                ),
            ]
        )

    status, exit_code = _overall_status(gates)
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "root": str(project_root),
        "status": status,
        "exit_code": exit_code,
        "strict_secrets": strict_secrets,
        "include_live": include_live,
        "gates": gates,
    }
    report["retune_advisory"] = _retune_advisory(state_dir)
    report["outputs"] = _write_report(report, state_dir)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run ETA project kaizen closeout gates.")
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--live", action="store_true", help="Probe public ops endpoints.")
    parser.add_argument("--live-url", default=_DEFAULT_LIVE_URL)
    parser.add_argument("--strict-secrets", action="store_true", help="Fail when required secrets are not local.")
    parser.add_argument("--skip-tests", action="store_true", help="Skip targeted pytest gate.")
    parser.add_argument("--timeout-s", type=int, default=120)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_closeout(
        output_dir=args.output_dir,
        include_live=args.live,
        live_url=args.live_url,
        run_tests=not args.skip_tests,
        strict_secrets=args.strict_secrets,
        timeout_s=args.timeout_s,
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"kaizen closeout: {report['status']} ({report['outputs']['latest']})")
        advisory = report.get("retune_advisory")
        if isinstance(advisory, dict) and advisory.get("available"):
            focus_realized = advisory.get("focus_total_realized_pnl")
            broker_mtd = advisory.get("broker_mtd_pnl")
            print(
                "  advisory: "
                f"{advisory.get('focus_bot') or 'n/a'} "
                f"closes={advisory.get('focus_closed_trade_count') or 'n/a'} "
                f"pnl=${(focus_realized if focus_realized is not None else 'n/a')} "
                f"mtd=${(broker_mtd if broker_mtd is not None else 'n/a')} "
                f"diag={advisory.get('diagnosis') or 'ok'}"
            )
            experiment = summarize_active_experiment(advisory.get("active_experiment"))
            if experiment:
                print(f"  experiment: {experiment['headline']}")
                print(
                    "             "
                    f"partial_profit_enabled={experiment['partial_profit_enabled_text']} "
                    f"closes={experiment['post_change_closed_trade_count_text']} "
                    f"pnl={experiment['post_change_total_realized_pnl_text']} "
                    f"pf={experiment['post_change_profit_factor_text']}"
                )
        for gate in report["gates"]:
            print(f"  [{gate['status'].upper()}] {gate['name']} - {gate['detail']}")
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
