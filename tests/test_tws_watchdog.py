from __future__ import annotations

import json
from pathlib import Path


def test_unhealthy_watchdog_status_includes_latest_ibgateway_jvm_oom(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import tws_watchdog

    crash_dir = tmp_path / "ibgateway"
    crash_dir.mkdir()
    (crash_dir / "hs_err_pid16004.log").write_text(
        "\n".join(
            [
                "# There is insufficient memory for the Java Runtime Environment to continue.",
                "# Native memory allocation (malloc) failed to allocate 1065696 bytes.",
                "#  Out of Memory Error (arena.cpp:191), pid=16004, tid=7540",
                "# Command Line: -Xmx768m -XX:ParallelGCThreads=20 -XX:ConcGCThreads=5",
            ],
        ),
        encoding="utf-8",
    )
    status_path = tmp_path / "tws_watchdog.json"
    monkeypatch.setattr(tws_watchdog, "_STATUS_PATH", status_path)
    monkeypatch.setattr(tws_watchdog, "_check_socket", lambda *_args, **_kwargs: False)

    rc = tws_watchdog.main(
        [
            "--host",
            "127.0.0.1",
            "--port",
            "4002",
            "--alert-after",
            "99",
            "--crash-log-dir",
            str(crash_dir),
        ],
    )

    assert rc == 1
    data = json.loads(status_path.read_text(encoding="utf-8"))
    crash = data["details"]["gateway_crash"]
    assert crash["reason_code"] == "jvm_native_memory_oom"
    assert crash["summary"] == "IB Gateway JVM native-memory OOM"
    assert "Native memory allocation" in crash["native_allocation"]
    assert crash["xmx"] == "768m"

