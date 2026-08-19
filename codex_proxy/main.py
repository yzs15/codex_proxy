"""Entrypoint: ``python -m codex_proxy`` (or ``codex-proxy`` if installed)."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import Config


def main() -> None:
    cfg = Config.from_env()
    app = create_app(cfg)
    print(
        f"codex-proxy listening on http://{cfg.host}:{cfg.port} "
        f"-> {cfg.upstream_base_url} "
        f"(max_retries={'∞' if cfg.max_retries is None else cfg.max_retries})"
    )
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
