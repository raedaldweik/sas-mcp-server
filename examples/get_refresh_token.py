#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
One-time helper to obtain a SAS Viya **refresh token** via an interactive
browser login (authorization-code flow with PKCE).

Why this exists
---------------
When your Viya environment authenticates users through an external identity
provider (for example Okta SSO), the OAuth2 *password* grant cannot be used —
SAS Logon never sees the user's password, it redirects the browser to the
provider. To run a headless MCP server (stdio or direct-HTTP / SAS Retrieval
Agent Manager) as such a user, you log in **once** here, capture a long-lived
refresh token, and let the server exchange it for access tokens forever.

What you get
------------
A ``refresh_token`` printed to the terminal. Put it in your environment as
``VIYA_REFRESH_TOKEN`` (store it as a secret). No password is ever stored.

Prerequisites
-------------
* An OAuth client registered with the ``authorization_code`` and
  ``refresh_token`` grants and a redirect URI of
  ``http://localhost:<port>/auth/callback`` — exactly what
  ``examples/register_mcp_client.py`` registers. Set a long
  ``refresh-token-validity`` on that client so you rarely have to repeat this.
* SAS Logon must allow the redirect back to localhost. If the browser shows a
  login page but the redirect never completes, disable the ``form-action``
  Content-Security-Policy directive on SAS Logon Manager (see
  examples/configuration.md, Step 1). You only need it disabled for this
  one-time step; the headless refresh_token grant performs no browser
  redirect, so you can re-enable it afterwards.

Usage
-----
    uv run python examples/get_refresh_token.py

Reads VIYA_ENDPOINT, CLIENT_ID, CLIENT_SECRET, HOST_PORT and SSL_VERIFY from
your ``.env``. Override the callback port with ``--port`` (it must match a
redirect URI registered on the client).
"""

import argparse
import base64
import hashlib
import os
import secrets
import ssl
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from dotenv import load_dotenv

load_dotenv()

VIYA_ENDPOINT = os.getenv("VIYA_ENDPOINT", "").rstrip("/")
CLIENT_ID = os.getenv("CLIENT_ID", "sas-mcp")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
HOST_PORT = int(os.getenv("HOST_PORT", "8134"))
SSL_VERIFY = os.getenv("SSL_VERIFY", "true").lower() not in ("false", "0", "no")

if SSL_VERIFY:
    _ssl_ctx = True
else:
    _ssl_ctx = ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = ssl.CERT_NONE


def _pkce_pair():
    """Return (code_verifier, code_challenge) for the S256 PKCE method."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    # Populated on the server instance once the redirect arrives.
    def do_GET(self):  # noqa: N802 (http.server API)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.server.callback_path:
            self.send_response(404)
            self.end_headers()
            return
        self.server.query = urllib.parse.parse_qs(parsed.query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in self.server.query
        msg = ("Authentication complete. You can close this tab and return to "
               "the terminal.") if ok else \
              ("Authentication failed. Check the terminal for details.")
        self.wfile.write(
            f"<html><body style='font-family:sans-serif;padding:2rem'>"
            f"<h3>SAS Viya MCP</h3><p>{msg}</p></body></html>".encode()
        )
        self.server.done.set()

    def log_message(self, *args):  # silence default request logging
        pass


def _await_redirect(port, callback_path, timeout=300):
    """Run a one-shot local server and return the redirect query params."""
    server = HTTPServer(("localhost", port), _CallbackHandler)
    server.callback_path = callback_path
    server.query = {}
    server.done = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not server.done.wait(timeout=timeout):
            raise TimeoutError(
                f"Timed out after {timeout}s waiting for the browser redirect."
            )
    finally:
        server.shutdown()
        thread.join()
    return server.query


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, default=HOST_PORT,
        help="Local callback port; must match a registered redirect URI "
             f"(default: {HOST_PORT})",
    )
    parser.add_argument(
        "--timeout", type=int, default=300,
        help="Seconds to wait for the browser login (default: 300)",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Do not auto-open a browser; just print the URL to visit",
    )
    args = parser.parse_args()

    if not VIYA_ENDPOINT:
        sys.exit("Error: VIYA_ENDPOINT is not set. Check your .env file.")

    callback_path = "/auth/callback"
    redirect_uri = f"http://localhost:{args.port}{callback_path}"
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)

    authorize_url = (
        f"{VIYA_ENDPOINT}/SASLogon/oauth/authorize?"
        + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": "openid",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
    )

    print(f"Viya endpoint: {VIYA_ENDPOINT}")
    print(f"Client ID:     {CLIENT_ID}")
    print(f"Redirect URI:  {redirect_uri}")
    print()
    print("Open this URL in your browser and sign in (e.g. via Okta):")
    print()
    print(f"  {authorize_url}")
    print()
    if not args.no_browser:
        webbrowser.open(authorize_url)
    print(f"Waiting for the login to complete (up to {args.timeout}s)...")

    query = _await_redirect(args.port, callback_path, timeout=args.timeout)

    if "error" in query:
        sys.exit("Authorization failed: "
                 f"{query.get('error', [''])[0]} "
                 f"{query.get('error_description', [''])[0]}")
    if query.get("state", [None])[0] != state:
        sys.exit("Authorization failed: state mismatch (possible CSRF). Aborting.")
    code = query.get("code", [None])[0]
    if not code:
        sys.exit("Authorization failed: no authorization code returned.")

    print("Authorization code received. Exchanging for tokens...")
    resp = httpx.post(
        f"{VIYA_ENDPOINT}/SASLogon/oauth/token",
        auth=(CLIENT_ID, CLIENT_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        verify=_ssl_ctx,
        timeout=60.0,
    )
    if resp.status_code != 200:
        sys.exit(f"Token exchange failed: {resp.status_code} {resp.text}")

    body = resp.json()
    refresh_token = body.get("refresh_token")
    if not refresh_token:
        sys.exit(
            "No refresh token returned. Ensure the OAuth client includes the "
            "'refresh_token' grant type (see examples/register_mcp_client.py)."
        )

    print()
    print("=" * 72)
    print("SUCCESS. Add this to your environment (store it as a secret):")
    print()
    print(f"  VIYA_REFRESH_TOKEN={refresh_token}")
    print()
    print(f"  (expires_in for the access token: {body.get('expires_in')}s; "
          "the refresh token's lifetime is governed by the client's "
          "refresh-token-validity setting)")
    print("=" * 72)


if __name__ == "__main__":
    main()
