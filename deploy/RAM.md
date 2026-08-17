# Hosting this server in SAS Retrieval Agent Manager (RAM)

RAM starts the MCP server as a container and points an agent at its HTTP
endpoint. It never opens a browser, so the default HTTP server — which makes
**every MCP client** complete an OAuth flow — cannot be used. This page covers
the other mode: **direct HTTP**, where the *server* holds one Viya identity and
authenticates itself.

| | Default HTTP (`app`) | Direct HTTP (`app-http-direct`) |
|---|---|---|
| Who authenticates | each MCP client, via browser OAuth 2.0 + PKCE | the server, once, with a service credential |
| Viya identity used | the end user's | the identity behind `VIYA_REFRESH_TOKEN` (or `VIYA_USERNAME`) |
| Token upkeep | per client | cached and refreshed automatically, rotation followed |
| Works in RAM | no | **yes** |

The container defaults to direct mode (`MCP_MODE=http-direct`), so an image
pulled into RAM is already in the right mode.

> **Everything below runs as one identity.** Viya authorization still applies —
> the server can only do what that identity is allowed to do — but the agent's
> own users are not distinguished from each other. Point it at a service
> account scoped to the data the use case needs, not at an administrator.

---

## 1. Mint a refresh token

A refresh token is the credential to use. It is the **only** option when Viya
identities are federated through an external IdP (Okta, Entra): SAS Logon never
sees those users' passwords, so the password grant cannot authenticate them.
It also survives password changes and needs no interactive login afterwards.

Sign in as the identity the agent should act as:

```sh
export VIYA_ENDPOINT=https://your-viya-host.com
uv run sas-mcp-login --print-refresh-token
```

The helper opens SAS Logon, takes the authorization code you paste back, and
prints the refresh token at the end. Copy it — that value goes into
`VIYA_REFRESH_TOKEN`.

If your OAuth client is not registered with the `refresh_token` grant type the
helper says so; ask an administrator to add it, or fall back to
`VIYA_USERNAME`/`VIYA_PASSWORD` (works only for accounts SAS Logon
authenticates directly — local `sasboot`, LDAP).

**Treat the refresh token as a password.** It carries the full identity and
privileges of whoever signed in. Refresh tokens also expire eventually (SAS
Logon's `refresh-token-validity`, commonly 90 days) — when they do, every tool
call starts failing with an authentication error that names this command; re-run
it and update the variable.

## 2. Publish the image

The image is built and pushed to GitHub Container Registry by
[`.github/workflows/publish-ghcr.yml`](../.github/workflows/publish-ghcr.yml) on
every push to `main`, to `ram`, and on every `v*` tag:

| Tag | Built from |
|---|---|
| `ram` | tip of the `ram` branch — **the tag RAM should point at** |
| `edge` | tip of `main` |
| `latest` | most recent `v*` release tag |
| `sha-<short>` | one exact commit |

```
ghcr.io/<owner>/sas-mcp-server:ram
```

Two things have to be true of the package itself, and neither is fixable from
the workflow:

1. **Actions must be allowed to write to it.** A GHCR package created outside
   Actions — by a `docker push` from a laptop, or by another CI system — is not
   linked to any repository, and no repository's `GITHUB_TOKEN` can push to it
   however many `packages: write` permissions the workflow declares. The push
   fails with `denied: permission_denied: write_package` *after* a successful
   build. Fix it once at *Package settings* → **Manage Actions access** → add
   this repository with the **Write** role.
2. **It must be public**, or RAM cannot pull it. Packages first published by a
   workflow are private: *Package settings* → *Change visibility* → **Public**.
   (A private package means registry credentials on the dialog's
   *Authentication* tab instead — public is simpler.)

Check the image is reachable from outside GitHub before wiring up RAM:

```sh
docker logout ghcr.io   # prove it works without credentials
docker pull ghcr.io/<owner>/sas-mcp-server:ram
```

## 3. Fill in "Create new container tool template"

**Settings tab**

| Field | Value |
|---|---|
| Name | e.g. `SAS MCP Server` |
| Description | optional |
| Container image | `ghcr.io/<owner>/sas-mcp-server:ram` |
| Arguments | *leave empty* — the entrypoint picks the server from `MCP_MODE` |
| Transport | `HTTP` |
| Port | `8000` (matching `HOST_PORT` below) |
| Base Path | `/mcp` |

**Authentication tab** — leave empty. The server authenticates to Viya itself;
RAM does not need to send anything. (If you set `MCP_API_KEY` below, RAM must
then send that key, so leave the variable unset unless you configure it here.)

**Environment Variables tab** — this is where the whole configuration lives:

| Variable | Value | Why |
|---|---|---|
| `VIYA_ENDPOINT` | `https://your-viya-host.com` | Required. No trailing slash. |
| `VIYA_REFRESH_TOKEN` | *(the token from step 1)* | The server's credential. Secret. |
| `HOST_PORT` | `8000` | Must equal the **Port** field. Defaults to `8134` if unset. |
| `MCP_MODE` | `http-direct` | The image's default; set it explicitly so the intent is visible. |
| `MCP_TRANSPORT` | `http` | Serves `/mcp`. Use `sse` only if the client needs `/sse`. |
| `SSL_VERIFY` | `true` | Set `false` only for a self-signed Viya certificate. |
| `CLIENT_ID` | `sas-mcp` | Only if your OAuth client is registered under another id. |
| `MCP_SERVER_NAME` | e.g. `SAS Viya (prod)` | Name the agent sees — worth setting when you run more than one. |
| `MCP_TIERS` | e.g. `0-4` | Optional. Expose only some tool tiers; unset means all. |
| `MCP_READ_ONLY` | `false` | Optional. `true` withholds every tool that changes state or starts work. |
| `COMPUTE_CONTEXT_NAME` | `SAS Job Execution compute context` | Only if your compute context is named differently. |

`.env.sample` documents every variable, including the ones not listed here.

**Files tab** — nothing to add. There is no `.env` file to mount; direct mode
reads its configuration from the environment.

## 4. Verify

Locally first, which isolates configuration problems from RAM's own plumbing:

```sh
docker run --rm -p 8000:8000 \
  -e VIYA_ENDPOINT=https://your-viya-host.com \
  -e VIYA_REFRESH_TOKEN=<token> \
  -e HOST_PORT=8000 \
  ghcr.io/<owner>/sas-mcp-server:ram

curl http://localhost:8000/health
# {"status":"healthy","service":"sas-viya-execution-mcp"}
```

`/health` needs no credentials and is deliberately exempt from the API key, so
it is a liveness check only — it does **not** prove the Viya credential works.
For that, watch the startup log: the server reports which grant it will use, or
warns that no credentials are configured. The first tool call is what actually
exercises the token.

## Keeping up with upstream

This work lives on the long-lived **`ram`** branch, and `main` is deliberately
left as a clean mirror of `sassoftware/sas-mcp-server`. That is what keeps
GitHub's *Sync fork* button a fast-forward: the moment `main` carries commits of
its own, every sync becomes a manual merge — `CHANGELOG.md` conflicts on
essentially every upstream release, since both sides insert at the top — and
GitHub starts offering a "Discard commits" button that would delete this work.

To pick up upstream changes, sync `main` (the button, or the commands below),
then merge it into `ram` and resolve any conflicts there, on your own schedule:

```sh
git fetch upstream                 # git remote add upstream https://github.com/sassoftware/sas-mcp-server.git
git checkout main && git merge --ff-only upstream/main && git push
git checkout ram && git merge main
# resolve conflicts (CHANGELOG.md is the usual one), then:
uv run ruff check . && uv run pyright src && uv run python -m pytest -m "not integration"
git push                           # republishes ghcr.io/<owner>/sas-mcp-server:ram
```

A red `ram` build leaves the previous `ram` image in place, so RAM keeps running
the last good one while you fix it.

Note that cutting a `v*` tag on this fork also fires the `publish-mcp-registry`
job, which will fail: it publishes under upstream's `io.github.sassoftware`
namespace, which a fork's OIDC token cannot claim. The image itself is pushed
before that job runs, so the failure is cosmetic — but `ram` is the tag to
deploy from anyway.

## Troubleshooting

| Symptom | Cause |
|---|---|
| RAM cannot pull the image | The GHCR package is still private (step 2), or the tag does not exist yet — check the workflow run finished. |
| The publish workflow fails with `denied: permission_denied: write_package` | The package is not linked to this repository — grant Actions **Write** access to it (step 2). The build itself succeeded; only the push was refused. |
| Connects, but every tool call fails with `AuthenticationError` | No credential in the environment, or the refresh token expired/was revoked. The message names the fix; re-run step 1. |
| `SAS Logon rejected the refresh_token grant (HTTP 401)` | The OAuth client does not have the `refresh_token` grant type, or `CLIENT_ID` here differs from the client the token was minted with. |
| `invalid_client` / "Missing credentials" | A `CLIENT_SECRET` was set for a client registered as public. Leave it empty unless the client is confidential. |
| Health check passes, agent sees no tools | Base Path or Transport mismatch: `http` serves `/mcp`, `sse` serves `/sse`. |
| Certificate errors against Viya | Self-signed certificate — set `SSL_VERIFY=false`. |
| Tool calls hang, then time out | Compute context name wrong, or the identity lacks access to it. Check `COMPUTE_CONTEXT_NAME`. |

---

Related: [`docker.md`](docker.md) for the image itself,
[`README.md`](README.md) for Kubernetes,
[`../examples/configuration.md`](../examples/configuration.md) for the
browser-OAuth client setup.
