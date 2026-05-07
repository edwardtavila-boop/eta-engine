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

from eta_engine.scripts import workspace_roots

_CANONICAL_ROOT = Path(r"C:\EvolutionaryTradingAlgo")
_DEFAULT_OUTPUT_DIR = workspace_roots.ETA_RUNTIME_STATE_DIR
_DEFAULT_LIVE_URL = "https://ops.evolutionarytradingalgo.com"
_MAX_CAPTURE = 4_000


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    duration_s: float


def _clip(value: str, limit: int = _MAX_CAPTURE) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


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
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=124,
            stdout=_clip(exc.stdout or ""),
            stderr=_clip(f"timeout after {timeout_s}s\n{exc.stderr or ''}"),
            duration_s=round(time.perf_counter() - start, 3),
        )
    except OSError as exc:
        return CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=127,
            stdout="",
            stderr=str(exc),
            duration_s=round(time.perf_counter() - start, 3),
        )


def _classify_command_exit(name: str, exit_code: int, *, strict_secrets: bool = False) -> str:
    if exit_code == 0:
        return "pass"
    if name == "health_check" and exit_code == 1:
        return "warn"
    if name == "secrets_validator" and not strict_secrets:
        return "warn"
    return "fail"


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
    status = _classify_command_exit(name, result.returncode, strict_secrets=strict_secrets)
    if status == "pass" and warn_on_stdout and result.stdout.strip():
        status = "warn"
    detail = f"exit={result.returncode} duration={result.duration_s}s"
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


def _live_endpoint_gate(name: str, url: str, *, timeout_s: int) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        status_code, data = _fetch_json(url, timeout_s=timeout_s)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
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
            ["git", "-C", str(project_root), "submodule", "status", "--recursive"],
            cwd=project_root,
            timeout_s=timeout_s,
            strict_secrets=strict_secrets,
            warn_on_stdout=True,
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
    gates.append(
        _command_gate(
            "health_check",
            [py, "-m", "eta_engine.scripts.health_check", "--output-dir", str(state_dir / "health")],
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
                    timeout_s=min(timeout_s, 30),
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
        for gate in report["gates"]:
            print(f"  [{gate['status'].upper()}] {gate['name']} - {gate['detail']}")
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
