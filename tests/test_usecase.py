# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for use-case scoping: the UseCaseScope allowlist logic, and the
filtering/guard behavior it produces in the registered tools.
"""
import pytest
from unittest.mock import AsyncMock, patch
from fastmcp import FastMCP, Client
from conftest import _make_mock_response

from sas_mcp_server.usecase import UseCaseScope, load_scope, _parse_list


# ---------------------------------------------------------------------------
# UseCaseScope unit logic
# ---------------------------------------------------------------------------


def test_parse_list_handles_commas_newlines_and_spaces():
    assert _parse_list("a, b ,c") == ["a", "b", "c"]
    assert _parse_list("a\nb\n c ") == ["a", "b", "c"]
    assert _parse_list("") == []
    assert _parse_list(None) == []


def test_scope_inactive_by_default():
    s = UseCaseScope()
    assert s.active is False
    assert s.enforced is False
    # empty allowlists permit everything
    assert s.allows_report("anything") is True
    assert s.allows_table("anything") is True


def test_scope_active_when_any_allowlist_set():
    assert UseCaseScope(reports=["r1"]).active is True
    assert UseCaseScope(tables=["t1"]).active is True
    assert UseCaseScope(models=["m1"]).active is True
    assert UseCaseScope(decisions=["d1"]).active is True


def test_report_matching_by_id_or_name_case_insensitive():
    s = UseCaseScope(reports=["RPT-1", "Sales Report"])
    assert s.allows_report("rpt-1")
    assert s.allows_report("zzz", "sales report")  # any candidate matches
    assert not s.allows_report("other-id", "Other Report")


def test_table_matching_supports_qualified_forms():
    s = UseCaseScope(tables=["Public.SALES"])
    assert s.allows_table(name="SALES", caslib="Public")
    assert s.allows_table(name="SALES", caslib="Public", server="cas-shared-default")
    # unqualified allowlist entry matches bare table name
    s2 = UseCaseScope(tables=["SALES"])
    assert s2.allows_table(name="sales", caslib="Public")
    # not in list
    assert not s.allows_table(name="HR", caslib="Public")


def test_enforced_requires_active_and_enforce_flag():
    assert UseCaseScope(reports=["r"], enforce=True).enforced is True
    assert UseCaseScope(reports=["r"], enforce=False).enforced is False
    assert UseCaseScope(enforce=True).enforced is False  # inactive


def test_manifest_contents():
    s = UseCaseScope(name="Fraud", description="d", reports=["r1"], tables=["t1"])
    m = s.manifest()
    assert m["useCaseName"] == "Fraud"
    assert m["scoped"] is True
    assert m["enforced"] is True
    assert m["allowedReports"] == ["r1"]
    assert m["allowedTables"] == ["t1"]


def test_load_scope_reads_env(monkeypatch):
    monkeypatch.setenv("USE_CASE_NAME", "Procurement")
    monkeypatch.setenv("ALLOWED_REPORTS", "rpt-1, rpt-2")
    monkeypatch.setenv("ALLOWED_TABLES", "Public.SUPPLIERS")
    monkeypatch.setenv("SCOPE_ENFORCE", "false")
    s = load_scope()
    assert s.name == "Procurement"
    assert s.reports == ["rpt-1", "rpt-2"]
    assert s.tables == ["Public.SUPPLIERS"]
    assert s.active is True
    assert s.enforce is False
    assert s.enforced is False  # active but not enforced


# ---------------------------------------------------------------------------
# Scoped server behavior (through the MCP protocol)
# ---------------------------------------------------------------------------


def _build_scoped_server(env: dict, monkeypatch):
    """Register tools with use-case env vars set; return (mcp, mock_client)."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get.return_value = _make_mock_response({"items": [], "count": 0})

    patcher = patch("sas_mcp_server.tools._make_client", return_value=mock_client)
    patcher.start()
    mcp = FastMCP("Scoped Test Server")

    async def mock_get_token(ctx):
        return "test-token"

    from sas_mcp_server.tools import register_tools
    register_tools(mcp, mock_get_token)
    return mcp, mock_client, patcher


async def test_get_use_case_returns_manifest(monkeypatch):
    mcp, _, patcher = _build_scoped_server(
        {"USE_CASE_NAME": "Fraud", "ALLOWED_REPORTS": "rpt-keep"}, monkeypatch)
    try:
        async with Client(mcp) as client:
            res = await client.call_tool("get_use_case", {})
        assert res.data["useCaseName"] == "Fraud"
        assert res.data["scoped"] is True
        assert res.data["allowedReports"] == ["rpt-keep"]
    finally:
        patcher.stop()


async def test_list_reports_filtered_to_allowlist(monkeypatch):
    mcp, mock_client, patcher = _build_scoped_server(
        {"ALLOWED_REPORTS": "rpt-keep"}, monkeypatch)
    try:
        mock_client.get.return_value = _make_mock_response({
            "items": [
                {"id": "rpt-keep", "name": "Keep Me"},
                {"id": "rpt-drop", "name": "Drop Me"},
            ],
            "count": 2,
        })
        async with Client(mcp) as client:
            res = await client.call_tool("list_reports", {})
        ids = {r["id"] for r in res.data}
        assert ids == {"rpt-keep"}
    finally:
        patcher.stop()


async def test_get_report_blocked_when_out_of_scope(monkeypatch):
    mcp, _, patcher = _build_scoped_server(
        {"ALLOWED_REPORTS": "rpt-keep"}, monkeypatch)
    try:
        async with Client(mcp) as client:
            # allowed report passes the guard (reaches the mocked GET)
            await client.call_tool("get_report", {"report_id": "rpt-keep"})
            # disallowed report is blocked before any HTTP call
            with pytest.raises(Exception) as ei:
                await client.call_tool("get_report", {"report_id": "rpt-secret"})
            assert "use case" in str(ei.value).lower()
    finally:
        patcher.stop()


async def test_scope_not_enforced_only_filters(monkeypatch):
    mcp, _, patcher = _build_scoped_server(
        {"ALLOWED_REPORTS": "rpt-keep", "SCOPE_ENFORCE": "false"}, monkeypatch)
    try:
        async with Client(mcp) as client:
            # with enforcement off, the guard does not block out-of-scope access
            res = await client.call_tool("get_report", {"report_id": "rpt-secret"})
            assert res.data is not None
    finally:
        patcher.stop()


async def test_score_data_blocked_when_module_out_of_scope(monkeypatch):
    mcp, _, patcher = _build_scoped_server(
        {"ALLOWED_DECISIONS": "fraud_decision"}, monkeypatch)
    try:
        async with Client(mcp) as client:
            with pytest.raises(Exception) as ei:
                await client.call_tool("score_data", {
                    "module_id": "other_module", "step_id": "execute",
                    "input_data": {"x": 1},
                })
            assert "use case" in str(ei.value).lower()
    finally:
        patcher.stop()


async def test_unscoped_server_allows_everything(monkeypatch):
    # No ALLOWED_* env vars → full access, guards inactive.
    for var in ("ALLOWED_TABLES", "ALLOWED_REPORTS", "ALLOWED_MODELS",
                "ALLOWED_DECISIONS", "USE_CASE_NAME"):
        monkeypatch.delenv(var, raising=False)
    mcp, _, patcher = _build_scoped_server({}, monkeypatch)
    try:
        async with Client(mcp) as client:
            res = await client.call_tool("get_use_case", {})
            assert res.data["scoped"] is False
            # any report id is reachable
            await client.call_tool("get_report", {"report_id": "anything"})
    finally:
        patcher.stop()
