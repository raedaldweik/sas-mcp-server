## Running with Docker
This repo provides a Dockerfile that can be used to build a docker image for the MCP server. Much more handy for production scenarios, portability, reproducibility, etc.

### Pulling the pre-built image
Official multi-arch images (`linux/amd64`, `linux/arm64`) are published to GitHub Container Registry:

```sh
docker pull ghcr.io/sassoftware/sas-mcp-server:latest
```

Tag → image-version mapping:

| Tag | Source | Notes |
|---|---|---|
| `latest` | most recent `v*` git tag | Stable, recommended for general use |
| `<major>.<minor>.<patch>` (e.g. `1.0.0`) | matching `v*` git tag | Pin to a specific release |
| `<major>.<minor>` (e.g. `1.0`) | latest patch of that minor line | Tracks patch updates |
| `edge` | tip of `main` | Unreleased / pre-release; expect breakage |
| `sha-<short>` (e.g. `sha-d3d89f4`) | specific commit | Pin to an exact build |

Each published image carries a signed build provenance attestation (verifiable with `gh attestation verify`).

### Building
Basic:
```sh
# From the repository root 
docker build -t MY_IMG:MY_TAG .
```

You can also pass in the expected host port at build time:
```sh
docker build --build-arg HOST_PORT=8500 -t MY_IMG_ARG:MY_TAG_ARG .
```

### Choosing which server runs
The image's entrypoint selects a server from `MCP_MODE`:

| `MCP_MODE` | Server | Authentication |
|---|---|---|
| `http-direct` (default) | `app-http-direct` | The server authenticates to Viya itself with `VIYA_REFRESH_TOKEN` (or `VIYA_USERNAME`/`VIYA_PASSWORD`). For hosts that cannot run a browser flow — see [`RAM.md`](RAM.md). |
| `http` | `app` | Per-user OAuth 2.0 PKCE; each MCP client signs in through a browser. |
| `stdio` | `app-stdio` | Reads the token cache mounted at `/app/.sas`. |

An explicit command overrides the mode entirely, so `docker run <image> app`
starts the browser-OAuth server whatever `MCP_MODE` says.

### Running
The docker container expects an .env file to be passed in at runtime.

Basic:
```sh
# From the repository root
docker run -d -p 8134:8134 --env-file .env --name YOUR_NAME MY_IMG:MY_TAG
```

If you set a non-default port at build time, make sure you set that in your .env!
```sh
docker run -d -p 8500:8500 --env-file .env --name YOUR_NAME MY_IMG_ARG:MY_TAG_ARG 

# In this case the .env should also have an entry for
HOST_PORT=8500
```
---

Usage is the exact same as when it was run locally.



