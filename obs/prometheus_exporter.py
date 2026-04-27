"""Prometheus scrape endpoint — P7_OPS prometheus.

Wraps :class:`eta_engine.obs.metrics.MetricsRegistry.to_prometheus` in an
aiohttp HTTP handler so Prometheus can scrape the process. Runs alongside
the main event loop — the bot's asyncio task just needs to call
:func:`start_server` at boot and :func:`stop_server` at shutdown.

Design constraints
------------------
* No new process / no sidecar — embedded in the bot's event loop.
* No auth by default — bind to ``127.0.0.1`` and let a reverse proxy handle
  TLS + basic auth. ``bind_host`` is a boot-time knob for prod.
* Handler is stateless; the only mutable thing is the aiohttp server ref.
"""

from __future__ import annotations

import logging

from aiohttp import web

from eta_engine.obs.metrics import REGISTRY, MetricsRegistry

logger = logging.getLogger(__name__)

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 9115  # avoid the default Prometheus-itself port 9090

# Typed aiohttp key so the registry read-path is statically checkable and
# avoids the NotAppKeyWarning aiohttp raises for raw string keys.
REGISTRY_KEY: web.AppKey[MetricsRegistry] = web.AppKey("registry", MetricsRegistry)


async def metrics_handler(request: web.Request) -> web.Response:
    """GET /metrics — emit Prometheus exposition format."""
    registry = request.app[REGISTRY_KEY]
    body = registry.to_prometheus()
    return web.Response(text=body, content_type="text/plain; version=0.0.4")


async def health_handler(_: web.Request) -> web.Response:
    """GET /health — liveness ping for k8s / systemd."""
    return web.json_response({"status": "ok"})


def build_app(registry: MetricsRegistry | None = None) -> web.Application:
    """Build the aiohttp application with routes + registry injected."""
    app = web.Application()
    app[REGISTRY_KEY] = registry or REGISTRY
    app.router.add_get("/metrics", metrics_handler)
    app.router.add_get("/health", health_handler)
    return app


async def start_server(
    *,
    host: str = DEFAULT_BIND_HOST,
    port: int = DEFAULT_BIND_PORT,
    registry: MetricsRegistry | None = None,
) -> web.AppRunner:
    """Start the metrics HTTP server and return the runner handle.

    The caller keeps the runner reference alive for the lifetime of the bot
    and passes it back to :func:`stop_server` at shutdown.
    """
    app = build_app(registry)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info("prometheus_exporter serving on http://%s:%d/metrics", host, port)
    return runner


async def stop_server(runner: web.AppRunner) -> None:
    """Drain + shut down the aiohttp runner."""
    await runner.cleanup()
    logger.info("prometheus_exporter stopped")
