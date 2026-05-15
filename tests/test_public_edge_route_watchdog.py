from __future__ import annotations

import json
from pathlib import Path

GOOD_SUMMARY = {
    "active_bots": 11,
    "runtime_active_bots": 11,
    "running_bots": 4,
    "live_attached_bots": 11,
    "live_in_trade_bots": 4,
    "idle_live_bots": 7,
    "inactive_runtime_bots": 0,
    "staged_bots": 0,
    "truth_status": "live",
}
GOOD_TRUTH_LINE = "Live ETA truth: 11/11 bot heartbeat(s) are fresh; 11 attached, 4 in trade, 7 flat/idle."
STALE_SUMMARY = {
    "active_bots": 0,
    "runtime_active_bots": 0,
    "running_bots": 0,
    "live_attached_bots": None,
    "live_in_trade_bots": None,
    "idle_live_bots": None,
    "inactive_runtime_bots": None,
    "staged_bots": 11,
    "truth_status": "live",
}
STALE_TRUTH_LINE = "Live ETA truth: 11/11 bot heartbeat(s) are fresh."


def _probe(wd, *, url: str, summary: dict[str, object], truth: str, healthy: bool = True):
    return wd.EndpointProbe(
        healthy=healthy,
        url=url,
        status_code=200 if healthy else None,
        reason="ok" if healthy else "probe_error",
        elapsed_ms=12,
        summary=summary,
        truth_summary_line=truth,
    )


def test_public_edge_route_watchdog_noops_when_edge_matches_canonical(tmp_path: Path) -> None:
    from eta_engine.scripts import public_edge_route_watchdog as wd

    repairs: list[dict[str, object]] = []

    def probe(url: str) -> wd.EndpointProbe:
        return _probe(wd, url=url, summary=GOOD_SUMMARY, truth=GOOD_TRUTH_LINE)

    decision = wd.run_once(
        public_url="http://127.0.0.1:8081/api/bot-fleet",
        canonical_url="http://127.0.0.1:8421/api/bot-fleet",
        expected_target="127.0.0.1:8421",
        caddyfile_path=tmp_path / "FirmCommandCenter.Caddyfile",
        heartbeat_path=tmp_path / "heartbeat.json",
        probe_fn=probe,
        inspect_target_fn=lambda _path: ("127.0.0.1:8421", "ok"),
        repair_fn=lambda **kwargs: repairs.append(kwargs) or None,
    )

    assert decision.action == "noop"
    assert decision.route_ok_before is True
    assert decision.route_ok_after is None
    assert decision.mismatch_reasons == []
    assert repairs == []

    payload = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert payload["component"] == "public_edge_route_watchdog"
    assert payload["decision"]["action"] == "noop"


def test_repair_public_edge_route_rewrites_stale_8420_target(tmp_path: Path) -> None:
    from eta_engine.scripts import public_edge_route_watchdog as wd

    caddyfile = tmp_path / "FirmCommandCenter.Caddyfile"
    caddyfile.write_text(
        (
            "{\n"
            "    admin off\n"
            "}\n\n"
            "https://ops.example.com {\n"
            "    reverse_proxy 127.0.0.1:8420 {\n"
            "        flush_interval -1\n"
            "    }\n"
            "}\n"
        ),
        encoding="utf-8",
    )

    validate_calls: list[tuple[Path, Path]] = []
    restart_calls: list[str] = []

    result = wd.repair_public_edge_route(
        caddyfile_path=caddyfile,
        expected_target="127.0.0.1:8421",
        caddy_exe=tmp_path / "caddy.exe",
        service_name="FirmCommandCenterEdge",
        validate_fn=lambda exe, path: validate_calls.append((exe, path)) or (True, "caddy_validate_ok"),
        restart_fn=lambda service_name: restart_calls.append(service_name) or (True, "service_restart_ok"),
    )

    assert result.ok is True
    assert result.changed_caddyfile is True
    assert result.previous_target == "127.0.0.1:8420"
    assert result.current_target == "127.0.0.1:8421"
    assert result.restart_ok is True
    assert result.backup_path is not None
    assert "127.0.0.1:8421" in caddyfile.read_text(encoding="utf-8")
    assert len(validate_calls) == 1
    assert restart_calls == ["FirmCommandCenterEdge"]


def test_public_edge_route_watchdog_repairs_drift_and_confirms_match(tmp_path: Path) -> None:
    from eta_engine.scripts import public_edge_route_watchdog as wd

    public_url = "http://127.0.0.1:8081/api/bot-fleet"
    canonical_url = "http://127.0.0.1:8421/api/bot-fleet"
    caddyfile = tmp_path / "FirmCommandCenter.Caddyfile"
    caddyfile.write_text(
        "https://ops.example.com {\n    reverse_proxy 127.0.0.1:8420 {\n        flush_interval -1\n    }\n}\n",
        encoding="utf-8",
    )

    probes = {
        public_url: [
            _probe(wd, url=public_url, summary=STALE_SUMMARY, truth=STALE_TRUTH_LINE),
            _probe(wd, url=public_url, summary=GOOD_SUMMARY, truth=GOOD_TRUTH_LINE),
        ],
        canonical_url: [
            _probe(wd, url=canonical_url, summary=GOOD_SUMMARY, truth=GOOD_TRUTH_LINE),
            _probe(wd, url=canonical_url, summary=GOOD_SUMMARY, truth=GOOD_TRUTH_LINE),
        ],
    }

    def probe(url: str) -> wd.EndpointProbe:
        return probes[url].pop(0)

    decision = wd.run_once(
        public_url=public_url,
        canonical_url=canonical_url,
        expected_target="127.0.0.1:8421",
        caddyfile_path=caddyfile,
        caddy_exe=tmp_path / "caddy.exe",
        heartbeat_path=tmp_path / "heartbeat.json",
        restart_delay_s=0,
        probe_fn=probe,
        repair_fn=lambda **kwargs: wd.repair_public_edge_route(
            validate_fn=lambda exe, path: (True, "caddy_validate_ok"),
            restart_fn=lambda service_name: (True, "service_restart_ok"),
            **kwargs,
        ),
    )

    assert decision.action == "repaired"
    assert decision.route_ok_before is False
    assert decision.route_ok_after is True
    assert decision.target_before == "127.0.0.1:8420"
    assert decision.target_after == "127.0.0.1:8421"
    assert decision.repair is not None
    assert decision.repair.changed_caddyfile is True
    assert decision.post_public_probe is not None
    assert decision.post_public_probe.summary == GOOD_SUMMARY


def test_public_edge_route_watchdog_exit_code_flags_failed_repair() -> None:
    from eta_engine.scripts import public_edge_route_watchdog as wd

    decision = wd.RouteWatchdogDecision(
        checked_at="2026-05-15T00:00:00+00:00",
        action="repair_failed",
        route_ok_before=False,
        route_ok_after=False,
        expected_target="127.0.0.1:8421",
        target_before="127.0.0.1:8420",
        target_after="127.0.0.1:8420",
        public_probe=_probe(
            wd,
            url="http://127.0.0.1:8081/api/bot-fleet",
            summary=STALE_SUMMARY,
            truth=STALE_TRUTH_LINE,
        ),
        canonical_probe=_probe(
            wd,
            url="http://127.0.0.1:8421/api/bot-fleet",
            summary=GOOD_SUMMARY,
            truth=GOOD_TRUTH_LINE,
        ),
        mismatch_reasons=["summary_mismatch", "route_target:127.0.0.1:8420"],
        repair=wd.RepairResult(
            ok=False,
            changed_caddyfile=True,
            previous_target="127.0.0.1:8420",
            current_target="127.0.0.1:8420",
            restart_ok=False,
            reason="restart_failed",
            restart_reason="service_restart_rc=1",
        ),
    )

    assert wd._exit_code(decision) == 2
