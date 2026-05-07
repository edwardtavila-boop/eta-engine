"""Process-tree helpers for ETA Windows VPS diagnostics."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence

ProcessRow = Mapping[str, object]
PsRunner = Callable[[str], str]


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
    command = _normalized_command(row)
    command_lower = command.casefold()

    for executable in ("python.exe", "pythonw.exe"):
        index = command_lower.find(executable)
        if index != -1:
            args = command[index + len(executable) :].lstrip('" ').strip()
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


def collect_windows_python_processes(ps_runner: PsRunner) -> list[ProcessRow]:
    """Collect python.exe Win32 process rows through the audit script's PowerShell runner."""

    query = (
        "Get-CimInstance Win32_Process -Filter 'Name=\"python.exe\"' | "
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
