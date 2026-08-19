#!/usr/bin/env bash
#
# Start the codex retry proxy.
#
# Configuration is read from CODEX_PROXY_* environment variables (see README).
# This script sources ~/.bashrc first, so whatever you set there
# (CODEX_PROXY_UPSTREAM_BASE_URL, CODEX_PROXY_API_KEY, ...) is picked up even
# when launched from a non-interactive shell / systemd / cron.
#
# Usage:  ./run.sh          # run in the foreground (Ctrl-C to stop)
#         ./run.sh &        # run in the background
#
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. Load user config. Tolerate anything inside .bashrc (don't let it abort us).
if [ -f "$HOME/.bashrc" ]; then
    set +e
    # shellcheck disable=SC1090
    source "$HOME/.bashrc" >/dev/null 2>&1
    set -e
fi

# 2. Activate the project virtualenv.
if [ -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.venv/bin/activate"
else
    echo "error: virtualenv not found at $SCRIPT_DIR/.venv" >&2
    echo "  create it with:" >&2
    echo "    python3 -m venv $SCRIPT_DIR/.venv" >&2
    echo "    $SCRIPT_DIR/.venv/bin/pip install -r $SCRIPT_DIR/requirements.txt" >&2
    exit 1
fi

HOST="${CODEX_PROXY_HOST:-127.0.0.1}"
PORT="${CODEX_PROXY_PORT:-8787}"

# 3. Warn if the upstream isn't configured (proxy would default to OpenAI).
if [ -z "${CODEX_PROXY_UPSTREAM_BASE_URL:-}" ]; then
    echo "warning: CODEX_PROXY_UPSTREAM_BASE_URL is not set; defaulting to https://api.openai.com" >&2
fi

# 4. Refuse to double-start on the same port.
CHECK_HOST="$HOST"
[ "$HOST" = "0.0.0.0" ] && CHECK_HOST="127.0.0.1"
if (echo > "/dev/tcp/$CHECK_HOST/$PORT") 2>/dev/null; then
    echo "error: something is already listening on $HOST:$PORT — proxy already running?" >&2
    exit 1
fi

# 5. Launch. exec so signals (and systemd, later) manage the python process directly.
cd "$SCRIPT_DIR"
exec python -m codex_proxy
