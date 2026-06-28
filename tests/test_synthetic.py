# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the synthetic-data generator (SAS builder + the MCP tool)."""

import pytest
from unittest.mock import patch
from fastmcp import Client
from conftest import _make_mock_response

from sas_mcp_server.tools import _build_synthetic_sas, _iso_to_sas_date


# --- SAS builder (no network) ----------------------------------------------

def test_build_synthetic_sas_covers_all_types():
    code = _build_synthetic_sas(
        "DRIVER_RISK", "Public", "cas-shared-default",
        [{"name": "driver_id", "type": "id"},
         {"name": "risk_score", "type": "int", "min": 0, "max": 100,
          "dist": "normal", "mean": 50, "std": 18},
         {"name": "km_driven", "type": "float", "min": 0, "max": 40000, "decimals": 1},
         {"name": "speeding", "type": "int", "dist": "poisson", "lambda": 2},
         {"name": "risk_band", "type": "category",
          "levels": ["Low", "Medium", "High"], "weights": [0.5, 0.3, 0.2]},
         {"name": "is_active", "type": "bool", "p_true": 0.8},
         {"name": "event_date", "type": "date",
          "start": "2025-01-01", "end": "2025-12-31"}],
        500, seed=42)
    assert "do _i = 1 to 500" in code
    assert 'casout="DRIVER_RISK"' in code
    assert 'outcaslib="Public"' in code
    assert "promote" in code
    assert "streaminit(42)" in code
    assert "driver_id = put(_i, z6.);" in code
    assert "rand('normal', 50.0, 18.0)" in code
    assert "rand('poisson', 2.0)" in code
    assert "format event_date date9." in code
    assert "drop _i _p;" in code  # _p introduced by the category column


def test_iso_to_sas_date():
    assert _iso_to_sas_date("2025-01-01") == '"01JAN2025"d'
    with pytest.raises(ValueError):
        _iso_to_sas_date("01/01/2025")


def test_build_synthetic_sas_clamps_normal_to_range():
    code = _build_synthetic_sas(
        "T", "Public", "cas-shared-default",
        [{"name": "s", "type": "int", "min": 0, "max": 100,
          "dist": "normal", "mean": 50, "std": 30}], 10)
    assert "if s < 0.0 then s = 0.0;" in code
    assert "if s > 100.0 then s = 100.0;" in code


def test_build_synthetic_sas_validation():
    with pytest.raises(ValueError):
        _build_synthetic_sas("T", "Public", "s",
                             [{"name": "bad name", "type": "int"}], 10)
    with pytest.raises(ValueError):  # category needs levels
        _build_synthetic_sas("T", "Public", "s",
                             [{"name": "c", "type": "category"}], 10)
    with pytest.raises(ValueError):  # unknown type
        _build_synthetic_sas("T", "Public", "s",
                             [{"name": "c", "type": "weird"}], 10)
    with pytest.raises(ValueError):  # invalid table name
        _build_synthetic_sas("bad table", "Public", "s",
                             [{"name": "c", "type": "int"}], 10)
    with pytest.raises(ValueError):  # empty spec
        _build_synthetic_sas("T", "Public", "s", [], 10)


# --- MCP tool (mocked Viya) -------------------------------------------------

@pytest.mark.asyncio
async def test_generate_synthetic_data_tool(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    # existence check returns no tables -> name used as-is
    mock_client.get.return_value = _make_mock_response({"items": [], "count": 0})
    with patch("sas_mcp_server.tools.run_one_snippet") as mock_run:
        mock_run.return_value = ("1", "completed", "NOTE: ok", "")
        async with Client(mcp) as client:
            res = await client.call_tool("generate_synthetic_data", {
                "table_name": "DRIVER_RISK_SCORE",
                "n_rows": 100,
                "columns": [
                    {"name": "driver_id", "type": "id"},
                    {"name": "risk_score", "type": "int", "min": 0, "max": 100},
                ],
            })
    code = mock_run.call_args[0][0]
    assert 'casout="DRIVER_RISK_SCORE"' in code
    assert "do _i = 1 to 100" in code
    assert res.data["table"] == "DRIVER_RISK_SCORE"
    assert res.data["rowCount"] == 100
    assert res.data["promoted"] is True


@pytest.mark.asyncio
async def test_generate_synthetic_data_auto_renames(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value = _make_mock_response(
        {"items": [{"name": "DRIVER_RISK_SCORE"}], "count": 1})
    with patch("sas_mcp_server.tools.run_one_snippet") as mock_run:
        mock_run.return_value = ("1", "completed", "ok", "")
        async with Client(mcp) as client:
            res = await client.call_tool("generate_synthetic_data", {
                "table_name": "DRIVER_RISK_SCORE",
                "n_rows": 10,
                "columns": [{"name": "x", "type": "int"}],
            })
    assert res.data["table"] == "DRIVER_RISK_SCORE_1"
    assert res.data.get("renamed_from") == "DRIVER_RISK_SCORE"
