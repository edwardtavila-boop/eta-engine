"""Prometheus /metrics + /health endpoint tests — P7_OPS prometheus."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from eta_engine.obs.metrics import REGISTRY, MetricsRegistry
from eta_engine.obs.prometheus_exporter import (
    DEFAULT_BIND_HOST,
    DEFAULT_BIND_PORT,
    REGISTRY_KEY,
    build_app,
    start_server,
    stop_server,
)


@pytest.fixture
def registry() -> MetricsRegistry:
    reg = MetricsRegistry()
    reg.inc("apex_test_counter", value=3.0)
    reg.gauge("apex_test_gauge", 42.5)
    return reg


@pytest.mark.asyncio
async def test_metrics_endpoint_emits_prometheus_exposition(registry: MetricsRegistry) -> None:
    app = build_app(registry)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/metrics")
        assert resp.status == 200
        assert resp.content_type == "text/plain"
        body = await resp.text()
        assert "apex_test_counter" in body
        assert "apex_test_gauge" in body
        assert "42.5" in body


@pytest.mark.asyncio
async def test_metrics_endpoint_sets_prometheus_version_header(registry: MetricsRegistry) -> None:
    app = build_app(registry)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/metrics")
        # aiohttp exposes the content-type parameters via charset/etc
        assert "0.0.4" in (resp.headers.get("Content-Type") or "")


@pytest.mark.asyncio
async def test_health_endpoint_returns_ok() -> None:
    app = build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        assert resp.status == 200
        payload = await resp.json()
        assert payload == {"status": "ok"}


@pytest.mark.asyncio
async def test_build_app_uses_default_registry_when_none() -> None:
    # If no registry is supplied, the app should pull the module-level REGISTRY
    app = build_app(None)
    assert app[REGISTRY_KEY] is REGISTRY


@pytest.mark.asyncio
async def test_build_app_routes_registered() -> None:
    app = build_app()
    paths = {r.resource.canonical for r in app.router.routes() if hasattr(r.resource, "canonical")}
    assert "/metrics" in paths
    assert "/health" in paths


@pytest.mark.asyncio
async def test_start_and_stop_server_lifecycle(registry: MetricsRegistry) -> None:
    runner = await start_server(host="127.0.0.1", port=0, registry=registry)
    try:
        # AppRunner is set up — cleanup should not error
        assert runner is not None
    finally:
        await stop_server(runner)


def test_default_bind_host_is_loopback() -> None:
    assert DEFAULT_BIND_HOST == "127.0.0.1"


def test_default_bind_port_avoids_prometheus_itself() -> None:
    # Prometheus's own default is 9090 — our exporter must not collide
    assert DEFAULT_BIND_PORT != 9090
    assert DEFAULT_BIND_PORT == 9115
