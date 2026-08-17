# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Headless (service-account) authentication to SAS Viya.

The HTTP server in :mod:`sas_mcp_server.mcp_server` makes *each MCP client*
complete a browser OAuth flow. Some hosts cannot do that: SAS Retrieval Agent
Manager (RAM) starts the container, points an agent at ``/mcp`` and never opens
a browser. For those, the server has to authenticate to Viya *on its own
behalf* — which is what this module provides, and what
:mod:`sas_mcp_server.http_direct_server` serves on top of.

Two grants are supported, and which one is usable depends on how the Viya
identity is backed:

* **refresh_token** — works for *any* identity, including ones federated
  through an external provider (Okta, Entra, …). For federated users SAS Logon
  never sees a password, so the password grant simply cannot work. Mint the
  refresh token once with ``uv run sas-mcp-login --print-refresh-token``.
* **password** — only for identities SAS Logon authenticates directly (the
  local ``sasboot`` account, LDAP users).

:func:`select_grant` centralises the preference order so every headless caller
behaves identically: a refresh token, when present, always wins.

:class:`ServiceTokenProvider` wraps that in the caching an unattended 24/7
deployment needs — an access token is reused until shortly before it expires,
concurrent callers at expiry collapse into a single upstream request, and a
rotated refresh token is carried forward so the chain does not break.
"""

import asyncio
import time

import httpx
from fastmcp.utilities.logging import get_logger

from .exceptions import AuthenticationError

logger = get_logger(__name__)

# Refresh the cached access token this many seconds before it actually expires,
# so a call already in flight cannot race the real expiry.
DEFAULT_EXPIRY_MARGIN = 60.0
# Cap on how much of a SAS Logon error body is folded into the exception text.
_MAX_ERROR_DETAIL = 400


def select_grant(
    refresh_token: str = "", username: str = "", password: str = ""
) -> dict[str, str] | None:
    """Return the OAuth token-request form data for the best available grant.

    Preference order:

    1. ``refresh_token`` — works for federated (SSO) identities and needs no
       stored password.
    2. ``password`` — only for identities SAS Logon authenticates directly.

    Returns ``None`` when no usable credentials are configured, so callers can
    raise one clear authentication error instead of a confusing 401 from Viya.
    """
    if refresh_token:
        return {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if username and password:
        return {
            "grant_type": "password",
            "username": username,
            "password": password,
        }
    return None


def client_request(
    grant_data: dict[str, str], client_id: str, client_secret: str = ""
) -> tuple[dict[str, str], tuple[str, str] | None]:
    """Return ``(data, auth)`` for a SAS Logon ``/oauth/token`` request.

    Public clients (registered ``allowpublic``/PKCE, no secret) must send
    ``client_id`` in the request **body** with **no** HTTP Basic auth header —
    SAS Logon rejects an empty-secret Basic header with ``invalid_client`` /
    "Missing credentials". Confidential clients (a secret is configured)
    authenticate with HTTP Basic auth.

    The input grant dict is copied, never mutated.
    """
    data = {**grant_data, "client_id": client_id}
    auth = (client_id, client_secret) if client_secret else None
    return data, auth


def _error_detail(resp: httpx.Response) -> str:
    """Pull SAS Logon's own error text out of a failed token response."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip()[:_MAX_ERROR_DETAIL]
    if not isinstance(body, dict):
        return resp.text.strip()[:_MAX_ERROR_DETAIL]
    parts = [str(body[k]) for k in ("error", "error_description") if body.get(k)]
    return " — ".join(parts)[:_MAX_ERROR_DETAIL] or resp.text.strip()[:_MAX_ERROR_DETAIL]


class ServiceTokenProvider:
    """Caching OAuth token source for the headless servers.

    One instance owns one Viya identity. :meth:`get_token` is the whole API:
    it returns a valid access token, minting or refreshing one only when the
    cached token is within *expiry_margin* seconds of expiring.

    Deliberately a class rather than module globals (the shape this started
    from): the cache, the lock and the rotating refresh token are one unit of
    state, so tests can build a provider per case instead of monkeypatching and
    resetting module attributes, and a second identity would just be a second
    instance.
    """

    def __init__(
        self,
        *,
        token_endpoint: str,
        client_id: str,
        client_secret: str = "",
        refresh_token: str = "",
        username: str = "",
        password: str = "",
        verify: bool = True,
        expiry_margin: float = DEFAULT_EXPIRY_MARGIN,
        timeout: float = 30.0,
    ) -> None:
        self.token_endpoint = token_endpoint
        self.client_id = client_id
        self.client_secret = client_secret
        # The refresh token as configured. Kept separate from the current one
        # so a broken rotation chain can be retried from the known-good value.
        self._configured_refresh_token = refresh_token
        self._refresh_token = refresh_token
        self.username = username
        self.password = password
        self.verify = verify
        self.expiry_margin = expiry_margin
        self.timeout = timeout
        self._token = ""
        self._expires_at = 0.0
        # Serialises refreshes so a burst of concurrent tool calls at expiry
        # does not stampede /SASLogon or race to consume a rotating refresh
        # token (SAS Logon invalidates the old one the moment it issues a new).
        self._lock = asyncio.Lock()

    @property
    def has_credentials(self) -> bool:
        """Whether *any* usable credential is configured."""
        return bool(
            self._refresh_token or self._configured_refresh_token
            or (self.username and self.password)
        )

    @property
    def grant_type(self) -> str:
        """The grant this provider will use — for logging at startup."""
        if self._refresh_token or self._configured_refresh_token:
            return "refresh_token"
        if self.username and self.password:
            return "password"
        return "none"

    def _cached(self) -> str:
        """The cached access token if it is still comfortably valid, else ''."""
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        return ""

    async def get_token(self) -> str:
        """Return a valid Viya access token, minting or refreshing as needed."""
        cached = self._cached()
        if cached:
            return cached

        async with self._lock:
            # Another coroutine may have refreshed while we waited for the lock.
            cached = self._cached()
            if cached:
                return cached
            return await self._acquire()

    async def _acquire(self) -> str:
        """Fetch a new access token. Caller must hold ``self._lock``."""
        grant = select_grant(
            refresh_token=self._refresh_token,
            username=self.username,
            password=self.password,
        )
        if grant is None:
            raise AuthenticationError(
                "No Viya credentials configured for direct HTTP mode. Set "
                "VIYA_REFRESH_TOKEN (recommended — the only option for "
                "SSO/federated identities; mint one with "
                "`uv run sas-mcp-login --print-refresh-token`) or "
                "VIYA_USERNAME and VIYA_PASSWORD."
            )

        try:
            body = await self._post_token(grant)
        except AuthenticationError:
            # A rotated refresh token that SAS Logon will not accept leaves the
            # server permanently down until someone re-mints one by hand — the
            # worst failure for an unattended deployment. If the rejected token
            # was a rotation (i.e. not the one that was configured), fall back
            # to the configured value once before giving up.
            rotated = (
                grant["grant_type"] == "refresh_token"
                and self._configured_refresh_token
                and self._refresh_token != self._configured_refresh_token
            )
            if not rotated:
                raise
            logger.warning(
                "Rotated refresh token was rejected; retrying with the "
                "configured VIYA_REFRESH_TOKEN"
            )
            self._refresh_token = self._configured_refresh_token
            body = await self._post_token(
                {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
            )

        token = body.get("access_token")
        if not token:
            raise AuthenticationError(
                "SAS Logon returned no access_token for the "
                f"{grant['grant_type']} grant."
            )
        expires_in = float(body.get("expires_in", 0) or 0)
        self._token = token
        self._expires_at = time.monotonic() + max(expires_in - self.expiry_margin, 0.0)
        # Honour rotation: if SAS Logon issued a new refresh token, use it next
        # time — the old one is dead the moment the new one is minted.
        if body.get("refresh_token"):
            self._refresh_token = body["refresh_token"]
        logger.info(
            "Obtained Viya access token via %s grant (expires in %ss)",
            grant["grant_type"],
            int(expires_in),
        )
        return token

    async def _post_token(self, grant: dict[str, str]) -> dict:
        """POST to SAS Logon's token endpoint, mapping failures to AuthenticationError."""
        data, auth = client_request(grant, self.client_id, self.client_secret)
        try:
            async with httpx.AsyncClient(verify=self.verify, timeout=self.timeout) as client:
                resp = await client.post(
                    self.token_endpoint,
                    # The client itself carries no auth, so the sentinel is how
                    # a public client sends *no* Basic header — which is the
                    # point of ``client_request`` returning None.
                    auth=auth if auth is not None else httpx.USE_CLIENT_DEFAULT,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data=data,
                )
        except httpx.HTTPError as exc:
            raise AuthenticationError(
                f"Could not reach SAS Logon at {self.token_endpoint}: {exc}"
            ) from exc

        if resp.status_code != httpx.codes.OK:
            detail = _error_detail(resp)
            hint = ""
            if grant["grant_type"] == "refresh_token":
                hint = (
                    " The refresh token may have expired or been revoked — mint "
                    "a new one with `uv run sas-mcp-login --print-refresh-token` "
                    "and update VIYA_REFRESH_TOKEN."
                )
            raise AuthenticationError(
                f"SAS Logon rejected the {grant['grant_type']} grant "
                f"(HTTP {resp.status_code}): {detail}.{hint}"
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise AuthenticationError(
                "SAS Logon returned a non-JSON token response."
            ) from exc
        return body
