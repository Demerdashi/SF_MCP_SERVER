"""Salesforce OAuth 2.0 Authorization Code + PKCE for Hosted MCP."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from salesforce_mcp_bridge.config import Settings

logger = logging.getLogger(__name__)

OAUTH_SCOPES = "mcp_api refresh_token"


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    instance_url: str | None
    issued_at: float | None
    expires_in: int | None
    token_type: str | None = "Bearer"
    id_url: str | None = None

    def is_access_token_fresh(self, skew_seconds: int = 60) -> bool:
        if not self.access_token:
            return False
        if self.issued_at is None or self.expires_in is None:
            # JWT lifetime is unknown; treat as usable until a 401 forces refresh.
            return True
        return (time.time() - self.issued_at) < (self.expires_in - skew_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "instance_url": self.instance_url,
            "issued_at": self.issued_at,
            "expires_in": self.expires_in,
            "token_type": self.token_type,
            "id": self.id_url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenSet:
        return cls(
            access_token=str(data.get("access_token") or ""),
            refresh_token=data.get("refresh_token"),
            instance_url=data.get("instance_url"),
            issued_at=_as_float(data.get("issued_at")),
            expires_in=_as_int(data.get("expires_in")),
            token_type=data.get("token_type") or "Bearer",
            id_url=data.get("id"),
        )


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def load_tokens(path: Path) -> TokenSet | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read token file %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or not data.get("access_token"):
        return None
    return TokenSet.from_dict(data)


def save_tokens(path: Path, tokens: TokenSet) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens.to_dict(), indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def tokens_from_oauth_response(payload: dict[str, Any]) -> TokenSet:
    issued_at = _as_float(payload.get("issued_at"))
    if issued_at is not None and issued_at > 1e12:
        # Salesforce sometimes returns epoch milliseconds.
        issued_at = issued_at / 1000.0
    if issued_at is None:
        issued_at = time.time()

    expires_in = _as_int(payload.get("expires_in"))
    if expires_in is None:
        # JWT access tokens often omit expires_in; assume 2 hours.
        expires_in = 7200

    return TokenSet(
        access_token=str(payload["access_token"]),
        refresh_token=payload.get("refresh_token"),
        instance_url=payload.get("instance_url"),
        issued_at=issued_at,
        expires_in=expires_in,
        token_type=payload.get("token_type") or "Bearer",
        id_url=payload.get("id"),
    )


class _CallbackResult:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.code: str | None = None
        self.state: str | None = None
        self.error: str | None = None
        self.error_description: str | None = None


def _make_handler(expected_state: str, result: _CallbackResult, path: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            logger.debug("OAuth callback: " + format, *args)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.rstrip("/") != path.rstrip("/"):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
                return

            params = parse_qs(parsed.query)
            if params.get("error"):
                result.error = params["error"][0]
                result.error_description = (params.get("error_description") or [None])[0]
                detail = result.error_description or result.error or ""
                # Keep message visible in the browser for local debugging.
                safe = (
                    detail.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                body = (
                    b"<html><body><h1>Authorization failed</h1>"
                    b"<p>"
                    + safe.encode("utf-8")
                    + b"</p><p>You can close this window.</p></body></html>"
                )
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                result.event.set()
                return

            code = (params.get("code") or [None])[0]
            state = (params.get("state") or [None])[0]
            if not code or state != expected_state:
                result.error = "invalid_callback"
                result.error_description = "Missing code or state mismatch"
                body = b"<html><body><h1>Invalid callback</h1></body></html>"
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                result.event.set()
                return

            result.code = code
            result.state = state
            body = (
                b"<html><body><h1>Salesforce authorization complete</h1>"
                b"<p>You can close this window and return to Cursor.</p></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            result.event.set()

    return Handler


def _run_browser_login(settings: Settings, timeout_seconds: float = 600.0) -> TokenSet:
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)

    redirect = urlparse(settings.redirect_uri)
    host = redirect.hostname or "localhost"
    port = redirect.port or (443 if redirect.scheme == "https" else 80)
    callback_path = redirect.path or "/callback"

    result = _CallbackResult()
    handler = _make_handler(state, result, callback_path)
    server = HTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": settings.client_id,
            "redirect_uri": settings.redirect_uri,
            "scope": OAUTH_SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    auth_url = f"{settings.authorize_url}?{query}"
    logger.info("Opening browser for Salesforce login...")
    print(f"Open this URL if the browser does not open:\n{auth_url}\n", flush=True)
    webbrowser.open(auth_url)

    try:
        if not result.event.wait(timeout=timeout_seconds):
            raise TimeoutError(
                f"Timed out waiting for OAuth callback on {settings.redirect_uri}"
            )
    finally:
        server.shutdown()
        server.server_close()

    if result.error:
        detail = result.error_description or result.error
        hint = ""
        lowered = (detail or "").lower()
        if "cross-org" in lowered or "oauth_authorization_blocked" in (result.error or "").lower():
            hint = (
                " Sign in to the same org where the External Client App was created "
                "(your Developer Edition), not another trial/orgfarm org. "
                "Set SF_LOGIN_HOST to that org's My Domain URL, e.g. "
                "https://YOURDOMAIN.my.salesforce.com"
            )
        raise RuntimeError(f"Salesforce OAuth error: {detail}.{hint}")
    if not result.code:
        raise RuntimeError("Salesforce OAuth callback did not include an authorization code")

    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            settings.token_url,
            data={
                "grant_type": "authorization_code",
                "code": result.code,
                "client_id": settings.client_id,
                "redirect_uri": settings.redirect_uri,
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Token exchange failed ({response.status_code}): {response.text}"
            )
        payload = response.json()

    tokens = tokens_from_oauth_response(payload)
    save_tokens(settings.token_path, tokens)
    return tokens


def refresh_access_token(settings: Settings, refresh_token: str) -> TokenSet:
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            settings.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": settings.client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Refresh token failed ({response.status_code}): {response.text}"
            )
        payload = response.json()

    # Refresh responses may omit refresh_token; keep the existing one.
    if not payload.get("refresh_token"):
        payload["refresh_token"] = refresh_token

    existing = load_tokens(settings.token_path)
    if existing and not payload.get("instance_url") and existing.instance_url:
        payload["instance_url"] = existing.instance_url

    tokens = tokens_from_oauth_response(payload)
    save_tokens(settings.token_path, tokens)
    return tokens


class AuthManager:
    """Loads, refreshes, and interactively obtains Salesforce tokens."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()
        self._tokens = load_tokens(settings.token_path)

    @property
    def tokens(self) -> TokenSet | None:
        return self._tokens

    def status(self) -> dict[str, Any]:
        tokens = self._tokens
        return {
            "authenticated": bool(tokens and tokens.access_token),
            "has_refresh_token": bool(tokens and tokens.refresh_token),
            "access_token_fresh": bool(tokens and tokens.is_access_token_fresh()),
            "instance_url": tokens.instance_url if tokens else None,
            "token_path": str(self.settings.token_path),
            "mcp_url": self.settings.mcp_url,
            "login_host": self.settings.login_host,
        }

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._get_access_token_sync, force_refresh)

    def _get_access_token_sync(self, force_refresh: bool) -> str:
        tokens = self._tokens or load_tokens(self.settings.token_path)
        self._tokens = tokens

        if tokens and tokens.access_token and not force_refresh and tokens.is_access_token_fresh():
            return tokens.access_token

        if tokens and tokens.refresh_token:
            try:
                tokens = refresh_access_token(self.settings, tokens.refresh_token)
                self._tokens = tokens
                return tokens.access_token
            except Exception as exc:  # noqa: BLE001
                logger.warning("Refresh failed, falling back to browser login: %s", exc)

        tokens = _run_browser_login(self.settings)
        self._tokens = tokens
        return tokens.access_token

    async def login_interactive(self) -> TokenSet:
        async with self._lock:
            tokens = await asyncio.to_thread(_run_browser_login, self.settings)
            self._tokens = tokens
            return tokens
