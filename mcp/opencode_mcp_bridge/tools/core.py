"""Core tools: health, events, sessions, messages."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from ..client import BridgeError, model_ref
from ..v2 import (
    bridge_request,
    get_version,
    map_command_body,
    map_fork_body,
    map_form_answer,
    map_permission_reply,
    map_prompt_body,
    map_session_create,
    map_shell_body,
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


async def _v2_form_directories(client: Any) -> list[str]:
    """Session directories on this serve: the scopes that can hold a form.

    Measured on v2.0.19: ``GET /api/form`` is scoped by ``location[directory]``
    with an EXACT match — bare it answers ``[]``, and a parent directory does NOT
    include the forms of its subfolders (a form planted in ``/opt/opencode/
    maps-scrape`` is invisible from ``/opt/opencode`` and from ``/``). ``GET
    /api/session`` is not location-scoped and every ``Session.Info`` carries
    ``location.directory``, so listing sessions is how the globally-visible v1
    ``GET /question`` view is rebuilt out of v2 routes.
    """
    try:
        payload = await client.request(
            "GET", v2_path(client, "/api/session"), params={"limit": "200"}
        )
    except BridgeError:
        return []
    items = unwrap(payload) or []
    directories: list[str] = []
    if isinstance(items, list):
        for item in items:
            location = item.get("location") if isinstance(item, dict) else None
            directory = (
                location.get("directory") if isinstance(location, dict) else None
            )
            if (
                isinstance(directory, str)
                and directory
                and directory not in directories
            ):
                directories.append(directory)
    return directories


async def _v2_pending_forms(client: Any) -> list[dict]:
    """Every pending v2 form, merged across the serve's session directories.

    One request per known directory (see :func:`_v2_form_directories`), deduped by
    form id. Falls back to the bare, unscoped ``GET /api/form`` when no directory
    is known (session list unavailable) — that call sees only the serve's default
    scope, which is why it is a fallback and not the primary path.
    """
    directories = await _v2_form_directories(client)
    scopes: list[dict | None] = [{"location[directory]": d} for d in directories] or [
        None
    ]
    forms: list[dict] = []
    seen: set = set()
    for params in scopes:
        try:
            items = unwrap(
                await client.request("GET", v2_path(client, "/api/form"), params=params)
            )
        except BridgeError:
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("id") in seen:
                continue
            seen.add(item.get("id"))
            forms.append(item)
    return forms


async def _find_v2_form(client: Any, form_id: str) -> dict:
    """Find a pending v2 form by id, across every session directory.

    v2 replies/cancels are session-scoped, so the sessionID has to come from the
    form itself; the directory-scoped list is the only way to get it from an id.
    """
    for item in await _v2_pending_forms(client):
        if item.get("id") == form_id:
            return item
    raise BridgeError(
        f"No pending v2 form with id {form_id!r} (session-scoped GET /api/form): "
        f"it may already be answered or cancelled."
    )


def register_core(mcp: FastMCP, get_client: Any, config: Any) -> None:
    @mcp.tool(
        annotations=READ,
        description="Check opencode server health and version. Start here.",
    )
    async def health_check() -> dict:
        # v2: GET /api/info (server.info). Verified: live docs
        # https://opencode.ai/v2/docs/api (server -> GET /api/info).
        return await bridge_request(
            get_client(), "GET", "/global/health", "/api/info", unwrap_v2=False
        )

    @mcp.tool(
        annotations=WRITE,
        description="One-shot poll of the SSE /event stream (returns up to `limit` events, then disconnects). MCP has no streaming.",
    )
    async def event_poll(limit: int = 20, timeout: float = 5.0) -> list[dict]:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2: GET /api/event (event.subscribe). Verified: live docs index.
            return await client.event_poll(
                limit=limit, timeout=timeout, path=v2_path(client, "/api/event")
            )
        return await client.event_poll(limit=limit, timeout=timeout)

    # -- projects / path ----------------------------------------------------
    @mcp.tool(annotations=READ, description="List all opencode projects.")
    async def project_list() -> Any:
        # v2: GET /api/project (project.list). Verified: live docs index.
        return await bridge_request(get_client(), "GET", "/project", "/api/project")

    @mcp.tool(annotations=READ, description="Get the current opencode project.")
    async def project_current() -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2: GET /api/location -> {directory, project:{id,directory,canonical}}.
            # No /project/current route exists on v2; location is the authoritative
            # answer for "which project is this server working in" (v2.0.19 spec).
            location = await client.request("GET", v2_path(client, "/api/location"))
            if isinstance(location, dict) and isinstance(location.get("project"), dict):
                return location["project"]
            return location
        return await get_client().request("GET", "/project/current")

    @mcp.tool(
        annotations=READ, description="Get the current working path of the server."
    )
    async def path_get() -> Any:
        # v2: GET /api/location (location.get). Verified: live docs index.
        return await bridge_request(
            get_client(), "GET", "/path", "/api/location", unwrap_v2=False
        )

    @mcp.tool(annotations=READ, description="Get VCS info for the current project.")
    async def vcs_info() -> Any:
        # v2: GET /api/vcs/status. Verified: live docs (vcs resource).
        return await bridge_request(get_client(), "GET", "/vcs", "/api/vcs/status")

    # -- sessions ------------------------------------------------------------
    @mcp.tool(annotations=READ, description="List all sessions.")
    async def session_list() -> Any:
        items, _extra = unwrap_list(
            await bridge_request(
                get_client(), "GET", "/session", "/api/session", unwrap_v2=False
            )
        )
        return items

    @mcp.tool(
        annotations=WRITE,
        description="Create a new session on the default server. Optional title. "
        "Prefer delegate_task() when handing off work for a specific project folder.",
    )
    async def session_create(
        title: str | None = None,
        providerID: str | None = None,
        modelID: str | None = None,
        agent: str | None = None,
        directory: str | None = None,
    ) -> Any:
        # v2: POST /api/session. Verified: live docs (session.create).
        # El cuerpo v2 de prompt no puede llevar modelo (map_prompt_body falla),
        # asi que el pin viaja aqui, en Model.Ref: map_session_create aplica el
        # pin del entorno cuando no hay par providerID/modelID explicito.
        _client, is_v2 = await _is_v2(get_client)
        if is_v2:
            body = map_session_create(
                title=title,
                providerID=providerID,
                modelID=modelID,
                agent=agent,
                directory=directory,
            )
            params = None
        else:
            if providerID or modelID or agent:
                raise RuntimeError(
                    "session_create(providerID/modelID/agent) only available on opencode v2"
                )
            body = {"title": title} if title else {}
            params = {"directory": directory} if directory else None
        return await bridge_request(
            get_client(), "POST", "/session", "/api/session", json=body, params=params
        )

    @mcp.tool(annotations=READ, description="Get session details by id.")
    async def session_get(id: str) -> Any:
        return await bridge_request(
            get_client(), "GET", f"/session/{id}", f"/api/session/{id}"
        )

    if config.enable_destructive:

        @mcp.tool(
            annotations=DESTRUCT,
            description="Delete a session and all its data. "
            "REQUIRES OPENCODE_ENABLE_DESTRUCTIVE=1.",
        )
        async def session_delete(id: str) -> Any:
            # v2: DELETE /api/session/{id} (measured on v2.0.19: 204, empty body).
            return await bridge_request(
                get_client(), "DELETE", f"/session/{id}", f"/api/session/{id}"
            )

    @mcp.tool(annotations=WRITE, description="Update session properties (e.g. title).")
    async def session_update(id: str, title: str) -> Any:
        # v2: PATCH /api/session/{id}. Verified: live docs (session.update).
        return await bridge_request(
            get_client(),
            "PATCH",
            f"/session/{id}",
            f"/api/session/{id}",
            json={"title": title},
            unwrap_v2=False,
        )

    @mcp.tool(
        annotations=WRITE,
        description="Switch a session's model mid-session (keeps context). "
        "Both providerID and modelID required; get valid pairs from models_list.",
    )
    async def session_model(
        id: str, providerID: str | None = None, modelID: str | None = None
    ) -> Any:
        # v2: POST /api/session/{id}/model with {"model": Model.Ref{id, providerID}}.
        model = model_ref(providerID, modelID, use_default=False)
        if not model:
            raise ValueError("providerID and modelID are required (provided together)")
        client, is_v2 = await _is_v2(get_client)
        if not is_v2:
            raise RuntimeError("session_model only available on opencode v2")
        return unwrap(
            await client.request(
                "POST",
                v2_path(client, f"/api/session/{id}/model"),
                json={
                    "model": {
                        "id": model["modelID"],
                        "providerID": model["providerID"],
                    }
                },
            )
        )

    @mcp.tool(annotations=READ, description="Get a session's context/token usage.")
    async def session_context(id: str) -> Any:
        # v2: GET /api/session/{id}/context.
        client, is_v2 = await _is_v2(get_client)
        if not is_v2:
            raise RuntimeError("session_context only available on opencode v2")
        return unwrap(
            await client.request("GET", v2_path(client, f"/api/session/{id}/context"))
        )

    @mcp.tool(annotations=READ, description="Get child sessions of a session.")
    async def session_children(id: str) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2 dropped /children: children are the sessions whose parentID
            # matches, listed by GET /api/session?parentID=.
            items, _extra = unwrap_list(
                await client.request(
                    "GET", v2_path(client, "/api/session"), params={"parentID": id}
                )
            )
            return items
        return await client.request("GET", f"/session/{id}/children")

    @mcp.tool(annotations=READ, description="Get the todo list for a session.")
    async def session_todo(id: str) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # The v2.0.19 surface has no todo route at all (no /todo in the live
            # openapi.json): the v1 path would 404, so fail loud instead.
            raise v2_error("session_todo")
        return await client.request("GET", f"/session/{id}/todo")

    @mcp.tool(
        annotations=READ,
        description="Get the file diff for a session (optional messageID to scope).",
    )
    async def session_diff(id: str, messageID: str | None = None) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2 diff takes from/to/context only (v2.0.19); the v1 messageID
            # scoping has no equivalent, so a set value raises instead of being
            # silently dropped. Payload is {location, data:[{file,patch,...}]}.
            if messageID is not None:
                raise v2_error("session_diff(messageID)")
            return unwrap(
                await client.request("GET", v2_path(client, f"/api/session/{id}/diff"))
            )
        params = {"messageID": messageID} if messageID else None
        return await client.request("GET", f"/session/{id}/diff", params=params)

    @mcp.tool(annotations=WRITE, description="Fork a session at an optional message.")
    async def session_fork(id: str, messageID: str | None = None) -> Any:
        # v2 body is {"before": msg_id} (v2.0.19); the v1 key {"messageID"} is
        # rejected there (additionalProperties: false).
        return await bridge_request(
            get_client(),
            "POST",
            f"/session/{id}/fork",
            f"/api/session/{id}/fork",
            json=map_fork_body(messageID),
        )

    @mcp.tool(annotations=WRITE, description="Abort a running session.")
    async def session_abort(id: str) -> Any:
        # v2: POST /api/session/{id}/interrupt (operationId session.interrupt).
        # Measured on v2.0.19: the v1 route /session/{id}/abort there answers 404
        # while /api/session/{id}/interrupt answers 200 {"interrupted": false}.
        # That body is not a {"data": ...} envelope, hence unwrap_v2=False.
        return await bridge_request(
            get_client(),
            "POST",
            f"/session/{id}/abort",
            f"/api/session/{id}/interrupt",
            unwrap_v2=False,
        )

    @mcp.tool(annotations=WRITE, description="Share a session.")
    async def session_share(id: str) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # No share routes on the v2.0.19 surface.
            raise v2_error("session_share")
        return await client.request("POST", f"/session/{id}/share")

    @mcp.tool(annotations=DESTRUCT, description="Unshare a session.")
    async def session_unshare(id: str) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            raise v2_error("session_unshare")
        return await client.request("DELETE", f"/session/{id}/share")

    @mcp.tool(
        annotations=WRITE, description="Summarize a session with a provider model."
    )
    async def session_summarize(
        id: str, providerID: str | None = None, modelID: str | None = None
    ) -> Any:
        # v2 compaction is POST /api/session/{id}/compact and carries no model
        # (the session's own model does the work), so a pinned model raises
        # there; v1 still requires the providerID/modelID pair.
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            if providerID is not None or modelID is not None:
                raise v2_error("session_summarize(providerID/modelID)")
            return unwrap(
                await client.request(
                    "POST", v2_path(client, f"/api/session/{id}/compact")
                )
            )
        if not providerID or not modelID:
            raise ValueError("providerID and modelID are required on opencode v1")
        return await client.request(
            "POST",
            f"/session/{id}/summarize",
            json={"providerID": providerID, "modelID": modelID},
        )

    @mcp.tool(
        annotations=WRITE,
        description="Analyze the app and create AGENTS.md for a session.",
    )
    async def session_init(
        id: str, messageID: str, providerID: str, modelID: str
    ) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # The v2 surface has no /init (nor an AGENTS.md bootstrap route).
            raise v2_error("session_init")
        return await client.request(
            "POST",
            f"/session/{id}/init",
            json={"messageID": messageID, "providerID": providerID, "modelID": modelID},
        )

    @mcp.tool(annotations=WRITE, description="Revert a message (optional partID).")
    async def session_revert(id: str, messageID: str, partID: str | None = None) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2 split the v1 revert into stage + commit (revert/clear|stage|
            # commit); partID has no v2 equivalent (raises instead of dropping).
            if partID:
                raise v2_error("session_revert(partID)")
            await client.request(
                "POST",
                v2_path(client, f"/api/session/{id}/revert/stage"),
                json={"messageID": messageID},
            )
            return unwrap(
                await client.request(
                    "POST", v2_path(client, f"/api/session/{id}/revert/commit")
                )
            )
        body: dict = {"messageID": messageID}
        if partID:
            body["partID"] = partID
        return await client.request("POST", f"/session/{id}/revert", json=body)

    @mcp.tool(
        annotations=WRITE, description="Restore all reverted messages in a session."
    )
    async def session_unrevert(id: str) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2: DELETE /api/session/{id}/revert (revert.clear) drops the staged
            # revert; there is no v1-style /unrevert route.
            return unwrap(
                await client.request(
                    "DELETE", v2_path(client, f"/api/session/{id}/revert")
                )
            )
        return await client.request("POST", f"/session/{id}/unrevert")

    @mcp.tool(annotations=READ, description="Get running status for all sessions.")
    async def session_status() -> Any:
        # v2: GET /api/session/active. Verified: live docs
        # (session -> getList active sessions).
        return await bridge_request(
            get_client(), "GET", "/session/status", "/api/session/active"
        )

    @mcp.tool(
        annotations=WRITE, description="Respond to a permission request in a session."
    )
    async def permission_respond(id: str, permissionID: str, response: str) -> Any:
        """`response` es un enum del serve: "once", "always" o "reject".

        El `remember` que aceptaba esta herramienta no existe en la API
        (`POST /session/{sessionID}/permissions/{permissionID}` solo admite
        `response`): se ha quitado en vez de mandar un campo muerto.
        """
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2 route + body both changed: POST
            # /api/session/{id}/permission/{requestID}/reply {"decision": ...}
            # (v2.0.19). Same enum values, different key.
            return unwrap(
                await client.request(
                    "POST",
                    v2_path(
                        client, f"/api/session/{id}/permission/{permissionID}/reply"
                    ),
                    json=map_permission_reply(response),
                )
            )
        return await client.request(
            "POST",
            f"/session/{id}/permissions/{permissionID}",
            json={"response": response},
        )

    @mcp.tool(
        annotations=READ,
        description="List pending permission requests on the server. A session waiting "
        "for one stays `busy` until it is answered with permission_respond.",
    )
    async def permission_list() -> Any:
        # v2: GET /api/permission/request. Verified: SPEC candidate + live
        # docs (permission -> getList pending permission requests).
        return await bridge_request(
            get_client(), "GET", "/permission", "/api/permission/request"
        )

    @mcp.tool(
        annotations=READ,
        description="List saved (always/never) permission rules.",
    )
    async def permission_saved_list() -> Any:
        # v2: GET /api/permission/saved.
        client, is_v2 = await _is_v2(get_client)
        if not is_v2:
            raise RuntimeError("permission_saved_list only available on opencode v2")
        items, _extra = unwrap_list(
            await client.request("GET", v2_path(client, "/api/permission/saved"))
        )
        return items

    if config.enable_destructive:

        @mcp.tool(
            annotations=DESTRUCT,
            description="Delete a saved permission rule. "
            "REQUIRES OPENCODE_ENABLE_DESTRUCTIVE=1.",
        )
        async def permission_saved_delete(id: str) -> Any:
            # v2: DELETE /api/permission/saved/{id}.
            client, is_v2 = await _is_v2(get_client)
            if not is_v2:
                raise RuntimeError(
                    "permission_saved_delete only available on opencode v2"
                )
            return await client.request(
                "DELETE", v2_path(client, f"/api/permission/saved/{id}")
            )

    # -- preguntas del agente -----------------------------------------------
    # Un agente que pregunta se queda bloqueado exactamente igual que uno que
    # pide un permiso; sin estas herramientas la sesion parece "colgada".
    @mcp.tool(
        annotations=READ,
        description="List pending question requests (an agent is waiting for an answer). "
        "A session blocked on one stays `busy` until question_reply or question_reject. "
        "Covers every session (on v2 one scoped call per session directory), so look at "
        "`sessionID`; delegate_status already filters this for its own session.",
    )
    async def question_list() -> Any:
        # v2: GET /api/form is location-scoped (exact directory, measured), so a
        # bare call hides every form of a session whose directory differs from the
        # serve default — the bug that made a write-access question invisible.
        # One call per known session directory instead. Session-scoped variant:
        # GET /api/session/{id}/form (session.form.list).
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            return await _v2_pending_forms(client)
        return await get_client().request("GET", "/question")

    @mcp.tool(
        annotations=WRITE,
        description="Answer a pending question request. `answers` has one entry per "
        "question, in the same order, and each entry is the list of chosen option labels "
        "(see question_list; a free-form answer is a single-element list).",
    )
    async def question_reply(requestID: str, answers: list[list[str]]) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2 replies are session-scoped and keyed by field key:
            # POST /api/session/{id}/form/{formID}/reply {"answer": {key: value}}.
            # The form is looked up in the global list (GET /api/form) for its
            # sessionID + field keys, and the v1 label answers are mapped to the
            # option values the form declares.
            form = await _find_v2_form(client, requestID)
            session_id = form.get("sessionID")
            if not session_id:
                raise BridgeError(f"v2 form {requestID!r} carries no sessionID")
            return unwrap(
                await client.request(
                    "POST",
                    v2_path(
                        client, f"/api/session/{session_id}/form/{requestID}/reply"
                    ),
                    json={"answer": map_form_answer(form.get("fields"), answers)},
                )
            )
        return await get_client().request(
            "POST", f"/question/{requestID}/reply", json={"answers": answers}
        )

    @mcp.tool(
        annotations=WRITE,
        description="Reject a pending question request: the agent continues without an answer.",
    )
    async def question_reject(requestID: str) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2 cancel: DELETE /api/session/{id}/form/{formID} (form.cancel),
            # session-scoped like the reply -> same global-list lookup.
            form = await _find_v2_form(client, requestID)
            session_id = form.get("sessionID")
            if not session_id:
                raise BridgeError(f"v2 form {requestID!r} carries no sessionID")
            return unwrap(
                await client.request(
                    "DELETE",
                    v2_path(client, f"/api/session/{session_id}/form/{requestID}"),
                )
            )
        return await get_client().request("POST", f"/question/{requestID}/reject")

    # -- messages -------------------------------------------------------------
    @mcp.tool(
        annotations=WRITE,
        description="Send a prompt to a session on the default server and wait for the response. "
        "Prefer delegate_task()/delegate_followup() for async delegation tied to a project folder. "
        "For long tasks prefer prompt_async, then poll session_status/message_list. "
        "Set noReply=true to inject context without triggering a reply.",
    )
    async def prompt(
        id: str,
        text: str,
        providerID: str | None = None,
        modelID: str | None = None,
        agent: str | None = None,
        system: str | None = None,
        noReply: bool = False,
        format: dict | None = None,
        files: list[str] | None = None,
        delivery: str | None = None,
    ) -> Any:
        # model_ref valida el par como en v1 (un valor suelto = ValueError),
        # pero use_default=False: el pin por defecto del entorno va en la
        # creacion de la sesion (v2) o en el mensaje v1 de mas abajo, porque el
        # cuerpo v2 de prompt no puede llevar modelo y map_prompt_body fallaria.
        model = model_ref(providerID, modelID, use_default=False)
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2: POST /api/session/{id}/prompt with {text}.
            # Verified: SPEC candidate + live docs (session -> postSend message)
            # + python SDK v2_session_prompt.
            body = map_prompt_body(
                text,
                model=model,
                agent=agent,
                system=system,
                noReply=noReply,
                format=format,
                files=files,
                delivery=delivery,
            )
            return unwrap(
                await client.request(
                    "POST", v2_path(client, f"/api/session/{id}/prompt"), json=body
                )
            )
        if files or delivery:
            raise RuntimeError("prompt(files/delivery) only available on opencode v2")
        body = {"parts": [{"type": "text", "text": text}], "noReply": noReply}
        # v1: el modelo puede ir por mensaje; sin par explicito, pin del entorno.
        model = model or model_ref(None, None)
        if model:
            body["model"] = model
        if agent:
            body["agent"] = agent
        if system:
            body["system"] = system
        if format:
            body["format"] = format
        return await get_client().request("POST", f"/session/{id}/message", json=body)

    @mcp.tool(
        annotations=WRITE,
        description="Send a prompt asynchronously (returns immediately, 204). Poll session_status/message_list for results.",
    )
    async def prompt_async(
        id: str,
        text: str,
        providerID: str | None = None,
        modelID: str | None = None,
        agent: str | None = None,
    ) -> str:
        model = model_ref(providerID, modelID)
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2: same prompt endpoint queues without blocking; the
            # deterministic wait is session_wait (POST /api/session/{id}/wait).
            # Fire-and-forget here to preserve prompt_async semantics.
            body = map_prompt_body(text, model=model, agent=agent)
            await client.request(
                "POST",
                v2_path(client, f"/api/session/{id}/prompt"),
                json=body,
            )
            return "accepted"
        body: dict = {"parts": [{"type": "text", "text": text}]}
        # v1: el modelo puede ir por mensaje; sin par explicito, pin del entorno.
        model = model or model_ref(None, None)
        if model:
            body["model"] = model
        if agent:
            body["agent"] = agent
        await get_client().request("POST", f"/session/{id}/prompt_async", json=body)
        return "accepted"

    @mcp.tool(
        annotations=READ, description="List messages in a session (optional limit)."
    )
    async def message_list(id: str, limit: int | None = None) -> Any:
        # v2: GET /api/session/{id}/message. Verified: live docs
        # (session -> getGet session messages).
        params = {"limit": str(limit)} if limit is not None else None
        items, _extra = unwrap_list(
            await bridge_request(
                get_client(),
                "GET",
                f"/session/{id}/message",
                f"/api/session/{id}/message",
                params=params,
                unwrap_v2=False,
            )
        )
        return items

    @mcp.tool(annotations=READ, description="Get message details.")
    async def message_get(id: str, messageID: str) -> Any:
        return await bridge_request(
            get_client(),
            "GET",
            f"/session/{id}/message/{messageID}",
            f"/api/session/{id}/message/{messageID}",
        )

    @mcp.tool(
        annotations=WRITE,
        description="Replace one part of a message (PATCH .../message/{messageID}/part/{partID}). "
        "`part` is the full Part object the server expects (same shape as in message_get): "
        "use it to fix what the agent wrote without deleting the message.",
    )
    async def part_update(id: str, messageID: str, partID: str, part: dict) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # No message-part PATCH route on the v2.0.19 surface.
            raise v2_error("part_update")
        return await client.request(
            "PATCH",
            f"/session/{id}/message/{messageID}/part/{partID}",
            json=part,
        )

    if config.enable_destructive:

        @mcp.tool(
            annotations=DESTRUCT,
            description="Delete one message from a session. Irreversible. "
            "REQUIRES OPENCODE_ENABLE_DESTRUCTIVE=1.",
        )
        async def message_delete(id: str, messageID: str) -> Any:
            client, is_v2 = await _is_v2(get_client)
            if is_v2:
                # v2 can read a message but not delete it (no DELETE
                # /api/session/{id}/message/{messageID}): fail loud.
                raise v2_error("message_delete")
            return await client.request("DELETE", f"/session/{id}/message/{messageID}")

        @mcp.tool(
            annotations=DESTRUCT,
            description="Delete one part of a message. Irreversible. "
            "REQUIRES OPENCODE_ENABLE_DESTRUCTIVE=1.",
        )
        async def part_delete(id: str, messageID: str, partID: str) -> Any:
            client, is_v2 = await _is_v2(get_client)
            if is_v2:
                raise v2_error("part_delete")
            return await client.request(
                "DELETE", f"/session/{id}/message/{messageID}/part/{partID}"
            )

    @mcp.tool(annotations=WRITE, description="Execute a slash command in a session.")
    async def run_command(
        id: str, command: str, arguments: str = "", agent: str | None = None
    ) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if is_v2:
            # v2 body is {"name": command, "text": arguments}; there is no agent
            # field (map_command_body raises when one is set).
            return unwrap(
                await client.request(
                    "POST",
                    v2_path(client, f"/api/session/{id}/command"),
                    json=map_command_body(command, arguments, agent),
                )
            )
        body: dict = {"command": command, "arguments": arguments}
        if agent:
            body["agent"] = agent
        return await client.request("POST", f"/session/{id}/command", json=body)

    if config.enable_destructive:

        @mcp.tool(
            annotations=DESTRUCT,
            description="Run a shell command via a session agent. "
            "REQUIRES OPENCODE_ENABLE_DESTRUCTIVE=1.",
        )
        async def run_shell(
            id: str, agent: str | None = None, command: str = ""
        ) -> Any:
            client, is_v2 = await _is_v2(get_client)
            if is_v2:
                # v2 shell body is {"command"} only: agent has no equivalent.
                return unwrap(
                    await client.request(
                        "POST",
                        v2_path(client, f"/api/session/{id}/shell"),
                        json=map_shell_body(command, agent),
                    )
                )
            if not agent:
                raise ValueError("agent is required on opencode v1")
            return await client.request(
                "POST",
                f"/session/{id}/shell",
                json={"agent": agent, "command": command},
            )

    @mcp.tool(
        annotations=WRITE,
        description="Wait until a v2 session's agent loop is idle (deterministic "
        "completion for prompt_async on v2; on v1 poll session_status/message_list).",
    )
    async def session_wait(id: str) -> Any:
        client, is_v2 = await _is_v2(get_client)
        if not is_v2:
            raise RuntimeError(
                "session_wait is only available against opencode v2 "
                "(on v1, poll session_status/message_list instead)"
            )
        # v2.0.19 exposes the wait ONLY as
        # POST /api/experimental/session/{id}/wait (there is no
        # /api/session/{id}/wait route); the SDK's v2_session_wait maps there.
        return unwrap(
            await client.request(
                "POST",
                v2_path(client, f"/api/experimental/session/{id}/wait"),
                json={},
            )
        )

    @mcp.tool(
        annotations=READ,
        description="Report the detected opencode serve API version (v1/v2) for "
        "the default server. Never returns credentials.",
    )
    async def bridge_version() -> dict:
        client = get_client()
        return {
            "version": await get_version(client),
            "base_url": client.config.base_url,
            "auth_configured": bool(client.config.password),
        }
