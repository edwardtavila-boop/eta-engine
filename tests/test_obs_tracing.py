"""Tests for ``eta_engine.obs.tracing`` -- OpenTelemetry shim."""

from __future__ import annotations

import eta_engine.obs.tracing as tracing_mod
import pytest
from eta_engine.obs.tracing import (
    get_tracer,
    init_tracing,
    is_otel_available,
    span,
)


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    # Each test gets a clean module state so init_tracing's "once per
    # process" memoization doesn't leak across cases.
    tracing_mod._TRACER = None
    tracing_mod._INITIALIZED = False
    yield
    tracing_mod._TRACER = None
    tracing_mod._INITIALIZED = False


def test_is_otel_available_returns_bool() -> None:
    assert isinstance(is_otel_available(), bool)


def test_get_tracer_before_init_returns_noop() -> None:
    tracer = get_tracer()
    # No-op tracer has no exporter; just verify we got something back.
    assert tracer is not None


def test_init_tracing_returns_tracer() -> None:
    tracer = init_tracing("test-service")
    assert tracer is not None


def test_init_tracing_is_idempotent() -> None:
    a = init_tracing("svc1")
    b = init_tracing("svc2")
    assert a is b


def test_span_works_without_otel() -> None:
    # Without init, span yields None and doesn't raise.
    with span("test.op", attrs={"k": "v"}) as s:
        # No-op path returns None.
        _ = s


def test_span_works_with_attrs_after_init() -> None:
    init_tracing("test-svc")
    with span("test.op", attrs={"bot": "mnq", "verdict": "CONTINUE"}):
        pass


def test_span_handles_unhashable_attr_values_gracefully() -> None:
    init_tracing("test-svc")
    # OTel rejects some attr value types; our wrapper must swallow.
    with span("test.op", attrs={"obj": object()}):
        pass


def test_span_with_none_attrs() -> None:
    init_tracing("test-svc")
    with span("test.no-attrs"):
        pass


def test_init_tracing_fallback_when_endpoint_unreachable() -> None:
    # Init with a definitely-dead endpoint shouldn't raise -- if otel
    # is installed it'll batch+drop on export; if not, the no-op path
    # already works.
    tracer = init_tracing(
        "test-svc",
        endpoint="http://127.0.0.1:1/v1/traces",
    )
    assert tracer is not None
    with span("test.op"):
        pass
