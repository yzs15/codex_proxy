"""Pure retry-classification logic.

Two decisions live here, both side-effect free:

* :func:`is_retryable_http` — should a non-streaming / pre-stream HTTP response
  be retried?
* :func:`classify_sse_event` — for the buffer-until-commit gate, is a streamed
  event a retry signal, harmless pre-generation lifecycle noise, or the point at
  which the response has committed to real content?
"""

from __future__ import annotations

from enum import Enum, auto

from .config import Config
from .sse import SSEEvent


class EventClass(Enum):
    RETRY = auto()  # capacity/overload signal — discard and retry upstream
    LIFECYCLE = auto()  # pre-generation noise — keep buffering, decision pending
    COMMIT = auto()  # real content (or a non-retryable end state) — flush + passthrough


def _matches_message(text: str, cfg: Config) -> bool:
    lowered = text.lower()
    return any(sub.lower() in lowered for sub in cfg.retry_message_substrings)


def is_retryable_http(status: int, body: bytes, cfg: Config) -> bool:
    """True if a non-streaming HTTP response should be retried.

    A response is retryable when its status is in the configured transient set,
    or when it is an error status whose body carries a known capacity/overload
    message.
    """
    if status in cfg.retryable_status_codes:
        return True
    if status >= 400:
        return _matches_message(body.decode("utf-8", errors="replace"), cfg)
    return False


def classify_sse_event(event: SSEEvent, cfg: Config) -> EventClass:
    """Classify a single SSE event within a ``200 OK`` stream.

    Ordering matters: a capacity message always wins (RETRY), then genuine
    errors that are *not* retryable are surfaced (COMMIT/forward), then lifecycle
    noise keeps the gate open, and anything else means content has started.
    """
    etype = (event.event or "").strip().lower()
    is_error = "error" in etype or "failed" in etype

    # 1. Capacity / overload signal anywhere (event type or payload) -> retry.
    if _matches_message(etype, cfg) or _matches_message(event.data, cfg):
        return EventClass.RETRY

    # 2. A real error that is NOT a known-transient one: don't loop forever,
    #    forward it downstream by committing.
    if is_error:
        return EventClass.COMMIT

    # 3. Empty-payload heartbeats/pings and known lifecycle events keep the gate
    #    open — nothing has been generated yet, retrying is still safe.
    if not event.data.strip():
        return EventClass.LIFECYCLE
    if etype in cfg.lifecycle_event_types:
        return EventClass.LIFECYCLE

    # 4. Anything with real payload and a non-lifecycle type => content started.
    return EventClass.COMMIT
