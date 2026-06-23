## Configuration details for the SAS MCP Server

### Viya Setup
The SAS MCP Server runs locally and expects to communicate with a Viya instance.

The Viya instance serves two important roles:
1. Acts as an authorization server for the MCP Server
2. It provides the SAS execution API for the MCP Server

In order for the local MCP server to function properly, there are a few tweaks that need to be made to the Viya instance.

NOTE: These steps require Administrative access over the Viya instance. If you do not have access, please ask your SAS Administrator for assistance.

#### Step 1: Disable form-action Content Security Policy on SAS Logon Manager
Since the MCP Server is an external client to Viya, after successful authentication, the redirect will fail to trigger due to the form-action directive CSP. For local development and testing, it is most straightforward to **disable the directive**.  

1. Log into Viya, assume the Administrator role
2. Go to SAS Environment Manager (left hand screen, Manage Environment)
3. Go to Configuration (left hand screen, under System)
4. View Definitions (Right next to the View:)
5. Filter by 'sas.commons.web.security', select it
6. Search for 'SAS Logon Manager', edit it
7. Go to 'content-security-policy', delete the 'form-action' component entirely. 
8. Save the configuration

IMPORTANT: This approach does not follow security best practices. While it is feasible for local development and testing, for production scenarios, we strongly recommend hosting the MCP Server with proper TLS termination and adding its domain to the form-action directive as an allowed domain.

#### Step 2. Register an OAuth Client for the MCP Server
Since Viya does not support Dynamic Client Registration (DCR) pattern. It is required to register the OAuth client ahead of time. The [MCP Authorization spec](https://modelcontextprotocol.io/specification/draft/basic/authorization) states that this must be Authorization Code Flow with PKCE.

Following best practies defined in this [SAS blog post](https://blogs.sas.com/content/sgf/2023/02/07/authentication-to-sas-viya)

If you are not comfortable with curl and the command line. Feel free to use any API client.

1\. Retrieve a Viya access token (user is assumed to be a SAS Administrator)
```sh
export BEARER_TOKEN=`curl -sk -X POST \
    "https://YOUR_VIYA_ENDPOINT/SASLogon/oauth/token" \
    -u "sas.cli:" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d 'grant_type=password&username=user&password=password' | awk -F: '{print $2}'|awk -F\" '{print $2}'`
```
Replace the endpoint, username and password with your own values.

2\. Register the OAuth Client
```sh
curl -k -X POST "https://YOUR_VIYA_ENDPOINT/SASLogon/oauth/clients" \
   -H "Content-Type: application/json" \
   -H "Authorization: Bearer $BEARER_TOKEN" \
   -d '{"client_id": "sas-mcp",
      "scope": ["openid"],
      "authorized_grant_types": ["authorization_code","refresh_token"],
      "redirect_uri": "http://localhost:8134/auth/callback", "autoapprove":true, "allowpublic":true}'
```
Replace the endpoint with your own value.
Note the client_id and the redirect_uri -- these are important for the environment file

**Alternative: Python script**

If you prefer, you can use the provided registration script instead of curl. It reads your `.env` file for the endpoint, client ID, and port, and handles self-signed certificates automatically.

```sh
uv run python examples/register_mcp_client.py
```

The script will prompt for your Viya admin credentials, delete any existing client with the same ID, register a new one, and verify the registration.

> **Tip:** the registration step uses the password grant, so authenticate it with an account SAS Logon can verify directly (e.g. the local `sasboot` admin). An SSO/Okta account cannot be used here. If `sasboot` lacks the authority to register clients, use the **Consul token** from your environment's console instead.

Congratulations! Your Viya is now configured and ready to connect with the MCP server.

---

### Headless authentication for SSO and federated environments

> Applies when users sign in through an external identity provider such as **Okta**, Microsoft Entra ID, or PingFederate.


The stdio and direct-HTTP servers log into Viya themselves. By default they use the OAuth2 **password grant** (`VIYA_USERNAME` / `VIYA_PASSWORD`). That grant only works for identities SAS Logon authenticates **directly** — the local `sasboot` account or LDAP users.

If your Viya users sign in through an **external identity provider (Okta, Entra ID, PingFederate, …)**, the password grant **cannot** authenticate them: SAS Logon never sees the password — it redirects the browser to the provider. This is why `sasboot` works but a federated admin account (e.g. `you@company.com`) returns `401` from `/SASLogon/oauth/token`.

The supported headless path is the **refresh-token grant**: log in interactively **once** (the browser goes through your provider), capture a long-lived refresh token, and let the server exchange it for access tokens indefinitely — no browser and no stored password afterward, with the **full identity and privileges** of the user who logged in. This is ideal for unattended 24/7 deployments such as SAS Retrieval Agent Manager.

**Step A — register the client** (Step 2 above). The registration already enables the `authorization_code` and `refresh_token` grants. `register_mcp_client.py` also sets a long `refresh-token-validity` (1 year by default; override with `REFRESH_TOKEN_VALIDITY`) so you rarely repeat the login.

**Step B — obtain a refresh token (one time, in a browser on your machine):**
```sh
uv run python examples/get_refresh_token.py
```
This opens your browser to SAS Logon, you authenticate through your identity provider, and the script prints a `VIYA_REFRESH_TOKEN=...` line.

> If the browser shows the login page but the redirect back to `localhost` never completes, disable the `form-action` CSP directive on SAS Logon Manager (Step 1). You only need it disabled for this one-time step — the refresh-token grant itself performs no browser redirect, so you can re-enable it afterward.

**Step C — run the server headless.** Put the token in the environment where the server runs (mark it **secret**):
```
VIYA_ENDPOINT=https://your-viya-server.com
VIYA_REFRESH_TOKEN=<the token from Step B>
```
The server uses `grant_type=refresh_token` automatically and refreshes access tokens on its own. Leave `VIYA_USERNAME` / `VIYA_PASSWORD` empty — when `VIYA_REFRESH_TOKEN` is set it takes precedence.

**Notes**
- **Validity / renewal:** you only repeat Step B when the refresh token expires (governed by the client's `refresh-token-validity`, capped by any global SAS Logon maximum).
- **Rotation:** if your SAS Logon rotates refresh tokens, the server tracks the rotated value in memory. To stay restart-safe, keep rotation disabled (the SAS Logon default) so the token you stored always works; otherwise update the stored value after a rotation.
- **Confidential clients:** if you registered the client with a secret, also set `CLIENT_SECRET`. For the default public/PKCE client, leave it empty.

---

### Environment file options
The .env file used by the MCP Server allows for customizable options that the user can set themselves.
| Variable            | Required | Default       | Description                                                 |
|---------------------|---------|--------------|---------------------------------------------------------------|
| `VIYA_ENDPOINT`     | Yes     | —            | Viya instance to use                                          |
| `CLIENT_ID`         | No      | `sas-mcp`    | OAuth2 Client ID registered in Viya                           |
| `CLIENT_SECRET`     | No      | —            | OAuth2 client secret — only for a confidential client; leave empty for public/PKCE |
| `VIYA_REFRESH_TOKEN`| Headless (SSO) | —     | Refresh token for stdio / direct-HTTP mode; required for SSO/federated users, preferred for 24/7 use. Obtain via `examples/get_refresh_token.py` |
| `HOST_PORT`         | No      |  `8134`      | Host Port the local MCP Server listens on                    |
| `MCP_SIGNING_KEY`   | No      | `default`    | Secret key used to sign [FastMCP Proxy JWTs](https://gofastmcp.com/servers/auth/oauth-proxy#param-jwt-signing-key)                                                           |
| `MCP_BASE_URL`         | No   | `http://localhost:{HOST_PORT}`             | External URL of the MCP server (set for k8s/reverse proxy deployments) |
| `COMPUTE_CONTEXT_NAME` | No   | `SAS Job Execution compute context`       | Viya compute context to use for code execution                |
| `SSL_VERIFY`        | No      | `true`       | Set to `false` to disable SSL certificate verification (e.g. for self-signed Viya certificates)  |
| `VIYA_USERNAME`     | Stdio only | —         | Viya username for stdio mode (password grant authentication)  |
| `VIYA_PASSWORD`     | Stdio only | —         | Viya password for stdio mode (password grant authentication)  |

The defaults listed here are the variable values used in the Viya setup step. If your SAS Administrator has used a different `CLIENT_ID`, `HOST_PORT` during the OAuth Client registration. Please use those values instead.

---

### SSL Certificate Configuration

If your Viya instance uses custom or internal CA certificates, Python needs to know where to find them. Rather than disabling verification entirely with `SSL_VERIFY=false`, you can point Python to your Viya certificate chain.

**Linux / macOS:**
```sh
export REQUESTS_CA_BUNDLE="/path/to/sas-viya-ca-certificate.pem"
export SSL_CERT_FILE="/path/to/sas-viya-ca-certificate.pem"
```

**Windows (PowerShell):**
```powershell
$env:REQUESTS_CA_BUNDLE = "C:\path\to\sas-viya-ca-certificate.pem"
$env:SSL_CERT_FILE = "C:\path\to\sas-viya-ca-certificate.pem"
```

Set these environment variables before starting the MCP server. The `.pem` file should contain the full certificate chain for your Viya instance (including any intermediate CA certificates).

To obtain the certificate, ask your SAS Administrator or extract it from the Viya ingress:
```sh
openssl s_client -connect your-viya-server.com:443 -showcerts </dev/null 2>/dev/null \
  | openssl x509 -outform PEM > sas-viya-ca-certificate.pem
```

You can also add these variables to your `.env` file so they are loaded automatically:
```
REQUESTS_CA_BUNDLE=/path/to/sas-viya-ca-certificate.pem
SSL_CERT_FILE=/path/to/sas-viya-ca-certificate.pem
```

> **Note:** `SSL_VERIFY=false` should only be used for local development and testing. For production, always configure the proper certificate chain.

---

### Kubernetes Deployment

When deploying the MCP server in Kubernetes for multi-user access, each user authenticates independently via the OAuth2 PKCE flow using their own Viya credentials. No shared service account is needed.

#### Key configuration

Set these environment variables on the container (via ConfigMap, Secret, or Helm values):

| Variable | Value | Why |
|----------|-------|-----|
| `VIYA_ENDPOINT` | `https://your-viya-server.com` | The Viya instance to connect to |
| `MCP_BASE_URL` | `https://sas-mcp.company.com` | The external URL users reach the MCP server at (must match the OAuth redirect URI registered in Viya) |
| `MCP_SIGNING_KEY` | A strong random string (24+ chars) | Signs proxy JWTs — use a Kubernetes Secret |
| `SSL_CERT_FILE` | `/etc/ssl/certs/viya-ca.pem` | Path to Viya CA certificate (mount via Secret or ConfigMap) |

#### OAuth client registration

The OAuth redirect URI registered in Viya (Step 2 above) must match your ingress URL. For example, if `MCP_BASE_URL=https://sas-mcp.company.com`, register:

```sh
curl -k -X POST "https://YOUR_VIYA_ENDPOINT/SASLogon/oauth/clients" \
   -H "Content-Type: application/json" \
   -H "Authorization: Bearer $BEARER_TOKEN" \
   -d '{"client_id": "sas-mcp",
      "scope": ["openid"],
      "authorized_grant_types": ["authorization_code","refresh_token"],
      "redirect_uri": "https://sas-mcp.company.com/auth/callback",
      "autoapprove":true, "allowpublic":true}'
```

#### Ingress

Expose the server via an Ingress with TLS termination:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: sas-mcp-server
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "300"
spec:
  tls:
    - hosts:
        - sas-mcp.company.com
      secretName: sas-mcp-tls
  rules:
    - host: sas-mcp.company.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: sas-mcp-server
                port:
                  number: 8134
```

#### MCP client configuration

Each user points their MCP client at the ingress URL:
```json
{
    "servers": {
        "sas-execution-mcp": {
            "url": "https://sas-mcp.company.com/mcp",
            "type": "http"
        }
    }
}
```

When a user first invokes a tool, their browser opens for Viya login. After authentication, their session is tied to their own Viya identity and permissions.

---

### Gemini CLI

Gemini CLI connects to MCP servers via stdio only — it does not support HTTP mode. Because it cannot participate in browser-based OAuth redirects, stdio mode with password grant credentials is required.

#### Configuration

Add to `~/.gemini/settings.json` or your project's `.gemini/settings.json`:
```json
{
    "mcpServers": {
        "sas-viya-mcp": {
            "command": "uv",
            "args": ["run", "app-stdio"],
            "cwd": "/path/to/sas-mcp-server",
            "timeout": 60000
        }
    }
}
```

Set `cwd` to the absolute path where `sas-mcp-server` is cloned.

A pre-built example is available at [`examples/gemini-settings.json`](gemini-settings.json).

#### Timeout

The `timeout` field (in milliseconds) controls how long Gemini CLI waits for a tool call to complete. The default is 10 seconds, which is too short for most SAS Viya API calls. **Set this to at least `60000` (60 seconds).**

Without this setting, tool calls will appear to fail with a timeout error even though the server and authentication are working correctly.

#### Required `.env` variables

Stdio mode authenticates via password grant, so you must set these in your `.env` file:
```
VIYA_ENDPOINT=https://your-viya-server.com
VIYA_USERNAME=your-username
VIYA_PASSWORD=your-password
SSL_VERIFY=false  # if using self-signed certificates
```

The `CLIENT_ID` can remain at the default (`sas-mcp`) or be changed to match an existing OAuth client registered on your Viya instance (e.g., `sas.cli`).

---

### Further MCP setup options
For examples on how to run with docker, refer to the **docker** folder.