#!/bin/sh
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Selects which server to run inside the container.
#
#   MCP_MODE=http-direct  (default) — streamable HTTP, server authenticates to
#                                     Viya with the .env/credentials env vars.
#                                     This is the mode used when SAS Retrieval
#                                     Agent Manager hosts the container.
#   MCP_MODE=http                   — HTTP with per-user OAuth2 PKCE (browser).
#   MCP_MODE=stdio                  — stdio transport.
#
# An explicit command passed to the container overrides MCP_MODE entirely.
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
