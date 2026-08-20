"""Shared pytest fixtures.

Tests build ``Config()`` directly, and ``Config`` reads ``CODEX_PROXY_*``
environment variables. Strip all of them before every test so the suite is
hermetic — its results can't depend on whatever a developer happens to have
exported in their shell (e.g. a real ``CODEX_PROXY_API_KEY``). A test that
needs a specific value sets it explicitly via a Config override.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("CODEX_PROXY_")]:
        monkeypatch.delenv(key, raising=False)
