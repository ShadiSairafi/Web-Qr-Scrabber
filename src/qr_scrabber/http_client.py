"""Proxy- and CA-aware `httpx` client factory resilient to bare IPv6 `NO_PROXY` entries."""

from __future__ import annotations

import os
import ssl
from typing import Any

import httpx


def build_ssl_context() -> ssl.SSLContext:
    """Build an SSL context trusting `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` if configured."""
    cafile = (
        os.environ.get("SSL_CERT_FILE")
        or os.environ.get("REQUESTS_CA_BUNDLE")
        or os.environ.get("CURL_CA_BUNDLE")
    )
    if cafile and os.path.isfile(cafile):
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def _proxy_mounts_sync(verify: ssl.SSLContext) -> dict[str, httpx.BaseTransport | None]:
    http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    mounts: dict[str, httpx.BaseTransport | None] = {
        "all://localhost": None,
        "all://127.0.0.1": None,
        "all://[::1]": None,
    }
    if http_proxy:
        mounts["http://"] = httpx.HTTPTransport(proxy=http_proxy, verify=verify)
    if https_proxy:
        mounts["https://"] = httpx.HTTPTransport(proxy=https_proxy, verify=verify)
    return mounts


def _proxy_mounts_async(verify: ssl.SSLContext) -> dict[str, httpx.AsyncBaseTransport | None]:
    http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    mounts: dict[str, httpx.AsyncBaseTransport | None] = {
        "all://localhost": None,
        "all://127.0.0.1": None,
        "all://[::1]": None,
    }
    if http_proxy:
        mounts["http://"] = httpx.AsyncHTTPTransport(proxy=http_proxy, verify=verify)
    if https_proxy:
        mounts["https://"] = httpx.AsyncHTTPTransport(proxy=https_proxy, verify=verify)
    return mounts


def make_sync_client(**kwargs: Any) -> httpx.Client:
    """Create a sync `httpx.Client` honoring `HTTP_PROXY`/`HTTPS_PROXY` and `SSL_CERT_FILE`."""
    verify = build_ssl_context()
    if "transport" in kwargs:
        return httpx.Client(trust_env=False, **kwargs)
    mounts = _proxy_mounts_sync(verify)
    return httpx.Client(trust_env=False, verify=verify, mounts=mounts, **kwargs)


def make_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """Create an async `httpx.AsyncClient` honoring proxy env vars and `SSL_CERT_FILE`."""
    verify = build_ssl_context()
    if "transport" in kwargs:
        return httpx.AsyncClient(trust_env=False, **kwargs)
    mounts = _proxy_mounts_async(verify)
    return httpx.AsyncClient(trust_env=False, verify=verify, mounts=mounts, **kwargs)


__all__ = ["build_ssl_context", "make_async_client", "make_sync_client"]
