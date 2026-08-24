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

## Model override

Set `CODEX_PROXY_MODEL` to force every request onto a specific model. The proxy
rewrites the `model` field of the (JSON) request body before forwarding — and
before the retry loop, so the override survives retries. Requests that aren't a
JSON object with a `model` field (health checks, `GET`s, etc.) pass through
untouched.

When the model Codex requested differs from the forced one, the proxy raises a
warning **out of band**, never in the response body (the assistant's answer
stays clean):

- a log line on the proxy's own terminal / `DEBUG_LOG`, e.g.
  `⚠️  model override: Codex requested 'gpt-5.6-sol' → proxy forces 'o3'`
  (logged once per distinct requested→forced pair to avoid spamming);
- an `X-Codex-Proxy-Warning: requested 'gpt-5.6-sol' but proxy served 'o3'`
  response header on every affected response, for tooling / inspection.

Note: the Codex CLI does **not** display arbitrary response headers, so the
header is for logs and tooling — watch the proxy's terminal for the warning.

## Per-model upstream routing

Set `CODEX_PROXY_ROUTES_FILE` to a JSON file to send different models to
different upstreams (each with its own credential). You name the upstreams once
(so a shared upstream's key isn't repeated) and map models to them:

```json
{
  "upstreams": {
    "ark":         { "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3", "api_key": "ark-..." },
    "modelserver": { "base_url": "https://code.ai.cs.ac.cn/v1",                    "api_key": "ms-..." }
  },
  "default": "modelserver",
  "models": {
    "glm-5.3":           "ark",
    "deepseek-v4-flash": "ark",
    "gpt-5.6-sol":       "modelserver"
  }
}
```

Resolution, per request, on the request body's `model` (after any override):

- `model` listed in `models` → that upstream's `base_url` + `api_key`;
- otherwise the `default` upstream;
- `default` omitted (or no routes file) → the global `UPSTREAM_BASE_URL` /
  `API_KEY` — so existing single-upstream setups are unchanged;
- an upstream may omit `api_key` → it inherits the global `API_KEY`.

The path stays transparent (`upstream.base_url` + the incoming path), so each
upstream's own prefix lives in its `base_url`. A malformed routes file (bad
JSON, or a model/`default` naming an unknown upstream) makes the proxy refuse to
start with a clear message rather than run misconfigured.

### Stateless upstreams: `strip_reasoning_ids`

Some backends implement the Responses API **statelessly** — they mandate
`store: false` and never persist items. Such a server returns **404** for any
`reasoning` item that carries an `id` (it tries to resolve the id against a store
that was never populated):

> `Item with id 'rs_...' not found. Items are not persisted when 'store' is set
> to false.`

This bites when you **switch models mid-session**: the coding agent replays the
earlier turns, and reasoning items minted by the *other* upstream travel along
with their ids to the stateless one, which doesn't recognise them → 404. (A
same-model session is fine: the server **does** recognise the reasoning ids it
just minted itself, so those replay cleanly.)

Set `"strip_reasoning_ids": true` on such an upstream (or globally via
`CODEX_PROXY_STRIP_REASONING_IDS=1`) to handle it **adaptively**:

1. the request is forwarded **verbatim** (reasoning ids intact);
2. **only if** the upstream then returns that specific *item-not-found* 404 does
   the proxy strip `id` from the `reasoning` items and **retry once**.

This is deliberately adaptive rather than unconditional: a same-model turn keeps
its own reasoning ids (the server accepts them), so nothing is stripped and
reasoning continuity is untouched; stripping happens only for the foreign ids
that actually get rejected. Only the id is ever removed — `encrypted_content` and
every other item are preserved. The cost is one extra round-trip on the turns
that do carry foreign ids.

```json
{
  "upstreams": {
    "ark":         { "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3", "api_key": "ark-..." },
    "modelserver": { "base_url": "https://code.ai.cs.ac.cn/v1", "api_key": "ms-...", "strip_reasoning_ids": true }
  },
  "default": "modelserver",
  "models": { "glm-5.3": "ark", "deepseek-v4-flash": "ark" }
}
```

**Only enable it for upstreams that need it.** A genuine OpenAI-compatible
backend that *does* support `store` expects the reasoning `id` on replay;
stripping it there would degrade or break reasoning continuity. A per-route
value overrides the global default (route `null`/unset → inherit global).

## Configuration

All settings are environment variables prefixed with `CODEX_PROXY_`.

| Variable | Default | Meaning |
|---|---|---|
| `UPSTREAM_BASE_URL` | `https://api.openai.com` | Where to forward requests. |
| `API_KEY` | *(unset)* | Upstream credential held by the proxy. When set, the proxy injects `Authorization: Bearer <key>` and **overrides** any Authorization Codex sent. Leave unset to pass Codex's own credential through unchanged. |
| `MODEL` | *(unset)* | Force a specific model. When set, the proxy rewrites the `model` field of a JSON request body to this value, overriding whatever model Codex requested. On a mismatch it warns **out of band** — see *Model override* below. Leave unset to forward the requested model unchanged. |
| `ROUTES_FILE` | *(unset)* | Path to a JSON file routing different models to different upstreams (each with its own `base_url` + optional `api_key`), with a `default` for unmatched models. See *Per-model upstream routing* below. Unset → single upstream via `UPSTREAM_BASE_URL`. |
| `STRIP_REASONING_IDS` | `false` | Global default for the adaptive reasoning-id fix: on an *item-not-found* 404 from a stateless upstream, strip `id` from `reasoning` items and retry once (same-model turns are forwarded verbatim). A per-upstream `strip_reasoning_ids` in the routes file overrides this. See *Stateless upstreams* above. |
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
