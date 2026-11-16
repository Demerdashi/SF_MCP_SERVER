"""Upstream MCP client for Salesforce Hosted MCP (Streamable HTTP)."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any

import httpx2
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from salesforce_mcp_bridge.auth import AuthManager
from salesforce_mcp_bridge.config import Settings

logger = logging.getLogger(__name__)


class SalesforceMcpClient:
    """Lazy-connected client that proxies tool calls to Salesforce Hosted MCP."""

    def __init__(self, settings: Settings, auth: AuthManager) -> None:
        self.settings = settings
        self.auth = auth
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._http: httpx2.AsyncClient | None = None

    @property
    def connected(self) -> bool:
        return self._session is not None

    async def close(self) -> None:
        stack = self._stack
        self._stack = None
        self._session = None
        self._http = None
        if stack is not None:
            await stack.aclose()

    async def ensure_session(self, *, force_reauth: bool = False) -> ClientSession:
        if self._session is not None and not force_reauth:
            return self._session
        await self.close()
        return await self._connect(force_refresh=force_reauth)

    async def _connect(self, *, force_refresh: bool = False) -> ClientSession:
        access_token = await self.auth.get_access_token(force_refresh=force_refresh)
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            http = httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=httpx2.Timeout(30.0, read=300.0),
            )
            await stack.enter_async_context(http)

            streams = await stack.enter_async_context(
                streamable_http_client(self.settings.mcp_url, http_client=http)
            )
            read_stream, write_stream = streams[0], streams[1]

            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()

            self._stack = stack
            self._http = http
            self._session = session
            logger.info("Connected to Salesforce MCP at %s", self.settings.mcp_url)
            return session
        except Exception:
            await stack.aclose()
            raise

    async def list_tools(self) -> list[types.Tool]:
        try:
            session = await self.ensure_session()
            result = await session.list_tools()
            return list(result.tools)
        except Exception as exc:
            if _is_auth_error(exc):
                logger.info("Upstream auth failed on list_tools; refreshing and retrying")
                session = await self.ensure_session(force_reauth=True)
                result = await session.list_tools()
                return list(result.tools)
            raise

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        args = arguments or {}
        try:
            session = await self.ensure_session()
            result = await session.call_tool(name, args)
        except Exception as exc:
            if _is_auth_error(exc):
                logger.info("Upstream auth failed on call_tool; refreshing and retrying")
                session = await self.ensure_session(force_reauth=True)
                result = await session.call_tool(name, args)
            else:
                raise

        if isinstance(result, types.CallToolResult):
            return result
        # Defensive: newer protocol variants may return other result shapes.
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(result))],
            isError=True,
        )


def _is_auth_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "401",
        "unauthorized",
        "invalid_auth",
        "invalid_jwt",
        "authentication",
        "expired",
        "www-authenticate",
    )
    return any(m in text for m in markers)
