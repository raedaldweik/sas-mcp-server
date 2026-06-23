# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Shared tool registration for both HTTP and stdio MCP servers.
All tools are registered via ``register_tools(mcp, get_token)``.
"""

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
from .usecase import load_scope


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
            "Use-case scope ACTIVE (%s): %d tables, %d reports, %d models, "
            "%d decisions; enforce=%s",
            scope.name or "unnamed", len(scope.tables), len(scope.reports),
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
        """Return this assistant's use-case scope: the datasets, reports, models, and decisions it is limited to.

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
    # Tier 3 — Reports & Visualization
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_reports(ctx: Context, limit: int = 50,
                           filter_name: Optional[str] = None) -> list:
        """List Visual Analytics reports.

        Args:
            limit: Maximum reports to return (default 50).
            filter_name: Optional name filter (substring match).
        """
        logger.info("--- TOOL USED: list_reports ---")
        token = await get_token(ctx)
        filters = f"contains(name,'{filter_name}')" if filter_name else None
        async with _make_client(token) as client:
            items, _ = await _get_paged_items("/reports/reports", client,
                                              limit=limit, filters=filters)
            if scope.active:
                items = [r for r in items
                         if scope.allows_report(r.get("id"), r.get("name"))]
            return [{"id": r.get("id"), "name": r.get("name"),
                     "description": r.get("description", ""),
                     "createdBy": r.get("createdBy", "")} for r in items]

    @mcp.tool()
    async def get_report(report_id: str, ctx: Context) -> dict:
        """Get a Visual Analytics report's metadata and definition.

        Args:
            report_id: ID of the report.
        """
        logger.info("--- TOOL USED: get_report ---")
        _guard(scope.allows_report(report_id), "reports", report_id, scope.reports)
        token = await get_token(ctx)
        async with _make_client(token) as client:
            return await _get_json(f"/reports/reports/{report_id}", client)

    @mcp.tool()
    async def get_report_image(report_id: str, ctx: Context,
                               image_type: str = "png",
                               section_index: int = 0) -> dict:
        """Render a Visual Analytics report section as an image.

        Args:
            report_id: ID of the report.
            image_type: Image format — 'png' or 'svg' (default 'png').
            section_index: Report section/page index (default 0).
        """
        logger.info("--- TOOL USED: get_report_image ---")
        _guard(scope.allows_report(report_id), "reports", report_id, scope.reports)
        token = await get_token(ctx)
        import json as _json
        from .viya_utils import VIYA_ENDPOINT
        async with _make_client(token) as client:
            body = {
                "reportUri": f"/reports/reports/{report_id}",
                "layoutType": "thumbnail",
                "selectionType": "perSection",
                "sectionIndex": section_index,
                "size": "800x600",
                "renderLimit": 1,
            }
            resp = await client.post(
                f"{VIYA_ENDPOINT}/reportImages/jobs",
                content=_json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/vnd.sas.report.images.job.request+json",
                    "Accept": "application/vnd.sas.report.images.job+json",
                },
            )
            resp.raise_for_status()
            return resp.json()

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

        Args:
            project_name: Name for the project.
            data_table_uri: URI of the training data table (e.g. '/dataTables/dataSources/cas~fs~cas-shared-default~fs~Public/tables/HMEQ').
            target_variable: Name of the target/response variable.
            description: Optional project description.
            prediction_type: 'binary', 'interval', or 'nominal' (default 'binary').
            target_event_level: Target event level for binary/nominal classification (default '1').
            auto_run: Whether to automatically run pipelines after creation (default True).
        """
        logger.info("--- TOOL USED: create_ml_project ---")
        token = await get_token(ctx)
        analytics_attrs = {
            "targetVariable": target_variable,
            "targetLevel": prediction_type,
            "partitionEnabled": True,
            "classSelectionStatistic": "ks" if prediction_type in ("binary", "nominal") else "ase",
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
                "applyGlobalMetadata": True,
                "autoRun": auto_run,
                "numberOfModels": 5,
            },
            "analyticsProjectAttributes": analytics_attrs,
        }
        async with _make_client(token) as client:
            return await _post_json("/mlPipelineAutomation/projects", client, body=body)

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
    # Tier 6 — Report Building (Visual Analytics authoring)
    # ------------------------------------------------------------------

    REPORT_CONTENT_TYPE = "application/vnd.sas.report.content+json"

    @mcp.tool()
    async def get_report_content(report_id: str, ctx: Context) -> dict:
        """Get the full content (BIRD definition) of a Visual Analytics report.

        The content describes the report's pages, visual elements (charts,
        tables, KPIs), data sources, and data queries. Use it to learn the
        structure of existing reports before authoring or editing one.

        Args:
            report_id: ID of the report.
        """
        logger.info("--- TOOL USED: get_report_content ---")
        _guard(scope.allows_report(report_id), "reports", report_id, scope.reports)
        token = await get_token(ctx)
        async with _make_client(token) as client:
            return await _get_json(f"/reports/reports/{report_id}/content",
                                   client, accept=REPORT_CONTENT_TYPE)

    @mcp.tool()
    async def create_report(report_name: str, ctx: Context,
                            description: str = "",
                            parent_folder_uri: Optional[str] = None) -> dict:
        """Create a new (empty) Visual Analytics report.

        The report is created without content; use update_report_content to
        save its definition afterwards.

        Args:
            report_name: Name for the new report.
            description: Optional report description.
            parent_folder_uri: Folder URI to create the report in
                (e.g. '/folders/folders/{folderId}'). Defaults to the
                user's personal folder (My Folder).
        """
        logger.info("--- TOOL USED: create_report ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            if not parent_folder_uri:
                folder = await _get_json("/folders/folders/@myFolder", client)
                parent_folder_uri = f"/folders/folders/{folder.get('id')}"
            return await _post_json(
                "/reports/reports", client,
                body={"name": report_name, "description": description},
                params={"parentFolderUri": parent_folder_uri})

    @mcp.tool()
    async def update_report_content(report_id: str, content: dict,
                                    ctx: Context) -> dict:
        """Save the content (BIRD definition) of a Visual Analytics report.

        The content must be valid report content JSON
        (application/vnd.sas.report.content+json). Validate it first with
        validate_report_content, and verify the result visually with
        get_report_image.

        Args:
            report_id: ID of the report to update.
            content: Report content JSON (the BIRD model).
        """
        logger.info("--- TOOL USED: update_report_content ---")
        _guard(scope.allows_report(report_id), "reports", report_id, scope.reports)
        token = await get_token(ctx)
        import json as _json
        from .viya_utils import VIYA_ENDPOINT
        async with _make_client(token) as client:
            # Fetch the current content to obtain the ETag required by the
            # Reports service for optimistic-concurrency on saves.
            get_resp = await client.get(
                f"{VIYA_ENDPOINT}/reports/reports/{report_id}/content",
                headers={"Accept": REPORT_CONTENT_TYPE})
            get_resp.raise_for_status()
            etag = get_resp.headers.get("etag", "")
            headers = {"Content-Type": REPORT_CONTENT_TYPE,
                       "Accept": "application/json"}
            if etag:
                headers["If-Match"] = etag
            resp = await client.put(
                f"{VIYA_ENDPOINT}/reports/reports/{report_id}/content",
                content=_json.dumps(content).encode("utf-8"),
                headers=headers)
            resp.raise_for_status()
            return {"status": "saved", "reportId": report_id}

    @mcp.tool()
    async def validate_report_content(content: dict, ctx: Context) -> dict:
        """Validate Visual Analytics report content against the report schema.

        Use this before saving generated content with update_report_content.

        Args:
            content: Report content JSON (the BIRD model) to validate.
        """
        logger.info("--- TOOL USED: validate_report_content ---")
        token = await get_token(ctx)
        import json as _json
        from .viya_utils import VIYA_ENDPOINT
        async with _make_client(token) as client:
            resp = await client.post(
                f"{VIYA_ENDPOINT}/reports/content/validation",
                content=_json.dumps(content).encode("utf-8"),
                headers={"Content-Type": REPORT_CONTENT_TYPE,
                         "Accept": "application/vnd.sas.report.content.validation+json, application/json"})
            resp.raise_for_status()
            return resp.json()

    @mcp.tool()
    async def delete_report(report_id: str, ctx: Context) -> str:
        """Delete a Visual Analytics report.

        Args:
            report_id: ID of the report to delete.
        """
        logger.info("--- TOOL USED: delete_report ---")
        _guard(scope.allows_report(report_id), "reports", report_id, scope.reports)
        token = await get_token(ctx)
        async with _make_client(token) as client:
            await _delete_resource(f"/reports/reports/{report_id}", client)
            return f"Report {report_id} deleted."

    @mcp.tool()
    async def create_report_from_template(template_report_id: str,
                                          new_report_name: str,
                                          server_id: str, caslib_name: str,
                                          table_name: str, ctx: Context,
                                          original_table: Optional[str] = None,
                                          parent_folder_uri: Optional[str] = None,
                                          column_mappings: Optional[dict] = None) -> dict:
        """Create a new Visual Analytics report from an existing report (template) by swapping its data source.

        Clones the template report and rebinds it to a different CAS table,
        optionally remapping individual columns. This is the most reliable way
        to build a polished report for new data.

        Args:
            template_report_id: ID of the existing report to use as a template.
            new_report_name: Name for the resulting report.
            server_id: CAS server of the replacement table.
            caslib_name: Caslib of the replacement table.
            table_name: Name of the replacement CAS table.
            original_table: Table name of the template's data source to replace.
                Only needed when the template uses more than one data source.
            parent_folder_uri: Folder URI to save the new report in. Defaults
                to the user's personal folder.
            column_mappings: Optional mapping of original column names in the
                template's table to replacement column names in the new table,
                e.g. {"sales": "revenue", "region": "territory"}. Columns with
                identical names are matched automatically.
        """
        logger.info("--- TOOL USED: create_report_from_template ---")
        _guard(scope.allows_report(template_report_id), "reports",
               template_report_id, scope.reports)
        _guard(scope.allows_table(table_name, caslib_name, server_id),
               "datasets", f"{caslib_name}.{table_name}", scope.tables)
        token = await get_token(ctx)
        async with _make_client(token) as client:
            content = await _get_json(
                f"/reports/reports/{template_report_id}/content", client,
                accept=REPORT_CONTENT_TYPE)
            cas_sources = [ds.get("casResource") for ds in content.get("dataSources", [])
                           if isinstance(ds, dict) and ds.get("casResource")]
            if original_table:
                matches = [c for c in cas_sources
                           if c.get("table", "").lower() == original_table.lower()]
            else:
                matches = cas_sources
            if len(matches) != 1:
                return {
                    "error": "Could not determine which data source of the template to replace. "
                             "Specify 'original_table' with one of the template's tables.",
                    "templateDataSources": cas_sources,
                }
            original = matches[0]

            replacement = {
                "purpose": "replacement",
                "namePattern": "serverLibraryTable",
                "server": server_id,
                "library": caslib_name,
                "table": table_name,
                "replacementLabel": table_name,
            }
            if column_mappings:
                replacement["dataItemReplacements"] = [
                    {"originalColumn": orig, "replacementColumn": repl}
                    for orig, repl in column_mappings.items()]

            body = {
                "inputReportUri": f"/reports/reports/{template_report_id}",
                "resultReportName": new_report_name,
                "dataSources": [
                    {"purpose": "original",
                     "namePattern": "serverLibraryTable",
                     "server": original.get("server"),
                     "library": original.get("library"),
                     "table": original.get("table")},
                    replacement,
                ],
            }
            if parent_folder_uri:
                body["resultParentFolderUri"] = parent_folder_uri

            result = await _post_json(
                "/reportTransforms/dataMappedReports", client, body=body,
                params={"useSavedReport": "true", "saveResult": "true",
                        "failOnDataSourceError": "false", "validate": "true"},
                accept="application/vnd.sas.report.transform+json, application/json")
            return {
                "evaluationStatus": result.get("evaluationStatus", ""),
                "newReport": result.get("resultReport", {}),
                "messages": result.get("messages", []),
                "errorMessages": result.get("errorMessages", []),
            }

    @mcp.tool()
    async def export_report_pdf(report_id: str, ctx: Context,
                                wait_seconds: int = 30) -> dict:
        """Export a Visual Analytics report as a PDF file.

        Starts an export job on the Visual Analytics service. The returned job
        contains links to the resulting PDF file when complete; poll with
        get_export_job if the job is still running.

        Args:
            report_id: ID of the report to export.
            wait_seconds: Seconds to wait for the export to finish before
                returning (default 30).
        """
        logger.info("--- TOOL USED: export_report_pdf ---")
        _guard(scope.allows_report(report_id), "reports", report_id, scope.reports)
        token = await get_token(ctx)
        async with _make_client(token) as client:
            return await _post_json(
                f"/visualAnalytics/reports/{report_id}/exportPdf", client,
                body={"version": 1, "options": {}, "wait": wait_seconds},
                accept="application/vnd.sas.visual.analytics.report.export.pdf.job+json, application/json")

    @mcp.tool()
    async def get_export_job(job_id: str, ctx: Context) -> dict:
        """Get the status of a Visual Analytics export job (PDF/image/data/package).

        Args:
            job_id: ID of the export job.
        """
        logger.info("--- TOOL USED: get_export_job ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            return await _get_json(f"/visualAnalytics/jobs/{job_id}", client)

    @mcp.tool()
    async def explain_data(server_id: str, caslib_name: str, table_name: str,
                           target_variable: str, ctx: Context,
                           date_variable: Optional[str] = None) -> dict:
        """Explain a column of a CAS table in relation to the other columns (SAS Insights).

        Returns natural-language descriptions of the variable, its outliers,
        and variable-screening results — useful for deciding which variables
        to feature when building a report about the data.

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
