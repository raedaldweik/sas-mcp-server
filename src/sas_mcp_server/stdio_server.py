#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Stdio MCP Server for SAS Viya.
Authenticates directly to Viya using password grant, allowing MCP clients
to start the server on demand without a pre-running HTTP server.
"""

import os
import time
import httpx
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from fastmcp.exceptions import FastMCPError
from .config import VIYA_ENDPOINT, CLIENT_ID, CLIENT_SECRET, SSL_VERIFY
from .auth import select_grant
from .viya_utils import logger
from .tools import register_tools
from .prompts import register_prompts

load_dotenv()

VIYA_USERNAME = os.getenv("VIYA_USERNAME", "")
VIYA_PASSWORD = os.getenv("VIYA_PASSWORD", "")
# Preferred for SSO / federated (e.g. Okta) environments: a refresh token
# obtained once via an interactive login (see examples/get_refresh_token.py).
# When set, it is used in preference to username/password.
VIYA_REFRESH_TOKEN = os.getenv("VIYA_REFRESH_TOKEN", "")

# Refresh the cached token this many seconds before it actually expires.
_TOKEN_EXPIRY_MARGIN = 60.0
# "refresh_token" is seeded lazily from VIYA_REFRESH_TOKEN and updated if
# SAS Logon rotates it.
_token_cache = {"token": "", "expires_at": 0.0, "refresh_token": ""}


class AuthenticationError(FastMCPError):
    def __init__(self, message):
        super().__init__(message)
        self.message = message

    def __str__(self):
        return f"AuthenticationError: {self.message}"


def _get_viya_token() -> str:
    """Return a Viya access token, with caching.

    Uses the refresh_token grant when ``VIYA_REFRESH_TOKEN`` is set (required
    for SSO/federated identities such as Okta users), otherwise falls back to
    the password grant. The access token is cached and reused until shortly
    before its expiry.
    """
    if _token_cache["token"] and time.monotonic() < _token_cache["expires_at"]:
        return _token_cache["token"]

    refresh_token = _token_cache["refresh_token"] or VIYA_REFRESH_TOKEN
    data = select_grant(
        refresh_token=refresh_token,
        username=VIYA_USERNAME,
        password=VIYA_PASSWORD,
    )
    if data is None:
        raise AuthenticationError(
            "No Viya credentials configured for stdio mode. Set "
            "VIYA_REFRESH_TOKEN (required for SSO/federated environments) "
            "or VIYA_USERNAME and VIYA_PASSWORD in .env."
        )
    resp = httpx.post(
        f"{VIYA_ENDPOINT}/SASLogon/oauth/token",
        auth=(CLIENT_ID, CLIENT_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=data,
        verify=SSL_VERIFY,
    )
    resp.raise_for_status()
    body = resp.json()
    _token_cache["token"] = body["access_token"]
    expires_in = float(body.get("expires_in", 0))
    _token_cache["expires_at"] = (
        time.monotonic() + max(expires_in - _TOKEN_EXPIRY_MARGIN, 0.0)
    )
    if body.get("refresh_token"):
        _token_cache["refresh_token"] = body["refresh_token"]
    return _token_cache["token"]


# Token getter for stdio mode: acquires token via password grant
async def _stdio_get_token(ctx: Context) -> str:
    return _get_viya_token()


# Initialize the FastMCP server (no auth — stdio clients handle auth differently)
logger.info(f"Connecting to SAS Viya at {VIYA_ENDPOINT}")
mcp = FastMCP("SAS Viya Execution MCP Server")

# Register all tools and prompts
register_tools(mcp, _stdio_get_token)
register_prompts(mcp)


def main():
    """Run the MCP server in stdio mode."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
