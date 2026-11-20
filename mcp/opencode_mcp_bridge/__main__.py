"""Stdio entrypoint: `python -m opencode_mcp_bridge` or `opencode-mcp-bridge`."""

from __future__ import annotations

import argparse
import sys

from .config import BridgeConfig
from .server import create_server


def build_config(argv: list[str] | None = None) -> BridgeConfig:
    parser = argparse.ArgumentParser(
        description="opencode-mcp-bridge (MCP stdio server for opencode serve)"
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="opencode serve URL (default $OPENCODE_BASE_URL or http://127.0.0.1:4096)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="HTTP timeout seconds, 0 = no timeout (default $OPENCODE_TIMEOUT or 60)",
    )
    parser.add_argument(
        "--enable-tui-tools",
        action="store_true",
        default=None,
        help="Register opt-in tui_* tools",
    )
    parser.add_argument(
        "--enable-destructive",
        action="store_true",
        default=None,
        help="Register the destructive/admin tools (instance_dispose, run_shell, "
        "session_delete, config_update, auth_set, mcp_add)",
    )
    args = parser.parse_args(argv)
    overrides: dict = {}
    if args.base_url:
        overrides["base_url"] = args.base_url
    if args.timeout is not None:
        overrides["timeout"] = args.timeout
    if args.enable_tui_tools:
        overrides["enable_tui_tools"] = True
    if args.enable_destructive:
        overrides["enable_destructive"] = True
    return BridgeConfig.from_env(overrides or None)


def main(argv: list[str] | None = None) -> None:
    config = build_config(argv)
    mcp = create_server(config)
    # FastMCP run() blocks; logs must go to stderr (stdout is MCP protocol).
    mcp.run()  # stdio by default


if __name__ == "__main__":
    sys.exit(main())
