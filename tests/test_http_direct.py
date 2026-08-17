# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the direct-auth HTTP server (``app-http-direct``).

The token mechanics live in ``test_service_auth``; what matters here is that
the server wires the provider in, guards the endpoint with the API key, honours
the transport setting, and registers the same tools as the other entry points.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import Client

from sas_mcp_server import http_direct_server as hds
from sas_mcp_server.exceptions import ConfigError

# ---------------------------------------------------------------------------
# Token getter
# ---------------------------------------------------------------------------


async def test_token_getter_delegates_to_the_provider():
    with patch.object(hds.token_provider, "get_token", AsyncMock(return_value="tok")):
        assert await hds._direct_get_token(None) == "tok"


async def test_token_getter_returns_empty_when_auth_disabled():
    get_token = AsyncMock(return_value="tok")
    with patch.object(hds, "AUTH_ENABLED", False), \
         patch.object(hds.token_provider, "get_token", get_token):
        assert await hds._direct_get_token(None) == ""
    get_token.assert_not_called()


# ---------------------------------------------------------------------------
# API key middleware
# ---------------------------------------------------------------------------


async def _ok_app(scope, receive, send):
    from starlette.responses import JSONResponse

    await JSONResponse({"ok": True})(scope, receive, send)


def _asgi_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, 401),
        ({"X-API-Key": "secret-key"}, 200),
        ({"Authorization": "Bearer secret-key"}, 200),
        ({"X-API-Key": "wrong"}, 401),
        ({"Authorization": "Bearer wrong"}, 401),
        # A key that is a prefix of the real one must not pass.
        ({"X-API-Key": "secret"}, 401),
    ],
)
async def test_api_key_middleware_gates_the_mcp_endpoint(headers, expected):
    app = hds.ApiKeyMiddleware(_ok_app, "secret-key")
    async with _asgi_client(app) as client:
        resp = await client.get("/mcp", headers=headers)
    assert resp.status_code == expected


async def test_api_key_middleware_leaves_health_open():
    """Liveness probes have no credentials to present."""
    app = hds.ApiKeyMiddleware(_ok_app, "secret-key")
    async with _asgi_client(app) as client:
        resp = await client.get("/health")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# App construction
# ---------------------------------------------------------------------------


def test_build_app_rejects_an_unsupported_transport():
    with patch.object(hds, "MCP_TRANSPORT", "websocket"), pytest.raises(ConfigError):
        hds.build_app()


def test_build_app_passes_the_transport_through():
    with patch.object(hds, "MCP_TRANSPORT", "sse"), \
         patch.object(hds, "MCP_API_KEY", ""), \
         patch.object(hds.mcp, "http_app") as http_app:
        hds.build_app()
    http_app.assert_called_once_with(transport="sse")


def test_build_app_wraps_with_api_key_when_configured():
    with patch.object(hds, "MCP_TRANSPORT", "http"), patch.object(hds, "MCP_API_KEY", "k"):
        assert isinstance(hds.build_app(), hds.ApiKeyMiddleware)


def test_build_app_is_unwrapped_without_an_api_key():
    with patch.object(hds, "MCP_TRANSPORT", "http"), patch.object(hds, "MCP_API_KEY", ""):
        assert not isinstance(hds.build_app(), hds.ApiKeyMiddleware)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


async def test_tools_are_registered_on_the_direct_server():
    async with Client(hds.mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert "execute_sas_code" in names
    assert "list_compute_contexts" in names


async def test_health_route_is_served():
    app = hds.build_app()
    async with _asgi_client(app) as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"
