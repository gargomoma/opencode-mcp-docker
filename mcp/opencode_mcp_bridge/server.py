"""Assemble the FastMCP server with all opencode tools."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastmcp import FastMCP

from . import __version__
from .client import OpencodeClient
from .config import BridgeConfig
from .servers import ServerManager
from .tools.admin import register_admin, register_files, register_guarded
from .tools.core import register_core
from .tools.delegate import register_delegate


def create_server(
    config: BridgeConfig | None = None, manager: ServerManager | None = None
) -> FastMCP:
    config = config or BridgeConfig.from_env()
    manager = manager or ServerManager(config)

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield
        finally:
            # Al apagar el MCP se cierran los httpx.Client en vuelo; antes se
            # quedaban abiertos hasta que muriera el proceso.
            await manager.aclose()

    # version=: sin esto el handshake MCP anuncia la version de la libreria fastmcp.
    mcp = FastMCP("opencode-bridge", version=__version__, lifespan=lifespan)

    def get_client() -> OpencodeClient:
        # Raw API-mirror tools always target the default server; use the
        # delegate_* tools for directory-routed multi-project work.
        return manager.default_client()

    register_delegate(mcp, manager)
    register_core(mcp, get_client, config)
    register_files(mcp, get_client)
    register_admin(mcp, get_client, config)
    register_guarded(mcp, get_client, config)
    return mcp
