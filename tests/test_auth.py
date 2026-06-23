# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the shared grant-selection logic used by the headless servers."""

from sas_mcp_server.auth import select_grant


def test_refresh_token_wins_over_password():
    data = select_grant(refresh_token="rt", username="u", password="p")
    assert data == {"grant_type": "refresh_token", "refresh_token": "rt"}


def test_refresh_token_only():
    data = select_grant(refresh_token="rt")
    assert data == {"grant_type": "refresh_token", "refresh_token": "rt"}


def test_password_grant_when_no_refresh_token():
    data = select_grant(username="u", password="p")
    assert data == {"grant_type": "password", "username": "u", "password": "p"}


def test_none_when_password_incomplete():
    assert select_grant(username="u") is None
    assert select_grant(password="p") is None


def test_none_when_nothing_configured():
    assert select_grant() is None
