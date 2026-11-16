"""Environment-based configuration for the Salesforce MCP bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
DEFAULT_MCP_SERVER = "platform/sobject-all"
DEFAULT_TOKEN_PATH = Path.home() / ".salesforce-mcp-bridge" / "tokens.json"


@dataclass(frozen=True)
class Settings:
    client_id: str
    login_host: str
    mcp_url: str
    redirect_uri: str
    token_path: Path
    sandbox: bool
    mcp_server: str

    @property
    def authorize_url(self) -> str:
        return f"{self.login_host.rstrip('/')}/services/oauth2/authorize"

    @property
    def token_url(self) -> str:
        return f"{self.login_host.rstrip('/')}/services/oauth2/token"


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _expand_path(raw: str) -> Path:
    return Path(os.path.expanduser(raw)).resolve()


def build_mcp_url(*, sandbox: bool, mcp_server: str) -> str:
    server = mcp_server.strip().strip("/")
    if sandbox:
        return f"https://api.salesforce.com/platform/mcp/v1/sandbox/{server}"
    return f"https://api.salesforce.com/platform/mcp/v1/{server}"


def load_settings(*, env_file: str | Path | None = None) -> Settings:
    """Load settings from the environment (and optional .env)."""
    # Prefer .env over a stale shell variable (e.g. a leftover test client id).
    if env_file is not None:
        load_dotenv(env_file, override=True)
    else:
        load_dotenv(override=True)

    client_id = (os.getenv("SF_CLIENT_ID") or "").strip()
    if not client_id:
        raise ValueError(
            "SF_CLIENT_ID is required. Set it to your External Client App consumer key."
        )

    login_host = (os.getenv("SF_LOGIN_HOST") or "https://login.salesforce.com").strip()
    if not login_host.startswith("http"):
        login_host = f"https://{login_host}"

    sandbox = _truthy(os.getenv("SF_MCP_SANDBOX"))
    mcp_server = (os.getenv("SF_MCP_SERVER") or DEFAULT_MCP_SERVER).strip()
    mcp_url = (os.getenv("SF_MCP_URL") or "").strip() or build_mcp_url(
        sandbox=sandbox, mcp_server=mcp_server
    )

    redirect_uri = (os.getenv("SF_REDIRECT_URI") or DEFAULT_REDIRECT_URI).strip()
    token_raw = (os.getenv("SF_TOKEN_PATH") or str(DEFAULT_TOKEN_PATH)).strip()
    token_path = _expand_path(token_raw)

    return Settings(
        client_id=client_id,
        login_host=login_host.rstrip("/"),
        mcp_url=mcp_url,
        redirect_uri=redirect_uri,
        token_path=token_path,
        sandbox=sandbox,
        mcp_server=mcp_server,
    )
