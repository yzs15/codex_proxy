"""Unit tests for the model-override logic (pure, no network).

The proxy can be told to force a specific model via ``CODEX_PROXY_MODEL``. When
the coding agent asked for a different model, the request body is rewritten to
the forced model and the mismatch is reported so the caller can warn about it
(out of band — never in the response body).
"""

from __future__ import annotations

import json

from codex_proxy.config import Config
from codex_proxy.proxy import RetryProxy, _sanitize_header_value


def make_proxy(**cfg_overrides) -> RetryProxy:
    cfg = Config()
    for key, value in cfg_overrides.items():
        setattr(cfg, key, value)
    return RetryProxy(cfg, client=None)


def test_override_rewrites_model_and_reports_mismatch():
    proxy = make_proxy(model="o3")
    body = json.dumps({"model": "gpt-5.6-sol", "input": "hi"}).encode()
    new_body, mismatch = proxy._apply_model_override(body)
    decoded = json.loads(new_body)
    assert decoded["model"] == "o3"
    assert decoded["input"] == "hi"  # the rest of the body is preserved
    assert mismatch == ("gpt-5.6-sol", "o3")


def test_no_override_when_model_matches():
    proxy = make_proxy(model="o3")
    body = json.dumps({"model": "o3"}).encode()
    new_body, mismatch = proxy._apply_model_override(body)
    assert new_body == body  # untouched, byte-for-byte
    assert mismatch is None


def test_no_override_when_model_unset():
    proxy = make_proxy(model=None)
    body = json.dumps({"model": "gpt-5.6-sol"}).encode()
    new_body, mismatch = proxy._apply_model_override(body)
    assert new_body == body
    assert mismatch is None


def test_non_json_body_is_left_untouched():
    proxy = make_proxy(model="o3")
    body = b"this is not json"
    new_body, mismatch = proxy._apply_model_override(body)
    assert new_body == body
    assert mismatch is None


def test_json_without_model_key_is_left_untouched():
    proxy = make_proxy(model="o3")
    body = json.dumps({"input": "hi"}).encode()
    new_body, mismatch = proxy._apply_model_override(body)
    assert new_body == body
    assert mismatch is None


def test_non_string_model_value_is_left_untouched():
    proxy = make_proxy(model="o3")
    body = json.dumps({"model": None}).encode()
    new_body, mismatch = proxy._apply_model_override(body)
    assert new_body == body
    assert mismatch is None


def test_sanitize_header_strips_crlf_and_controls():
    # A client-supplied model name must not be able to inject a header line.
    assert _sanitize_header_value("o3\r\nX-Evil: 1") == "o3X-Evil: 1"
    assert _sanitize_header_value("gpt-5.6-sol") == "gpt-5.6-sol"
