from __future__ import annotations

import json
from pathlib import Path


def test_dashboard_proxy_watchdog_noops_when_proxy_is_healthy(tmp_path: Path) -> None:
    from eta_engine.scripts import dashboard_proxy_watchdog as wd

    restarts: list[str] = []

    def healthy_probe(**kwargs) -> wd.ProxyProbe:
        return wd.ProxyProbe(
            healthy=True,
            url=kwargs["url"],
            status_code=200,
            reason="ok",
            elapsed_ms=12,
            body_len=4000,
        )

    decision = wd.run_once(
        url="http://127.0.0.1:8421/",
        heartbeat_path=tmp_path / "heartbeat.json",
        restart_delay_s=0,
        probe_fn=healthy_probe,
        restart_fn=lambda task_name: (restarts.append(task_name) or (True, "bad")),
    )

    assert decision.action == "noop"
    assert decision.restart_ok is None
    assert restarts == []

    payload = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert payload["component"] == "dashboard_proxy_watchdog"
    assert payload["decision"]["probe"]["reason"] == "ok"


def test_dashboard_proxy_watchdog_restarts_when_proxy_probe_fails(tmp_path: Path) -> None:
    from eta_engine.scripts import dashboard_proxy_watchdog as wd

    probes = [
        wd.ProxyProbe(
            healthy=False,
            url="http://127.0.0.1:8421/",
            status_code=None,
            reason="probe_error:ConnectionRefused",
            elapsed_ms=5,
        ),
        wd.ProxyProbe(
            healthy=True,
            url="http://127.0.0.1:8421/",
            status_code=200,
            reason="ok",
            elapsed_ms=20,
            body_len=77000,
        ),
    ]
    restart_calls: list[str] = []

    def probe(**_kwargs) -> wd.ProxyProbe:
        return probes.pop(0)

    decision = wd.run_once(
        heartbeat_path=tmp_path / "heartbeat.json",
        restart_delay_s=0.01,
        probe_fn=probe,
        restart_fn=lambda task_name: (restart_calls.append(task_name) or (True, "schtasks_run_ok")),
    )

    assert decision.action == "restarted"
    assert decision.restart_ok is True
    assert decision.restart_reason == "schtasks_run_ok"
    assert decision.post_restart_probe is not None
    assert decision.post_restart_probe.healthy is True
    assert restart_calls == ["ETA-Proxy-8421"]

    payload = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert payload["decision"]["action"] == "restarted"
    assert payload["decision"]["post_restart_probe"]["reason"] == "ok"


def test_dashboard_proxy_watchdog_exit_code_flags_failed_repair() -> None:
    from eta_engine.scripts import dashboard_proxy_watchdog as wd

    decision = wd.ProxyWatchdogDecision(
        checked_at="2026-05-08T00:00:00+00:00",
        action="restart_failed",
        task_name="ETA-Proxy-8421",
        probe=wd.ProxyProbe(
            healthy=False,
            url="http://127.0.0.1:8421/",
            status_code=502,
            reason="bad_gateway_body",
            elapsed_ms=100,
        ),
        restart_ok=False,
        restart_reason="schtasks_rc=1",
    )

    assert wd._exit_code(decision) == 2
