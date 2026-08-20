"""Unit tests for per-model upstream routing.

A routes file maps each model to a named upstream (base_url + optional api_key),
with a ``default`` for unmatched models. The proxy resolves, per request, which
upstream base_url + api_key to use based on the request's model.
"""

from __future__ import annotations

import json

import pytest

from codex_proxy.config import Config, Route, _load_routes
from codex_proxy.proxy import RetryProxy


def write_routes(tmp_path, obj) -> str:
    p = tmp_path / "routes.json"
    p.write_text(json.dumps(obj))
    return str(p)


# --------------------------------------------------------------- _load_routes


def test_load_routes_maps_models_to_upstreams(tmp_path):
    path = write_routes(tmp_path, {
        "upstreams": {
            "ark": {"base_url": "https://ark/api", "api_key": "ark-k"},
            "cs": {"base_url": "https://cs/v1", "api_key": "cs-k"},
        },
        "default": "cs",
        "models": {"glm-5.3": "ark", "gpt-5.6-sol": "cs"},
    })
    routes, default = _load_routes(path)
    assert routes["glm-5.3"] == Route("https://ark/api", "ark-k")
    assert routes["gpt-5.6-sol"] == Route("https://cs/v1", "cs-k")
    assert default == Route("https://cs/v1", "cs-k")


def test_load_routes_api_key_is_optional(tmp_path):
    path = write_routes(tmp_path, {
        "upstreams": {"ark": {"base_url": "https://ark/api"}},
        "models": {"glm-5.3": "ark"},
    })
    routes, default = _load_routes(path)
    assert routes["glm-5.3"] == Route("https://ark/api", None)
    assert default is None


def test_load_routes_unknown_upstream_in_models_errors(tmp_path):
    path = write_routes(tmp_path, {
        "upstreams": {"ark": {"base_url": "https://ark/api"}},
        "models": {"glm-5.3": "nope"},
    })
    with pytest.raises(ValueError, match="unknown upstream"):
        _load_routes(path)


def test_load_routes_unknown_default_errors(tmp_path):
    path = write_routes(tmp_path, {
        "upstreams": {"ark": {"base_url": "https://ark/api"}},
        "default": "nope",
    })
    with pytest.raises(ValueError, match="unknown upstream"):
        _load_routes(path)


def test_load_routes_bad_json_errors(tmp_path):
    p = tmp_path / "routes.json"
    p.write_text("{ not json")
    with pytest.raises(ValueError):
        _load_routes(str(p))


def test_load_routes_missing_file_errors(tmp_path):
    with pytest.raises(ValueError):
        _load_routes(str(tmp_path / "does-not-exist.json"))


def test_config_loads_routes_file(tmp_path):
    path = write_routes(tmp_path, {
        "upstreams": {"ark": {"base_url": "https://ark/api", "api_key": "ark-k"}},
        "models": {"glm-5.3": "ark"},
    })
    cfg = Config(routes_file=path)
    assert cfg.model_routes["glm-5.3"] == Route("https://ark/api", "ark-k")


# --------------------------------------------------------------- _resolve_route


def make_proxy(**cfg_overrides) -> RetryProxy:
    cfg = Config()
    for key, value in cfg_overrides.items():
        setattr(cfg, key, value)
    return RetryProxy(cfg, client=None)


def test_resolve_route_matched_model():
    proxy = make_proxy(model_routes={"glm-5.3": Route("https://ark/api", "ark-k")})
    assert proxy._resolve_route("glm-5.3") == ("https://ark/api", "ark-k")


def test_resolve_route_unmatched_uses_default():
    proxy = make_proxy(model_routes={}, default_route=Route("https://cs/v1", "cs-k"))
    assert proxy._resolve_route("whatever") == ("https://cs/v1", "cs-k")


def test_resolve_route_unmatched_no_default_uses_global():
    proxy = make_proxy(
        upstream_base_url="https://global", api_key="g-k",
        model_routes={}, default_route=None,
    )
    assert proxy._resolve_route("whatever") == ("https://global", "g-k")


def test_resolve_route_missing_api_key_falls_back_to_global():
    proxy = make_proxy(
        api_key="g-k",
        model_routes={"glm-5.3": Route("https://ark/api", None)},
    )
    assert proxy._resolve_route("glm-5.3") == ("https://ark/api", "g-k")
