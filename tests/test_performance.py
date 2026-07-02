# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the performance features: pooled HTTP clients, compute session
reuse (with dead-session retry), and TOOL_GROUPS tool scoping."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastmcp import FastMCP, Client

from sas_mcp_server import viya_utils
from sas_mcp_server.viya_utils import run_one_snippet, _make_client


def _mock_client_for(mock_client_class):
    """An AsyncMock client usable on both the pooled and per-call paths."""
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client_class.return_value = mock_client
    return mock_client


def _response(json_value=None, text=None):
    resp = AsyncMock()
    if json_value is not None:
        resp.json = MagicMock(return_value=json_value)
    if text is not None:
        resp.text = text
    return resp


def _job_run_responses():
    """GET responses for one successful job run: state, log, listing."""
    return [
        _response(text="completed"),
        _response(json_value={"items": [{"line": "Log output"}]}),
        _response(json_value={"items": [{"line": "Listing output"}]}),
    ]


# ---------------------------------------------------------------------------
# Compute session reuse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_snippet_reuses_pooled_session(sample_sas_code, mock_env_vars):
    """The second call must skip context lookup + session creation and reuse
    the session parked by the first call — the core latency fix."""
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_client_class:
        mock_client = _mock_client_for(mock_client_class)

        mock_client.get.side_effect = [
            _response(json_value={"items": [{"id": "ctx-id"}]}),  # context (1st only)
            *_job_run_responses(),
            *_job_run_responses(),  # 2nd call: no context lookup
        ]
        mock_client.post.side_effect = [
            _response(json_value={"id": "sess-1"}),  # create session (1st only)
            _response(json_value={"id": "job-1"}),
            _response(json_value={"id": "job-2"}),   # 2nd call: straight to job
        ]

        r1 = await run_one_snippet(sample_sas_code, "1", "tok")
        r2 = await run_one_snippet(sample_sas_code, "2", "tok")

        assert r1[1] == r2[1] == "completed"
        # Exactly one session was created and it was never deleted.
        assert mock_client.post.call_count == 3
        mock_client.delete.assert_not_called()
        # The session is parked for the next call.
        assert viya_utils._session_pool.get("Bearer tok") == ["sess-1"]


@pytest.mark.asyncio
async def test_dead_pooled_session_is_replaced_and_job_retried(sample_sas_code, mock_env_vars):
    """A pooled session that died server-side must be discarded and the job
    retried once on a brand-new session."""
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_client_class:
        mock_client = _mock_client_for(mock_client_class)

        # Park a (stale) session id in the pool.
        viya_utils._session_pool["Bearer tok"] = ["stale-sess"]

        submit_fail = _response(json_value=None)
        submit_fail.json = MagicMock(side_effect=KeyError("id"))  # dead session

        mock_client.get.side_effect = [
            _response(json_value={"items": [{"id": "ctx-id"}]}),  # fresh context
            *_job_run_responses(),
        ]
        mock_client.post.side_effect = [
            submit_fail,                                # submit on stale session
            _response(json_value={"id": "sess-2"}),     # create fresh session
            _response(json_value={"id": "job-1"}),      # submit succeeds
        ]

        result = await run_one_snippet(sample_sas_code, "1", "tok")

        assert result[1] == "completed"
        # The stale session was deleted; the fresh one was parked.
        mock_client.delete.assert_called_once()
        assert "stale-sess" in str(mock_client.delete.call_args)
        assert viya_utils._session_pool.get("Bearer tok") == ["sess-2"]


@pytest.mark.asyncio
async def test_session_reuse_disabled_restores_per_call_sessions(
        sample_sas_code, mock_env_vars, monkeypatch):
    """COMPUTE_SESSION_REUSE=false must delete the session after the call."""
    monkeypatch.setattr(viya_utils, "COMPUTE_SESSION_REUSE", False)
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_client_class:
        mock_client = _mock_client_for(mock_client_class)

        mock_client.get.side_effect = [
            _response(json_value={"items": [{"id": "ctx-id"}]}),
            *_job_run_responses(),
        ]
        mock_client.post.side_effect = [
            _response(json_value={"id": "sess-1"}),
            _response(json_value={"id": "job-1"}),
        ]

        result = await run_one_snippet(sample_sas_code, "1", "tok")

        assert result[1] == "completed"
        mock_client.delete.assert_called_once()
        assert not viya_utils._session_pool.get("Bearer tok")


# ---------------------------------------------------------------------------
# Pooled HTTP clients
# ---------------------------------------------------------------------------


def test_make_client_reuses_pooled_client_per_token(mock_env_vars):
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_cls:
        mock_cls.return_value = MagicMock(is_closed=False)
        lease1 = _make_client("my-token")
        lease2 = _make_client("Bearer my-token")  # same identity, same client
        assert mock_cls.call_count == 1
        assert lease1._client is lease2._client


def test_make_client_separate_clients_per_token(mock_env_vars):
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_cls:
        mock_cls.side_effect = lambda **kw: MagicMock(is_closed=False)
        lease1 = _make_client("token-a")
        lease2 = _make_client("token-b")
        assert mock_cls.call_count == 2
        assert lease1._client is not lease2._client


# ---------------------------------------------------------------------------
# TOOL_GROUPS scoping
# ---------------------------------------------------------------------------


async def _registered_tool_names(monkeypatch, groups_value):
    if groups_value is None:
        monkeypatch.delenv("TOOL_GROUPS", raising=False)
    else:
        monkeypatch.setenv("TOOL_GROUPS", groups_value)
    from sas_mcp_server.tools import register_tools

    mcp = FastMCP("Tool Groups Test")

    async def fake_token(ctx):
        return "tok"

    register_tools(mcp, fake_token)
    async with Client(mcp) as client:
        return {t.name for t in await client.list_tools()}


@pytest.mark.asyncio
async def test_no_tool_groups_registers_everything(monkeypatch, mock_env_vars):
    names = await _registered_tool_names(monkeypatch, None)
    assert "execute_sas_code" in names
    assert "create_ml_project" in names
    assert "render_chart" in names
    assert "get_use_case" in names
    assert len(names) >= 28


@pytest.mark.asyncio
async def test_tool_groups_limits_surface(monkeypatch, mock_env_vars):
    names = await _registered_tool_names(monkeypatch, "data, insights")
    assert "list_castables" in names
    assert "get_castable_data" in names
    assert "explain_data" in names
    assert "get_use_case" in names          # always available
    assert "execute_sas_code" not in names  # sas group not enabled
    assert "create_ml_project" not in names
    assert "generate_synthetic_data" not in names


@pytest.mark.asyncio
async def test_tool_groups_unknown_names_are_ignored(monkeypatch, mock_env_vars):
    names = await _registered_tool_names(monkeypatch, "ml, bogus-group")
    assert "create_ml_project" in names
    assert "run_ml_project" in names
    assert "execute_sas_code" not in names
