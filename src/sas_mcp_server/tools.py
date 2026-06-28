# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Shared tool registration for both HTTP and stdio MCP servers.
All tools are registered via ``register_tools(mcp, get_token)``.
"""

import os
import re
from datetime import datetime
from typing import Optional
import httpx as _httpx
from fastmcp import Context
from fastmcp.tools import ToolResult
from .viya_utils import (
    _get_json,
    _get_paged_items,
    _post_json,
    _delete_resource,
    _make_client,
    run_one_snippet,
    logger,
)
from .config import MAX_SAS_OUTPUT_CHARS
from .usecase import load_scope

# Safety ceiling on synthetic-data rows so an over-eager request can't error or
# exhaust resources. Requests above this are clamped (not rejected). Override
# with MAX_SYNTHETIC_ROWS.
MAX_SYNTHETIC_ROWS = int(os.getenv("MAX_SYNTHETIC_ROWS", "1000000"))

_VALID_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
_SYNTH_TYPES = ("id", "int", "float", "category", "bool", "date")


def _truncate_output(text, limit=MAX_SAS_OUTPUT_CHARS):
    """Cap large SAS log/listing text so it can't overflow the agent's context.

    Keeps the head and tail (errors and the final NOTE summary usually sit at
    the end) with a marker noting how much was removed. ``limit`` of 0 disables
    capping.
    """
    if not text or limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - head - tail
    return (
        f"{text[:head]}\n\n...[truncated {omitted} characters to fit the model "
        f"context — re-run a narrower query (fewer columns/rows) for full "
        f"detail]...\n\n{text[-tail:]}"
    )


def _sas_quote(value) -> str:
    """Return a single-quoted SAS string literal, escaping embedded quotes."""
    s = str(value).replace("'", "''")
    return f"'{s[:200]}'"


def _iso_to_sas_date(value) -> str:
    """Convert 'YYYY-MM-DD' to a SAS date literal like \"01JAN2025\"d."""
    try:
        d = datetime.strptime(str(value).strip()[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        raise ValueError(f"Invalid date '{value}'; use ISO format YYYY-MM-DD.")
    return '"' + d.strftime("%d%b%Y").upper() + '"d'


def _num(value, default=None):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Expected a number, got {value!r}.")


def _build_synthetic_sas(table, caslib, server, columns, n_rows, seed=12345):
    """Build a SAS program that synthesises *n_rows* rows from a column spec and
    loads the result into CAS as a promoted (global) table.

    Each column is a dict with ``name`` and ``type`` (id|int|float|category|
    bool|date) plus type-specific options. Raises ValueError on an invalid spec
    so the caller (agent) can correct it. All user-supplied strings are quoted/
    validated, so the spec cannot inject arbitrary SAS.
    """
    if not isinstance(columns, list) or not columns:
        raise ValueError("Provide a non-empty list of column specs.")
    for nm in (table, caslib):
        if not _VALID_NAME.match(str(nm or "")):
            raise ValueError(
                f"'{nm}' is not a valid SAS/CAS name (letters, digits, "
                f"underscore; start with a letter/underscore; <=32 chars).")

    n = int(n_rows)
    length_decls, format_decls, body = [], [], []
    uses_p = False
    seen = set()

    for col in columns:
        if not isinstance(col, dict):
            raise ValueError("Each column must be an object with 'name'/'type'.")
        name = str(col.get("name", "")).strip()
        if not _VALID_NAME.match(name):
            raise ValueError(f"Invalid column name '{name}'.")
        if name.lower() in seen:
            raise ValueError(f"Duplicate column name '{name}'.")
        seen.add(name.lower())
        ctype = str(col.get("type", "float")).lower()
        if ctype not in _SYNTH_TYPES:
            raise ValueError(
                f"Column '{name}': type must be one of {', '.join(_SYNTH_TYPES)}.")

        if ctype == "id":
            width = max(6, len(str(max(n, 1))))
            length_decls.append(f"{name} $ {width}")
            body.append(f"{name} = put(_i, z{width}.);")

        elif ctype in ("int", "float"):
            dist = str(col.get("dist", "uniform")).lower()
            mn, mx = _num(col.get("min")), _num(col.get("max"))
            if dist == "normal":
                expr = f"rand('normal', {_num(col.get('mean'), 0.0)}, {_num(col.get('std'), 1.0)})"
            elif dist == "poisson":
                expr = f"rand('poisson', {_num(col.get('lambda'), 1.0)})"
            elif dist == "uniform":
                lo = mn if mn is not None else 0.0
                hi = mx if mx is not None else (1.0 if ctype == "float" else 100.0)
                expr = (f"{lo} + floor(rand('uniform') * ({hi} - {lo} + 1))"
                        if ctype == "int" else
                        f"{lo} + rand('uniform') * ({hi} - {lo})")
            else:
                raise ValueError(
                    f"Column '{name}': dist must be uniform, normal, or poisson.")
            body.append(f"{name} = {expr};")
            if ctype == "int":
                body.append(f"{name} = round({name});")
            elif col.get("decimals") is not None:
                body.append(f"{name} = round({name}, {10 ** (-int(col['decimals'])):.10f});")
            if mn is not None:
                body.append(f"if {name} < {mn} then {name} = {mn};")
            if mx is not None:
                body.append(f"if {name} > {mx} then {name} = {mx};")

        elif ctype == "category":
            levels = col.get("levels")
            if not isinstance(levels, list) or not levels:
                raise ValueError(
                    f"Column '{name}': category requires a non-empty 'levels' list.")
            weights = col.get("weights")
            w = ([max(0.0, _num(x, 0.0)) for x in weights]
                 if weights and len(weights) == len(levels) else [1.0] * len(levels))
            total = sum(w) or 1.0
            length_decls.append(f"{name} $ {min(64, max(len(str(x)) for x in levels))}")
            uses_p = True
            body.append("_p = rand('uniform');")
            cum = 0.0
            for i, lvl in enumerate(levels):
                cum += w[i] / total
                lit = _sas_quote(lvl)
                if i == 0:
                    body.append(f"if _p < {cum:.6f} then {name} = {lit};")
                elif i < len(levels) - 1:
                    body.append(f"else if _p < {cum:.6f} then {name} = {lit};")
                else:
                    body.append(f"else {name} = {lit};")

        elif ctype == "bool":
            body.append(f"{name} = (rand('uniform') < {_num(col.get('p_true'), 0.5)});")

        elif ctype == "date":
            start, end = _iso_to_sas_date(col.get("start")), _iso_to_sas_date(col.get("end"))
            body.append(
                f"{name} = {start} + floor(rand('uniform') * ({end} - {start} + 1));")
            format_decls.append(f"{name} date9.")

    lines = ["data work._mcp_synth;", f"  call streaminit({int(seed)});"]
    if length_decls:
        lines.append("  length " + " ".join(length_decls) + ";")
    if format_decls:
        lines.append("  format " + " ".join(format_decls) + ";")
    lines.append(f"  do _i = 1 to {n};")
    lines += [f"    {s}" for s in body]
    lines += ["    output;", "  end;", f"  drop _i{' _p' if uses_p else ''};", "run;", ""]
    lines += ["cas mcpcas;", "caslib _all_ assign;", "proc casutil;",
              f'  load data=work._mcp_synth outcaslib="{caslib}" casout="{table}" promote;',
              "quit;", "cas mcpcas terminate;"]
    return "\n".join(lines)


async def _resolve_free_table_name(client, server, caslib, base):
    """Return *base*, or the first free ``base_N`` if a CAS table of that name
    already exists in *caslib* (case-insensitive)."""
    try:
        items, _ = await _get_paged_items(
            f"/casManagement/servers/{server}/caslibs/{caslib}/tables",
            client, limit=1000)
    except Exception:
        return base
    existing = {str(t.get("name", "")).upper() for t in items}
    if base.upper() not in existing:
        return base
    for i in range(1, 1000):
        if f"{base}_{i}".upper() not in existing:
            return f"{base}_{i}"
    return base


class ScopeError(Exception):
    """Raised when a tool is asked to act on a resource outside the use-case scope."""


def register_tools(mcp, get_token):
    """Register all tools on *mcp*.

    Parameters
    ----------
    mcp : FastMCP
        The server instance to register tools on.
    get_token : callable
        ``async def get_token(ctx: Context) -> str`` — returns a Viya access
        token.  HTTP mode pulls it from context state; stdio mode acquires it
        via password grant.
    """

    # Use-case scope (allowlist) read from environment variables. When no
    # ALLOWED_* variables are set, ``scope.active`` is False and every tool
    # behaves exactly as it did before — full access to the environment.
    scope = load_scope()
    if scope.active:
        logger.info(
            "Use-case scope ACTIVE (%s): %d tables, %d models, "
            "%d decisions; enforce=%s",
            scope.name or "unnamed", len(scope.tables),
            len(scope.models), len(scope.decisions), scope.enforce)

    def _guard(allowed: bool, kind: str, value, allowed_list):
        """Block an out-of-scope resource access when enforcement is on."""
        if scope.enforced and not allowed:
            allowed_str = ", ".join(allowed_list) if allowed_list else "(none)"
            raise ScopeError(
                f"'{value}' is outside this assistant's use case and cannot be "
                f"accessed. This assistant is limited to these {kind}: {allowed_str}. "
                f"Call get_use_case to see the full scope."
            )

    # ------------------------------------------------------------------
    # Use-case scope
    # ------------------------------------------------------------------

    @mcp.tool()
    async def get_use_case(ctx: Context) -> dict:
        """Return this assistant's use-case scope: the datasets, models, and decisions it is limited to.

        Call this first to learn which resources you may work with. If the
        assistant is not scoped to a use case, ``scoped`` is false and you have
        full access to the environment.
        """
        logger.info("--- TOOL USED: get_use_case ---")
        return scope.manifest()

    # ------------------------------------------------------------------
    # Original tool
    # ------------------------------------------------------------------

    @mcp.tool()
    async def execute_sas_code(sas_code: str, ctx: Context) -> ToolResult:
        """
        Executes the provided SAS code in the Viya environment and returns information about the completed Job.
        This will create a job definition for the SAS code, execute it, and then retrieve the results.

        Args:
            sas_code (str): the SAS code snippet to be executed using the Viya Job Execution API Service

        Returns:
            Structured output data containing detailed information about the executed sas code.
            This includes a listing field and a log field. The listing output represents the intended output
            of the SAS code when executed, if the code ran successfully. The log output represents information
            about the execution of the sas code, such as if it ran successfully or not and whether or not there are
            errors or issues with the execution.

        """
        logger.info("--- TOOL USED: execute_sas_code ---")
        token = await get_token(ctx)
        output = await run_one_snippet(sas_code, "1", token)
        # Cap log/listing so a verbose PROC can't blow up the agent's context
        # (output is (snippet_id, state, log, listing)).
        if isinstance(output, (list, tuple)) and len(output) >= 4:
            sid, state, log_text, listing_text = output[:4]
            output = (sid, state,
                      _truncate_output(log_text),
                      _truncate_output(listing_text))
        return output

    # ------------------------------------------------------------------
    # Visualization (rendered client-side by the custom UI)
    # ------------------------------------------------------------------

    _CHART_TYPES = ("bar", "line", "area", "pie", "scatter")

    @mcp.tool()
    async def render_chart(chart_type: str, title: str, data: list,
                           x_key: str, y_keys: list, ctx: Context,
                           subtitle: str = "", stacked: bool = False) -> dict:
        """Render an interactive chart in the chat UI.

        Use whenever the user asks to show / plot / visualize / graph / compare
        data, or when a chart makes the answer clearer than text. Call this AFTER
        fetching the rows with the data tools (e.g. get_castable_data) or computing
        them with execute_sas_code, then pass the rows in as ``data``. Keep ``data``
        small — aggregate or limit to just the rows you want to chart.

        The chart is drawn by the user interface from this call; the tool itself
        does no plotting and returns the normalized chart spec.

        Args:
            chart_type: One of bar, line, area, pie, scatter.
            title: Chart title.
            data: List of row objects, e.g. [{"month": "Jan", "sales": 120}, ...].
            x_key: Field for the x-axis / category (for pie, the slice label).
            y_keys: Field(s) plotted as series / values (for pie or scatter, one or two).
            subtitle: Optional subtitle.
            stacked: For bar/area, stack the series instead of grouping them.
        """
        logger.info("--- TOOL USED: render_chart ---")
        ct = (chart_type or "").strip().lower()
        if ct not in _CHART_TYPES:
            raise ValueError(
                f"chart_type must be one of {', '.join(_CHART_TYPES)}; got '{chart_type}'.")
        if not isinstance(data, list) or not data:
            raise ValueError("data must be a non-empty list of row objects.")
        if not isinstance(data[0], dict):
            raise ValueError("each item in data must be an object (key/value row).")
        keys = list(data[0].keys())
        missing = [k for k in [x_key, *y_keys] if k not in keys]
        if missing:
            raise ValueError(
                f"these keys are not present in the data rows: {', '.join(missing)}. "
                f"Available keys: {', '.join(keys)}.")
        return {
            "kind": "chart",
            "type": ct,
            "title": title,
            "subtitle": subtitle,
            "data": data,
            "xKey": x_key,
            "yKeys": list(y_keys),
            "stacked": bool(stacked),
        }

    # ------------------------------------------------------------------
    # Tier 1 — Data Discovery (CAS Management)
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_cas_servers(ctx: Context) -> list:
        """List available CAS servers on the Viya environment."""
        logger.info("--- TOOL USED: list_cas_servers ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items("/casManagement/servers", client)
            return [{"name": s.get("name"), "id": s.get("id"),
                     "description": s.get("description", "")} for s in items]

    @mcp.tool()
    async def list_caslibs(server_id: str, ctx: Context,
                           limit: int = 50) -> list:
        """List CAS libraries (caslibs) available on a CAS server.

        Args:
            server_id: CAS server name or ID (e.g. 'cas-shared-default').
            limit: Maximum number of caslibs to return (default 50).
        """
        logger.info("--- TOOL USED: list_caslibs ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items(
                f"/casManagement/servers/{server_id}/caslibs", client, limit=limit)
            return [{"name": c.get("name"), "type": c.get("type", ""),
                     "description": c.get("description", "")} for c in items]

    @mcp.tool()
    async def list_castables(server_id: str, caslib_name: str, ctx: Context,
                             limit: int = 50) -> list:
        """List tables in a CAS library.

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            limit: Maximum number of tables to return (default 50).
        """
        logger.info("--- TOOL USED: list_castables ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables",
                client, limit=limit)
            if scope.active:
                items = [t for t in items
                         if scope.allows_table(t.get("name"), caslib_name, server_id)]
            return [{"name": t.get("name"),
                     "rowCount": t.get("rowCount"),
                     "columnCount": t.get("columnCount")} for t in items]

    @mcp.tool()
    async def get_castable_info(server_id: str, caslib_name: str,
                                table_name: str, ctx: Context) -> dict:
        """Get metadata for a CAS table (row count, column count, size, etc.).

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            table_name: Name of the table.
        """
        logger.info("--- TOOL USED: get_castable_info ---")
        _guard(scope.allows_table(table_name, caslib_name, server_id),
               "datasets", f"{caslib_name}.{table_name}", scope.tables)
        token = await get_token(ctx)
        async with _make_client(token) as client:
            return await _get_json(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables/{table_name}",
                client)

    @mcp.tool()
    async def get_castable_columns(server_id: str, caslib_name: str,
                                   table_name: str, ctx: Context,
                                   limit: int = 200) -> list:
        """Get column metadata for a CAS table (names, types, labels, formats).

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            table_name: Name of the table.
            limit: Maximum columns to return (default 200).
        """
        logger.info("--- TOOL USED: get_castable_columns ---")
        _guard(scope.allows_table(table_name, caslib_name, server_id),
               "datasets", f"{caslib_name}.{table_name}", scope.tables)
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables/{table_name}/columns",
                client, limit=limit)
            return [{"name": c.get("name"), "type": c.get("type"),
                     "rawLength": c.get("rawLength"),
                     "label": c.get("label", ""),
                     "format": c.get("format", "")} for c in items]

    @mcp.tool()
    async def get_castable_data(server_id: str, caslib_name: str,
                                table_name: str, ctx: Context,
                                limit: int = 100, start: int = 0) -> dict:
        """Fetch rows from a CAS table with column names.

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            table_name: Name of the table.
            limit: Maximum rows to return (default 100).
            start: Row offset (default 0).
        """
        logger.info("--- TOOL USED: get_castable_data ---")
        _guard(scope.allows_table(table_name, caslib_name, server_id),
               "datasets", f"{caslib_name}.{table_name}", scope.tables)
        token = await get_token(ctx)
        from .viya_utils import VIYA_ENDPOINT
        data_source_id = f"cas~fs~{server_id}~fs~{caslib_name}"
        table_id = f"cas~fs~{server_id}~fs~{caslib_name}~fs~{table_name}"
        async with _make_client(token) as client:
            columns = []
            col_start = 0
            col_limit = 100
            while True:
                col_resp = await client.get(
                    f"{VIYA_ENDPOINT}/dataTables/dataSources/{data_source_id}/tables/{table_name}/columns",
                    params={"start": col_start, "limit": col_limit},
                    follow_redirects=True,
                )
                col_resp.raise_for_status()
                col_data = col_resp.json()
                for item in col_data.get("items", []):
                    columns.append({"name": item.get("name"), "type": item.get("type"),
                                    "index": item.get("index")})
                total = col_data.get("count", 0)
                col_start += col_limit
                if col_start >= total:
                    break

            row_resp = await client.get(
                f"{VIYA_ENDPOINT}/rowSets/tables/{table_id}/rows",
                params={"start": start, "limit": limit},
                follow_redirects=True,
            )
            row_resp.raise_for_status()
            row_data = row_resp.json()

            col_names = [c["name"] for c in columns]
            rows = []
            for item in row_data.get("items", []):
                cells = item.get("cells", [])
                rows.append(dict(zip(col_names, cells)))

            return {
                "columns": col_names,
                "rows": rows,
                "count": row_data.get("count", len(rows)),
                "start": start,
                "limit": limit,
            }

    # ------------------------------------------------------------------
    # Tier 2 — Data Operations & Files
    # ------------------------------------------------------------------

    @mcp.tool()
    async def upload_data(server_id: str, caslib_name: str, table_name: str,
                          csv_data: str, ctx: Context) -> dict:
        """Upload CSV data into a CAS table.

        Args:
            server_id: CAS server name or ID.
            caslib_name: Target caslib name.
            table_name: Name for the new table.
            csv_data: CSV-formatted data string (including header row).
        """
        logger.info("--- TOOL USED: upload_data ---")
        token = await get_token(ctx)
        from .viya_utils import VIYA_ENDPOINT
        async with _make_client(token) as client:
            resp = await client.post(
                f"{VIYA_ENDPOINT}/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables",
                data={
                    "tableName": table_name,
                    "format": "csv",
                    "containsHeaderRow": "true",
                },
                files={"file": ("data.csv", csv_data.encode("utf-8"), "text/csv")},
            )
            if resp.status_code == 409:
                return {
                    "status": "table_already_exists",
                    "table_name": table_name,
                    "caslib": caslib_name,
                    "message": f"Table '{table_name}' already exists in caslib '{caslib_name}'. Drop or rename before re-uploading.",
                }
            resp.raise_for_status()
            body = resp.json()
            return {
                "status": "success",
                "table_name": body.get("name"),
                "rows_uploaded": body.get("rowCount", 0),
                "column_count": body.get("columnCount", 0),
                "caslib": body.get("caslibName"),
                "scope": body.get("scope"),
            }

    @mcp.tool()
    async def promote_table_to_memory(server_id: str, caslib_name: str,
                                      table_name: str, ctx: Context) -> dict:
        """Promote a CAS table to global scope (makes it visible to all sessions).

        Args:
            server_id: CAS server name or ID.
            caslib_name: Caslib containing the table.
            table_name: Table to promote.
        """
        logger.info("--- TOOL USED: promote_table_to_memory ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            try:
                return await _post_json(
                    f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables/{table_name}",
                    client, body={"scope": "global"})
            except _httpx.HTTPStatusError as e:
                if e.response.status_code == 409:
                    return {"status": "already_promoted", "table": f"{caslib_name}.{table_name}"}
                raise

    @mcp.tool()
    async def list_files(ctx: Context, limit: int = 50,
                         filter_name: Optional[str] = None) -> list:
        """List files in the Viya Files Service.

        Args:
            limit: Maximum files to return (default 50).
            filter_name: Optional name filter (substring match).
        """
        logger.info("--- TOOL USED: list_files ---")
        token = await get_token(ctx)
        filters = f"contains(name,'{filter_name}')" if filter_name else None
        async with _make_client(token) as client:
            items, _ = await _get_paged_items("/files/files", client,
                                              limit=limit, filters=filters)
            return [{"id": f.get("id"), "name": f.get("name"),
                     "contentType": f.get("contentType", ""),
                     "size": f.get("size")} for f in items]

    @mcp.tool()
    async def upload_file(file_name: str, content: str, ctx: Context,
                          content_type: str = "text/plain") -> dict:
        """Upload a file to the Viya Files Service.

        Args:
            file_name: Name for the file.
            content: File content as a string.
            content_type: MIME type (default 'text/plain').
        """
        logger.info("--- TOOL USED: upload_file ---")
        token = await get_token(ctx)
        from .viya_utils import VIYA_ENDPOINT
        async with _make_client(token) as client:
            resp = await client.post(
                f"{VIYA_ENDPOINT}/files/files",
                content=content.encode("utf-8"),
                headers={"Content-Type": content_type,
                         "Content-Disposition": f'attachment; filename="{file_name}"',
                         "Accept": "application/json"})
            resp.raise_for_status()
            return resp.json()

    @mcp.tool()
    async def download_file(file_id: str, ctx: Context) -> str:
        """Download file content from the Viya Files Service.

        Args:
            file_id: ID of the file to download.
        """
        logger.info("--- TOOL USED: download_file ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            from .viya_utils import VIYA_ENDPOINT
            resp = await client.get(f"{VIYA_ENDPOINT}/files/files/{file_id}/content")
            resp.raise_for_status()
            return resp.text

    # ------------------------------------------------------------------
    # Data Generation
    # ------------------------------------------------------------------

    @mcp.tool()
    async def generate_synthetic_data(table_name: str, columns: list,
                                      ctx: Context, n_rows: int = 1000,
                                      caslib_name: str = "Public",
                                      server_id: str = "cas-shared-default",
                                      seed: int = 12345) -> dict:
        """Generate a synthetic CAS table from a column specification.

        Use this to create realistic mock data on request (e.g. a driver-risk
        dataset for a demo). Recommended flow: first PROPOSE the column schema to
        the user in chat and get their agreement, THEN call this tool. The rows
        are generated in SAS and saved to CAS as a promoted (global) table,
        immediately usable by the data, charting, AutoML, and scoring tools.

        If a table with the requested name already exists, a numbered variant is
        created automatically (no error). Very large requests are capped to a
        safe maximum rather than failing.

        Args:
            table_name: Name for the new CAS table.
            columns: List of column specs. Each is an object with ``name`` and
                ``type`` (one of: id, int, float, category, bool, date) plus
                type-specific options:
                  - id: sequential zero-padded identifier
                  - int / float: ``min``, ``max``; or ``dist`` "normal"
                    (``mean``, ``std``) or "poisson" (``lambda``); float also
                    accepts ``decimals``
                  - category: ``levels`` (list) and optional ``weights`` (list)
                  - bool: ``p_true`` (probability of 1; default 0.5)
                  - date: ``start`` and ``end`` as YYYY-MM-DD
            n_rows: Number of rows to generate (default 1000).
            caslib_name: Target caslib (default Public).
            server_id: CAS server (default cas-shared-default).
            seed: Random seed for reproducibility (default 12345).
        """
        logger.info("--- TOOL USED: generate_synthetic_data ---")
        n = max(1, min(int(n_rows), MAX_SYNTHETIC_ROWS))
        clamped = int(n_rows) > MAX_SYNTHETIC_ROWS
        _guard(scope.allows_table(table_name, caslib_name, server_id),
               "datasets", f"{caslib_name}.{table_name}", scope.tables)
        token = await get_token(ctx)
        async with _make_client(token) as client:
            target = await _resolve_free_table_name(
                client, server_id, caslib_name, table_name)
        # _build_synthetic_sas validates the spec and raises ValueError on error.
        code = _build_synthetic_sas(target, caslib_name, server_id, columns, n, seed)
        output = await run_one_snippet(code, "1", token)
        state = output[1] if len(output) > 1 else "unknown"
        log_text = output[2] if len(output) > 2 else ""
        if state not in ("completed", "warning"):
            return {"error": True, "state": state,
                    "log": _truncate_output(log_text),
                    "message": (f"Generation of {caslib_name}.{target} failed; "
                                f"check the log and adjust the spec.")}
        result = {
            "table": target, "caslib": caslib_name, "server": server_id,
            "rowCount": n, "promoted": True,
            "columns": [{"name": c.get("name"), "type": c.get("type", "float")}
                        for c in columns if isinstance(c, dict)],
            "message": (f"Created {caslib_name}.{target} with {n} rows. "
                        f"Preview it with get_castable_data."),
        }
        if target != table_name:
            result["renamed_from"] = table_name
            result["note"] = (f"'{table_name}' already existed; created "
                              f"'{target}' instead.")
        if clamped:
            result["rows_capped"] = MAX_SYNTHETIC_ROWS
        return result

    # ------------------------------------------------------------------
    # Tier 4 — Batch Jobs & Async Execution
    # ------------------------------------------------------------------

    @mcp.tool()
    async def submit_batch_job(sas_code: str, ctx: Context,
                               job_name: Optional[str] = None) -> dict:
        """Submit a SAS job for asynchronous execution via the Job Execution service.

        Args:
            sas_code: SAS code to execute.
            job_name: Optional descriptive name for the job.
        """
        logger.info("--- TOOL USED: submit_batch_job ---")
        token = await get_token(ctx)
        from .viya_utils import CONTEXT_NAME
        body = {
            "name": job_name or "mcp-batch-job",
            "jobDefinition": {
                "type": "Compute",
                "code": sas_code,
            },
            "arguments": {
                "_contextName": CONTEXT_NAME,
            },
        }
        async with _make_client(token) as client:
            return await _post_json("/jobExecution/jobs", client, body=body)

    @mcp.tool()
    async def get_job_status(job_id: str, ctx: Context) -> dict:
        """Check the status of a submitted job.

        Args:
            job_id: ID of the job.
        """
        logger.info("--- TOOL USED: get_job_status ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            return await _get_json(f"/jobExecution/jobs/{job_id}", client)

    @mcp.tool()
    async def list_jobs(ctx: Context, limit: int = 20) -> list:
        """List recent jobs from the Job Execution service.

        Args:
            limit: Maximum jobs to return (default 20).
        """
        logger.info("--- TOOL USED: list_jobs ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items("/jobExecution/jobs", client,
                                              limit=limit)
            return [{"id": j.get("id"), "name": j.get("name", ""),
                     "state": j.get("state", ""),
                     "creationTimeStamp": j.get("creationTimeStamp", "")} for j in items]

    @mcp.tool()
    async def cancel_job(job_id: str, ctx: Context) -> str:
        """Cancel a running job.

        Args:
            job_id: ID of the job to cancel.
        """
        logger.info("--- TOOL USED: cancel_job ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            await _delete_resource(f"/jobExecution/jobs/{job_id}", client)
            return f"Job {job_id} cancelled."

    @mcp.tool()
    async def get_job_log(job_id: str, ctx: Context) -> str:
        """Retrieve the log of a completed job.

        Args:
            job_id: ID of the job.
        """
        logger.info("--- TOOL USED: get_job_log ---")
        token = await get_token(ctx)
        from .viya_utils import VIYA_ENDPOINT
        async with _make_client(token) as client:
            data = await _get_json(f"/jobExecution/jobs/{job_id}", client)
            results = data.get("results", {})

            log_uri = None
            for key, value in results.items():
                if key.endswith(".log.txt"):
                    log_uri = value
                    break
            if not log_uri:
                for key, value in results.items():
                    if key.endswith(".log"):
                        log_uri = value
                        break

            if not log_uri:
                state = data.get("state", "unknown")
                error = data.get("error", {})
                if error:
                    return f"Job {state}: {error.get('message', 'No error details')}"
                return f"No log available. Job state: {state}"

            resp = await client.get(f"{VIYA_ENDPOINT}{log_uri}/content")
            resp.raise_for_status()
            return resp.text

    # ------------------------------------------------------------------
    # Tier 5 — Model Management & Scoring
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_ml_projects(ctx: Context, limit: int = 50) -> list:
        """List AutoML pipeline automation projects.

        Args:
            limit: Maximum projects to return (default 50).
        """
        logger.info("--- TOOL USED: list_ml_projects ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items(
                "/mlPipelineAutomation/projects", client, limit=limit)
            return [{"id": p.get("id"), "name": p.get("name", ""),
                     "state": p.get("state", ""),
                     "description": p.get("description", "")} for p in items]

    @mcp.tool()
    async def create_ml_project(project_name: str, data_table_uri: str,
                                target_variable: str, ctx: Context,
                                description: str = "",
                                prediction_type: str = "binary",
                                target_event_level: str = "1",
                                auto_run: bool = True) -> dict:
        """Create a new AutoML pipeline automation project.

        SAS auto-detects the target's measurement level from the data; for
        classification targets, ``target_event_level`` selects the modeled
        event level. The data table must be loaded in CAS.

        Args:
            project_name: Name for the project.
            data_table_uri: URI of the training data table (e.g. '/dataTables/dataSources/cas~fs~cas-shared-default~fs~Public/tables/HMEQ').
            target_variable: Name of the target/response variable.
            description: Optional project description.
            prediction_type: 'binary', 'interval', or 'nominal' (default 'binary'). For 'binary'/'nominal', target_event_level is included.
            target_event_level: Event level for classification targets (default '1'); ignored for 'interval'.
            auto_run: Whether to automatically run pipelines after creation (default True).
        """
        logger.info("--- TOOL USED: create_ml_project ---")
        token = await get_token(ctx)
        # SAS media type required by the MLPA service. Keep
        # analyticsProjectAttributes to the documented, valid fields only —
        # extra attributes (e.g. targetLevel, classSelectionStatistic) make the
        # underlying analytics-project metadata step fail ("...failed to update
        # project metadata. Make sure that the parameters ... are valid").
        mlpa_type = "application/vnd.sas.analytics.ml.pipeline.automation.project+json"
        analytics_attrs = {
            "targetVariable": target_variable,
            "partitionEnabled": True,
        }
        if prediction_type in ("binary", "nominal"):
            analytics_attrs["targetEventLevel"] = target_event_level
        body = {
            "name": project_name,
            "description": description,
            "type": "predictive",
            "dataTableUri": data_table_uri,
            "pipelineBuildMethod": "automatic",
            "settings": {
                "autoRun": auto_run,
                "applyGlobalMetadata": False,
                "numberOfModels": 5,
            },
            "analyticsProjectAttributes": analytics_attrs,
        }
        async with _make_client(token) as client:
            return await _post_json("/mlPipelineAutomation/projects", client,
                                    body=body, accept=mlpa_type)

    @mcp.tool()
    async def run_ml_project(project_id: str, ctx: Context) -> dict:
        """Run an AutoML pipeline automation project.

        Args:
            project_id: ID of the project to run.
        """
        logger.info("--- TOOL USED: run_ml_project ---")
        token = await get_token(ctx)
        import json as _json
        from .viya_utils import VIYA_ENDPOINT
        mlpa_type = "application/vnd.sas.analytics.ml.pipeline.automation.project+json"
        async with _make_client(token) as client:
            get_resp = await client.get(
                f"{VIYA_ENDPOINT}/mlPipelineAutomation/projects/{project_id}",
                headers={"Accept": mlpa_type},
            )
            get_resp.raise_for_status()
            project_body = get_resp.json()
            etag = get_resp.headers.get("etag", "")
            resp = await client.put(
                f"{VIYA_ENDPOINT}/mlPipelineAutomation/projects/{project_id}",
                params={"action": "retrainProject"},
                content=_json.dumps(project_body).encode(),
                headers={
                    "Content-Type": mlpa_type,
                    "Accept": mlpa_type,
                    "If-Match": etag,
                    "Accept-Language": "en",
                },
            )
            resp.raise_for_status()
            if resp.status_code == 204 or not resp.content:
                return {"status": "running", "projectId": project_id}
            return resp.json()

    @mcp.tool()
    async def delete_ml_project(project_id: str, ctx: Context) -> dict:
        """Delete an AutoML pipeline automation project.

        Use this to remove a project (for example, to start over with a
        different configuration) instead of calling the REST API from SAS
        code.

        Args:
            project_id: ID of the project to delete.
        """
        logger.info("--- TOOL USED: delete_ml_project ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            await _delete_resource(
                f"/mlPipelineAutomation/projects/{project_id}", client)
        return {"status": "deleted", "projectId": project_id}

    @mcp.tool()
    async def list_registered_models(ctx: Context, limit: int = 50) -> list:
        """List models in the Model Repository.

        Args:
            limit: Maximum models to return (default 50).
        """
        logger.info("--- TOOL USED: list_registered_models ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items("/modelRepository/models", client,
                                              limit=limit)
            if scope.active:
                items = [m for m in items
                         if scope.allows_model(m.get("id"), m.get("name"))]
            return [{"id": m.get("id"), "name": m.get("name", ""),
                     "description": m.get("description", ""),
                     "modelVersionName": m.get("modelVersionName", "")} for m in items]

    @mcp.tool()
    async def list_models_and_decisions(ctx: Context, limit: int = 50) -> list:
        """List published scoring models and decisions (MAS modules).

        Args:
            limit: Maximum modules to return (default 50).
        """
        logger.info("--- TOOL USED: list_models_and_decisions ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items("/microanalyticScore/modules", client,
                                              limit=limit)
            if scope.active:
                items = [m for m in items
                         if scope.allows_decision(m.get("id"), m.get("name"))]
            return [{"id": m.get("id"), "name": m.get("name", ""),
                     "description": m.get("description", "")} for m in items]

    @mcp.tool()
    async def score_data(module_id: str, step_id: str, input_data: dict,
                         ctx: Context) -> dict:
        """Score data against a published model or decision (MAS module).

        Args:
            module_id: MAS module ID.
            step_id: Step ID within the module (usually 'score' or 'execute').
            input_data: Dictionary of input variable name-value pairs.
        """
        logger.info("--- TOOL USED: score_data ---")
        _guard(scope.allows_decision(module_id), "models or decisions",
               module_id, scope.decisions)
        token = await get_token(ctx)
        body = {"inputs": [{"name": k, "value": v} for k, v in input_data.items()]}
        async with _make_client(token) as client:
            return await _post_json(
                f"/microanalyticScore/modules/{module_id}/steps/{step_id}", client,
                body=body)

    # ------------------------------------------------------------------
    # Tier 6 — Data Insights
    # ------------------------------------------------------------------

    @mcp.tool()
    async def explain_data(server_id: str, caslib_name: str, table_name: str,
                           target_variable: str, ctx: Context,
                           date_variable: Optional[str] = None) -> dict:
        """Explain a column of a CAS table in relation to the other columns (SAS Insights).

        Returns natural-language descriptions of the variable, its outliers,
        and variable-screening results — useful for understanding which
        variables drive a target before exploring or modelling the data.

        Args:
            server_id: CAS server name (e.g. 'cas-shared-default').
            caslib_name: Caslib containing the table.
            table_name: Name of the table.
            target_variable: Column to explain.
            date_variable: Optional time-series column; enables forecast insights.
        """
        logger.info("--- TOOL USED: explain_data ---")
        _guard(scope.allows_table(table_name, caslib_name, server_id),
               "datasets", f"{caslib_name}.{table_name}", scope.tables)
        token = await get_token(ctx)
        body = {
            "cas": {"server": server_id, "library": caslib_name,
                    "table": table_name},
            "targetVariable": target_variable,
            "includeVariableDescription": True,
            "includeOutlierDescription": True,
        }
        if date_variable:
            body["dateVariable"] = date_variable
        async with _make_client(token) as client:
            return await _post_json("/insights/explain", client, body=body)
