# codex-proxy

A transparent reverse proxy for the **Codex CLI**. When the upstream returns a
transient failure such as:

> `Selected model is at capacity. Please try a different model`

the proxy **retries with exponential backoff** instead of surfacing the error to
Codex. Downstream never sees the transient error — the request simply takes a
little longer and then succeeds.

It handles the failure in **both** forms:

- an **HTTP error status** (429 / 5xx) before any bytes are streamed, and
- a **mid-stream SSE failure** (`200 OK`, then a `response.failed` / `error`
  event carrying the capacity message).

## How it works

```
Codex CLI ──HTTP──▶ codex-proxy ──retries w/ backoff──▶ upstream (OpenAI / ChatGPT backend / custom)
```

For streaming responses the proxy uses a **buffer-until-commit gate**: it holds
back pre-generation lifecycle events, and

- if a capacity signal appears there → it discards the attempt and retries
  cleanly (nothing was sent downstream yet);
- the moment real content begins → it flushes the buffer and switches to a
  byte-for-byte passthrough for the rest of the stream (live streaming is
  preserved).

Genuine, non-transient errors (e.g. a `400 invalid request`) are **not** retried
— they are forwarded immediately, so the proxy never loops forever on a real
problem.

## Install & run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt        # or: pip install -e .

# Point at whatever backend Codex talks to, then launch:
export CODEX_PROXY_UPSTREAM_BASE_URL="https://api.openai.com"
export CODEX_PROXY_API_KEY="sk-..."   # optional: let the proxy hold the key
python -m codex_proxy        # listens on 127.0.0.1:8787 by default
```

Or just use the launcher (sources `~/.bashrc` for config, activates the venv,
refuses to double-start on the same port):

```bash
./run.sh          # foreground (Ctrl-C to stop)
./run.sh &        # background
```

Then point Codex at the proxy — set its base URL to `http://127.0.0.1:8787`.
The proxy is fully path-transparent: whatever endpoint Codex hits is forwarded
under `CODEX_PROXY_UPSTREAM_BASE_URL`, with all headers preserved.

**Credentials:** by default the proxy forwards Codex's own `Authorization`
header unchanged, so if Codex already has the key configured, nothing more is
needed. If you'd rather the proxy own the upstream key (so Codex needn't have a
valid one), set `CODEX_PROXY_API_KEY` — the proxy then injects
`Authorization: Bearer <key>` and overrides whatever Codex sent.

Health check: `GET /__proxy_health` → `ok`.

## Configuration

All settings are environment variables prefixed with `CODEX_PROXY_`.

| Variable | Default | Meaning |
|---|---|---|
| `UPSTREAM_BASE_URL` | `https://api.openai.com` | Where to forward requests. |
| `API_KEY` | *(unset)* | Upstream credential held by the proxy. When set, the proxy injects `Authorization: Bearer <key>` and **overrides** any Authorization Codex sent. Leave unset to pass Codex's own credential through unchanged. |
| `HOST` / `PORT` | `127.0.0.1` / `8787` | Listen address. |
| `MAX_RETRIES` | *(unset = infinite)* | Cap attempts; after the cap the real error is forwarded. `none`/`0` = retry forever. |
| `BACKOFF_INITIAL` | `1.0` | First backoff ceiling, seconds. |
| `BACKOFF_MULTIPLIER` | `2.0` | Growth factor per attempt. |
| `BACKOFF_MAX` | `30.0` | Backoff ceiling cap, seconds. Backoff uses full jitter. |
| `RETRYABLE_STATUS` | `429,500,502,503,504,529` | HTTP statuses treated as transient. |
| `RETRY_SUBSTRINGS` | `at capacity,try a different model,overloaded,currently overloaded` | Case-insensitive message signals that trigger a retry. |
| `LIFECYCLE_EVENTS` | `response.created,response.in_progress,response.queued` | SSE event types held back by the commit gate. |
| `MAX_PRELUDE_BYTES` | `65536` | Safety cap on pre-commit buffering. |
| `CONNECT_TIMEOUT` / `READ_TIMEOUT` / `WRITE_TIMEOUT` / `POOL_TIMEOUT` | `10` / `none` / `30` / `10` | httpx timeouts. `READ_TIMEOUT=none` waits indefinitely (needed for slow SSE). |

## Behaviour notes / limitations

- **Client disconnect** during retries/backoff stops the retry loop and cleans up.
- **Infinite retry** (the default) keeps retrying capacity errors forever with a
  capped backoff interval — the requested behaviour. Set `MAX_RETRIES` to bound it.
- A failure that arrives **after** content has already streamed cannot be retried
  safely (bytes were already sent) and is forwarded as-is. Capacity errors are
  pre-generation, so this boundary is not hit in practice.
- The request body is **buffered in memory** so it can be replayed on retry;
  fine for Responses-API-sized bodies.

## Development

```bash
pip install -e ".[test]"
pytest            # unit + end-to-end tests (real localhost servers)
```

Module layout (each independently testable):

| Module | Responsibility |
|---|---|
| `config.py` | Env-driven configuration. |
| `backoff.py` | Exponential backoff with full jitter (pure). |
| `detector.py` | Retry classification for HTTP + SSE events (pure). |
| `sse.py` | Incremental SSE decoder preserving exact raw bytes. |
| `proxy.py` | The retry engine + streaming commit gate. |
| `app.py` / `main.py` | Starlette wiring + entrypoint. |
