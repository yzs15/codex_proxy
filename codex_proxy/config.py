"""Configuration for the Codex retry proxy.

All values can be overridden via environment variables prefixed with
``CODEX_PROXY_``. Sensible defaults let the proxy run with zero configuration
against the OpenAI API; point ``CODEX_PROXY_UPSTREAM_BASE_URL`` at whatever
backend Codex actually talks to.
"""

from __future__ import annotations

import json
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


@dataclass(frozen=True)
class Route:
    """An upstream target: where to forward, and the credential to use."""

    base_url: str
    api_key: str | None = None
    # Whether to strip ``id`` from ``reasoning`` items in the request body before
    # forwarding to this upstream. ``None`` means "inherit the global
    # ``Config.strip_reasoning_ids``"; an explicit bool overrides it. Needed for
    # stateless Responses upstreams that 404 on a reasoning item carrying an id
    # (they try to look it up in a store that ``store=false`` never populated).
    strip_reasoning_ids: bool | None = None


def _load_routes(path: str) -> tuple[dict[str, Route], Route | None]:
    """Parse a routes file into ``(model -> Route, default Route or None)``.

    The file names upstreams (each ``base_url`` + optional ``api_key``) and maps
    models to them, so a shared upstream's key is written once. Validation is
    strict and fails loudly — a misconfigured proxy should not start silently.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ValueError(f"routes file not found: {path}")
    except OSError as exc:
        raise ValueError(f"could not read routes file {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"routes file {path} is not valid JSON: {exc}")

    if not isinstance(data, dict):
        raise ValueError("routes file must be a JSON object")

    raw_upstreams = data.get("upstreams") or {}
    if not isinstance(raw_upstreams, dict):
        raise ValueError("'upstreams' must be an object")
    upstreams: dict[str, Route] = {}
    for name, spec in raw_upstreams.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("base_url"), str):
            raise ValueError(f"upstream {name!r} must have a string 'base_url'")
        api_key = spec.get("api_key")
        if api_key is not None and not isinstance(api_key, str):
            raise ValueError(f"upstream {name!r} 'api_key' must be a string")
        strip = spec.get("strip_reasoning_ids")
        if strip is not None and not isinstance(strip, bool):
            raise ValueError(
                f"upstream {name!r} 'strip_reasoning_ids' must be a boolean"
            )
        upstreams[name] = Route(
            base_url=spec["base_url"], api_key=api_key, strip_reasoning_ids=strip
        )

    raw_models = data.get("models") or {}
    if not isinstance(raw_models, dict):
        raise ValueError("'models' must be an object")
    model_routes: dict[str, Route] = {}
    for model, upstream_name in raw_models.items():
        if upstream_name not in upstreams:
            raise ValueError(
                f"model {model!r} references unknown upstream {upstream_name!r}"
            )
        model_routes[model] = upstreams[upstream_name]

    default_route: Route | None = None
    default_name = data.get("default")
    if default_name is not None:
        if default_name not in upstreams:
            raise ValueError(f"'default' references unknown upstream {default_name!r}")
        default_route = upstreams[default_name]

    return model_routes, default_route


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

    # Optional model override. When set, the proxy rewrites the ``model`` field of
    # a JSON request body to this value, forcing the upstream to use it regardless
    # of what the coding agent requested. A mismatch is surfaced out of band (a
    # log line + an ``X-Codex-Proxy-Warning`` response header) and never in the
    # response body. Leave unset to forward the requested model unchanged.
    model: str | None = field(default_factory=lambda: _env_opt("MODEL"))

    # Optional per-model upstream routing. When ``routes_file`` is set, its JSON
    # maps each model to a named upstream (base_url + optional api_key), with an
    # optional ``default`` for unmatched models. Resolved at load time into
    # ``model_routes`` and ``default_route``; a model with no route (and no
    # default) falls back to ``upstream_base_url`` / ``api_key`` above.
    routes_file: str | None = field(default_factory=lambda: _env_opt("ROUTES_FILE"))
    model_routes: dict[str, Route] = field(default_factory=dict)
    default_route: Route | None = None

    # Strip ``id`` from ``reasoning`` items in the (JSON) request body before
    # forwarding. Global default for upstreams that don't set their own
    # ``strip_reasoning_ids``; a per-route value in the routes file overrides it.
    # Only the id is removed — ``encrypted_content`` is preserved — so same-model
    # sessions are unaffected. See ``RetryProxy._strip_reasoning_ids``.
    strip_reasoning_ids: bool = field(
        default_factory=lambda: _env_bool("STRIP_REASONING_IDS", False)
    )

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
    # Experimental mode: buffer the complete SSE response before sending any
    # bytes downstream. This catches capacity failures that arrive after model
    # output has started, at the cost of streaming latency and memory usage.
    buffer_full_sse: bool = field(
        default_factory=lambda: _env_bool("BUFFER_FULL_SSE", False)
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

    def __post_init__(self) -> None:
        if self.routes_file:
            self.model_routes, self.default_route = _load_routes(self.routes_file)

    @classmethod
    def from_env(cls) -> "Config":
        return cls()
