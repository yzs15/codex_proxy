from codex_proxy.config import Config
from codex_proxy.detector import EventClass, classify_sse_event, is_retryable_http
from codex_proxy.sse import SSEEvent

CFG = Config()
CAPACITY = "Selected model is at capacity. Please try a different model."


def _ev(event, data):
    return SSEEvent(event=event, data=data, raw=b"")


# --- HTTP-level -------------------------------------------------------------


def test_http_transient_status_is_retryable():
    assert is_retryable_http(503, b"", CFG)
    assert is_retryable_http(429, b"", CFG)
    assert is_retryable_http(500, b"", CFG)


def test_http_success_not_retryable():
    assert not is_retryable_http(200, b"ok", CFG)


def test_http_capacity_message_on_error_status_is_retryable():
    body = f'{{"error":{{"message":"{CAPACITY}"}}}}'.encode()
    assert is_retryable_http(400, body, CFG)


def test_http_plain_client_error_not_retryable():
    assert not is_retryable_http(400, b'{"error":"invalid request"}', CFG)


# --- SSE gate ---------------------------------------------------------------


def test_sse_capacity_failure_retries():
    ev = _ev("response.failed", f'{{"error":{{"message":"{CAPACITY}"}}}}')
    assert classify_sse_event(ev, CFG) is EventClass.RETRY


def test_sse_capacity_in_generic_error_event_retries():
    ev = _ev("error", '{"error":{"message":"The server is currently overloaded."}}')
    assert classify_sse_event(ev, CFG) is EventClass.RETRY


def test_sse_lifecycle_keeps_gate_open():
    assert classify_sse_event(_ev("response.created", '{"id":"x"}'), CFG) is EventClass.LIFECYCLE
    assert classify_sse_event(_ev("response.in_progress", "{}"), CFG) is EventClass.LIFECYCLE


def test_sse_heartbeat_is_lifecycle():
    assert classify_sse_event(_ev(None, ""), CFG) is EventClass.LIFECYCLE


def test_sse_content_commits():
    ev = _ev("response.output_text.delta", '{"delta":"Hello"}')
    assert classify_sse_event(ev, CFG) is EventClass.COMMIT


def test_sse_completed_commits():
    ev = _ev("response.completed", '{"status":"completed"}')
    assert classify_sse_event(ev, CFG) is EventClass.COMMIT


def test_sse_non_retryable_error_commits_and_forwards():
    # A genuine error that isn't a capacity signal must not loop forever.
    ev = _ev("response.failed", '{"error":{"message":"invalid prompt"}}')
    assert classify_sse_event(ev, CFG) is EventClass.COMMIT
