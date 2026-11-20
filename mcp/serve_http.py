#!/usr/bin/env python3
"""Serve the opencode MCP bridge over streamable-http, behind the same password as the web UI.

Auth: `Authorization: Bearer <OPENCODE_SERVER_PASSWORD>` or HTTP Basic with the same secret.
If OPENCODE_SERVER_PASSWORD is empty the endpoint is open (documented, not recommended).
"""
from __future__ import annotations

import base64
import hmac
import os

import uvicorn
from fastmcp import FastMCP  # noqa: F401  (imported for the type check below)

from opencode_mcp_bridge.config import BridgeConfig
from opencode_mcp_bridge.server import create_server

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8123"))
PATH = os.environ.get("MCP_PATH", "/mcp")
PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "")


def _authorized(headers: dict[bytes, bytes]) -> bool:
    raw = headers.get(b"authorization", b"").decode("latin-1").strip()
    if not raw:
        return False
    scheme, _, value = raw.partition(" ")
    value = value.strip()
    if scheme.lower() == "bearer":
        return hmac.compare_digest(value, PASSWORD)
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(value).decode("utf-8")
        except Exception:
            return False
        user, _, supplied = decoded.partition(":")
        return user == "opencode" and hmac.compare_digest(supplied, PASSWORD)
    return False


class RequirePassword:
    """ASGI wrapper: 401 unless the caller presents the shared password."""

    def __init__(self, app, password: str):
        self.app = app
        self.password = password

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.password:
            headers = dict(scope.get("headers") or [])
            if not _authorized(headers):
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"text/plain"),
                                (b"www-authenticate", b'Bearer realm="opencode-mcp"')],
                })
                await send({"type": "http.response.body", "body": b"unauthorized\n"})
                return
        await self.app(scope, receive, send)


def build_app():
    server = create_server(BridgeConfig.from_env())
    app = server.http_app(path=PATH)
    return RequirePassword(app, PASSWORD)


if __name__ == "__main__":
    uvicorn.run(build_app(), host=HOST, port=PORT, log_level=os.environ.get("MCP_LOG_LEVEL", "info"))
