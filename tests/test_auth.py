"""Header/credential handling in RetryProxy._request_headers."""

from starlette.requests import Request

from codex_proxy.config import Config
from codex_proxy.proxy import RetryProxy


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/responses",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def _proxy(**cfg_overrides) -> RetryProxy:
    cfg = Config()
    for key, value in cfg_overrides.items():
        setattr(cfg, key, value)
    # client is unused for header construction.
    return RetryProxy(cfg, client=None)


def _auth_values(headers: dict[str, str]) -> list[str]:
    return [v for k, v in headers.items() if k.lower() == "authorization"]


def test_passthrough_when_no_proxy_key():
    proxy = _proxy()  # api_key unset
    out = proxy._request_headers(
        _request({"Authorization": "Bearer codex-token", "X-Foo": "1"})
    )
    assert _auth_values(out) == ["Bearer codex-token"]
    assert out["x-foo"] == "1"


def test_proxy_key_overrides_downstream_auth():
    proxy = _proxy(api_key="sk-upstream")
    out = proxy._request_headers(_request({"Authorization": "Bearer codex-token"}))
    # exactly one Authorization header, carrying the proxy's key
    assert _auth_values(out) == ["Bearer sk-upstream"]


def test_proxy_key_injected_when_downstream_has_none():
    proxy = _proxy(api_key="sk-upstream")
    out = proxy._request_headers(_request({"X-Foo": "1"}))
    assert _auth_values(out) == ["Bearer sk-upstream"]


def test_hop_by_hop_and_host_headers_are_stripped():
    proxy = _proxy()
    out = proxy._request_headers(
        _request(
            {
                "Host": "proxy:8787",
                "Connection": "keep-alive",
                "Content-Length": "10",
                "Accept-Encoding": "gzip",
                "X-Keep": "yes",
            }
        )
    )
    lowered = {k.lower() for k in out}
    assert {"host", "connection", "content-length", "accept-encoding"} & lowered == set()
    assert out["x-keep"] == "yes"
