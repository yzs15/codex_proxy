"""Configuration for the Codex retry proxy.

All values can be overridden via environment variables prefixed with
``CODEX_PROXY_``. Sensible defaults let the proxy run with zero configuration
against the OpenAI API; point ``CODEX_PROXY_UPSTREAM_BASE_URL`` at whatever
backend Codex actually talks to.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(f"CODEX_PROXY_{name}", default)


def _env_opt(name: str) -> str | None:
    value = os.environ.get(f"CODEX_PROXY_{name}")
    return value if value not in (None, "") else None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(f"CODEX_PROXY_{name}")
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(f"CODEX_PROXY_{name}")
    return float(raw) if raw not in (None, "") else default


def _env_int_or_none(name: str, default: int | None) -> int | None:
    raw = os.environ.get(f"CODEX_PROXY_{name}")
    if raw in (None, ""):
        return default
    if raw.strip().lower() in ("none", "0", "-1", "inf", "infinite"):
        return None
    return int(raw)


def _env_read_timeout(name: str, default: float | None) -> float | None:
    """Read timeout accepts ``none`` to disable (required for long SSE streams)."""
    raw = os.environ.get(f"CODEX_PROXY_{name}")
    if raw is None or raw == "":
        return default
    if raw.strip().lower() in ("none", "0"):
        return None
    return float(raw)


def _env_csv(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(f"CODEX_PROXY_{name}")
    if raw in (None, ""):
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_int_set(name: str, default: set[int]) -> set[int]:
    raw = os.environ.get(f"CODEX_PROXY_{name}")
    if raw in (None, ""):
        return set(default)
    return {int(item.strip()) for item in raw.split(",") if item.strip()}


# Default substrings that mark an upstream response as "retry me". Kept tight so
# they only match capacity/overload signals, never legitimate model output. The
# buffer-until-commit gate also guarantees these are only tested against
# pre-generation lifecycle/error events, never streamed content.
DEFAULT_RETRY_SUBSTRINGS = [
    "at capacity",
    "try a different model",
    "overloaded",
    "currently overloaded",
]

# HTTP status codes that are always considered transient.
DEFAULT_RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}

# SSE ``event:`` types that are pre-generation lifecycle noise. While only these
# (or empty-data heartbeats) have arrived, the response has not committed to real
# content and can still be safely retried.
DEFAULT_LIFECYCLE_EVENTS = {
    "response.created",
    "response.in_progress",
    "response.queued",
}


@dataclass
class Config:
    # --- networking ---
    upstream_base_url: str = field(
        default_factory=lambda: _env("UPSTREAM_BASE_URL", "https://api.openai.com")
    )
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8787")))

    # Optional upstream credential held by the proxy. When set, the proxy injects
    # ``Authorization: Bearer <api_key>`` on the upstream request, overriding any
    # Authorization header sent by Codex. Leave unset to pass Codex's own
    # credential through unchanged.
    api_key: str | None = field(default_factory=lambda: _env_opt("API_KEY"))

    # --- retry policy ---
    # ``None`` means retry forever (the default behaviour requested for capacity
    # errors). A positive integer caps attempts, after which the last upstream
    # error is forwarded downstream instead of hanging.
    max_retries: int | None = field(
        default_factory=lambda: _env_int_or_none("MAX_RETRIES", None)
    )
    backoff_initial: float = field(
        default_factory=lambda: _env_float("BACKOFF_INITIAL", 1.0)
    )
    backoff_multiplier: float = field(
        default_factory=lambda: _env_float("BACKOFF_MULTIPLIER", 2.0)
    )
    backoff_max: float = field(
        default_factory=lambda: _env_float("BACKOFF_MAX", 30.0)
    )

    # --- detection ---
    retryable_status_codes: set[int] = field(
        default_factory=lambda: _env_int_set("RETRYABLE_STATUS", DEFAULT_RETRYABLE_STATUS)
    )
    retry_message_substrings: list[str] = field(
        default_factory=lambda: _env_csv("RETRY_SUBSTRINGS", DEFAULT_RETRY_SUBSTRINGS)
    )
    lifecycle_event_types: set[str] = field(
        default_factory=lambda: set(
            _env_csv("LIFECYCLE_EVENTS", sorted(DEFAULT_LIFECYCLE_EVENTS))
        )
    )
    # Safety valve: if this many bytes of pre-commit SSE events accumulate without
    # a decision, force a commit + passthrough rather than buffering unbounded.
    max_prelude_bytes: int = field(
        default_factory=lambda: int(_env("MAX_PRELUDE_BYTES", "65536"))
    )

    # --- diagnostics ---
    # When on, logs every upstream attempt's status/body/SSE events, each retry
    # decision, and warns if a capacity signal leaks downstream. Off by default.
    debug: bool = field(default_factory=lambda: _env_bool("DEBUG", False))
    # File to write diagnostic logs to; when unset, they go to stderr.
    debug_log: str | None = field(default_factory=lambda: _env_opt("DEBUG_LOG"))
    # Max bytes of any body/event payload written to the diagnostic log.
    debug_body_limit: int = field(
        default_factory=lambda: int(_env("DEBUG_BODY_LIMIT", "4000"))
    )

    # --- httpx timeouts ---
    connect_timeout: float = field(
        default_factory=lambda: _env_float("CONNECT_TIMEOUT", 10.0)
    )
    # ``None`` (the default) waits indefinitely between SSE chunks, which is
    # required for long-running generations that stream slowly.
    read_timeout: float | None = field(
        default_factory=lambda: _env_read_timeout("READ_TIMEOUT", None)
    )
    write_timeout: float = field(
        default_factory=lambda: _env_float("WRITE_TIMEOUT", 30.0)
    )
    pool_timeout: float = field(
        default_factory=lambda: _env_float("POOL_TIMEOUT", 10.0)
    )

    @classmethod
    def from_env(cls) -> "Config":
        return cls()
