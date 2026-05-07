from __future__ import annotations

from deploy.scripts.process_diagnostics import (
    ProcessCommandSummary,
    collect_windows_processes,
    collect_windows_python_processes,
    duplicate_python_daemons,
    effective_python_processes,
    summarize_process_commands,
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


def test_summarize_process_commands_flags_exact_cloudflared_duplicate() -> None:
    tunnel_command = (
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe tunnel "
        r"--config C:\EvolutionaryTradingAlgo\var\cloudflare\eta-engine-cloudflared.yml run"
    )
    rows = [
        _row(
            401,
            1,
            tunnel_command,
            name="cloudflared.exe",
        ),
        _row(
            402,
            1,
            tunnel_command,
            name="cloudflared.exe",
        ),
    ]

    assert summarize_process_commands(
        rows,
        process_name="cloudflared",
        executables=("cloudflared.exe",),
    ) == ProcessCommandSummary(total=2, unique_commands=1, duplicate_groups=1, extra_instances=1)


def test_summarize_process_commands_allows_distinct_cloudflared_commands() -> None:
    tunnel_command = (
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe tunnel "
        r"--config C:\EvolutionaryTradingAlgo\var\cloudflare\eta-engine-cloudflared.yml run"
    )
    rows = [
        _row(
            501,
            1,
            tunnel_command,
            name="cloudflared.exe",
        ),
        _row(
            502,
            1,
            r"C:\Program Files (x86)\cloudflared\cloudflared.exe tunnel --url http://127.0.0.1:8000 --no-autoupdate",
            name="cloudflared.exe",
        ),
    ]

    assert summarize_process_commands(
        rows,
        process_name="cloudflared",
        executables=("cloudflared.exe",),
    ) == ProcessCommandSummary(total=2, unique_commands=2, duplicate_groups=0, extra_instances=0)


def test_collect_windows_processes_parses_list_payload() -> None:
    raw = (
        '[{"ProcessId":601,"ParentProcessId":1,"Name":"cloudflared.exe",'
        '"CommandLine":"cloudflared.exe tunnel run"},'
        '{"ProcessId":602,"ParentProcessId":1,"Name":"cloudflared.exe",'
        '"CommandLine":"cloudflared.exe tunnel run"}]'
    )

    rows = collect_windows_processes(lambda _query: raw, "cloudflared.exe")

    assert [row["ProcessId"] for row in rows] == [601, 602]
