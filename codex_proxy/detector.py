"""Pure retry-classification logic.

Two decisions live here, both side-effect free:

* :func:`is_retryable_http` — should a non-streaming / pre-stream HTTP response
  be retried?
* :func:`classify_sse_event` — for the buffer-until-commit gate, is a streamed
  event a retry signal, harmless pre-generation lifecycle noise, or the point at
  which the response has committed to real content?
"""

from __future__ import annotations

import json
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


def contains_retry_signal(text: str, cfg: Config) -> bool:
    """Public: does this text carry a known capacity/overload signal?

    Used for non-SSE bodies. SSE streams use :func:`is_capacity_sse_event` so
    ordinary generated text that quotes the phrase is not treated as a failure.
    """
    return _matches_message(text, cfg)


_PRE_CONTENT_EVENTS = {
    # These announce an output item/content part, but do not carry generated
    # bytes themselves. In particular, Responses streams commonly emit a
    # reasoning ``response.output_item.added`` before either text or a tool
    # call starts. Committing there makes a later capacity failure leak.
    "response.output_item.added",
    "response.output_item.done",
    "response.content_part.added",
    "response.content_part.done",
    # Reasoning summaries are metadata, not user-visible generated content.
    "response.reasoning_summary_part.added",
    "response.reasoning_summary_part.done",
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_text.done",
    # Gateway-level heartbeats may carry a non-empty JSON payload. They are
    # still pre-generation noise and must not commit the stream before a
    # capacity/error event arrives.
    "keepalive",
    "ping",
    "heartbeat",
    "response.keepalive",
}


def _payload(data: str) -> dict | None:
    """Decode an SSE data payload when it is a JSON object."""
    try:
        value = json.loads(data)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def is_capacity_sse_event(event: SSEEvent, cfg: Config) -> bool:
    """Return whether an SSE event is an upstream capacity/overload failure.

    Capacity matching is intentionally scoped to error-shaped events. Looking
    for the phrase in every post-commit chunk produces false positives whenever
    an otherwise valid assistant answer quotes the error message. A few
    gateways encode the failure as ``response.output_text.done`` with a visible
    warning marker, so that form is supported explicitly as well.
    """
    explicit_type = (event.event or "").strip().lower()
    payload = _payload(event.data)
    payload_type = str(payload.get("type", "")).strip().lower() if payload else ""
    etype = explicit_type or payload_type

    # A custom event name itself can carry the configured signal.
    if _matches_message(etype, cfg):
        return True

    is_error = "error" in etype or "failed" in etype
    if is_error:
        return _matches_message(event.data, cfg)

    # Some Responses-compatible gateways put the error object on a generic
    # event. Only inspect structured error fields here, never arbitrary text.
    if payload:
        error = payload.get("error")
        if error is not None and _matches_message(
            json.dumps(error, ensure_ascii=False), cfg
        ):
            return True
        response = payload.get("response")
        if isinstance(response, dict) and response.get("error") is not None:
            if _matches_message(
                json.dumps(response["error"], ensure_ascii=False), cfg
            ):
                return True

        # The observed gateway's fallback form is a warning rendered in the
        # output_text.done event. Require the warning marker to distinguish it
        # from a normal answer that merely mentions capacity.
        if etype == "response.output_text.done":
            text = payload.get("text")
            if isinstance(text, str) and text.lstrip().startswith(("⚠", "⚠️")):
                return _matches_message(text, cfg)
    return False


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

    Ordering matters: a structured capacity message always wins (RETRY), then
    genuine errors that are *not* retryable are surfaced (COMMIT/forward), then
    lifecycle noise keeps the gate open, and anything else means content has
    started.
    """
    explicit_type = (event.event or "").strip().lower()
    payload = _payload(event.data)
    payload_type = str(payload.get("type", "")).strip().lower() if payload else ""
    etype = explicit_type or payload_type
    is_error = "error" in etype or "failed" in etype

    # 1. Capacity / overload signal in an error-shaped event -> retry.
    if is_capacity_sse_event(event, cfg):
        return EventClass.RETRY

    # 2. A real error that is NOT a known-transient one: don't loop forever,
    #    forward it downstream by committing.
    if is_error:
        return EventClass.COMMIT

    # 3. Empty-payload heartbeats/pings and known lifecycle events keep the gate
    #    open — nothing has been generated yet, retrying is still safe.
    if not event.data.strip():
        return EventClass.LIFECYCLE
    if etype in cfg.lifecycle_event_types or etype in _PRE_CONTENT_EVENTS:
        return EventClass.LIFECYCLE

    # 4. Anything with real payload and a non-lifecycle type => content started.
    return EventClass.COMMIT
