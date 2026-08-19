from codex_proxy.sse import SSEDecoder, parse_event


def test_decode_two_complete_events():
    d = SSEDecoder()
    events = d.feed(b"event: a\ndata: 1\n\nevent: b\ndata: 2\n\n")
    assert [(e.event, e.data) for e in events] == [("a", "1"), ("b", "2")]
    assert events[0].raw == b"event: a\ndata: 1\n\n"
    assert d.leftover == b""


def test_partial_event_is_buffered_until_complete():
    d = SSEDecoder()
    assert d.feed(b"event: a\ndata: 1") == []
    assert d.leftover == b"event: a\ndata: 1"
    events = d.feed(b"\n\nevent: b\n")
    assert len(events) == 1
    assert events[0].event == "a"
    assert d.leftover == b"event: b\n"


def test_multiline_data_joined_with_newline():
    ev = parse_event(b"data: line1\ndata: line2\n\n")
    assert ev.data == "line1\nline2"


def test_comment_lines_ignored():
    ev = parse_event(b": this is a heartbeat\n\n")
    assert ev.event is None
    assert ev.data == ""


def test_crlf_framing():
    d = SSEDecoder()
    events = d.feed(b"event: a\r\ndata: 1\r\n\r\n")
    assert len(events) == 1
    assert events[0].event == "a"
    assert events[0].data == "1"
    assert events[0].raw == b"event: a\r\ndata: 1\r\n\r\n"
    assert d.leftover == b""


def test_raw_bytes_preserved_exactly():
    d = SSEDecoder()
    payload = b"event: x\ndata: {\"k\": \"v\"}\n\n"
    events = d.feed(payload)
    assert events[0].raw == payload
