# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for headless (service-account) Viya authentication.

Covers the grant-selection helpers and :class:`ServiceTokenProvider`'s caching,
single-flight refresh, refresh-token rotation and failure reporting.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sas_mcp_server.exceptions import AuthenticationError
from sas_mcp_server.service_auth import (
    ServiceTokenProvider,
    client_request,
    select_grant,
)

TOKEN_URL = "https://viya.example.com/SASLogon/oauth/token"


# ---------------------------------------------------------------------------
# Grant selection
# ---------------------------------------------------------------------------


def test_refresh_token_wins_over_password():
    assert select_grant(refresh_token="rt", username="u", password="p") == {
        "grant_type": "refresh_token",
        "refresh_token": "rt",
    }


def test_password_grant_when_no_refresh_token():
    assert select_grant(username="u", password="p") == {
        "grant_type": "password",
        "username": "u",
        "password": "p",
    }


def test_none_when_credentials_incomplete():
    assert select_grant(username="u") is None
    assert select_grant(password="p") is None
    assert select_grant() is None


def test_client_request_public_sends_client_id_without_basic_auth():
    data, auth = client_request({"grant_type": "refresh_token"}, "sas-mcp")
    assert data == {"grant_type": "refresh_token", "client_id": "sas-mcp"}
    assert auth is None  # public client: an empty-secret Basic header is rejected


def test_client_request_confidential_uses_basic_auth():
    data, auth = client_request({"grant_type": "refresh_token"}, "sas-mcp", "shh")
    assert auth == ("sas-mcp", "shh")
    assert data["client_id"] == "sas-mcp"


def test_client_request_does_not_mutate_input():
    grant = {"grant_type": "password"}
    client_request(grant, "sas-mcp")
    assert grant == {"grant_type": "password"}


# ---------------------------------------------------------------------------
# ServiceTokenProvider
# ---------------------------------------------------------------------------


def _response(status_code=200, body=None, text=""):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    resp.json = MagicMock(return_value=body if body is not None else {})
    if body is None:
        resp.json.side_effect = ValueError("no json")
    return resp


def _token_body(token="tok", expires_in=3600, refresh_token=None):
    body = {"access_token": token, "expires_in": expires_in}
    if refresh_token is not None:
        body["refresh_token"] = refresh_token
    return body


def _patched_client(post: AsyncMock):
    """Patch httpx.AsyncClient so the provider's POSTs hit *post* instead."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = post
    return patch.object(httpx, "AsyncClient", return_value=client)


def _provider(**kwargs) -> ServiceTokenProvider:
    kwargs.setdefault("token_endpoint", TOKEN_URL)
    kwargs.setdefault("client_id", "sas-mcp")
    return ServiceTokenProvider(**kwargs)


async def test_password_grant_is_used_when_no_refresh_token():
    post = AsyncMock(return_value=_response(body=_token_body("tok-abc")))
    provider = _provider(username="user", password="pass")
    with _patched_client(post):
        assert await provider.get_token() == "tok-abc"

    assert post.call_args[0][0] == TOKEN_URL
    assert post.call_args[1]["data"]["grant_type"] == "password"
    assert post.call_args[1]["data"]["username"] == "user"
    assert post.call_args[1]["auth"] is httpx.USE_CLIENT_DEFAULT  # no Basic header


async def test_refresh_grant_preferred_over_password():
    post = AsyncMock(return_value=_response(body=_token_body("tok-r")))
    provider = _provider(refresh_token="rt-1", username="user", password="pass")
    with _patched_client(post):
        await provider.get_token()

    assert post.call_args[1]["data"]["grant_type"] == "refresh_token"
    assert post.call_args[1]["data"]["refresh_token"] == "rt-1"


async def test_confidential_client_uses_basic_auth():
    post = AsyncMock(return_value=_response(body=_token_body()))
    provider = _provider(refresh_token="rt-1", client_secret="shh")
    with _patched_client(post):
        await provider.get_token()

    assert post.call_args[1]["auth"] == ("sas-mcp", "shh")


async def test_token_is_cached_between_calls():
    post = AsyncMock(return_value=_response(body=_token_body("tok-cached")))
    provider = _provider(refresh_token="rt-1")
    with _patched_client(post):
        first, second = await provider.get_token(), await provider.get_token()

    assert first == second == "tok-cached"
    assert post.call_count == 1


async def test_token_is_refetched_once_inside_the_expiry_margin():
    # expires_in below the margin means the token is already "expired" on arrival.
    post = AsyncMock(side_effect=[
        _response(body=_token_body("tok-1", expires_in=30)),
        _response(body=_token_body("tok-2")),
    ])
    provider = _provider(refresh_token="rt-1")
    with _patched_client(post):
        assert await provider.get_token() == "tok-1"
        assert await provider.get_token() == "tok-2"
    assert post.call_count == 2


async def test_concurrent_callers_share_a_single_refresh():
    post = AsyncMock(return_value=_response(body=_token_body("tok-one")))
    provider = _provider(refresh_token="rt-1")
    with _patched_client(post):
        tokens = await asyncio.gather(*(provider.get_token() for _ in range(10)))

    assert set(tokens) == {"tok-one"}
    # The lock must collapse the burst into one upstream request, so a rotating
    # refresh token is never consumed twice.
    assert post.call_count == 1


async def test_rotated_refresh_token_is_used_next_time():
    post = AsyncMock(side_effect=[
        _response(body=_token_body("tok-1", expires_in=30, refresh_token="rt-2")),
        _response(body=_token_body("tok-2")),
    ])
    provider = _provider(refresh_token="rt-1")
    with _patched_client(post):
        await provider.get_token()
        await provider.get_token()

    assert post.call_args_list[0][1]["data"]["refresh_token"] == "rt-1"
    assert post.call_args_list[1][1]["data"]["refresh_token"] == "rt-2"


async def test_rejected_rotation_falls_back_to_the_configured_token():
    post = AsyncMock(side_effect=[
        # Mint with the configured token, receiving a rotated one.
        _response(body=_token_body("tok-1", expires_in=30, refresh_token="rt-2")),
        # The rotated token is refused (e.g. the rotation never committed).
        _response(status_code=400, body={"error": "invalid_token"}),
        # Retry with the configured token succeeds.
        _response(body=_token_body("tok-3")),
    ])
    provider = _provider(refresh_token="rt-1")
    with _patched_client(post):
        await provider.get_token()
        assert await provider.get_token() == "tok-3"

    assert [c[1]["data"]["refresh_token"] for c in post.call_args_list] == [
        "rt-1",
        "rt-2",
        "rt-1",
    ]


async def test_configured_token_rejection_is_not_retried():
    post = AsyncMock(return_value=_response(status_code=401, body={"error": "unauthorized"}))
    provider = _provider(refresh_token="rt-1")
    with _patched_client(post), pytest.raises(AuthenticationError) as exc:
        await provider.get_token()

    assert post.call_count == 1
    assert "unauthorized" in str(exc.value)
    # The message has to say how to recover — an expired refresh token is the
    # normal way an unattended deployment dies.
    assert "sas-mcp-login" in str(exc.value)


async def test_missing_credentials_raise_before_any_request():
    post = AsyncMock()
    provider = _provider()
    assert provider.has_credentials is False
    assert provider.grant_type == "none"
    with _patched_client(post), pytest.raises(AuthenticationError, match="No Viya credentials"):
        await provider.get_token()
    post.assert_not_called()


async def test_network_failure_is_reported_as_authentication_error():
    post = AsyncMock(side_effect=httpx.ConnectError("no route to host"))
    provider = _provider(refresh_token="rt-1")
    with _patched_client(post), pytest.raises(AuthenticationError, match="Could not reach SAS Logon"):
        await provider.get_token()


async def test_response_without_access_token_is_rejected():
    post = AsyncMock(return_value=_response(body={"expires_in": 3600}))
    provider = _provider(refresh_token="rt-1")
    with _patched_client(post), pytest.raises(AuthenticationError, match="no access_token"):
        await provider.get_token()
