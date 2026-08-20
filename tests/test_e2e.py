"""End-to-end tests over real HTTP.

A fake upstream and the proxy each run on their own ephemeral localhost port
inside the test event loop, and a plain httpx client (standing in for Codex)
talks to the proxy. These exercise the whole path: sockets, headers, streaming,
retry, and backoff.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from codex_proxy.app import create_app
from codex_proxy.config import Config

pytestmark = pytest.mark.asyncio

CAPACITY = "Selected model is at capacity. Please try a different model."


# --------------------------------------------------------------------------- infra


class RunningServer:
    """Run an ASGI app on an ephemeral port for the duration of a context."""

    def __init__(self, app):
        self.app = app

    async def __aenter__(self):
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"
        )
        self.server = uvicorn.Server(config)
        self.server.install_signal_handlers = lambda: None  # safe inside a loop
        self._task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        self.server.should_exit = True
        await self._task


def fast_cfg(upstream: str, **overrides) -> Config:
    cfg = Config()
    cfg.upstream_base_url = upstream
    cfg.backoff_initial = 0.01
    cfg.backoff_multiplier = 2.0
    cfg.backoff_max = 0.05
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


async def _sse_success(text: str):
    yield b'event: response.created\ndata: {"id":"resp_1"}\n\n'
    yield f'event: response.output_text.delta\ndata: {{"delta":"{text}"}}\n\n'.encode()
    yield b'event: response.completed\ndata: {"status":"completed"}\n\n'


async def _sse_capacity_failure():
    # Upstream returns 200 + opens a stream, then fails mid-stream (pre-content).
    yield b'event: response.created\ndata: {"id":"resp_x"}\n\n'
    yield (
        b'event: response.failed\ndata: {"error":{"message":"'
        + CAPACITY.encode()
        + b'"}}\n\n'
    )


def make_upstream(
    mode: str,
    fail_times: int,
    success_text: str = "Hello",
    sse_media_type: str = "text/event-stream",
):
    """Fake upstream that fails ``fail_times`` times, then succeeds. Counts calls.

    ``sse_media_type`` lets a test mimic a gateway that streams SSE under a
    non-standard content-type such as ``text/plain``.
    """
    state = {"calls": 0}

    async def handler(request):
        state["calls"] += 1
        failing = state["calls"] <= fail_times
        if mode == "http":
            if failing:
                return Response(
                    content=f'{{"error":{{"message":"{CAPACITY}"}}}}'.encode(),
                    status_code=503,
                    media_type="application/json",
                )
            return StreamingResponse(_sse_success(success_text), media_type=sse_media_type)
        # mode == "sse"
        if failing:
            return StreamingResponse(_sse_capacity_failure(), media_type=sse_media_type)
        return StreamingResponse(_sse_success(success_text), media_type=sse_media_type)

    app = Starlette(
        routes=[Route("/{path:path}", handler, methods=["GET", "POST"])]
    )
    return app, state


# --------------------------------------------------------------------------- tests


@pytest.mark.parametrize("mode", ["http", "sse"])
async def test_retries_until_success_and_hides_error(mode):
    upstream_app, state = make_upstream(mode, fail_times=3, success_text="Hello")
    async with RunningServer(upstream_app) as up:
        proxy_app = create_app(fast_cfg(up.base))
        async with RunningServer(proxy_app) as px:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"{px.base}/v1/responses", json={"model": "x"})

    assert r.status_code == 200
    assert "Hello" in r.text  # the successful content reached downstream
    assert "capacity" not in r.text.lower()  # the transient error never leaked
    assert state["calls"] == 4  # 3 failures + 1 success


async def test_survives_many_capacity_failures():
    upstream_app, state = make_upstream("sse", fail_times=15, success_text="Done")
    async with RunningServer(upstream_app) as up:
        proxy_app = create_app(fast_cfg(up.base))  # max_retries=None (infinite)
        async with RunningServer(proxy_app) as px:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(f"{px.base}/v1/responses", json={})

    assert r.status_code == 200
    assert "Done" in r.text
    assert "capacity" not in r.text.lower()
    assert state["calls"] == 16


async def test_non_retryable_error_is_forwarded_immediately():
    async def handler(request):
        return Response(
            b'{"error":{"message":"invalid request"}}',
            status_code=400,
            media_type="application/json",
        )

    app = Starlette(routes=[Route("/{path:path}", handler, methods=["POST"])])
    async with RunningServer(app) as up:
        proxy_app = create_app(fast_cfg(up.base))
        async with RunningServer(proxy_app) as px:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{px.base}/v1/responses", json={})

    assert r.status_code == 400
    assert "invalid request" in r.text


async def test_max_retries_cap_forwards_last_error():
    upstream_app, state = make_upstream("http", fail_times=1000)
    async with RunningServer(upstream_app) as up:
        proxy_app = create_app(fast_cfg(up.base, max_retries=3))
        async with RunningServer(proxy_app) as px:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{px.base}/v1/responses", json={})

    assert r.status_code == 503
    assert "capacity" in r.text.lower()  # exhausted -> real error surfaced
    assert state["calls"] == 3


async def test_headers_and_body_are_forwarded_upstream():
    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = (await request.body()).decode()
        seen["path"] = request.url.path
        return StreamingResponse(_sse_success("ok"), media_type="text/event-stream")

    app = Starlette(routes=[Route("/{path:path}", handler, methods=["POST"])])
    async with RunningServer(app) as up:
        proxy_app = create_app(fast_cfg(up.base))
        async with RunningServer(proxy_app) as px:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{px.base}/backend-api/codex/responses",
                    headers={"Authorization": "Bearer secret-token"},
                    json={"model": "gpt", "input": "hi"},
                )

    assert seen["auth"] == "Bearer secret-token"
    assert '"model": "gpt"' in seen["body"] or '"model":"gpt"' in seen["body"]
    assert seen["path"] == "/backend-api/codex/responses"


async def test_proxy_injects_api_key_overriding_downstream():
    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return StreamingResponse(_sse_success("ok"), media_type="text/event-stream")

    app = Starlette(routes=[Route("/{path:path}", handler, methods=["POST"])])
    async with RunningServer(app) as up:
        proxy_app = create_app(fast_cfg(up.base, api_key="sk-upstream-secret"))
        async with RunningServer(proxy_app) as px:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{px.base}/v1/responses",
                    headers={"Authorization": "Bearer codex-token"},
                    json={},
                )

    # Codex sent "codex-token"; upstream must see the proxy's key instead.
    assert seen["auth"] == "Bearer sk-upstream-secret"


async def test_capacity_hidden_when_sse_served_as_text_plain():
    # Regression for the real gateway: it streams SSE with content-type
    # text/plain, so the capacity failure event must still be caught & retried
    # rather than leaking through as a plain 2xx body.
    upstream_app, state = make_upstream(
        "sse", fail_times=2, success_text="pong", sse_media_type="text/plain"
    )
    async with RunningServer(upstream_app) as up:
        proxy_app = create_app(fast_cfg(up.base))
        async with RunningServer(proxy_app) as px:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"{px.base}/v1/responses", json={})

    assert r.status_code == 200
    assert "pong" in r.text
    assert "capacity" not in r.text.lower()  # the leak is fixed
    assert state["calls"] == 3  # 2 capacity failures + 1 success


async def test_capacity_in_200_json_body_is_retried():
    # Some gateways answer 200 with an error JSON body (not SSE). It must be
    # inspected and retried, not forwarded as success.
    state = {"calls": 0}

    async def handler(request):
        state["calls"] += 1
        if state["calls"] <= 2:
            return Response(
                content=f'{{"error":{{"message":"{CAPACITY}"}}}}'.encode(),
                status_code=200,
                media_type="application/json",
            )
        return Response(content=b'{"ok":true}', status_code=200, media_type="application/json")

    app = Starlette(routes=[Route("/{path:path}", handler, methods=["POST"])])
    async with RunningServer(app) as up:
        proxy_app = create_app(fast_cfg(up.base))
        async with RunningServer(proxy_app) as px:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"{px.base}/v1/responses", json={})

    assert r.status_code == 200
    assert "capacity" not in r.text.lower()
    assert '"ok":true' in r.text
    assert state["calls"] == 3


async def test_model_override_rewrites_upstream_and_warns_via_header():
    # Proxy forces "o3"; Codex asked for "gpt-5.6-sol". Upstream must receive the
    # forced model, the mismatch must surface via a response header, and the
    # response body (the 正文) must stay clean.
    seen = {}

    async def handler(request):
        seen["body"] = (await request.body()).decode()
        return StreamingResponse(_sse_success("hi"), media_type="text/event-stream")

    app = Starlette(routes=[Route("/{path:path}", handler, methods=["POST"])])
    async with RunningServer(app) as up:
        proxy_app = create_app(fast_cfg(up.base, model="o3"))
        async with RunningServer(proxy_app) as px:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{px.base}/v1/responses", json={"model": "gpt-5.6-sol", "input": "x"}
                )

    # Upstream saw the forced model, not what Codex requested.
    assert '"model": "o3"' in seen["body"] or '"model":"o3"' in seen["body"]
    assert "gpt-5.6-sol" not in seen["body"]
    # Warning surfaced via the header, naming both models.
    warn = r.headers.get("x-codex-proxy-warning")
    assert warn is not None
    assert "gpt-5.6-sol" in warn and "o3" in warn
    # The body stays clean — the warning never leaks into the answer.
    assert "proxy served" not in r.text
    assert "o3" not in r.text
    assert r.status_code == 200
    assert "hi" in r.text


async def test_model_override_no_warning_when_model_matches():
    seen = {}

    async def handler(request):
        seen["body"] = (await request.body()).decode()
        return StreamingResponse(_sse_success("hi"), media_type="text/event-stream")

    app = Starlette(routes=[Route("/{path:path}", handler, methods=["POST"])])
    async with RunningServer(app) as up:
        proxy_app = create_app(fast_cfg(up.base, model="o3"))
        async with RunningServer(proxy_app) as px:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{px.base}/v1/responses", json={"model": "o3"})

    assert '"model": "o3"' in seen["body"] or '"model":"o3"' in seen["body"]
    assert r.headers.get("x-codex-proxy-warning") is None


async def test_no_model_override_when_unset_forwards_body_verbatim():
    seen = {}

    async def handler(request):
        seen["body"] = (await request.body()).decode()
        return StreamingResponse(_sse_success("hi"), media_type="text/event-stream")

    app = Starlette(routes=[Route("/{path:path}", handler, methods=["POST"])])
    async with RunningServer(app) as up:
        proxy_app = create_app(fast_cfg(up.base, model=None))
        async with RunningServer(proxy_app) as px:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{px.base}/v1/responses", json={"model": "gpt-5.6-sol"}
                )

    assert "gpt-5.6-sol" in seen["body"]  # untouched
    assert r.headers.get("x-codex-proxy-warning") is None


async def test_routes_send_each_model_to_its_own_upstream(tmp_path, monkeypatch):
    # Two upstreams; glm-5.3 -> A, everything else -> default B. Each upstream
    # must receive its own model's request with its own api_key.
    seenA, seenB = {}, {}

    def make(seen):
        async def handler(request):
            seen["auth"] = request.headers.get("authorization")
            seen["hit"] = True
            return StreamingResponse(_sse_success("ok"), media_type="text/event-stream")
        return Starlette(routes=[Route("/{path:path}", handler, methods=["POST"])])

    async with RunningServer(make(seenA)) as upA, RunningServer(make(seenB)) as upB:
        routes = {
            "upstreams": {
                "A": {"base_url": upA.base, "api_key": "key-A"},
                "B": {"base_url": upB.base, "api_key": "key-B"},
            },
            "default": "B",
            "models": {"glm-5.3": "A"},
        }
        routes_path = tmp_path / "routes.json"
        routes_path.write_text(__import__("json").dumps(routes))
        monkeypatch.setenv("CODEX_PROXY_ROUTES_FILE", str(routes_path))

        proxy_app = create_app(fast_cfg("http://unused.invalid"))
        async with RunningServer(proxy_app) as px:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(f"{px.base}/v1/responses", json={"model": "glm-5.3"})
                await client.post(f"{px.base}/v1/responses", json={"model": "other-model"})

    assert seenA.get("hit") and seenA["auth"] == "Bearer key-A"  # matched route
    assert seenB.get("hit") and seenB["auth"] == "Bearer key-B"  # default route


async def test_health_endpoint():
    proxy_app = create_app(fast_cfg("http://127.0.0.1:1"))
    async with RunningServer(proxy_app) as px:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{px.base}/__proxy_health")
    assert r.status_code == 200
    assert r.text == "ok"
