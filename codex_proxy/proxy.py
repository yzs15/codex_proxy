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
import itertools
import json
import logging

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from .backoff import Backoff
from .config import Config
from .detector import (
    EventClass,
    classify_sse_event,
    contains_retry_signal,
    is_retryable_http,
)
from .sse import SSEDecoder

logger = logging.getLogger("codex_proxy")


class RetrySignal(Exception):
    """Internal: the current attempt failed transiently; back off and retry.

    ``reason`` is a human-readable explanation used only for diagnostic logging.
    """

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


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

# SSE field prefixes. Used to sniff a stream as Server-Sent-Events by content,
# because some upstreams (e.g. the ChatGPT/Codex gateways) serve SSE with
# content-type: text/plain instead of text/event-stream.
_SSE_LINE_PREFIXES = (b"event:", b"data:", b"id:", b"retry:", b":")

# Sentinel so _request_headers can tell "use the configured key" apart from an
# explicitly-passed ``None`` (which means "inject no key, pass client auth through").
_USE_CFG_KEY = object()


def _looks_like_sse(data: bytes) -> bool:
    """Heuristic: does this response body start with SSE framing?"""
    head = data.lstrip()[:64].lower()
    return head.startswith(_SSE_LINE_PREFIXES)


def _sanitize_header_value(value: str) -> str:
    """Reduce a string to printable ASCII, capped in length.

    The requested model name is client-controlled, so it must be scrubbed of
    CR/LF and other control characters before it goes into a response header —
    otherwise a crafted model name could split or inject headers.
    """
    return "".join(c for c in value if 32 <= ord(c) < 127)[:200]


class RetryProxy:
    def __init__(self, cfg: Config, client: httpx.AsyncClient) -> None:
        self.cfg = cfg
        self.client = client
        self.backoff = Backoff.from_config(cfg)
        self._rid = itertools.count(1)
        # (requested, forced) pairs already warned about, to avoid logging the
        # same model override on every single request.
        self._warned_pairs: set[tuple[str, str]] = set()

    # ------------------------------------------------------------- diagnostics

    def _snippet(self, data: bytes | str) -> str:
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        limit = self.cfg.debug_body_limit
        return data[:limit] + ("…(truncated)" if len(data) > limit else "")

    # ------------------------------------------------------------------ public

    async def handle(self, request: Request) -> Response:
        body = await request.body()
        method = request.method
        rid = next(self._rid)
        debug = self.cfg.debug

        # Force the configured model (if any) before the retry loop, so the
        # rewritten body is what gets replayed on every attempt. A mismatch is
        # reported out of band via _stamp — never in the response body.
        body, mismatch = self._apply_model_override(body)
        warning = None
        if mismatch:
            requested, forced = mismatch
            self._log_model_override(requested, forced, rid)
            warning = _sanitize_header_value(
                f"requested {requested!r} but proxy served {forced!r}"
            )

        def _stamp(resp: Response) -> Response:
            if warning:
                resp.headers["X-Codex-Proxy-Warning"] = warning
            return resp

        # Route on the (possibly overridden) model: pick the upstream + credential.
        base_url, api_key = self._resolve_route(self._effective_model(body))
        url = self._build_url(request, base_url)
        headers = self._request_headers(request, api_key)

        if debug:
            logger.info("[%d] %s %s (request body %dB)", rid, method, url, len(body))

        attempt = 0
        while True:
            attempt += 1
            if await request.is_disconnected():
                if debug:
                    logger.info("[%d] client disconnected before attempt %d", rid, attempt)
                return Response(status_code=499)
            try:
                return _stamp(await self._attempt(method, url, headers, body, attempt, rid))
            except RetrySignal as sig:
                if debug:
                    logger.info("[%d] attempt %d -> RETRY (%s)", rid, attempt, sig.reason)
            except httpx.RequestError as exc:
                # Connect refused / reset / timeout before a response arrived:
                # treat as transient and retry (subject to max_retries).
                if debug:
                    logger.warning("[%d] attempt %d transport error: %r", rid, attempt, exc)
                if not self._should_retry(attempt):
                    return _stamp(Response(
                        content=f"upstream request failed: {exc!r}".encode(),
                        status_code=502,
                        media_type="text/plain; charset=utf-8",
                    ))
            try:
                await self._sleep_backoff(attempt, request, rid)
            except ClientGone:
                if debug:
                    logger.info("[%d] client disconnected during backoff", rid)
                return Response(status_code=499)

    # --------------------------------------------------------------- one attempt

    async def _attempt(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes,
        attempt: int,
        rid: int,
    ) -> Response:
        debug = self.cfg.debug
        req = self.client.build_request(method, url, headers=headers, content=body)
        resp = await self.client.send(req, stream=True)
        status = resp.status_code
        content_type = resp.headers.get("content-type", "")
        resp_headers = self._response_headers(resp)

        if debug:
            logger.info(
                "[%d] attempt %d <- status=%d content-type=%r", rid, attempt, status, content_type
            )

        if not 200 <= status < 300:
            raw = await resp.aread()
            await self._safe_close(resp)
            if debug:
                logger.info("[%d] attempt %d non-2xx body: %s", rid, attempt, self._snippet(raw))
            if is_retryable_http(status, raw, self.cfg) and self._should_retry(attempt):
                raise RetrySignal(f"http {status} matched retryable signature")
            if debug:
                logger.info("[%d] forwarding non-retryable status %d downstream", rid, status)
            return Response(
                content=raw,
                status_code=status,
                headers=resp_headers,
                media_type=content_type or None,
            )

        # 2xx: decide whether the body is SSE by content, NOT just by
        # content-type — some gateways stream SSE as text/plain. Peek the first
        # chunk to sniff, then either gate it as a stream or buffer & forward.
        aiter = resp.aiter_bytes()
        try:
            initial = await anext(aiter)
        except StopAsyncIteration:
            initial = b""
        except httpx.RequestError:
            await self._safe_close(resp)
            if self._should_retry(attempt):
                raise RetrySignal("transport error before first response chunk")
            raise

        is_sse = "text/event-stream" in content_type.lower() or _looks_like_sse(initial)
        if debug:
            logger.info(
                "[%d] attempt %d 2xx sniff: is_sse=%s (content-type=%r head=%r)",
                rid, attempt, is_sse, content_type, initial[:32],
            )
        if is_sse:
            return await self._gate_stream(
                resp, attempt, resp_headers, content_type, rid, aiter, initial
            )

        # Genuinely non-SSE 2xx: buffer fully and forward. Still inspect the body
        # for a capacity signal — covers a gateway that answers 200 + error JSON.
        rest = bytearray(initial)
        try:
            async for chunk in aiter:
                rest += chunk
        except httpx.RequestError:
            await self._safe_close(resp)
            if self._should_retry(attempt):
                raise RetrySignal("transport error reading 2xx body")
            raise
        await self._safe_close(resp)
        raw = bytes(rest)
        if contains_retry_signal(raw.decode("utf-8", errors="replace"), self.cfg) and (
            self._should_retry(attempt)
        ):
            if debug:
                logger.info(
                    "[%d] attempt %d 2xx non-SSE body matched capacity signal -> retry: %s",
                    rid, attempt, self._snippet(raw),
                )
            raise RetrySignal("2xx non-SSE body matched capacity signal")
        if debug:
            logger.info(
                "[%d] attempt %d 2xx non-SSE body (%dB) forwarded", rid, attempt, len(raw)
            )
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
        rid: int,
        aiter,
        initial: bytes = b"",
    ) -> Response:
        """Buffer the SSE stream until it either retries or commits to content.

        ``aiter`` is the upstream byte iterator (the caller created it so it could
        peek the first chunk); ``initial`` is that already-pulled first chunk.
        """
        debug = self.cfg.debug
        decoder = SSEDecoder()
        to_flush = bytearray()  # buffered pre-commit events, flushed on commit
        remainder = bytearray()  # already-parsed bytes after the commit point
        committed = False

        async def consume(chunk: bytes) -> bool:
            """Feed one chunk through the gate; return True once it commits."""
            nonlocal committed
            events = decoder.feed(chunk)
            for i, ev in enumerate(events):
                cls = classify_sse_event(ev, self.cfg)
                if debug:
                    logger.info(
                        "[%d] attempt %d sse event type=%r class=%s data=%s",
                        rid, attempt, ev.event, cls.name, self._snippet(ev.data),
                    )
                if cls is EventClass.RETRY and self._should_retry(attempt):
                    await self._safe_close(resp)
                    raise RetrySignal(
                        f"sse event type={ev.event!r} matched capacity signal"
                    )
                # RETRY-but-exhausted, or COMMIT, both commit and forward.
                to_flush.extend(ev.raw)
                if cls is not EventClass.LIFECYCLE:
                    committed = True
                    if debug:
                        logger.info(
                            "[%d] attempt %d committed at event type=%r (streaming passthrough)",
                            rid, attempt, ev.event,
                        )
                    for later in events[i + 1 :]:
                        remainder.extend(later.raw)
                    remainder.extend(decoder.leftover)
                    return True
            return False

        try:
            if initial:
                await consume(initial)
            if not committed:
                async for chunk in aiter:
                    if await consume(chunk):
                        break
                    if len(to_flush) > self.cfg.max_prelude_bytes:
                        # Safety valve: forward instead of buffering forever.
                        if debug:
                            logger.warning(
                                "[%d] attempt %d prelude exceeded %dB without commit — "
                                "forcing passthrough", rid, attempt, self.cfg.max_prelude_bytes,
                            )
                        remainder.extend(decoder.leftover)
                        committed = True
                        break
        except RetrySignal:
            raise
        except httpx.RequestError as exc:
            # Connection died before we committed anything downstream -> retry.
            await self._safe_close(resp)
            if self._should_retry(attempt):
                raise RetrySignal(f"stream transport error before commit: {exc!r}")
            raise

        if not committed:
            # Clean EOF with only lifecycle/heartbeat events. Nothing marks it
            # retryable, so forward verbatim rather than loop forever.
            final = bytes(to_flush) + decoder.leftover
            await self._safe_close(resp)
            if debug:
                leaked = contains_retry_signal(
                    final.decode("utf-8", errors="replace"), self.cfg
                )
                logger.log(
                    logging.WARNING if leaked else logging.INFO,
                    "[%d] attempt %d SSE ended without commit%s: %s",
                    rid, attempt,
                    " — CONTAINS CAPACITY SIGNAL (LEAKED)" if leaked else "",
                    self._snippet(final),
                )
            return Response(
                content=final,
                status_code=resp.status_code,
                headers=resp_headers,
                media_type=content_type or None,
            )

        async def body_gen():
            leak_logged = False
            try:
                if to_flush:
                    yield bytes(to_flush)
                if remainder:
                    yield bytes(remainder)
                async for chunk in aiter:
                    if debug and not leak_logged and contains_retry_signal(
                        chunk.decode("utf-8", errors="replace"), self.cfg
                    ):
                        logger.warning(
                            "[%d] CAPACITY SIGNAL in POST-COMMIT stream — LEAKED to client "
                            "(arrived after content started; cannot retry safely): %s",
                            rid, self._snippet(chunk),
                        )
                        leak_logged = True
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

    def _apply_model_override(self, body: bytes) -> tuple[bytes, tuple[str, str] | None]:
        """Force ``cfg.model`` into a JSON request body, reporting any mismatch.

        Returns ``(body, mismatch)`` where ``mismatch`` is ``(requested, forced)``
        when the request asked for a different model, else ``None``. Only a JSON
        object carrying a string ``model`` field is ever touched; anything else
        (no override configured, non-JSON body, missing/non-string ``model``) is
        returned byte-for-byte unchanged.
        """
        forced = self.cfg.model
        if not forced or not body:
            return body, None
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return body, None
        if not isinstance(data, dict):
            return body, None
        requested = data.get("model")
        if not isinstance(requested, str) or requested == forced:
            return body, None
        data["model"] = forced
        new_body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return new_body, (requested, forced)

    def _log_model_override(self, requested: str, forced: str, rid: int) -> None:
        """Warn (once per distinct pair) that a request's model was overridden."""
        pair = (requested, forced)
        if pair in self._warned_pairs:
            logger.debug("[%d] model override (repeat): %r -> %r", rid, requested, forced)
            return
        self._warned_pairs.add(pair)
        logger.warning(
            "⚠️  model override: Codex requested %r → proxy forces %r", requested, forced
        )

    def _should_retry(self, attempt: int) -> bool:
        return self.cfg.max_retries is None or attempt < self.cfg.max_retries

    async def _sleep_backoff(self, attempt: int, request: Request, rid: int = 0) -> None:
        remaining = self.backoff.delay(attempt)
        if self.cfg.debug:
            logger.info("[%d] backing off %.2fs before attempt %d", rid, remaining, attempt + 1)
        slice_seconds = 0.5
        while remaining > 0:
            if await request.is_disconnected():
                raise ClientGone()
            nap = min(slice_seconds, remaining)
            await asyncio.sleep(nap)
            remaining -= nap

    def _build_url(self, request: Request, base_url: str | None = None) -> str:
        base = (base_url or self.cfg.upstream_base_url).rstrip("/")
        url = base + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        return url

    def _request_headers(self, request: Request, api_key=_USE_CFG_KEY) -> dict[str, str]:
        key = self.cfg.api_key if api_key is _USE_CFG_KEY else api_key
        out = {
            k: v for k, v in request.headers.items() if k.lower() not in _REQUEST_DROP
        }
        if key:
            # Proxy-held credential always wins: drop any incoming Authorization
            # (case-insensitively) and inject ours.
            out = {k: v for k, v in out.items() if k.lower() != "authorization"}
            out["Authorization"] = f"Bearer {key}"
        return out

    def _effective_model(self, body: bytes) -> str | None:
        """The model in a JSON request body (after any override), else ``None``."""
        if not body:
            return None
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return None
        if isinstance(data, dict) and isinstance(data.get("model"), str):
            return data["model"]
        return None

    def _resolve_route(self, model: str | None) -> tuple[str, str | None]:
        """Pick the (base_url, api_key) for a request based on its model.

        Matched model -> its route; otherwise the default route; otherwise the
        global ``upstream_base_url`` / ``api_key``. A route without its own
        ``api_key`` inherits the global one.
        """
        route = None
        if model is not None and model in self.cfg.model_routes:
            route = self.cfg.model_routes[model]
        elif self.cfg.default_route is not None:
            route = self.cfg.default_route
        if route is None:
            return self.cfg.upstream_base_url, self.cfg.api_key
        api_key = route.api_key if route.api_key is not None else self.cfg.api_key
        return route.base_url, api_key

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
