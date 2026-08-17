#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
HTTP MCP Server for SAS Viya with direct (service-account) authentication.

The standard HTTP server (:mod:`sas_mcp_server.mcp_server`, entry point ``app``)
requires every MCP client to complete a browser-based OAuth flow and forwards
that user's own token upstream. Hosts that cannot open a browser — SAS
Retrieval Agent Manager (RAM), scheduled agents, other server-to-server
integrations — need the opposite arrangement: the *server* holds one Viya
identity, and clients simply call the MCP endpoint.

That is this module. It serves the same tools over the same streamable-HTTP
transport, but resolves the Viya token from
:class:`sas_mcp_server.service_auth.ServiceTokenProvider` (refresh-token grant
preferred, password grant as fallback) instead of from a per-request header.

Because every request then acts as the configured identity, the endpoint should
not be open to anyone who can route to it: set ``MCP_API_KEY`` and clients must
present it as ``X-API-Key`` or ``Authorization: Bearer``. When the endpoint is
only reachable from inside the platform that hosts the container (RAM's own
network), leaving it unset is a deliberate choice — the server logs a warning
either way.

Entry point: ``app-http-direct`` (the container's default, ``MCP_MODE=http-direct``).
"""

import hmac
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import (
    AUTH_ENABLED,
    CLIENT_ID,
    CLIENT_SECRET,
    HOST_PORT,
    MCP_API_KEY,
    MCP_TRANSPORT,
    SERVER_NAME,
    SSL_VERIFY,
    TOKEN_ENDPOINT,
    VIYA_ENDPOINT,
    VIYA_PASSWORD,
    VIYA_REFRESH_TOKEN,
    VIYA_USERNAME,
)
from .exceptions import ConfigError
from .prompts import register_prompts
from .service_auth import ServiceTokenProvider
from .telemetry import install_telemetry
from .tools import register_tools
from .viya_client import logger
from .viya_utils import shutdown_session_cache

load_dotenv()

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

# One provider per process: it owns the cached access token and the rotating
# refresh token, so every tool call shares one upstream credential.
token_provider = ServiceTokenProvider(
    token_endpoint=TOKEN_ENDPOINT,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    refresh_token=VIYA_REFRESH_TOKEN,
    username=VIYA_USERNAME,
    password=VIYA_PASSWORD,
    verify=SSL_VERIFY,
)


async def _direct_get_token(ctx: Context) -> str:
    """Token getter for direct mode: the server's own Viya token."""
    if not AUTH_ENABLED:
        return ""
    return await token_provider.get_token()


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Tear down warm compute sessions when the server stops."""
    try:
        yield {}
    finally:
        await shutdown_session_cache()


logger.info("Connecting to SAS Viya at %s", VIYA_ENDPOINT)
if not AUTH_ENABLED:
    logger.warning(
        "VIYA_AUTH=false: SASLogon authentication is disabled; "
        "Viya API calls are sent without Authorization headers"
    )
elif not token_provider.has_credentials:
    # Warn at startup rather than only on the first tool call: a container that
    # boots "successfully" and then fails every call is far harder to diagnose.
    logger.warning(
        "No Viya credentials configured for direct HTTP mode — set "
        "VIYA_REFRESH_TOKEN (recommended) or VIYA_USERNAME/VIYA_PASSWORD. "
        "Every tool call will fail with an authentication error."
    )
else:
    logger.info("Direct mode will authenticate with the %s grant", token_provider.grant_type)

mcp = FastMCP(SERVER_NAME, lifespan=_lifespan)
# Opt-in telemetry (no-op unless COLLECTION_MODE is enabled). Added first so it
# is the outermost middleware, wrapping the tool call.
install_telemetry(mcp, "http-direct")
register_tools(mcp, _direct_get_token)
register_prompts(mcp)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy", "service": "sas-viya-execution-mcp"})


class ApiKeyMiddleware:
    """ASGI middleware that rejects HTTP requests lacking the API key.

    Accepts the key as ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``.
    ``/health`` stays open so liveness and readiness probes work without
    credentials.
    """

    def __init__(self, app: Any, api_key: str) -> None:
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        provided = headers.get("x-api-key", "")
        if not provided:
            parts = headers.get("authorization", "").split()
            if len(parts) == 2 and parts[0].lower() == "bearer":
                provided = parts[1]
        # Constant-time compare so the endpoint does not leak the key's prefix
        # through response timing.
        if not hmac.compare_digest(provided, self.api_key):
            response = JSONResponse({"error": "invalid or missing API key"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_app() -> Any:
    """Build the ASGI app, wrapping it with API key auth when one is configured."""
    if MCP_TRANSPORT not in ("http", "sse"):
        raise ConfigError(
            f"MCP_TRANSPORT must be 'http' or 'sse', got '{MCP_TRANSPORT}'."
        )
    endpoint = "/mcp" if MCP_TRANSPORT == "http" else "/sse"
    logger.info("Serving MCP over '%s' transport (endpoint: %s)", MCP_TRANSPORT, endpoint)
    app = mcp.http_app(transport=MCP_TRANSPORT)
    if MCP_API_KEY:
        logger.info("API key protection enabled (MCP_API_KEY is set)")
        return ApiKeyMiddleware(app, MCP_API_KEY)
    logger.warning(
        "MCP_API_KEY is not set — the MCP endpoint is unauthenticated. Anyone "
        "who can reach it can act on Viya as the configured identity."
    )
    return app


def main() -> None:
    """Run the MCP server over HTTP with direct Viya authentication."""
    uvicorn.run(build_app(), host="0.0.0.0", port=HOST_PORT)


if __name__ == "__main__":
    main()
