"""Starlette application wiring.

A single catch-all route forwards every method and path to :class:`RetryProxy`,
so the proxy is fully path-transparent: whatever endpoint Codex hits is
forwarded under ``upstream_base_url``. A tiny health route is exposed at an
unlikely path so it never shadows a real upstream endpoint.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from .config import Config
from .proxy import RetryProxy


def _configure_debug_logging(cfg: Config) -> None:
    """Send codex_proxy diagnostic logs to a file (or stderr) at DEBUG level."""
    log = logging.getLogger("codex_proxy")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    handler: logging.Handler
    if cfg.debug_log:
        handler = logging.FileHandler(cfg.debug_log)
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.propagate = False

_PROXIED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def build_client(cfg: Config) -> httpx.AsyncClient:
    timeout = httpx.Timeout(
        connect=cfg.connect_timeout,
        read=cfg.read_timeout,  # None => wait indefinitely, required for SSE
        write=cfg.write_timeout,
        pool=cfg.pool_timeout,
    )
    # Redirects are forwarded downstream verbatim, not followed here.
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


def create_app(
    cfg: Config | None = None, client: httpx.AsyncClient | None = None
) -> Starlette:
    cfg = cfg or Config.from_env()
    if cfg.debug:
        _configure_debug_logging(cfg)
    owns_client = client is None
    client = client or build_client(cfg)
    proxy = RetryProxy(cfg, client)

    async def health(request):
        return PlainTextResponse("ok")

    routes = [
        Route("/__proxy_health", health, methods=["GET"]),
        Route("/{path:path}", proxy.handle, methods=_PROXIED_METHODS),
    ]

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            # Only tear down clients we created; injected ones are the caller's.
            if owns_client:
                await client.aclose()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.config = cfg
    app.state.client = client
    return app
