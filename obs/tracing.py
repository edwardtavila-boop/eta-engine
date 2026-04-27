"""
EVOLUTIONARY TRADING ALGO  //  obs.tracing
==========================================
OpenTelemetry tracing wrapper for the decision-journal spans.

The decision-journal already records per-tick spans (start_ts, end_ts,
verdict, latency_ms). This module gives subsystems a uniform way to
*emit* those into OpenTelemetry so they show up in a real tracing UI
(Tempo, Jaeger, Honeycomb, Grafana Cloud Tempo).

Public API
----------

* :func:`is_otel_available` -- import-time probe of opentelemetry-api.
* :func:`init_tracing(service_name, endpoint=None)` -- one-call init.
  When ``opentelemetry-api`` isn't installed, returns a no-op tracer.
* :func:`get_tracer()` -- returns the configured tracer (no-op if init
  was never called or otel is missing).
* :func:`span(name, attrs)` -- context manager for ad-hoc spans.

Defaults
--------

* Service name comes from caller (we expect ``"jarvis-live"``,
  ``"avengers-fleet"``, ``"tradingview-capture"``).
* OTLP endpoint defaults to ``$OTEL_EXPORTER_OTLP_ENDPOINT`` or
  ``http://127.0.0.1:4318/v1/traces`` (process-compose's local Tempo).
* Resource attrs include hostname + git-rev so traces are correlated
  with the deployed build.

The point of this module is: make tracing **opt-in and zero-cost** when
not configured. Code paths can call ``span("...")`` unconditionally;
in the no-otel state the calls compile down to two attribute lookups
and a try/finally.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

log = logging.getLogger(__name__)

_TRACER: Any = None
_INITIALIZED = False


def is_otel_available() -> bool:
    """True iff ``opentelemetry-api`` imports cleanly."""
    try:
        import opentelemetry.trace  # noqa: F401
    except ImportError:
        return False
    return True


def init_tracing(
    service_name: str,
    endpoint: str | None = None,
    resource_attrs: dict[str, str] | None = None,
) -> Any:  # noqa: ANN401 -- otel Tracer is dynamically resolved
    """Configure OpenTelemetry tracing once per process.

    Returns a tracer (real or no-op). Safe to call multiple times --
    after first call, subsequent calls return the cached tracer
    unchanged.
    """
    global _TRACER, _INITIALIZED
    if _INITIALIZED:
        return _TRACER
    _INITIALIZED = True

    if not is_otel_available():
        log.info("obs.tracing: opentelemetry not installed; using no-op tracer")
        _TRACER = _NoOpTracer()
        return _TRACER

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        res_attrs = {
            "service.name":     service_name,
            "host.name":        socket.gethostname(),
            "deployment.env":   os.environ.get("ETA_ENV", "vps-prod"),
            "service.version":  os.environ.get("ETA_GIT_REV", "unknown"),
        }
        if resource_attrs:
            res_attrs.update(resource_attrs)

        provider = TracerProvider(resource=Resource.create(res_attrs))
        url = endpoint or os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "http://127.0.0.1:4318/v1/traces",
        )
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=url)))
        trace.set_tracer_provider(provider)
        _TRACER = trace.get_tracer(service_name)
        log.info("obs.tracing: OTel initialized -> %s", url)
    except Exception as e:  # noqa: BLE001 -- otel raises a wide set
        log.warning(
            "obs.tracing: OTel init failed (%s); falling back to no-op", e,
        )
        _TRACER = _NoOpTracer()
    return _TRACER


def get_tracer() -> Any:  # noqa: ANN401
    """Return the configured tracer, or the no-op fallback."""
    if _TRACER is None:
        return _NoOpTracer()
    return _TRACER


@contextlib.contextmanager
def span(
    name: str,
    attrs: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Open + close a span; safe to call when otel isn't installed.

    Usage::

        with span("jarvis.tick", attrs={"bot": "mnq", "verdict": "CONTINUE"}):
            do_work()
    """
    tracer = get_tracer()
    if isinstance(tracer, _NoOpTracer):
        yield None
        return
    with tracer.start_as_current_span(name) as s:
        if attrs:
            for k, v in attrs.items():
                with contextlib.suppress(Exception):
                    s.set_attribute(k, v)
        yield s


# ---------------------------------------------------------------------------
# No-op tracer (used when OTel isn't installed)
# ---------------------------------------------------------------------------


class _NoOpSpan:
    """Drop-in replacement for ``opentelemetry.trace.Span``."""

    def set_attribute(self, *_: Any, **__: Any) -> None:  # noqa: ANN401 -- mirrors otel Span signature
        return None

    def add_event(self, *_: Any, **__: Any) -> None:  # noqa: ANN401 -- mirrors otel Span signature
        return None

    def __enter__(self) -> _NoOpSpan:
        return self

    def __exit__(self, *_: Any) -> None:  # noqa: ANN401 -- standard ctx-mgr shape
        return None


class _NoOpTracer:
    """Drop-in replacement for ``opentelemetry.trace.Tracer``."""

    @contextlib.contextmanager
    def start_as_current_span(self, _name: str) -> Iterator[_NoOpSpan]:
        yield _NoOpSpan()
