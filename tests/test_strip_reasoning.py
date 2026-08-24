"""Unit tests for reasoning-id stripping (pure, no network).

Some upstreams implement the Responses API *statelessly* (they mandate
``store=false`` and never persist items). Such a server rejects any ``reasoning``
item that carries an ``id`` with a 404 — it tries to look the id up in a store
that does not exist. This bites when the coding agent replays cross-model
history (e.g. after switching models mid-session), because those reasoning items
carry ids minted by a different upstream.

The proxy can strip ``id`` from ``reasoning`` items before forwarding, which the
stateless server accepts. Only the id is removed; ``encrypted_content`` (which
carries the actual reasoning across turns) is preserved, so a same-model session
is unaffected.
"""

from __future__ import annotations

import json

from codex_proxy.config import Config, Route
from codex_proxy.proxy import RetryProxy, _is_reasoning_id_404


# ------------------------------------------------------------ _is_reasoning_id_404

_NOT_FOUND = (
    b'{"error":{"message":"Item with id \'rs_x\' not found. Items are not '
    b'persisted when `store` is set to false. Try again..."}}'
)


def test_is_reasoning_id_404_matches_the_stateless_error():
    assert _is_reasoning_id_404(404, _NOT_FOUND) is True


def test_is_reasoning_id_404_ignores_ordinary_404():
    assert _is_reasoning_id_404(404, b'{"error":{"message":"route not found"}}') is False


def test_is_reasoning_id_404_ignores_non_404_status():
    # Same wording but a 400 must not trigger the strip-and-retry path.
    assert _is_reasoning_id_404(400, _NOT_FOUND) is False


def make_proxy(**cfg_overrides) -> RetryProxy:
    cfg = Config()
    for key, value in cfg_overrides.items():
        setattr(cfg, key, value)
    return RetryProxy(cfg, client=None)


# ------------------------------------------------------------ _strip_reasoning_ids


def test_strips_id_from_reasoning_item_keeps_encrypted_content():
    proxy = make_proxy()
    body = json.dumps({
        "model": "gpt-5.6-sol",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "reasoning", "id": "rs_ark_123", "summary": [],
             "encrypted_content": "gAAAAAB_foreign"},
            {"role": "user", "content": [{"type": "input_text", "text": "go"}]},
        ],
    }).encode()
    out = json.loads(proxy._strip_reasoning_ids(body))
    reasoning = out["input"][1]
    assert reasoning["type"] == "reasoning"
    assert "id" not in reasoning  # the offending id is gone
    assert reasoning["encrypted_content"] == "gAAAAAB_foreign"  # content preserved
    assert reasoning["summary"] == []


def test_leaves_assistant_message_id_intact():
    # The stateless server only chokes on reasoning ids; message ids are fine and
    # must be preserved so the conversation shape is unchanged.
    proxy = make_proxy()
    body = json.dumps({
        "input": [
            {"type": "message", "role": "assistant", "id": "msg_123",
             "content": [{"type": "output_text", "text": "hello"}]},
        ],
    }).encode()
    out = json.loads(proxy._strip_reasoning_ids(body))
    assert out["input"][0]["id"] == "msg_123"


def test_strips_every_reasoning_item():
    proxy = make_proxy()
    body = json.dumps({
        "input": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {"type": "message", "role": "assistant", "id": "msg_1", "content": []},
            {"type": "reasoning", "id": "rs_2", "summary": []},
        ],
    }).encode()
    out = json.loads(proxy._strip_reasoning_ids(body))
    assert "id" not in out["input"][0]
    assert out["input"][1]["id"] == "msg_1"
    assert "id" not in out["input"][2]


def test_no_reasoning_id_is_byte_for_byte_unchanged():
    proxy = make_proxy()
    body = json.dumps({
        "input": [
            {"type": "reasoning", "summary": [], "encrypted_content": "x"},
            {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        ],
    }).encode()
    assert proxy._strip_reasoning_ids(body) is body  # untouched, same object


def test_non_json_body_untouched():
    proxy = make_proxy()
    body = b"this is not json"
    assert proxy._strip_reasoning_ids(body) is body


def test_json_without_input_untouched():
    proxy = make_proxy()
    body = json.dumps({"model": "gpt-5.6-sol"}).encode()
    assert proxy._strip_reasoning_ids(body) is body


def test_string_input_untouched():
    proxy = make_proxy()
    body = json.dumps({"model": "x", "input": "just a string"}).encode()
    assert proxy._strip_reasoning_ids(body) is body


def test_empty_body_untouched():
    proxy = make_proxy()
    assert proxy._strip_reasoning_ids(b"") is not None
    assert proxy._strip_reasoning_ids(b"") == b""


# -------------------------------------------------- effective flag via _resolve_route


def test_resolve_route_returns_strip_flag_from_route():
    proxy = make_proxy(
        model_routes={"gpt-5.6-sol": Route("https://cs/v1", "cs-k", strip_reasoning_ids=True)}
    )
    base, key, strip = proxy._resolve_route("gpt-5.6-sol")
    assert (base, key, strip) == ("https://cs/v1", "cs-k", True)


def test_resolve_route_flag_defaults_false():
    proxy = make_proxy(model_routes={"m": Route("https://a", "k")})
    assert proxy._resolve_route("m") == ("https://a", "k", False)


def test_resolve_route_flag_inherits_global_when_route_unset():
    proxy = make_proxy(
        strip_reasoning_ids=True,
        model_routes={"m": Route("https://a", "k", strip_reasoning_ids=None)},
    )
    assert proxy._resolve_route("m") == ("https://a", "k", True)


def test_resolve_route_flag_route_overrides_global():
    proxy = make_proxy(
        strip_reasoning_ids=True,
        model_routes={"m": Route("https://a", "k", strip_reasoning_ids=False)},
    )
    assert proxy._resolve_route("m") == ("https://a", "k", False)


def test_resolve_route_global_fallback_uses_global_flag():
    proxy = make_proxy(
        upstream_base_url="https://g", api_key="g-k",
        strip_reasoning_ids=True, model_routes={}, default_route=None,
    )
    assert proxy._resolve_route("whatever") == ("https://g", "g-k", True)
