"""Stdio MCP server that proxies tools to Salesforce Hosted MCP."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import NotificationOptions, Server

from salesforce_mcp_bridge import __version__
from salesforce_mcp_bridge.auth import AuthManager
from salesforce_mcp_bridge.config import Settings, load_settings
from salesforce_mcp_bridge.sf_client import SalesforceMcpClient

logger = logging.getLogger(__name__)

LOCAL_AUTH_STATUS = "sf_auth_status"
LOCAL_RELOGIN = "sf_relogin"

EMPTY_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def _configure_logging() -> None:
    # Stdio MCP uses stdout for protocol; keep logs on stderr.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _local_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=LOCAL_AUTH_STATUS,
            description=(
                "Show Salesforce MCP bridge auth status (token presence, MCP URL, "
                "instance URL). Does not expose secrets."
            ),
            inputSchema=EMPTY_OBJECT_SCHEMA,
        ),
        types.Tool(
            name=LOCAL_RELOGIN,
            description=(
                "Force a browser Salesforce OAuth login for this bridge and replace "
                "stored tokens."
            ),
            inputSchema=EMPTY_OBJECT_SCHEMA,
        ),
    ]


def create_server(settings: Settings) -> Server[dict[str, Any]]:
    auth = AuthManager(settings)
    upstream = SalesforceMcpClient(settings, auth)

    @asynccontextmanager
    async def lifespan(_server: Server[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {"auth": auth, "upstream": upstream, "settings": settings}
        finally:
            await upstream.close()

    async def on_list_tools(
        ctx: ServerRequestContext[dict[str, Any]],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        up: SalesforceMcpClient = ctx.lifespan_context["upstream"]
        tools = _local_tools()
        try:
            tools.extend(await up.list_tools())
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to list upstream Salesforce tools")
            tools.append(
                types.Tool(
                    name="sf_upstream_error",
                    description=f"Upstream list_tools failed: {exc}",
                    inputSchema=EMPTY_OBJECT_SCHEMA,
                )
            )
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(
        ctx: ServerRequestContext[dict[str, Any]],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        auth_mgr: AuthManager = ctx.lifespan_context["auth"]
        up: SalesforceMcpClient = ctx.lifespan_context["upstream"]
        cfg: Settings = ctx.lifespan_context["settings"]
        name = params.name
        args = params.arguments or {}

        if name == LOCAL_AUTH_STATUS:
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=json.dumps(auth_mgr.status(), indent=2))
                ]
            )

        if name == LOCAL_RELOGIN:
            tokens = await auth_mgr.login_interactive()
            await up.close()
            payload = {
                "ok": True,
                "instance_url": tokens.instance_url,
                "token_path": str(cfg.token_path),
            }
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload, indent=2))]
            )

        if name == "sf_upstream_error":
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=(
                            "Upstream Salesforce MCP is unavailable. Check SF_CLIENT_ID, "
                            "run sf_relogin, and confirm Hosted MCP is activated in the org."
                        ),
                    )
                ],
                isError=True,
            )

        return await up.call_tool(name, dict(args) if args else {})

    return Server(
        "salesforce-mcp-bridge",
        version=__version__,
        instructions=(
            "Bridge to Salesforce Hosted MCP. Use sf_auth_status / sf_relogin for auth; "
            "other tools are proxied from Salesforce."
        ),
        lifespan=lifespan,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def run_stdio(settings: Settings | None = None) -> None:
    _configure_logging()
    cfg = settings or load_settings()
    server = create_server(cfg)

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        )


def main() -> None:
    import asyncio

    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
