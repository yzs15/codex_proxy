"""Minimal incremental Server-Sent-Events decoder.

Only what the proxy needs: split a byte stream into whole events while keeping
the *exact* raw bytes of each event so they can be forwarded downstream verbatim
once the response commits. Field parsing (``event:`` / ``data:``) is just enough
to classify events; it is never used to re-serialise the stream.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SSEEvent:
    event: str | None  # the ``event:`` field, if present
    data: str  # concatenated ``data:`` lines, joined with "\n"
    raw: bytes  # exact bytes of this event including its terminating blank line


def _find_boundary(buf: bytes) -> int | None:
    """Return the index just past the earliest event terminator, or ``None``.

    An SSE event is terminated by a blank line: ``\\n\\n`` or ``\\r\\n\\r\\n``.
    """
    lf = buf.find(b"\n\n")
    crlf = buf.find(b"\r\n\r\n")
    candidates = [i for i in (lf, crlf) if i != -1]
    if not candidates:
        return None
    idx = min(candidates)
    # Advance past the full terminator so the boundary bytes stay with the event.
    if buf[idx:idx + 4] == b"\r\n\r\n":
        return idx + 4
    return idx + 2


def parse_event(raw: bytes) -> SSEEvent:
    event: str | None = None
    data_lines: list[str] = []
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line or line.startswith(":"):
            # blank line (terminator) or comment/heartbeat
            continue
        if ":" in line:
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
    return SSEEvent(event=event, data="\n".join(data_lines), raw=raw)


class SSEDecoder:
    """Feed bytes, get back whole ``SSEEvent`` objects; partial data is retained."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        self._buf += chunk
        events: list[SSEEvent] = []
        while True:
            end = _find_boundary(self._buf)
            if end is None:
                break
            raw = self._buf[:end]
            self._buf = self._buf[end:]
            events.append(parse_event(raw))
        return events

    @property
    def leftover(self) -> bytes:
        """Bytes buffered so far that do not yet form a complete event."""
        return self._buf
