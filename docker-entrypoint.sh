#!/bin/sh
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Selects which server the container runs.
#
#   MCP_MODE=http-direct  (default) — streamable HTTP; the server authenticates
#                                     to Viya itself with VIYA_REFRESH_TOKEN (or
#                                     VIYA_USERNAME/VIYA_PASSWORD). This is the
#                                     mode SAS Retrieval Agent Manager and other
#                                     hosts that cannot run a browser OAuth flow
#                                     need.
#   MCP_MODE=http                   — HTTP with per-user OAuth 2.0 PKCE; each
#                                     MCP client signs in through a browser.
#   MCP_MODE=stdio                  — stdio transport.
#
# An explicit command passed to the container overrides MCP_MODE entirely, so
# `docker run <image> app` still starts the browser-OAuth server.
set -e

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

case "${MCP_MODE:-http-direct}" in
    http-direct) exec app-http-direct ;;
    http)        exec app ;;
    stdio)       exec app-stdio ;;
    *)
        echo "Unknown MCP_MODE='${MCP_MODE}' (use http-direct|http|stdio)" >&2
        exit 1
        ;;
esac
