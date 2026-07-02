# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import os
import httpx
from fastmcp.utilities.logging import get_logger
from .config import VIYA_ENDPOINT, CONTEXT_NAME, SSL_VERIFY

logger = get_logger(__name__)

# Safety net: the longest we will poll a single compute job before giving up,
# so a stuck job can never hang the agent indefinitely. Generous by default
# (1 hour) so legitimate long-running SAS code (heavy PROCs, large-data steps)
# is unaffected; override with the JOB_POLL_TIMEOUT environment variable
# (seconds) — raise it for very long workloads, lower it for snappier failure.
JOB_POLL_TIMEOUT = float(os.getenv("JOB_POLL_TIMEOUT", "3600"))

# ---------------------------------------------------------------------------
# Performance knobs
# ---------------------------------------------------------------------------
# These default ON because they are the difference between a ~1-2 second and a
# ~30-second execute_sas_code call. Set to "false" only to restore the old
# one-session/one-connection-per-call behaviour (e.g. when debugging).
#
# HTTP_CLIENT_POOL        — reuse one httpx client (TCP+TLS connections) per
#                           token across tool calls instead of a fresh
#                           handshake on every call.
# COMPUTE_SESSION_REUSE   — keep SAS compute sessions warm between
#                           execute_sas_code calls instead of paying the
#                           15-45s session start-up on every call. Note that a
#                           reused session keeps WORK datasets and macro
#                           variables from earlier calls, like a real SAS
#                           session.
# COMPUTE_SESSION_POOL_MAX— how many idle sessions to keep per identity.
# JOB_POLL_INITIAL        — first job-state poll delay; polling backs off
#                           toward the 2s cadence so short jobs return fast.
HTTP_CLIENT_POOL = os.getenv(
    "HTTP_CLIENT_POOL", "true").lower() not in ("false", "0", "no")
COMPUTE_SESSION_REUSE = os.getenv(
    "COMPUTE_SESSION_REUSE", "true").lower() not in ("false", "0", "no")
COMPUTE_SESSION_POOL_MAX = int(os.getenv("COMPUTE_SESSION_POOL_MAX", "3"))
JOB_POLL_INITIAL = float(os.getenv("JOB_POLL_INITIAL", "0.25"))

_client_pool = {}          # "Bearer <token>" -> httpx.AsyncClient
_MAX_POOLED_CLIENTS = 8    # tokens rotate rarely; keeps the pool bounded
_context_cache = {}        # (auth, context name) -> compute context id
_session_pool = {}         # "Bearer <token>" -> [idle compute session ids]
_session_lock = asyncio.Lock()


def _reset_pools():
    """Forget pooled clients, cached contexts, and idle sessions (test hook)."""
    _client_pool.clear()
    _context_cache.clear()
    _session_pool.clear()


def _normalize_token(token):
    return token if token.startswith("Bearer ") else f"Bearer {token}"


class _ClientLease:
    """Async context manager that yields a pooled client and leaves it open,
    so existing ``async with _make_client(token) as client:`` call sites keep
    working unchanged while the underlying connections are reused."""

    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Generic API helpers (used by new tools)
# ---------------------------------------------------------------------------

async def _get_json(url, client, params=None, accept="application/json"):
    """GET a JSON response from a Viya REST endpoint."""
    full_url = f"{VIYA_ENDPOINT}{url}"
    resp = await client.get(full_url, headers={"Accept": accept}, params=params or {})
    resp.raise_for_status()
    return resp.json()


async def _get_paged_items(url, client, limit=20, start=0, filters=None, extra_params=None):
    """GET a paginated collection and return the items list plus total count."""
    params = {"start": start, "limit": limit}
    if filters:
        params["filter"] = filters
    if extra_params:
        params.update(extra_params)
    data = await _get_json(url, client, params=params,
                           accept="application/vnd.sas.collection+json")
    return data.get("items", []), data.get("count", 0)


async def _post_json(url, client, body=None, params=None, accept="application/json"):
    """POST JSON to a Viya REST endpoint and return the response JSON."""
    full_url = f"{VIYA_ENDPOINT}{url}"
    resp = await client.post(full_url, json=body,
                             headers={"Content-Type": "application/json",
                                      "Accept": accept},
                             params=params or {})
    resp.raise_for_status()
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


async def _put_data(url, client, data, content_type="text/csv", params=None):
    """PUT raw data (e.g. CSV upload) to a Viya REST endpoint."""
    full_url = f"{VIYA_ENDPOINT}{url}"
    resp = await client.put(full_url, content=data,
                            headers={"Content-Type": content_type},
                            params=params or {})
    resp.raise_for_status()
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


async def _delete_resource(url, client):
    """DELETE a Viya REST resource."""
    full_url = f"{VIYA_ENDPOINT}{url}"
    resp = await client.delete(full_url)
    resp.raise_for_status()


def _make_client(token):
    """Return an async context manager yielding an authorized httpx client.

    With HTTP_CLIENT_POOL on (default) the underlying client is cached per
    token and kept open across tool calls, so repeated calls reuse pooled
    TCP+TLS connections instead of paying a fresh handshake every time.
    """
    auth = _normalize_token(token)
    headers = {"Authorization": auth}
    if not HTTP_CLIENT_POOL:
        return httpx.AsyncClient(headers=headers, verify=SSL_VERIFY, timeout=300.0)
    client = _client_pool.get(auth)
    if client is None or client.is_closed is True:
        while len(_client_pool) >= _MAX_POOLED_CLIENTS:
            old = _client_pool.pop(next(iter(_client_pool)))
            try:
                asyncio.get_running_loop().create_task(old.aclose())
            except RuntimeError:
                pass  # no running loop — let GC collect the old client
        client = httpx.AsyncClient(headers=headers, verify=SSL_VERIFY,
                                   timeout=300.0)
        _client_pool[auth] = client
    return _ClientLease(client)


# ---------------------------------------------------------------------------
# Original helpers (log/listing fetching)
# ---------------------------------------------------------------------------

async def _get_text(url, client, verify=True, extra_params=None):
    # Try text/plain in one shot
    full_url = f"{VIYA_ENDPOINT}{url}"
    r = await client.get(
        full_url, headers={"Accept": "text/plain"}, params=extra_params or {}
    )
    if r.status_code == 200 and r.headers.get("Content-Type", "").startswith(
        "text/plain"
    ):
        return r.text
    # Some deployments need an explicit query hint
    r = await client.get(
        full_url,
        headers={"Accept": "text/plain"},
        params={**(extra_params or {}), "type": "text"},
    )
    if r.status_code == 200 and r.headers.get("Content-Type", "").startswith(
        "text/plain"
    ):
        return r.text
    return None  # caller will fallback to paged JSON


async def _get_paged_lines(url, client, page_limit=10000):
    start = 0
    lines = []
    headers = {"Accept": "application/vnd.sas.collection+json"}
    full_url = f"{VIYA_ENDPOINT}{url}"
    while True:
        resp = await client.get(
            full_url, headers=headers, params={"start": start, "limit": page_limit}
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        if not items:
            break
        # items can be dicts like {"line": "..."} or {"text": "..."} depending on endpoint
        for it in items:
            lines.append(it.get("line") or it.get("text") or "")
        if len(items) < page_limit:
            break
        start += page_limit
    return "\n".join(lines)


async def fetch_full_job_log(client, session_id, job_id):
    base = f"/compute/sessions/{session_id}/jobs/{job_id}"
    # 1) Try whole job log as text
    text = await _get_text(f"{base}/log", client)
    if text is not None:
        return text
    # 2) Fallback to paged JSON
    return await _get_paged_lines(f"{base}/log", client)


async def fetch_full_job_listing(client, session_id, job_id):
    base = f"/compute/sessions/{session_id}/jobs/{job_id}"
    text = await _get_text(f"{base}/listing", client)
    if text is not None:
        return text
    return await _get_paged_lines(f"{base}/listing", client)


async def fetch_full_session_log(client, session_id):
    # Entire session log (useful if you want everything the session produced)
    text = await _get_text(f"/compute/sessions/{session_id}/log", client)
    if text is not None:
        return text
    return await _get_paged_lines(f"/compute/sessions/{session_id}/log", client)


async def get_context_id(client, context_name):
    url = f"{VIYA_ENDPOINT}/compute/contexts?name={context_name}"
    resp = await client.get(url)
    coll = resp.json()
    items = coll.get("items", [])
    if not items:
        raise RuntimeError(f"Compute context not found: {context_name}")
    return items[0]["id"]


async def create_session(client, context_id, name="py-parallel"):
    url = f"{VIYA_ENDPOINT}/compute/contexts/{context_id}/sessions"
    resp = await client.post(url, json={"name": name})
    return resp.json()["id"]


async def submit_job(client, session_id, code):
    body = {"code": code.splitlines()}
    url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs"
    resp = await client.post(url, json=body)
    job = resp.json()
    return job["id"]


async def wait_job(client, session_id, job_id, poll=2, timeout=None):
    """Poll a job until it reaches a terminal state, then fetch log + listing.

    Polling starts fast (JOB_POLL_INITIAL) and backs off toward *poll*, so a
    sub-second PROC returns in well under a second while long jobs settle into
    the gentle 2-second cadence.
    """
    timeout = JOB_POLL_TIMEOUT if timeout is None else timeout
    deadline = asyncio.get_running_loop().time() + timeout
    interval = min(JOB_POLL_INITIAL, poll)
    while True:
        state_url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/state"
        resp = await client.get(state_url)
        state = resp.text.strip()
        if state in ("completed", "error", "warning", "canceled"):
            # Fetch log
            log_url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/log"
            log_resp = await client.get(log_url)
            log = log_resp.json()
            lines = [item["line"] for item in log.get("items", [])]
            log_text = "\n".join(lines)

            # Fetch listing (plain text output)
            listing_url = (
                f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/listing"
            )
            listing_resp = await client.get(listing_url)
            listing_json = listing_resp.json()
            listing_lines = [item["line"] for item in listing_json.get("items", [])]
            listing_text = (
                "\n".join(listing_lines) if listing_lines else "(no listing output)"
            )

            return state, log_text, listing_text
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(
                f"SAS job {job_id} did not finish within {timeout:.0f}s "
                f"(last state: '{state}'). Increase JOB_POLL_TIMEOUT if this "
                f"job legitimately runs longer."
            )
        await asyncio.sleep(interval)
        interval = min(interval * 1.6, poll)


async def _cached_context_id(client, auth, context_name):
    """Resolve a compute context id once per (identity, name) and cache it —
    the context lookup is one extra round trip on every job otherwise."""
    key = (auth, context_name)
    ctx_id = _context_cache.get(key)
    if not ctx_id:
        ctx_id = await get_context_id(client, context_name)
        _context_cache[key] = ctx_id
    return ctx_id


async def _checkout_session(client, auth):
    """Return (session_id, reused) — an idle pooled session when available,
    otherwise a freshly created one."""
    if COMPUTE_SESSION_REUSE:
        async with _session_lock:
            idle = _session_pool.get(auth)
            if idle:
                return idle.pop(), True
    ctx_id = await _cached_context_id(client, auth, CONTEXT_NAME)
    sid = await create_session(client, ctx_id, name="sas-mcp-pooled")
    return sid, False


async def _checkin_session(client, auth, sid):
    """Park a healthy session for reuse, or delete it when pooling is off or
    the pool is already full."""
    if COMPUTE_SESSION_REUSE:
        async with _session_lock:
            idle = _session_pool.setdefault(auth, [])
            if len(idle) < COMPUTE_SESSION_POOL_MAX:
                idle.append(sid)
                return
    await _delete_session(client, sid)


async def _delete_session(client, sid):
    """Best-effort session delete — never mask the caller's real result."""
    try:
        await client.delete(f"{VIYA_ENDPOINT}/compute/sessions/{sid}")
        logger.info(f"Session {sid} deleted successfully")
    except Exception as e:
        logger.error(f"Failed to delete session {sid}: {str(e)}")


async def _run_in_session(client, session_id, code):
    jid = await submit_job(client, session_id, code)
    logger.info(f"Job submitted: {jid}")
    result = await wait_job(client, session_id, jid)
    logger.info(f"Job completed: {result[0]}")
    return result


async def run_one_snippet(snippet_data, snippet_id, token):
    """Execute one SAS snippet and return (snippet_id, state, log, listing).

    Compute sessions are pooled per identity (COMPUTE_SESSION_REUSE): session
    creation is the dominant cost of a compute call (tens of seconds while a
    compute server spins up), so completed sessions are parked and reused by
    the next call instead of being deleted. A reused session that has expired
    server-side is detected on failure and the job is retried once on a fresh
    session.
    """
    code = snippet_data
    auth = _normalize_token(token)

    async with _make_client(token) as client:
        sid, reused = await _checkout_session(client, auth)
        logger.info(f"{'Reusing pooled' if reused else 'Created'} compute "
                    f"session: {sid}")
        try:
            result = await _run_in_session(client, sid, code)
        except Exception as e:
            if not reused:
                logger.error(f"Error executing SAS job: {str(e)}")
                await _delete_session(client, sid)
                raise
            # The pooled session may have timed out or died between calls —
            # replace it and retry the job once before giving up.
            logger.warning(f"Pooled session {sid} failed ({e}); retrying on "
                           f"a fresh session")
            await _delete_session(client, sid)
            ctx_id = await _cached_context_id(client, auth, CONTEXT_NAME)
            sid = await create_session(client, ctx_id, name="sas-mcp-pooled")
            try:
                result = await _run_in_session(client, sid, code)
            except Exception as e2:
                logger.error(f"Error executing SAS job: {str(e2)}")
                await _delete_session(client, sid)
                raise
        await _checkin_session(client, auth, sid)
        return (snippet_id, *result)