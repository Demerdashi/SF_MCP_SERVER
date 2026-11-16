# Salesforce Hosted MCP Bridge

Standalone **Python stdio MCP server** for [Cursor](https://cursor.com). Cursor talks to this bridge; the bridge authenticates to **Salesforce Hosted MCP** (OAuth 2.0 + PKCE) and **proxies** tools.

```text
Cursor  --stdio-->  salesforce-mcp-bridge  --HTTPS+Bearer-->  Salesforce Hosted MCP
```

## Prerequisites

- Python 3.11+
- A Salesforce org with **Hosted MCP Servers** available (Enterprise+ or Developer Edition)
- An **External Client App** (Connected Apps are not supported for Hosted MCP)

## Salesforce org setup

1. **Activate a Hosted MCP server**  
   Setup → Quick Find → **MCP Servers** (under API Catalog) → activate at least `sobject-all` (and others if you need them).

2. **Create an External Client App**  
   Setup → **External Client App Manager** → New External Client App:
   - Enable OAuth
   - **Callback URL**: `http://localhost:8765/callback` (must match `SF_REDIRECT_URI`)
   - **Scopes**: `mcp_api`, `refresh_token`
   - **Security**: enable **Issue JSON Web Token (JWT)-based access tokens for named users**; enable PKCE / do not require a client secret for this desktop bridge
   - Create, then copy the **Consumer Key**

3. Wait up to ~30 minutes after creating the External Client App if auth fails oddly.

## Install

From this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Copy env template and set your consumer key:

```powershell
copy .env.example .env
# edit .env → SF_CLIENT_ID=...
```

### Optional: one-shot browser login

```powershell
python scripts/auth_login.py
# or, after install:
sf-mcp-login
```

Tokens are stored at `%USERPROFILE%\.salesforce-mcp-bridge\tokens.json` by default (`SF_TOKEN_PATH`).

## Cursor MCP config

This machine’s Cursor user MCP config is at `%USERPROFILE%\.cursor\mcp.json` and already includes `salesforce-bridge` (see [cursor-mcp.example.json](cursor-mcp.example.json) for a copyable template).

Prefer the venv Python so dependencies resolve:

```json
{
  "mcpServers": {
    "salesforce-bridge": {
      "command": "D:/MCP/SalesForce/.venv/Scripts/python.exe",
      "args": ["-m", "salesforce_mcp_bridge"],
      "cwd": "D:/MCP/SalesForce",
      "env": {
        "SF_CLIENT_ID": "<consumer-key>",
        "SF_LOGIN_HOST": "https://YOURDOMAIN.my.salesforce.com",
        "SF_MCP_URL": "https://api.salesforce.com/platform/mcp/v1/platform/sobject-all",
        "SF_REDIRECT_URI": "http://localhost:8765/callback",
        "SF_MCP_SANDBOX": "false"
      }
    }
  }
}
```

After editing `mcp.json`, reload MCP servers or restart Cursor. Then call `sf_auth_status` to confirm.

Sandbox / scratch org example:

```json
{
  "SF_LOGIN_HOST": "https://test.salesforce.com",
  "SF_MCP_SANDBOX": "true",
  "SF_MCP_URL": "https://api.salesforce.com/platform/mcp/v1/sandbox/platform/sobject-all"
}
```

Use the org’s **My Domain** for `SF_LOGIN_HOST` so OAuth cannot land in the wrong org.

### With uv (optional)

If you use [uv](https://github.com/astral-sh/uv):

```json
{
  "mcpServers": {
    "salesforce-bridge": {
      "command": "uv",
      "args": ["run", "--directory", "D:/MCP/SalesForce", "python", "-m", "salesforce_mcp_bridge"],
      "env": {
        "SF_CLIENT_ID": "<consumer-key>",
        "SF_LOGIN_HOST": "https://login.salesforce.com",
        "SF_MCP_URL": "https://api.salesforce.com/platform/mcp/v1/platform/sobject-all"
      }
    }
  }
}
```

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SF_CLIENT_ID` | yes | External Client App consumer key |
| `SF_LOGIN_HOST` | no | Default `https://login.salesforce.com` |
| `SF_MCP_URL` | no | Full Hosted MCP URL; if unset, built from sandbox + server name |
| `SF_MCP_SANDBOX` | no | `true` inserts `/sandbox/` in the default URL |
| `SF_MCP_SERVER` | no | Default `platform/sobject-all` |
| `SF_REDIRECT_URI` | no | Default `http://localhost:8765/callback` |
| `SF_TOKEN_PATH` | no | Default `~/.salesforce-mcp-bridge/tokens.json` |

## Local bridge tools

In addition to proxied Salesforce tools, the bridge exposes:

- `sf_auth_status` — whether tokens exist, freshness, MCP URL, instance URL (no secrets)
- `sf_relogin` — force browser OAuth and replace stored tokens

## Smoke test

1. Install and set `SF_CLIENT_ID`.
2. Run `sf-mcp-login` (or enable the server in Cursor and call `sf_relogin`).
3. In Cursor, confirm Salesforce tools appear under the `salesforce-bridge` server.
4. Call a read tool from `sobject-all` against a known object.
5. Restart Cursor later and confirm tools still work via refresh token (no new browser trip).

## Notes

- All Hosted MCP calls run as the **signed-in Salesforce user** (CRUD/FLS/sharing apply).
- Hosted MCP usage may consume Flex Credits outside free Developer Edition entitlements.
- This bridge does not implement multi-org routing or DX/metadata tools.

## License

Use and modify as needed for your org.
