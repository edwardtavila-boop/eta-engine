"""Tests for health_dashboard — unified status across all log surfaces."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eta_engine.scripts import health_dashboard as hd


@pytest.fixture()
def isolated_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(hd, "LOG_DIR", tmp_path)
    # Re-bind every SOURCES entry to the tmp dir
    new_sources = {k: tmp_path / v.name for k, v in hd.SOURCES.items()}
    monkeypatch.setattr(hd, "SOURCES", new_sources)
    return tmp_path


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


# ── helpers ───────────────────────────────────────────────────────


def test_age_str_seconds() -> None:
    ts = datetime.now(UTC).isoformat()
    assert hd._age_str(ts) in {"0s", "1s"}


def test_age_str_minutes() -> None:
    ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    assert "m" in hd._age_str(ts)


def test_age_str_hours() -> None:
    ts = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    assert "h" in hd._age_str(ts)


def test_age_str_days() -> None:
    ts = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    assert "d" in hd._age_str(ts)


def test_age_str_unknown() -> None:
    assert hd._age_str(None) == "?"
    assert hd._age_str("garbage") == "?"
    assert hd._age_str([]) == "?"  # type: ignore[arg-type]


def test_age_str_handles_epoch_float() -> None:
    """Mixed-shape alert log: some records have unix-epoch floats."""
    epoch = (datetime.now(UTC) - timedelta(hours=1)).timestamp()
    assert "h" in hd._age_str(epoch)


def test_age_str_handles_epoch_int() -> None:
    epoch_int = int((datetime.now(UTC) - timedelta(minutes=10)).timestamp())
    assert "m" in hd._age_str(epoch_int)


def test_verdict_emoji_known() -> None:
    assert hd._verdict_emoji("GREEN") == "[OK]"
    assert hd._verdict_emoji("RED") == "[!!]"
    assert hd._verdict_emoji("CRITICAL") == "[XX]"
    assert hd._verdict_emoji("info") == "[OK]"


def test_verdict_emoji_unknown() -> None:
    assert hd._verdict_emoji("NEW_LEVEL") == "[?]"


def test_alert_render_helpers_handle_event_payload_shape() -> None:
    alert = {
        "event": "kill_switch_latched",
        "level": "critical",
        "payload": {
            "verdict": {
                "action": "PAUSE_NEW_ENTRIES",
                "reason": "paper loss guard",
            },
        },
    }

    assert hd._alert_level(alert) == "CRITICAL"
    assert hd._alert_source(alert) == "kill_switch_latched"
    assert hd._alert_message(alert) == "PAUSE_NEW_ENTRIES: paper loss guard"


def test_alert_render_helpers_summarize_runtime_payload() -> None:
    alert = {
        "event": "runtime_start",
        "level": "info",
        "payload": {"active_bots": ["mnq"], "live": False},
    }

    assert hd._alert_message(alert) == "active_bots=['mnq'] live=False"


def test_alert_helpers_classify_consistency_status_payload() -> None:
    violation = {
        "event": "consistency_status",
        "level": "unknown",
        "payload": {"status": "VIOLATION", "largest_day_ratio": 0.91},
    }
    warning = {
        "event": "consistency_status",
        "level": "unknown",
        "payload": {"status": "WARNING", "largest_day_ratio": 0.28},
    }

    assert hd._alert_level(violation) == "RED"
    assert hd._alert_message(violation) == "30% consistency VIOLATION"
    assert hd._alert_level(warning) == "WARN"
    assert hd._alert_message(warning) == "30% consistency WARNING"


def test_alert_level_classifies_kill_switch_latched_as_critical() -> None:
    alert = {
        "event": "kill_switch_latched",
        "level": "unknown",
        "payload": {"reason": "apex cushion 200 <= preempt 400"},
    }

    assert hd._alert_level(alert) == "CRITICAL"
    assert hd._alert_message(alert) == "apex cushion 200 <= preempt 400"


def test_alert_message_normalizes_consistency_circuit_trip_detail() -> None:
    alert = {
        "event": "circuit_trip",
        "level": "WARN",
        "payload": {
            "verdict": {
                "action": "PAUSE_NEW_ENTRIES",
                "reason": ("apex 30% consistency VIOLATION: largest day 250.0 exceeds max 75.0"),
            },
        },
    }

    assert hd._alert_message(alert) == "PAUSE_NEW_ENTRIES: apex 30% consistency VIOLATION"


def test_alert_message_uses_title_when_body_is_placeholder() -> None:
    alert = {
        "title": "broker ibkr YELLOW",
        "body": "x",
    }

    assert hd._alert_message(alert) == "broker ibkr YELLOW"


def test_alert_message_normalizes_broker_credential_aliases() -> None:
    variants = ["creds", "creds missing", "missing creds"]

    rendered = {
        hd._alert_message(
            {
                "source": "broker-session-monitor",
                "title": "broker ibkr YELLOW",
                "body": variant,
            }
        )
        for variant in variants
    }

    assert rendered == {"broker ibkr YELLOW: credentials missing"}


def test_render_text_groups_broker_credential_aliases(
    isolated_logs: Path,
) -> None:
    now = datetime.now(UTC)
    _write_jsonl(
        hd.SOURCES["alerts"],
        [
            {
                "ts": (now - timedelta(minutes=5)).timestamp(),
                "level": "WARN",
                "source": "broker-session-monitor",
                "title": "broker ibkr YELLOW",
                "body": "creds",
            },
            {
                "ts": (now - timedelta(minutes=4)).timestamp(),
                "level": "WARN",
                "source": "broker-session-monitor",
                "title": "broker ibkr YELLOW",
                "body": "creds missing",
            },
            {
                "ts": (now - timedelta(minutes=3)).timestamp(),
                "level": "WARN",
                "source": "broker-session-monitor",
                "title": "broker ibkr YELLOW",
                "body": "missing creds",
            },
        ],
    )

    text = hd.render_text(hd.build_dashboard())

    assert text.count("credentials missing") == 1
    assert "x3" in text


def test_render_text_drops_generic_alert_when_specific_detail_exists(
    isolated_logs: Path,
) -> None:
    now = datetime.now(UTC)
    _write_jsonl(
        hd.SOURCES["alerts"],
        [
            {
                "ts": (now - timedelta(minutes=5)).timestamp(),
                "level": "WARN",
                "source": "broker-session-monitor",
                "title": "broker ibkr YELLOW",
                "body": "x",
            },
            {
                "ts": (now - timedelta(minutes=4)).timestamp(),
                "level": "WARN",
                "source": "broker-session-monitor",
                "title": "broker ibkr YELLOW",
                "body": "credentials missing",
            },
            {
                "ts": (now - timedelta(minutes=3)).timestamp(),
                "level": "WARN",
                "source": "broker-session-monitor",
                "title": "broker tastytrade YELLOW",
                "body": "x",
            },
        ],
    )

    d = hd.build_dashboard()
    groups = hd._recent_alert_groups(d["recent_alerts"])
    messages = [group["message"] for group in groups]
    text = hd.render_text(d)

    assert "broker ibkr YELLOW: credentials missing" in text
    assert "broker tastytrade YELLOW" in text
    assert "broker ibkr YELLOW" not in messages
    assert "broker ibkr YELLOW: credentials missing" in messages


def test_render_text_groups_consistency_circuit_trip_variants(
    isolated_logs: Path,
) -> None:
    now = datetime.now(UTC)
    _write_jsonl(
        hd.SOURCES["alerts"],
        [
            {
                "ts": (now - timedelta(minutes=6)).timestamp(),
                "level": "WARN",
                "event": "circuit_trip",
                "payload": {
                    "verdict": {
                        "action": "PAUSE_NEW_ENTRIES",
                        "reason": "apex 30% consistency VIOLATION: largest day 250",
                    },
                },
            },
            {
                "ts": (now - timedelta(minutes=5)).timestamp(),
                "level": "WARN",
                "event": "circuit_trip",
                "payload": {
                    "verdict": {
                        "action": "PAUSE_NEW_ENTRIES",
                        "reason": "apex 30% consistency VIOLATION: largest day 1000",
                    },
                },
            },
        ],
    )

    groups = hd._recent_alert_groups(hd.build_dashboard()["recent_alerts"])
    messages = [group["message"] for group in groups]

    assert messages == ["PAUSE_NEW_ENTRIES: apex 30% consistency VIOLATION"]
    assert groups[0]["count"] == 2


def test_render_text_prioritizes_actionable_alert_groups(
    isolated_logs: Path,
) -> None:
    now = datetime.now(UTC)
    _write_jsonl(
        hd.SOURCES["alerts"],
        [
            {
                "ts": (now - timedelta(minutes=10)).timestamp(),
                "level": "INFO",
                "source": "runtime_start",
                "payload": {"active_bots": ["mnq"], "live": False},
            },
            {
                "ts": (now - timedelta(minutes=9)).timestamp(),
                "level": "INFO",
                "source": "runtime_stop",
                "payload": {"bars": 2},
            },
            {
                "ts": (now - timedelta(minutes=2)).timestamp(),
                "level": "WARN",
                "source": "broker-session-monitor",
                "title": "broker ibkr YELLOW",
                "body": "credentials missing",
            },
        ],
    )

    text = hd.render_text(hd.build_dashboard())

    assert "broker ibkr YELLOW: credentials missing" in text
    assert "runtime_start" not in text
    assert "runtime_stop" not in text


def test_render_text_groups_duplicate_recent_alerts(isolated_logs: Path) -> None:
    now = datetime.now(UTC)
    _write_jsonl(
        hd.SOURCES["alerts"],
        [
            {
                "ts": (now - timedelta(minutes=5)).timestamp(),
                "level": "WARN",
                "source": "broker-session-monitor",
                "title": "broker ibkr YELLOW",
                "body": "x",
            },
            {
                "ts": (now - timedelta(minutes=4)).timestamp(),
                "level": "WARN",
                "source": "broker-session-monitor",
                "title": "broker ibkr YELLOW",
                "body": "x",
            },
            {
                "ts": (now - timedelta(minutes=3)).timestamp(),
                "level": "WARN",
                "source": "broker-session-monitor",
                "title": "broker ibkr YELLOW",
                "body": "x",
            },
            {
                "ts": (now - timedelta(minutes=2)).timestamp(),
                "level": "CRITICAL",
                "source": "broker-session-monitor",
                "title": "broker tastytrade RED",
                "body": "500",
            },
        ],
    )

    text = hd.render_text(hd.build_dashboard())

    assert text.count("broker ibkr YELLOW") == 1
    assert "x3" in text
    assert "broker tastytrade RED" in text


# ── _last_jsonl_record ────────────────────────────────────────────


def test_last_jsonl_record_missing(isolated_logs: Path) -> None:
    assert hd._last_jsonl_record(isolated_logs / "nope.jsonl") is None


def test_last_jsonl_record_returns_last_line(isolated_logs: Path) -> None:
    p = isolated_logs / "test.jsonl"
    _write_jsonl(p, [{"a": 1}, {"a": 2}, {"a": 3}])
    assert hd._last_jsonl_record(p) == {"a": 3}


def test_last_jsonl_record_skips_empty_lines(isolated_logs: Path) -> None:
    p = isolated_logs / "test.jsonl"
    p.write_text(json.dumps({"a": 1}) + "\n\n\n", encoding="utf-8")
    assert hd._last_jsonl_record(p) == {"a": 1}


# ── _read_recent_alerts ───────────────────────────────────────────


def test_read_recent_alerts_filters_by_hours(isolated_logs: Path) -> None:
    p = isolated_logs / "alerts.jsonl"
    now = datetime.now(UTC)
    _write_jsonl(
        p,
        [
            {
                "timestamp_utc": (now - timedelta(hours=48)).isoformat(),
                "source": "old",
                "level": "RED",
                "message": "way old",
            },
            {
                "timestamp_utc": (now - timedelta(hours=2)).isoformat(),
                "source": "recent",
                "level": "YELLOW",
                "message": "recent",
            },
        ],
    )
    out = hd._read_recent_alerts(p, hours=24)
    assert len(out) == 1
    assert out[0]["source"] == "recent"


def test_read_recent_alerts_handles_missing(isolated_logs: Path) -> None:
    out = hd._read_recent_alerts(isolated_logs / "nope.jsonl", hours=24)
    assert out == []


def test_read_recent_alerts_handles_mixed_ts_shapes(isolated_logs: Path) -> None:
    """Older alert writers use float epoch_s in `ts`; newer use ISO in `timestamp_utc`."""
    p = isolated_logs / "alerts.jsonl"
    now = datetime.now(UTC)
    p.write_text(
        "\n".join(
            [
                # ISO-string shape (newer)
                json.dumps(
                    {
                        "timestamp_utc": (now - timedelta(hours=1)).isoformat(),
                        "source": "newer",
                        "level": "RED",
                        "message": "iso fmt",
                    }
                ),
                # Float epoch shape (older)
                json.dumps(
                    {
                        "ts": (now - timedelta(hours=2)).timestamp(),
                        "source": "older",
                        "level": "YELLOW",
                        "message": "epoch fmt",
                    }
                ),
                # Garbage ts that doesn't parse
                json.dumps({"ts": "not-a-date", "source": "broken"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out = hd._read_recent_alerts(p, hours=24)
    assert len(out) == 2  # newer + older accepted; broken skipped
    sources = {r["source"] for r in out}
    assert sources == {"newer", "older"}


# ── build_dashboard (integration) ─────────────────────────────────


def test_build_dashboard_all_never_run(isolated_logs: Path) -> None:
    d = hd.build_dashboard()
    # Every section should report NEVER_RUN with no input data
    for sec in d["sections"].values():
        assert sec.get("status") == "NEVER_RUN"
    assert d["recent_alerts_count"] == 0
    # Overall is the worst — NEVER_RUN ranks as YELLOW-equivalent (1)
    assert d["overall"] == "NEVER_RUN"


def test_build_dashboard_green_when_all_fresh(isolated_logs: Path) -> None:
    now = datetime.now(UTC).isoformat()
    _write_jsonl(
        hd.SOURCES["supercharge_runs"],
        [
            {
                "ts": now,
                "tier": "sweep",
                "phase2": {"n_bots": 5, "n_verdicts": 5, "n_skipped_cached": 0},
                "phase3": {"n_agreements": 3, "n_dissents": 0},
            }
        ],
    )
    _write_jsonl(
        hd.SOURCES["ibkr_sub_status"],
        [
            {
                "ts": now,
                "all_realtime": True,
                "results": [{"exchange": "CME", "verdict": "PASS"}],
            }
        ],
    )
    _write_jsonl(
        hd.SOURCES["capture_health"],
        [
            {
                "ts": now,
                "verdict": "GREEN",
                "n_symbols": 8,
                "issues": [],
            }
        ],
    )
    _write_jsonl(
        hd.SOURCES["disk_space"],
        [
            {
                "ts": now,
                "verdict": "GREEN",
                "checks": [{"label": "ticks", "free_gb": 400}],
            }
        ],
    )
    _write_jsonl(
        hd.SOURCES["capture_rotation"],
        [
            {
                "ts": now,
                "apply": True,
                "totals": {"n_compressed": 2, "n_cold_archived": 1},
            }
        ],
    )
    hd.SOURCES["verdict_cache"].write_text(
        json.dumps(
            {
                "bot_a": {"verdict": "GREEN"},
                "bot_b": {"verdict": "GREEN"},
                "bot_c": {"verdict": "YELLOW"},
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        hd.SOURCES["jarvis_recs"],
        [
            {"bot_id": "bot_a", "size_cap_mult": 1.0, "ts": now},
            {"bot_id": "bot_b", "size_cap_mult": 0.8, "ts": now},
        ],
    )

    d = hd.build_dashboard()
    assert d["sections"]["supercharge"]["status"] == "GREEN"
    assert d["sections"]["ibkr_subscriptions"]["status"] == "PASS"
    assert d["sections"]["capture_health"]["status"] == "GREEN"
    assert d["sections"]["disk_space"]["status"] == "GREEN"
    assert d["sections"]["capture_rotation"]["status"] == "GREEN"
    assert d["sections"]["fleet_verdicts"]["n_green"] == 2
    assert d["sections"]["fleet_verdicts"]["n_yellow"] == 1
    assert d["sections"]["fleet_verdicts"]["n_red"] == 0
    assert d["sections"]["jarvis_recent"]["n_recent"] == 2
    assert d["overall"] in {"GREEN", "PASS"}


def test_build_dashboard_surfaces_ibkr_setup_blocked(isolated_logs: Path) -> None:
    now = datetime.now(UTC).isoformat()
    action = "Seed IBC credentials before starting Gateway."
    _write_jsonl(
        hd.SOURCES["ibkr_sub_status"],
        [
            {
                "ts": now,
                "setup_status": "BLOCKED",
                "setup_error_code": "ibc_credentials_missing",
                "operator_action": action,
                "all_realtime": False,
                "all_depth_ok": False,
                "results": [],
                "depth_results": [],
            }
        ],
    )

    d = hd.build_dashboard()

    sub = d["sections"]["ibkr_subscriptions"]
    assert sub["status"] == "BLOCKED"
    assert sub["setup_error_code"] == "ibc_credentials_missing"
    assert sub["operator_action"] == action
    assert d["overall"] == "BLOCKED"
    assert action in hd.render_text(d)


def test_build_dashboard_marks_capture_blocked_by_ibkr_setup(
    isolated_logs: Path,
) -> None:
    now = datetime.now(UTC).isoformat()
    action = "Seed IBC credentials before starting Gateway."
    _write_jsonl(
        hd.SOURCES["ibkr_sub_status"],
        [
            {
                "ts": now,
                "setup_status": "BLOCKED",
                "setup_error_code": "ibc_credentials_missing",
                "operator_action": action,
                "all_realtime": False,
                "all_depth_ok": False,
            }
        ],
    )
    _write_jsonl(
        hd.SOURCES["capture_health"],
        [
            {
                "ts": now,
                "verdict": "RED",
                "n_symbols": 2,
                "issues": ["ticks MNQ: MISSING", "depth MNQ: MISSING"],
            }
        ],
    )

    d = hd.build_dashboard()
    cap = d["sections"]["capture_health"]

    assert cap["status"] == "BLOCKED"
    assert cap["raw_status"] == "RED"
    assert cap["blocked_by"] == "ibkr_subscriptions"
    text = hd.render_text(d)
    assert "blocked by ibkr_subscriptions" in text
    assert action in text


def test_build_dashboard_surfaces_capture_rotation_notes(isolated_logs: Path) -> None:
    now = datetime.now(UTC).isoformat()
    _write_jsonl(
        hd.SOURCES["capture_rotation"],
        [
            {
                "ts": now,
                "apply": False,
                "ticks": {
                    "kind": "ticks",
                    "n_compressed": 0,
                    "n_cold_archived": 0,
                    "note": "dir missing",
                },
                "depth": {
                    "kind": "depth",
                    "n_compressed": 0,
                    "n_cold_archived": 0,
                    "note": "dir missing",
                },
                "totals": {"n_compressed": 0, "n_cold_archived": 0},
            }
        ],
    )

    d = hd.build_dashboard()

    rotation = d["sections"]["capture_rotation"]
    assert rotation["status"] == "DRY-RUN"
    assert rotation["notes"] == ["ticks: dir missing", "depth: dir missing"]
    text = hd.render_text(d)
    assert "ticks: dir missing" in text
    assert "depth: dir missing" in text


def test_build_dashboard_marks_capture_rotation_blocked_by_capture_health(
    isolated_logs: Path,
) -> None:
    now = datetime.now(UTC).isoformat()
    action = "Seed IBC credentials before starting Gateway."
    _write_jsonl(
        hd.SOURCES["ibkr_sub_status"],
        [
            {
                "ts": now,
                "setup_status": "BLOCKED",
                "setup_error_code": "ibc_credentials_missing",
                "operator_action": action,
                "all_realtime": False,
                "all_depth_ok": False,
            }
        ],
    )
    _write_jsonl(
        hd.SOURCES["capture_health"],
        [
            {
                "ts": now,
                "verdict": "RED",
                "n_symbols": 2,
                "issues": ["ticks MNQ: MISSING", "depth MNQ: MISSING"],
            }
        ],
    )
    _write_jsonl(
        hd.SOURCES["capture_rotation"],
        [
            {
                "ts": now,
                "apply": False,
                "ticks": {"kind": "ticks", "note": "dir missing"},
                "depth": {"kind": "depth", "note": "dir missing"},
                "totals": {"n_compressed": 0, "n_cold_archived": 0},
            }
        ],
    )

    d = hd.build_dashboard()

    rotation = d["sections"]["capture_rotation"]
    assert rotation["status"] == "BLOCKED"
    assert rotation["blocked_by"] == "capture_health"
    assert rotation["blocked_reason"] == "clear upstream capture health first"
    text = hd.render_text(d)
    assert "blocked by capture_health" in text
    assert "ticks: dir missing" in text


def test_build_dashboard_marks_jarvis_idle_when_no_green_candidates(
    isolated_logs: Path,
) -> None:
    now = datetime.now(UTC).isoformat()
    _write_jsonl(
        hd.SOURCES["supercharge_runs"],
        [
            {
                "ts": now,
                "tier": "sweep",
                "phase2": {"n_bots": 25, "n_verdicts": 0},
                "phase3": {"n_consulted": 0, "n_agreements": 0, "n_dissents": 0},
                "phase4": {"n_arbitrated": 0},
            }
        ],
    )

    d = hd.build_dashboard()

    jarvis = d["sections"]["jarvis_recent"]
    assert jarvis["status"] == "IDLE"
    assert jarvis["reason"] == "no sage-approved GREEN bots to arbitrate"
    text = hd.render_text(d)
    assert "no sage-approved GREEN bots to arbitrate" in text


def test_build_dashboard_warns_when_jarvis_expected_but_missing(
    isolated_logs: Path,
) -> None:
    now = datetime.now(UTC).isoformat()
    _write_jsonl(
        hd.SOURCES["supercharge_runs"],
        [
            {
                "ts": now,
                "tier": "sweep",
                "phase2": {"n_bots": 25, "n_verdicts": 3},
                "phase3": {"n_consulted": 3, "n_agreements": 2, "n_dissents": 1},
                "phase4": {"n_arbitrated": 0},
            }
        ],
    )

    d = hd.build_dashboard()

    jarvis = d["sections"]["jarvis_recent"]
    assert jarvis["status"] == "YELLOW"
    assert jarvis["reason"] == "sage agreements exist but no Jarvis recommendations were logged"


def test_build_dashboard_overall_worst_when_critical(isolated_logs: Path) -> None:
    now = datetime.now(UTC).isoformat()
    _write_jsonl(
        hd.SOURCES["disk_space"],
        [
            {
                "ts": now,
                "verdict": "CRITICAL",
                "checks": [{"label": "ticks", "free_gb": 1.0}],
            }
        ],
    )
    d = hd.build_dashboard()
    assert d["overall"] == "CRITICAL"


# ── render_text ───────────────────────────────────────────────────


def test_render_text_returns_string(isolated_logs: Path) -> None:
    d = hd.build_dashboard()
    txt = hd.render_text(d)
    assert "ETA HEALTH DASHBOARD" in txt
    assert "OVERALL" in txt
    assert "supercharge orchestrator" in txt


# ── main() exit-code mapping ──────────────────────────────────────


def test_main_exits_per_overall(
    isolated_logs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    now = datetime.now(UTC).isoformat()
    _write_jsonl(
        hd.SOURCES["disk_space"],
        [
            {
                "ts": now,
                "verdict": "RED",
                "checks": [{"label": "ticks", "free_gb": 5.0}],
            }
        ],
    )
    monkeypatch.setattr("sys.argv", ["health_dashboard"])
    rc = hd.main()
    assert rc == 2  # RED → exit 2
