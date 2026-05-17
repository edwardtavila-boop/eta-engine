from __future__ import annotations

import logging
from types import SimpleNamespace

from eta_engine.scripts import broker_router_utils


def test_env_numeric_helpers_fall_back_and_warn_on_bad_values(caplog) -> None:
    logger = logging.getLogger("test_broker_router_utils")
    with caplog.at_level(logging.WARNING):
        assert broker_router_utils.env_int("ETA_X", 7, logger=logger, environ={"ETA_X": "bad"}) == 7
        assert broker_router_utils.env_float("ETA_Y", 1.5, logger=logger, environ={"ETA_Y": "bad"}) == 1.5
    assert "invalid integer env ETA_X='bad'; using 7" in caplog.text
    assert "invalid float env ETA_Y='bad'; using 1.5" in caplog.text


def test_env_flag_helpers_read_expected_truth_contract() -> None:
    environ = {
        "ETA_GATE_BOOTSTRAP": "1",
        "ETA_BROKER_ROUTER_ENFORCE_READINESS": "1",
        "ETA_FLAG": "yes",
    }
    assert broker_router_utils.gate_bootstrap_enabled(environ=environ) is True
    assert broker_router_utils.readiness_enforced(environ=environ) is True
    assert broker_router_utils.truthy_env("ETA_FLAG", environ=environ) is True


def test_extract_broker_fill_ts_prefers_canonical_filled_at() -> None:
    result = SimpleNamespace(
        filled_at="2026-05-17T18:00:00+00:00",
        raw={"filled_at": "2026-05-17T17:59:00+00:00"},
    )
    assert broker_router_utils.extract_broker_fill_ts(result) == "2026-05-17T18:00:00+00:00"


def test_extract_broker_fill_ts_falls_back_to_legacy_raw_statuses() -> None:
    result = SimpleNamespace(
        filled_at=None,
        raw={
            "ib_statuses": [
                {"status": "Submitted", "time": "2026-05-17T17:58:00+00:00"},
                {"status": "Filled", "execution_time": "2026-05-17T18:01:00+00:00"},
            ]
        },
    )
    assert broker_router_utils.extract_broker_fill_ts(result) == "2026-05-17T18:01:00+00:00"
