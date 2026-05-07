from __future__ import annotations

from deploy.scripts.process_diagnostics import (
    collect_windows_python_processes,
    duplicate_python_daemons,
    effective_python_processes,
)


def _row(pid: int, parent_pid: int, command_line: str, name: str = "python.exe") -> dict[str, object]:
    return {
        "ProcessId": pid,
        "ParentProcessId": parent_pid,
        "Name": name,
        "CommandLine": command_line,
    }


def test_effective_python_processes_collapses_venv_launcher_parent() -> None:
    rows = [
        _row(
            100,
            1,
            r"C:\EvolutionaryTradingAlgo\.venv\Scripts\python.exe jarvis_live.py --mode paper_live",
        ),
        _row(
            101,
            100,
            r"C:\Program Files\Python312\python.exe jarvis_live.py --mode paper_live",
        ),
    ]

    effective = effective_python_processes(rows)

    assert [row["ProcessId"] for row in effective] == [101]
    assert duplicate_python_daemons(rows, ["jarvis_live"]) == []


def test_duplicate_python_daemons_keeps_independent_duplicate_workers() -> None:
    rows = [
        _row(201, 1, r"C:\Program Files\Python312\python.exe jarvis_live.py --mode paper_live"),
        _row(202, 1, r"C:\Program Files\Python312\python.exe jarvis_live.py --mode paper_live"),
        _row(203, 1, r"C:\Program Files\Python312\python.exe avengers_daemon.py"),
    ]

    assert duplicate_python_daemons(rows, ["jarvis_live", "avengers_daemon"]) == ["jarvis_livex2"]


def test_collect_windows_python_processes_parses_singleton_and_invalid_json() -> None:
    singleton = (
        '{"ProcessId":301,"ParentProcessId":1,"Name":"python.exe",'
        '"CommandLine":"C:\\\\Python312\\\\python.exe jarvis_live.py"}'
    )

    rows = collect_windows_python_processes(lambda _query: singleton)

    assert len(rows) == 1
    assert rows[0]["ProcessId"] == 301
    assert collect_windows_python_processes(lambda _query: "not-json") == []
