"""The retry engine.

``RetryProxy.handle`` is a Starlette endpoint that transparently forwards a
request to the configured upstream and retries — with exponential backoff, up to
:attr:`Config.max_retries` (``None`` == forever) — whenever the upstream reports
a transient failure. Downstream (the Codex CLI) never sees the transient error.

The subtle part is streaming: a capacity failure can arrive *after* the upstream
has already answered ``200 OK`` and opened an SSE stream. We therefore buffer the
stream through a "commit gate": pre-generation lifecycle events are held back, a
capacity signal there triggers a clean retry (nothing was sent downstream yet),
and the moment real content appears we flush the buffer and switch to a
byte-for-byte passthrough.
"""

from __future__ import annotations

import asyncio

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from .backoff import Backoff
from .config import Config
from .detector import EventClass, classify_sse_event, is_retryable_http
from .sse import SSEDecoder


class RetrySignal(Exception):
    """Internal: the current attempt failed transiently; back off and retry."""


class ClientGone(Exception):
    """Internal: the downstream client disconnected while we were waiting."""


_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
# Stripped from the outgoing upstream request. ``accept-encoding`` is forced to
# identity so we never have to reconcile a forwarded ``content-encoding`` header
# with httpx's transparently-decoded body.
_REQUEST_DROP = _HOP_BY_HOP | {"host", "content-length", "accept-encoding"}
# ``content-type`` is dropped from the dict and re-applied via ``media_type`` so
# Starlette owns it (avoids a duplicated header).
_RESPONSE_DROP = _HOP_BY_HOP | {"content-length", "content-encoding", "content-type"}


class RetryProxy:
    def __init__(self, cfg: Config, client: httpx.AsyncClient) -> None:
        self.cfg = cfg
        self.client = client
        self.backoff = Backoff.from_config(cfg)

    # ------------------------------------------------------------------ public

    async def handle(self, request: Request) -> Response:
        body = await request.body()
        method = request.method
        url = self._build_url(request)
        headers = self._request_headers(request)

        attempt = 0
        while True:
            attempt += 1
            if await request.is_disconnected():
                return Response(status_code=499)
            try:
                return await self._attempt(method, url, headers, body, attempt)
            except RetrySignal:
                pass
            except httpx.RequestError as exc:
                # Connect refused / reset / timeout before a response arrived:
                # treat as transient and retry (subject to max_retries).
                if not self._should_retry(attempt):
                    return Response(
                        content=f"upstream request failed: {exc!r}".encode(),
                        status_code=502,
                        media_type="text/plain; charset=utf-8",
                    )
            try:
                await self._sleep_backoff(attempt, request)
            except ClientGone:
                return Response(status_code=499)

    # --------------------------------------------------------------- one attempt

    async def _attempt(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes,
        attempt: int,
    ) -> Response:
        req = self.client.build_request(method, url, headers=headers, content=body)
        resp = await self.client.send(req, stream=True)
        status = resp.status_code
        content_type = resp.headers.get("content-type", "")
        resp_headers = self._response_headers(resp)

        if not 200 <= status < 300:
            raw = await resp.aread()
            await self._safe_close(resp)
            if is_retryable_http(status, raw, self.cfg) and self._should_retry(attempt):
                raise RetrySignal()
            return Response(
                content=raw,
                status_code=status,
                headers=resp_headers,
                media_type=content_type or None,
            )

        if "text/event-stream" in content_type.lower():
            return await self._gate_stream(resp, attempt, resp_headers, content_type)

        # Non-streaming 2xx: buffer fully and forward.
        raw = await resp.aread()
        await self._safe_close(resp)
        return Response(
            content=raw,
            status_code=status,
            headers=resp_headers,
            media_type=content_type or None,
        )

    async def _gate_stream(
        self,
        resp: httpx.Response,
        attempt: int,
        resp_headers: dict[str, str],
        content_type: str,
    ) -> Response:
        """Buffer the SSE stream until it either retries or commits to content."""
        decoder = SSEDecoder()
        aiter = resp.aiter_bytes()
        to_flush = bytearray()  # buffered pre-commit events, flushed on commit
        remainder = bytearray()  # already-parsed bytes after the commit point
        committed = False

        try:
            async for chunk in aiter:
                events = decoder.feed(chunk)
                for i, ev in enumerate(events):
                    cls = classify_sse_event(ev, self.cfg)
                    if cls is EventClass.RETRY and self._should_retry(attempt):
                        await self._safe_close(resp)
                        raise RetrySignal()
                    # RETRY-but-exhausted, or COMMIT, both commit and forward.
                    to_flush += ev.raw
                    if cls is not EventClass.LIFECYCLE:
                        committed = True
                        for later in events[i + 1 :]:
                            remainder += later.raw
                        remainder += decoder.leftover
                        break
                if committed:
                    break
                if len(to_flush) > self.cfg.max_prelude_bytes:
                    # Safety valve: forward what we have instead of buffering forever.
                    remainder += decoder.leftover
                    committed = True
                    break
        except RetrySignal:
            raise
        except httpx.RequestError:
            # Connection died before we committed anything downstream -> retry.
            await self._safe_close(resp)
            if self._should_retry(attempt):
                raise RetrySignal()
            raise

        if not committed:
            # Clean EOF with only lifecycle/heartbeat events. Nothing marks it
            # retryable, so forward verbatim rather than loop forever.
            final = bytes(to_flush) + decoder.leftover
            await self._safe_close(resp)
            return Response(
                content=final,
                status_code=resp.status_code,
                headers=resp_headers,
                media_type=content_type or None,
            )

        async def body_gen():
            try:
                if to_flush:
                    yield bytes(to_flush)
                if remainder:
                    yield bytes(remainder)
                async for chunk in aiter:
                    yield chunk
            finally:
                await self._safe_close(resp)

        return StreamingResponse(
            body_gen(),
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=content_type or None,
        )

    # ------------------------------------------------------------------ helpers

    def _should_retry(self, attempt: int) -> bool:
        return self.cfg.max_retries is None or attempt < self.cfg.max_retries

    async def _sleep_backoff(self, attempt: int, request: Request) -> None:
        remaining = self.backoff.delay(attempt)
        slice_seconds = 0.5
        while remaining > 0:
            if await request.is_disconnected():
                raise ClientGone()
            nap = min(slice_seconds, remaining)
            await asyncio.sleep(nap)
            remaining -= nap

    def _build_url(self, request: Request) -> str:
        url = self.cfg.upstream_base_url.rstrip("/") + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        return url

    def _request_headers(self, request: Request) -> dict[str, str]:
        out = {
            k: v for k, v in request.headers.items() if k.lower() not in _REQUEST_DROP
        }
        if self.cfg.api_key:
            # Proxy-held credential always wins: drop any incoming Authorization
            # (case-insensitively) and inject ours.
            out = {k: v for k, v in out.items() if k.lower() != "authorization"}
            out["Authorization"] = f"Bearer {self.cfg.api_key}"
        return out

    def _response_headers(self, resp: httpx.Response) -> dict[str, str]:
        return {
            k: v for k, v in resp.headers.items() if k.lower() not in _RESPONSE_DROP
        }

    @staticmethod
    async def _safe_close(resp: httpx.Response) -> None:
        try:
            await resp.aclose()
        except Exception:
            pass
