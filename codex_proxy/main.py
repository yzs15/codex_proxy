"""Entrypoint: ``python -m codex_proxy`` (or ``codex-proxy`` if installed)."""

from __future__ import annotations

import sys

import uvicorn

from .app import create_app
from .config import Config


def main() -> None:
    try:
        cfg = Config.from_env()
        app = create_app(cfg)
    except ValueError as exc:
        # Bad configuration (e.g. a malformed routes file) — fail fast with a
        # clear message instead of a traceback, and don't start misconfigured.
        print(f"codex-proxy: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(
        f"codex-proxy listening on http://{cfg.host}:{cfg.port} "
        f"-> {cfg.upstream_base_url} "
        f"(max_retries={'∞' if cfg.max_retries is None else cfg.max_retries})"
    )
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
