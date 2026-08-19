"""Codex retry proxy.

A transparent reverse proxy that sits between the Codex CLI and its upstream
(OpenAI API / ChatGPT backend / any custom base URL). When the upstream reports
a transient failure such as ``Selected model is at capacity. Please try a
different model``, the proxy retries with exponential backoff instead of
surfacing the error downstream.
"""

from .config import Config

__all__ = ["Config"]
