"""Process-tree helpers for ETA Windows VPS diagnostics."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

ProcessRow = Mapping[str, object]
PsRunner = Callable[[str], str]


@dataclass(frozen=True)
class ProcessCommandSummary:
    """Summary of command-line uniqueness for a process family."""

    total: int
    unique_commands: int
    duplicate_groups: int
    extra_instances: int


def _field(row: ProcessRow, *names: str) -> object | None:
    for name in names:
        if name in row:
            return row[name]

        name_lower = name.casefold()
        for key, value in row.items():
            if str(key).casefold() == name_lower:
                return value

    return None


def _text_field(row: ProcessRow, *names: str) -> str:
    value = _field(row, *names)
    return value if isinstance(value, str) else ""


def _int_field(row: ProcessRow, *names: str) -> int | None:
    value = _field(row, *names)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalized_command(row: ProcessRow) -> str:
    return " ".join(_text_field(row, "CommandLine", "command_line", "cmdline").split())


def _is_python_process(row: ProcessRow) -> bool:
    name = _text_field(row, "Name", "name").casefold()
    command = _normalized_command(row).casefold()
    return name.startswith("python") or "python.exe" in command or "pythonw.exe" in command


def _is_venv_launcher(row: ProcessRow) -> bool:
    command = _normalized_command(row).replace("\\", "/").casefold()
    return "/scripts/python.exe" in command and ("/.venv/" in command or "/venv/" in command)


def _python_args_signature(row: ProcessRow) -> str:
    return _args_signature(row, ("python.exe", "pythonw.exe"))


def _args_signature(row: ProcessRow, executables: Sequence[str]) -> str:
    command = _normalized_command(row)
    command_lower = command.casefold()

    for executable in executables:
        executable_lower = executable.casefold()
        index = command_lower.find(executable_lower)
        if index != -1:
            args = command[index + len(executable_lower) :].lstrip('" ').strip()
            return " ".join(args.split()).casefold()

    return command_lower


def effective_python_processes(rows: Iterable[ProcessRow]) -> list[ProcessRow]:
    """Collapse Windows venv launcher parents that mirror a real child process."""

    python_rows = [row for row in rows if _is_python_process(row)]
    children_by_parent: dict[int, list[ProcessRow]] = {}
    ignored_parent_pids: set[int] = set()

    for row in python_rows:
        parent_pid = _int_field(row, "ParentProcessId", "parent_process_id", "ppid")
        if parent_pid is not None:
            children_by_parent.setdefault(parent_pid, []).append(row)

    for row in python_rows:
        pid = _int_field(row, "ProcessId", "process_id", "pid")
        if pid is None or not _is_venv_launcher(row):
            continue

        parent_signature = _python_args_signature(row)
        for child in children_by_parent.get(pid, []):
            if _python_args_signature(child) == parent_signature:
                ignored_parent_pids.add(pid)
                break

    return [
        row
        for row in python_rows
        if _int_field(row, "ProcessId", "process_id", "pid") not in ignored_parent_pids
    ]


def duplicate_python_daemons(rows: Iterable[ProcessRow], task_names: Sequence[str]) -> list[str]:
    """Return duplicate daemon labels after launcher/child process collapse."""

    effective_rows = effective_python_processes(rows)
    duplicates: list[str] = []

    for task_name in task_names:
        needle = task_name.casefold()
        count = sum(1 for row in effective_rows if needle in _normalized_command(row).casefold())
        if count > 1:
            duplicates.append(f"{task_name}x{count}")

    return duplicates


def summarize_process_commands(
    rows: Iterable[ProcessRow],
    *,
    process_name: str,
    executables: Sequence[str],
) -> ProcessCommandSummary:
    """Summarize exact command duplication for a named process family."""

    process_name_lower = process_name.casefold()
    matching_rows = [
        row
        for row in rows
        if process_name_lower in _text_field(row, "Name", "name").casefold()
        or process_name_lower in _normalized_command(row).casefold()
    ]
    counts: dict[str, int] = {}

    for row in matching_rows:
        signature = _args_signature(row, executables)
        counts[signature] = counts.get(signature, 0) + 1

    duplicate_groups = sum(1 for count in counts.values() if count > 1)
    extra_instances = sum(count - 1 for count in counts.values() if count > 1)
    return ProcessCommandSummary(
        total=len(matching_rows),
        unique_commands=len(counts),
        duplicate_groups=duplicate_groups,
        extra_instances=extra_instances,
    )


def collect_windows_processes(ps_runner: PsRunner, process_name: str) -> list[ProcessRow]:
    """Collect Win32 process rows through the audit script's PowerShell runner."""

    safe_process_name = process_name.replace("'", "''")
    query = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.Name -eq '{safe_process_name}' }} | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    raw = ps_runner(query).strip()
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []

    if isinstance(parsed, list):
        return [row for row in parsed if isinstance(row, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    return []


def collect_windows_python_processes(ps_runner: PsRunner) -> list[ProcessRow]:
    """Collect python.exe Win32 process rows through the audit script's PowerShell runner."""

    return collect_windows_processes(ps_runner, "python.exe")
