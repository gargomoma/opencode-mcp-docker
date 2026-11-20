"""Files, config/providers, agents/commands, dev-status tools."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from ..v2 import (
    V2_NOT_AVAILABLE,
    bridge_request,
    fs_location_params,
    get_version,
    unwrap,
    unwrap_list,
    v2_error,
    v2_path,
)
from ._annotations import DESTRUCT, READ, WRITE


async def _is_v2(get_client: Any) -> tuple[Any, bool]:
    """Return (client, is_v2) for the default server (detection cached per serve)."""
    client = get_client()
    return client, await get_version(client) == "v2"


def register_files(mcp: FastMCP, get_client: Any) -> None:
    @mcp.tool(annotations=READ, description="Search for text in files (pattern).")
    async def find_text(pattern: str) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2 fs resource offers name search only (find/list), no content
            # grep. Verified: live docs filesystem resource lists only
            # read/list/find (+ experimental write). No text-search route.
            raise RuntimeError(f"find_text {V2_NOT_AVAILABLE}")
        return await client.request("GET", "/find", params={"pattern": pattern})

    @mcp.tool(
        annotations=READ,
        description='Find files/dirs by fuzzy name. type is "file" or "directory", limit 1-200.',
    )
    async def find_files(
        query: str,
        type: str | None = None,
        directory: str | None = None,
        limit: int | None = None,
    ) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2: GET /api/fs/find with query (required), type, limit, location
            # (v2.0.19 spec) -> {location, data:[{path,type}]}. The v1
            # `directory` scope is called `location` on v2, so it is mapped
            # instead of being sent as an ignored extra parameter.
            if type is not None and type not in ("file", "directory"):
                raise ValueError("type must be 'file' or 'directory'")
            params: dict = {"query": query}
            if type is not None:
                params["type"] = type
            if limit is not None:
                params["limit"] = str(limit)
            if directory:
                # v2 `Location` is a deepObject query param (style: deepObject,
                # explode: true). The plain `location=<path>` form is rejected
                # by the live serve: 400 "Expected object | undefined at
                # [\"location\"]" -- it must ride as location[directory]=<path>.
                params["location[directory]"] = directory
            items, _extra = unwrap_list(
                await client.request(
                    "GET", v2_path(client, "/api/fs/find"), params=params
                )
            )
            return items
        params = {"query": query}
        if type:
            params["type"] = type
        if directory:
            params["directory"] = directory
        if limit is not None:
            params["limit"] = str(limit)
        return await get_client().request("GET", "/find/file", params=params)

    @mcp.tool(annotations=READ, description="Find workspace symbols by query.")
    async def find_symbols(query: str) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # No symbol-search route in the v2 HttpApi surface (filesystem
            # resource is read/list/find only).
            raise RuntimeError(f"find_symbols {V2_NOT_AVAILABLE}")
        return await client.request("GET", "/find/symbol", params={"query": query})

    @mcp.tool(annotations=READ, description="List files and directories at path.")
    async def file_list(path: str) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2: GET /api/fs/list -> {location, data:[{path,type}]}. Measured
            # live: an absolute path must ride as the deepObject location
            # (location[directory]=<dir>), which also returns entries relative
            # to it; `?path=<abs>` alone resolved against the serve default and
            # listed "../../opt/..." instead.
            params: dict = {}
            if path.startswith("/"):
                params["location[directory]"] = path.rstrip("/") or "/"
            else:
                params["path"] = path
            items, _extra = unwrap_list(
                await client.request(
                    "GET", v2_path(client, "/api/fs/list"), params=params
                )
            )
            return items
        return await client.request("GET", "/file", params={"path": path})

    @mcp.tool(annotations=READ, description="Read a file by path.")
    async def file_read(path: str) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2: GET /api/fs/read/*. Verified: search excerpt shows
            # GET /api/fs/read/* ("Serve one file relative to the location").
            # Unlike v1 (?path= query), v2 embeds the path in segments, so
            # each segment is percent-encoded and ".." is rejected instead
            # of escaping the location root (see check_path exemption).
            from urllib.parse import quote

            params, relative = fs_location_params(path)
            segments = relative.split("/")
            if any(seg == ".." for seg in segments):
                raise ValueError("file_read: '..' segments are not allowed")
            clean = "/".join(quote(seg, safe="") for seg in segments)
            return await client.request(
                "GET",
                v2_path(client, f"/api/fs/read/{clean}"),
                params=params or None,
            )
        return await client.request("GET", "/file/content", params={"path": path})

    @mcp.tool(annotations=READ, description="Get VCS status for tracked files.")
    async def file_status() -> Any:
        # v2: GET /api/vcs/status. Verified: live docs (vcs resource).
        return await bridge_request(
            get_client(), "GET", "/file/status", "/api/vcs/status"
        )


def register_admin(mcp: FastMCP, get_client: Any, config: Any) -> None:
    @mcp.tool(annotations=READ, description="Get opencode config.")
    async def config_get() -> Any:
        # v2: GET /api/config (config.get). Verified: live docs index.
        return await bridge_request(get_client(), "GET", "/config", "/api/config")

    @mcp.tool(annotations=READ, description="List providers and default models.")
    async def providers_list() -> Any:
        # v2: GET /api/provider (provider.list).
        # Verified: live docs (provider -> getList providers).
        return await bridge_request(
            get_client(), "GET", "/config/providers", "/api/provider"
        )

    @mcp.tool(annotations=READ, description="List all providers with connection state.")
    async def provider_list() -> Any:
        return await bridge_request(get_client(), "GET", "/provider", "/api/provider")

    @mcp.tool(
        annotations=READ,
        description="List available models (capabilities, variants, cost, context/output limits). "
        "providers_list returns providers only — use this to pick a providerID/modelID pair.",
    )
    async def models_list() -> Any:
        # v2: GET /api/model (model.list). Verified: live serve 2.0.19 and both
        # OpenAPI copies in tests/data/. La API responde {location, data: [...]};
        # bridge_request desenvuelve y devuelve la lista.
        return await bridge_request(get_client(), "GET", "/model", "/api/model")

    @mcp.tool(
        annotations=READ,
        description="Get the serve's current default model (location-scoped; the serve picks one "
        "when a session has no explicit model).",
    )
    async def model_default() -> Any:
        # v2: GET /api/model/default (model.getDefault). Verified: live serve 2.0.19.
        return await bridge_request(
            get_client(), "GET", "/model/default", "/api/model/default"
        )

    @mcp.tool(
        annotations=READ,
        description="List the serve's configured web search providers (get a providerID "
        "for websearch).",
    )
    async def websearch_provider_list() -> Any:
        # v2: GET /api/websearch/provider.
        client, is_v2 = await _is_v2(get_client)
        if not is_v2:
            raise RuntimeError("websearch_provider_list only available on opencode v2")
        items, _extra = unwrap_list(
            await client.request("GET", v2_path(client, "/api/websearch/provider"))
        )
        return items

    @mcp.tool(
        annotations=READ,
        description="Web search through the serve's configured search provider "
        "(POST /api/websearch). providerID is required despite the API schema — "
        "get one from websearch_provider_list.",
    )
    async def websearch(query: str, providerID: str) -> Any:
        # v2: POST /api/websearch with {query, providerID}. Medido: sin providerID
        # el serve responde 400 websearch_provider_required.
        client, is_v2 = await _is_v2(get_client)
        if not is_v2:
            raise RuntimeError("websearch only available on opencode v2")
        return unwrap(
            await client.request(
                "POST",
                v2_path(client, "/api/websearch"),
                json={"query": query, "providerID": providerID},
            )
        )

    @mcp.tool(
        annotations=READ,
        description="List integrations (auth providers) on the serve — ids for "
        "auth_set/oauth_authorize on v2.",
    )
    async def integration_list() -> Any:
        # v2: GET /api/integration (integration.list).
        return await bridge_request(get_client(), "GET", "/integration", "/api/integration")

    @mcp.tool(
        annotations=READ,
        description="Get one integration with its connect methods (use a methodID "
        "with oauth_authorize on v2).",
    )
    async def integration_get(id: str) -> Any:
        # v2: GET /api/integration/{integrationID} (integration.get).
        return await bridge_request(
            get_client(), "GET", f"/integration/{id}", f"/api/integration/{id}"
        )

    @mcp.tool(annotations=READ, description="Get provider authentication methods.")
    async def provider_auth() -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2 moved auth to integration connect flows; no direct equivalent.
            raise RuntimeError(f"provider_auth {V2_NOT_AVAILABLE}")
        return await client.request("GET", "/provider/auth")

    @mcp.tool(
        annotations=WRITE,
        description="Start OAuth authorization for a provider (returns URL for the human to open). "
        "On v2 `methodID` is required (get it from integration_get).",
    )
    async def oauth_authorize(id: str, methodID: str | None = None) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2: POST /api/integration/{integrationID}/connect/oauth
            # {"methodID": ...} -> {attemptID, url?}; el complete es aparte.
            if not methodID:
                raise ValueError(
                    "methodID is required on opencode v2 (integration_get(id) lists methods)"
                )
            return unwrap(
                await client.request(
                    "POST",
                    v2_path(client, f"/api/integration/{id}/connect/oauth"),
                    json={"methodID": methodID},
                )
            )
        return await client.request("POST", f"/provider/{id}/oauth/authorize")

    @mcp.tool(
        annotations=WRITE,
        description="Handle OAuth callback for a provider (code/token dict from the provider). "
        "On v2 the dict must carry `attemptID` (from oauth_authorize) and `code`.",
    )
    async def oauth_callback(id: str, callback: dict) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2: POST /api/integration/{integrationID}/connect/oauth/{attemptID}
            # /complete {"code": ...}.
            attempt = callback.get("attemptID")
            if not attempt:
                raise ValueError("callback must contain attemptID on opencode v2")
            return unwrap(
                await client.request(
                    "POST",
                    v2_path(
                        client,
                        f"/api/integration/{id}/connect/oauth/{attempt}/complete",
                    ),
                    json={"code": callback.get("code")},
                )
            )
        return await client.request(
            "POST", f"/provider/{id}/oauth/callback", json=callback
        )

    @mcp.tool(annotations=READ, description="List all available agents.")
    async def agents_list() -> Any:
        # v2: GET /api/agent (agent.list -> {location, data}).
        # Verified: live docs index.
        return await bridge_request(get_client(), "GET", "/agent", "/api/agent")

    @mcp.tool(annotations=READ, description="List all slash commands.")
    async def commands_list() -> Any:
        # v2: GET /api/command (command.list). Verified: live docs index.
        return await bridge_request(get_client(), "GET", "/command", "/api/command")

    @mcp.tool(annotations=READ, description="List all (experimental) tool IDs.")
    async def tool_ids() -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # No tool-ids route in the v2 HttpApi surface (session/form/
            # permission/fs/command/... but no tool).
            raise RuntimeError(f"tool_ids {V2_NOT_AVAILABLE}")
        return await client.request("GET", "/experimental/tool/ids")

    @mcp.tool(
        annotations=READ,
        description="List tools with JSON schemas for a provider model. On v2 there is no "
        "per-model tool schema: returns the model record from GET /api/model "
        "(its `capabilities` say whether it supports tools).",
    )
    async def tools_for_model(provider: str, model: str) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            items, _extra = unwrap_list(
                await client.request("GET", v2_path(client, "/api/model"))
            )
            for entry in items if isinstance(items, list) else []:
                if entry.get("id") == model and entry.get("providerID") == provider:
                    return entry
            raise ValueError(f"model {provider}/{model} not found in GET /api/model")
        return await client.request(
            "GET", "/experimental/tool", params={"provider": provider, "model": model}
        )

    @mcp.tool(annotations=READ, description="Get LSP server status.")
    async def lsp_status() -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            raise RuntimeError(f"lsp_status {V2_NOT_AVAILABLE}")
        return await client.request("GET", "/lsp")

    @mcp.tool(annotations=READ, description="Get formatter status.")
    async def formatter_status() -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            raise RuntimeError(f"formatter_status {V2_NOT_AVAILABLE}")
        return await client.request("GET", "/formatter")

    @mcp.tool(annotations=READ, description="Get MCP server status.")
    async def mcp_status() -> Any:
        # v2: GET /api/mcp (mcp.list). Verified: live docs index.
        return await bridge_request(get_client(), "GET", "/mcp", "/api/mcp")

    @mcp.tool(
        annotations=WRITE, description="Write a log entry to the opencode server."
    )
    async def log_write(
        service: str, level: str, message: str, extra: dict | None = None
    ) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # The v2.0.19 surface has no log route.
            raise v2_error("log_write")
        body: dict = {"service": service, "level": level, "message": message}
        if extra:
            body["extra"] = extra
        return await client.request("POST", "/log", json=body)

    if not config.enable_destructive:
        return

    # file_write vive aqui y no en register_files porque esta seccion es la que
    # tiene `config` para el gate: escribir en el fs del serve es destructivo.
    @mcp.tool(
        annotations=DESTRUCT,
        description="Write a file on the serve's filesystem (UTF-8 text; creates parent "
        "directories). Absolute paths split into location like file_read. "
        "REQUIRES OPENCODE_ENABLE_DESTRUCTIVE=1.",
    )
    async def file_write(path: str, content: str) -> Any:
        # v2: POST /api/experimental/fs/write — query {location[directory], path},
        # body = raw octet-stream (schema measured in tests/data/*_openapi.json).
        client, is_v2 = await _is_v2(get_client)
        if not is_v2:
            raise RuntimeError("file_write only available on opencode v2")
        params, rel = fs_location_params(path)
        params["path"] = rel
        # Sin Content-Type el serve asume application/json y responde 415
        # (medido en vivo): el cuerpo es octet-stream, como declara el schema.
        return await client.request(
            "POST",
            v2_path(client, "/api/experimental/fs/write"),
            params=params,
            content=content.encode("utf-8"),
            headers={"Content-Type": "application/octet-stream"},
        )

    # Administración: cambian estado global del serve (config, credenciales,
    # servidores MCP). Solo se registran con OPENCODE_ENABLE_DESTRUCTIVE=1.
    # permission_respond NO se gatea: es la unica via para desbloquear un
    # permiso pendiente de una sesion delegada.
    @mcp.tool(
        annotations=DESTRUCT,
        description="Update opencode config (PATCH body as dict). "
        "REQUIRES OPENCODE_ENABLE_DESTRUCTIVE=1.",
    )
    async def config_update(patch: dict) -> Any:
        # v2 has GET /api/config but no PATCH /api/config: the write route is
        # PATCH /api/experimental/config (v2.0.19 spec).
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            return unwrap(
                await client.request(
                    "PATCH", v2_path(client, "/api/experimental/config"), json=patch
                )
            )
        return await client.request("PATCH", "/config", json=patch)

    @mcp.tool(
        annotations=DESTRUCT,
        description="Set authentication credentials for a provider id (body must match "
        "provider schema). REQUIRES OPENCODE_ENABLE_DESTRUCTIVE=1.",
    )
    async def auth_set(id: str, credentials: dict) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2: POST /api/integration/{integrationID}/connect/key {"key": ...}
            # (schema: key requerido, answer/label opcionales).
            key = credentials.get("key")
            if not key:
                raise ValueError('credentials must contain "key" on opencode v2')
            return unwrap(
                await client.request(
                    "POST",
                    v2_path(client, f"/api/integration/{id}/connect/key"),
                    json={"key": key},
                )
            )
        return await client.request("PUT", f"/auth/{id}", json=credentials)

    @mcp.tool(
        annotations=DESTRUCT,
        description="Add an MCP server dynamically (name + config dict) and connect it. "
        "REQUIRES OPENCODE_ENABLE_DESTRUCTIVE=1.",
    )
    async def mcp_add(name: str, config: dict) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2: PUT /api/experimental/mcp/{server} {"config": ...} registra;
            # POST .../connect lo arranca (sin esto el server queda inactivo).
            put = await client.request(
                "PUT",
                v2_path(client, f"/api/experimental/mcp/{name}"),
                json={"config": config},
            )
            connect = await client.request(
                "POST", v2_path(client, f"/api/experimental/mcp/{name}/connect")
            )
            return {"config": unwrap(put), "connect": unwrap(connect)}
        return await client.request(
            "POST", "/mcp", json={"name": name, "config": config}
        )


def register_guarded(mcp: FastMCP, get_client: Any, config: Any) -> None:
    @mcp.tool(
        annotations=DESTRUCT,
        description="Dispose the current server instance. REQUIRES enable_destructive + confirm=true.",
    )
    async def instance_dispose(confirm: bool = False) -> Any:
        if not config.enable_destructive or not confirm:
            raise RuntimeError(
                "Refusing: set OPENCODE_ENABLE_DESTRUCTIVE=1 and pass confirm=true to dispose the instance."
            )
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # No instance/dispose route on the v2.0.19 surface.
            raise v2_error("instance_dispose")
        return await client.request("POST", "/instance/dispose")

    if not config.enable_tui_tools:
        return

    async def _tui_guard() -> None:
        """v2 has no /tui/* routes: fail loud instead of hitting a 404 page."""
        _client, is_v2 = await _is_v2(get_client)
        if is_v2:
            raise v2_error("TUI tools")

    @mcp.tool(annotations=WRITE, description="[TUI] Append text to the TUI prompt.")
    async def tui_append_prompt(text: str) -> Any:
        await _tui_guard()
        return await get_client().request(
            "POST", "/tui/append-prompt", json={"text": text}
        )

    @mcp.tool(annotations=WRITE, description="[TUI] Open the help dialog.")
    async def tui_open_help() -> Any:
        await _tui_guard()
        return await get_client().request("POST", "/tui/open-help")

    @mcp.tool(annotations=WRITE, description="[TUI] Open the session selector.")
    async def tui_open_sessions() -> Any:
        await _tui_guard()
        return await get_client().request("POST", "/tui/open-sessions")

    @mcp.tool(annotations=WRITE, description="[TUI] Open the theme selector.")
    async def tui_open_themes() -> Any:
        await _tui_guard()
        return await get_client().request("POST", "/tui/open-themes")

    @mcp.tool(annotations=WRITE, description="[TUI] Open the model selector.")
    async def tui_open_models() -> Any:
        await _tui_guard()
        return await get_client().request("POST", "/tui/open-models")

    @mcp.tool(annotations=WRITE, description="[TUI] Submit the current prompt.")
    async def tui_submit_prompt() -> Any:
        await _tui_guard()
        return await get_client().request("POST", "/tui/submit-prompt")

    @mcp.tool(annotations=WRITE, description="[TUI] Clear the prompt.")
    async def tui_clear_prompt() -> Any:
        await _tui_guard()
        return await get_client().request("POST", "/tui/clear-prompt")

    @mcp.tool(annotations=WRITE, description="[TUI] Execute a command by name.")
    async def tui_execute_command(command: str) -> Any:
        await _tui_guard()
        return await get_client().request(
            "POST", "/tui/execute-command", json={"command": command}
        )

    @mcp.tool(annotations=WRITE, description="[TUI] Show a toast notification.")
    async def tui_show_toast(
        message: str, title: str | None = None, variant: str = "info"
    ) -> Any:
        await _tui_guard()
        body: dict = {"message": message, "variant": variant}
        if title:
            body["title"] = title
        return await get_client().request("POST", "/tui/show-toast", json=body)
