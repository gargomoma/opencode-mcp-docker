"""Delegation tools: one-call async handoff of coding work to an opencode agent.

The delegating agent never manages raw sessions: delegate_task() picks (or spawns)
the serve instance owning project_dir, creates a session, fires the prompt
asynchronously and returns immediately. Poll delegate_status() for results.
"""

from __future__ import annotations

import os
from typing import Any

from fastmcp import FastMCP

from ..client import BridgeError, model_ref, truncate_text
from ..config import http_dir
from ..servers import ServerManager
from ..v2 import get_version, map_session_create, unwrap, v2_error, v2_path

MAX_DIFF_BYTES = 200 * 1024
MAX_SUMMARY_BYTES = 20 * 1024

from ._annotations import READ, READ_LOCAL, WRITE


async def _is_v2_client(client: Any) -> bool:
    """True when the serve behind this client speaks opencode v2 (cached)."""
    return await get_version(client) == "v2"


def register_delegate(mcp: FastMCP, manager: ServerManager) -> None:
    @mcp.tool(
        annotations=WRITE,
        description="DELEGATE a coding task to the opencode agent working in project_dir. "
        "Async: returns immediately with {session_id, project_dir, server_url}; "
        "poll delegate_status(session_id) for completion. "
        "The opencode server for that folder is reused if mapped/running, else started on demand. "
        "Phrase `task` as a self-contained assignment (goal, files/area, acceptance criteria).",
    )
    async def delegate_task(
        project_dir: str,
        task: str,
        title: str | None = None,
        providerID: str | None = None,
        modelID: str | None = None,
        agent: str | None = None,
        system: str | None = None,
    ) -> dict:
        if not task or not task.strip():
            raise ValueError("task is required")
        directory = http_dir(project_dir)
        client = await manager.resolve(directory)
        if await _is_v2_client(client):
            # v2: POST /api/session + POST /api/session/{id}/prompt.
            # The folder rides in the create BODY (`location.directory`): v2
            # create declares no query parameters, so `?directory=` was
            # ignored and every session landed in the serve default
            # /home/dev (measured on v2.0.19). The session then carries its
            # own location, so the prompt needs no directory at all.
            # agent/model ride into create (v2 create accepts them); system
            # has no v2 equivalent (map raises instead of dropping it).
            session = unwrap(
                await client.request(
                    "POST",
                    v2_path(client, "/api/session"),
                    json=map_session_create(
                        title=title,
                        providerID=providerID,
                        modelID=modelID,
                        agent=agent,
                        system=system,
                        directory=directory,
                    ),
                )
            )
            session_id = _session_id(session)
            _verify_session_location(session, directory)
            await client.request(
                "POST",
                v2_path(client, f"/api/session/{session_id}/prompt"),
                json={"text": task},
            )
            manager.remember_session(session_id, client.config.base_url, directory)
            return {
                "status": "accepted",
                "session_id": session_id,
                "project_dir": directory,
                "server_url": client.config.base_url,
                "next": f"poll delegate_status(session_id={session_id!r}) until it reports completion",
            }
        session = await client.request(
            "POST",
            "/session",
            json={"title": title} if title else {},
            params={"directory": directory},
        )
        session_id = _session_id(session)
        body: dict = {"parts": [{"type": "text", "text": task}]}
        model = model_ref(providerID, modelID)
        if model:
            body["model"] = model
        if agent:
            body["agent"] = agent
        if system:
            body["system"] = system
        await client.request(
            "POST",
            f"/session/{session_id}/prompt_async",
            json=body,
            params={"directory": directory},
        )
        manager.remember_session(session_id, client.config.base_url, directory)
        return {
            "status": "accepted",
            "session_id": session_id,
            "project_dir": directory,
            "server_url": client.config.base_url,
            "next": f"poll delegate_status(session_id={session_id!r}) until it reports completion",
        }

    @mcp.tool(
        annotations=WRITE,
        description="Send a FOLLOW-UP task to an already-delegated opencode session. "
        "Async: returns immediately; poll delegate_status(session_id). "
        "Use for iterations ('also add tests', 'fix the lint errors') on the same work.",
    )
    async def delegate_followup(
        session_id: str,
        task: str,
        providerID: str | None = None,
        modelID: str | None = None,
        agent: str | None = None,
    ) -> dict:
        if not task or not task.strip():
            raise ValueError("task is required")
        client = manager.client_for_session(session_id)
        if await _is_v2_client(client):
            # v2 prompt carries {text} only: validate the pair as on v1,
            # then fail loudly on routing fields instead of silently
            # running the server-default model/agent.
            model_ref(providerID, modelID)
            if providerID is not None or modelID is not None or agent is not None:
                raise v2_error("delegate_followup(model/agent)")
            await client.request(
                "POST",
                v2_path(client, f"/api/session/{session_id}/prompt"),
                json={"text": task},
                params=_session_dir_params(manager, session_id),
            )
            info = manager.session_info(session_id) or {}
            return {
                "status": "accepted",
                "session_id": session_id,
                "project_dir": info.get("project_dir"),
                "next": f"poll delegate_status(session_id={session_id!r}) until it reports completion",
            }
        body: dict = {"parts": [{"type": "text", "text": task}]}
        model = model_ref(providerID, modelID)
        if model:
            body["model"] = model
        if agent:
            body["agent"] = agent
        await client.request(
            "POST",
            f"/session/{session_id}/prompt_async",
            json=body,
            params=_session_dir_params(manager, session_id),
        )
        info = manager.session_info(session_id) or {}
        return {
            "status": "accepted",
            "session_id": session_id,
            "project_dir": info.get("project_dir"),
            "next": f"poll delegate_status(session_id={session_id!r}) until it reports completion",
        }

    @mcp.tool(
        annotations=READ,
        description="Check a delegated session: `running`, real `status` "
        "(busy/idle/unknown), last assistant reply (preview), `completed`, `last_error` "
        "and anything blocking it (`pending_permissions`, `pending_questions`), plus the "
        "current file diff. "
        "DONE = running=false AND no pending permissions/questions.",
    )
    async def delegate_status(
        session_id: str, include_diff: bool = True, message_limit: int = 20
    ) -> dict:
        client = manager.client_for_session(session_id)
        if await _is_v2_client(client):
            return await _delegate_status_v2(
                manager, client, session_id, include_diff, message_limit
            )
        dir_params = _session_dir_params(manager, session_id)
        statuses = (
            await client.request("GET", "/session/status", params=dir_params) or {}
        )
        running = _is_running(statuses, session_id)
        if running:
            status = "busy"
        else:
            # /session/status omite las sesiones idle, asi que "no esta en el
            # mapa" no distingue no-empezada, fallida y terminada: hay que
            # preguntar si la sesion existe.
            status = await _idle_or_unknown(client, session_id, dir_params)
        session = manager.session_info(session_id) or {}
        if status == "unknown":
            # La sesion no existe en ese serve (registro viejo, o la borro alguien):
            # no hay mensajes ni diff que pedir, y pedirlos reventaba con BridgeError
            # en vez de degradar. Medido: una entrada de registro apuntando a una
            # sesion borrada daba "HTTP 404 Session not found" en crudo.
            return {
                "session_id": session_id,
                "project_dir": session.get("project_dir"),
                "server_url": client.config.base_url,
                "running": running,
                "status": status,
                "completed": False,
                "last_error": None,
                "reply_message_id": None,
                "reply_completed_at": None,
                "pending_permissions": [],
                "pending_questions": [],
                "summary": "",
                "files_changed": [],
                "diff": "",
            }
        msg_params: dict = {"limit": str(message_limit)}
        if dir_params:
            msg_params.update(dir_params)
        try:
            messages = (
                await client.request(
                    "GET",
                    f"/session/{session_id}/message",
                    params=msg_params,
                )
                or []
            )
        except BridgeError:
            # La sesion desaparecio entre la comprobacion y esta llamada.
            messages = []
        latest = _latest_assistant_message(messages) or {}
        info = latest.get("info") or {}
        info_time = info.get("time") or {}
        error = info.get("error")
        summary = _message_text(latest)
        files_changed: list = []
        diff_text = ""
        if include_diff:
            try:
                diff = (
                    await client.request(
                        "GET", f"/session/{session_id}/diff", params=dir_params
                    )
                    or []
                )
            except BridgeError:
                diff = []
            files_changed = _diff_files(diff)
            diff_text = truncate_text(_diff_text(diff), MAX_DIFF_BYTES)
        return {
            "session_id": session_id,
            "project_dir": session.get("project_dir"),
            "server_url": client.config.base_url,
            "running": running,
            "status": status,
            "completed": bool(info_time.get("completed")) and not error,
            "last_error": _error_summary(error),
            "reply_message_id": info.get("id"),
            "reply_completed_at": info_time.get("completed"),
            "pending_permissions": await _pending_permissions(client, session_id),
            "pending_questions": await _pending_questions(client, session_id),
            "summary": truncate_text(summary, MAX_SUMMARY_BYTES),
            "files_changed": files_changed,
            "diff": diff_text,
        }

    @mcp.tool(
        annotations=READ_LOCAL,
        description="List the opencode serve instances this bridge manages "
        "(mapped via OPENCODE_SERVERS_JSON or spawned on demand per project_dir).",
    )
    async def bridge_servers() -> list[dict]:
        return manager.managed_servers()


async def _delegate_status_v2(
    manager: ServerManager,
    client: Any,
    session_id: str,
    include_diff: bool,
    message_limit: int,
) -> dict:
    """v2 twin of delegate_status: same shape, v2 routes.

    running <- GET /api/session/active; existence <- GET /api/session/{id};
    messages <- GET .../message; pending permissions <- GET
    /api/session/{id}/permission; pending questions <- GET
    /api/session/{id}/form; diff <- GET .../diff. Every fetch unwraps the
    v2 {"data"} envelope and degrades like its v1 counterpart (missing ->
    idle/unknown/empty, never a traceback).
    """
    dir_params = _session_dir_params(manager, session_id)
    statuses = (
        unwrap(
            await client.request(
                "GET", v2_path(client, "/api/session/active"), params=dir_params
            )
        )
        or {}
    )
    running = _is_running_v2(statuses, session_id)
    if running:
        status = "busy"
    else:
        status = await _idle_or_unknown_v2(client, session_id, dir_params)
    session = manager.session_info(session_id) or {}
    if status == "unknown":
        return {
            "session_id": session_id,
            "project_dir": session.get("project_dir"),
            "server_url": client.config.base_url,
            "running": running,
            "status": status,
            "completed": False,
            "last_error": None,
            "reply_message_id": None,
            "reply_completed_at": None,
            "pending_permissions": [],
            "pending_questions": [],
            "summary": "",
            "files_changed": [],
            "diff": "",
        }
    msg_params: dict = {"limit": str(message_limit)}
    if dir_params:
        msg_params.update(dir_params)
    try:
        messages = (
            unwrap(
                await client.request(
                    "GET",
                    v2_path(client, f"/api/session/{session_id}/message"),
                    params=msg_params,
                )
            )
            or []
        )
    except BridgeError:
        messages = []
    messages_list = messages if isinstance(messages, list) else []
    # v2 messages are FLAT discriminated-union items:
    #   assistant {id, time:{created,completed}, type, content:[{type:"text",text}],
    #              finish, error}
    #   idle      {id, time, type:"idle", outcome:"succeeded"|"failed"|"interrupted"}
    # (v2.0.19 spec + measured live), not the v1 {info:{role}, parts} envelope.
    assistant = _v2_latest_message(messages_list, "assistant")
    idle = _v2_latest_message(messages_list, "idle")
    assistant_time = (assistant or {}).get("time") or {}
    error = _v2_error(assistant, idle)
    summary = _v2_message_text(assistant)
    files_changed: list = []
    diff_text = ""
    if include_diff:
        try:
            diff = (
                unwrap(
                    await client.request(
                        "GET",
                        v2_path(client, f"/api/session/{session_id}/diff"),
                        params=dir_params,
                    )
                )
                or []
            )
        except BridgeError:
            diff = []
        files_changed = _diff_files(diff if isinstance(diff, list) else [])
        diff_text = truncate_text(
            _diff_text(diff if isinstance(diff, list) else []), MAX_DIFF_BYTES
        )
    return {
        "session_id": session_id,
        "project_dir": session.get("project_dir"),
        "server_url": client.config.base_url,
        "running": running,
        "status": status,
        # DONE = the trailing idle message says the loop finished clean, or the
        # last assistant turn closed without an error.
        "completed": _v2_completed(assistant, idle),
        "last_error": error,
        "reply_message_id": (assistant or {}).get("id"),
        "reply_completed_at": assistant_time.get("completed")
        or ((idle or {}).get("time") or {}).get("created"),
        "pending_permissions": await _pending_permissions_v2(client, session_id),
        "pending_questions": await _pending_questions_v2(client, session_id),
        "summary": truncate_text(summary, MAX_SUMMARY_BYTES),
        "files_changed": files_changed,
        "diff": diff_text,
    }


async def _idle_or_unknown_v2(
    client: Any, session_id: str, dir_params: dict | None
) -> str:
    """`idle` si la sesion existe en el serve v2, `unknown` si no la conoce."""
    try:
        await client.request(
            "GET", v2_path(client, f"/api/session/{session_id}"), params=dir_params
        )
    except BridgeError:
        return "unknown"
    return "idle"


async def _pending_permissions_v2(client: Any, session_id: str) -> list[dict]:
    """Permisos pendientes de ESA sesion (v2, best-effort como en v1)."""
    try:
        data = (
            unwrap(
                await client.request(
                    "GET", v2_path(client, f"/api/session/{session_id}/permission")
                )
            )
            or []
        )
    except BridgeError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                "id": entry.get("id") or entry.get("requestID"),
                # v2 names these `action` / `resources`; the v1 key names are
                # kept so the tool output shape does not change per version.
                "permission": entry.get("action") or entry.get("permission"),
                "patterns": entry.get("resources") or entry.get("patterns"),
            }
        )
    return out


async def _pending_questions_v2(client: Any, session_id: str) -> list[dict]:
    """Preguntas pendientes de ESA sesion (v2, best-effort como en v1)."""
    try:
        data = (
            unwrap(
                await client.request(
                    "GET", v2_path(client, f"/api/session/{session_id}/form")
                )
            )
            or []
        )
    except BridgeError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                # v2 form ids are `frm_...` and the global list is GET /api/form.
                "id": entry.get("id") or entry.get("formID") or entry.get("requestID"),
                "questions": _v2_form_questions(entry),
            }
        )
    return out


def _v2_form_questions(entry: dict) -> list[dict]:
    """Extrae {key, header, question, options, multiple} de un form v2.

    Forma verificada contra el openapi.json vivo de v2.0.19: el form trae
    `fields`, cada campo {key, title, description, required, hidden, type,
    options:[{value,label,description}]}. `key` se devuelve porque el reply v2
    se indexa por el (`{answer: {key: value}}`), y las opciones se listan por
    `label` (que es lo que aceptan question_reply/question_reject).
    """
    out = []
    fields = entry.get("fields")
    if not isinstance(fields, list):
        return []
    for field in fields:
        if not isinstance(field, dict):
            continue
        options = []
        for option in field.get("options") or []:
            if isinstance(option, dict):
                label = option.get("label") or option.get("value")
                if label is not None:
                    options.append(label)
            elif isinstance(option, str):
                options.append(option)
        out.append(
            {
                "key": field.get("key"),
                "header": field.get("title"),
                "question": field.get("description") or field.get("title"),
                "options": options,
                "multiple": field.get("type") == "multiselect",
                "required": field.get("required"),
            }
        )
    return out


def _is_running_v2(active: Any, session_id: str) -> bool:
    """v2: GET /api/session/active -> {data: {ses_x: {"type": "running"}}}.

    Presence in that map IS the signal — the only enum value is "running"
    (v2.0.19 spec) — so a session listed there is busy. (:func:`_is_running`,
    the v1 helper, looks for "busy" and would always answer False here.)
    """
    if isinstance(active, dict):
        if session_id in active:
            entry = active[session_id]
            if isinstance(entry, dict):
                if entry.get("type"):
                    return entry["type"] != "idle"
                if "running" in entry:
                    return bool(entry["running"])
                return True
            return bool(entry)
        # {"data": ...} already unwrapped by the caller, but be tolerant:
        if "data" in active:
            return _is_running_v2(active["data"], session_id)
        return False
    if isinstance(active, list):
        for entry in active:
            if not isinstance(entry, dict):
                continue
            if (
                entry.get("sessionID") == session_id
                or entry.get("sessionId") == session_id
                or entry.get("id") == session_id
            ):
                return True
    return False


def _v2_latest_message(messages: Any, kind: str) -> dict | None:
    """Ultimo item v2 de ese `type` (assistant / idle / user)."""
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("type") == kind:
            return msg
    return None


def _v2_message_text(msg: dict | None) -> str:
    """Texto de un mensaje v2: `content` (partes type "text") o su `text`.

    El usuario tambien trae `text`; quien llama filtra por type == assistant,
    asi que el prompt no puede colarse como resumen.
    """
    if not isinstance(msg, dict):
        return ""
    texts = [
        part.get("text", "")
        for part in (msg.get("content") or [])
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    if not texts and isinstance(msg.get("text"), str):
        texts = [msg["text"]]
    return "\n".join(text for text in texts if text).strip()


def _v2_completed(assistant: dict | None, idle: dict | None) -> bool:
    """True cuando el turno cerro bien: idle `succeeded` o assistant sin error."""
    if isinstance(idle, dict) and idle.get("outcome") == "succeeded":
        return True
    if not isinstance(assistant, dict):
        return False
    return bool(
        (assistant.get("time") or {}).get("completed") and not assistant.get("error")
    )


def _v2_error(assistant: dict | None, idle: dict | None) -> dict | None:
    """Error del ultimo turno v2, con la misma forma que en v1 + `status`.

    `assistant.error` es el detalle del proveedor (medido:
    {type:"provider.quota", message:"...", status:429}); si no lo hay pero el
    idle dice `failed`, se reporta ese outcome en vez de decir "sin error".
    """
    error = (assistant or {}).get("error")
    if isinstance(error, dict):
        return {
            "name": error.get("type") or error.get("name"),
            "data": error.get("message"),
            "status": error.get("status"),
        }
    if isinstance(idle, dict) and idle.get("outcome") == "failed":
        return {"name": "failed", "data": None, "status": None}
    return None


def _session_dir_params(manager: ServerManager, session_id: str) -> dict | None:
    """Query `?directory=<project_dir>` para una sesión ya delegada.

    Lee la carpeta del registro (`remember_session` / `session_info`): sin
    ella `GET /session/status` no lista la sesión y `prompt_async` / `diff`
    no son fiables.
    """
    info = manager.session_info(session_id) or {}
    project_dir = info.get("project_dir")
    if project_dir:
        return {"directory": project_dir}
    return None


async def _idle_or_unknown(
    client: Any, session_id: str, dir_params: dict | None
) -> str:
    """`idle` si la sesion existe, `unknown` si el serve no la conoce."""
    try:
        await client.request("GET", f"/session/{session_id}", params=dir_params)
    except BridgeError:
        return "unknown"
    return "idle"


async def _pending_permissions(client: Any, session_id: str) -> list[dict]:
    """Permisos pendientes de ESA sesion.

    `GET /permission` ignora `?directory=` (medido), asi que el filtro por
    sessionID se hace aqui. Best-effort: si falla, la sesion no se bloquea.
    """
    try:
        data = await client.request("GET", "/permission")
    except BridgeError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        if not isinstance(entry, dict) or entry.get("sessionID") != session_id:
            continue
        out.append(
            {
                "id": entry.get("id"),
                "permission": entry.get("permission"),
                "patterns": entry.get("patterns"),
            }
        )
    return out


async def _pending_questions(client: Any, session_id: str) -> list[dict]:
    """Preguntas pendientes de ESA sesion.

    Un agente que pregunta se queda bloqueado igual que uno que pide un permiso,
    pero `delegate_status` solo miraba permisos: la sesion parecia colgada sin
    motivo. `GET /question` tampoco filtra por sesion, asi que se filtra aqui.
    Best-effort: si falla, no se rompe el estado de la sesion.
    """
    try:
        data = await client.request("GET", "/question")
    except BridgeError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        if not isinstance(entry, dict) or entry.get("sessionID") != session_id:
            continue
        out.append(
            {
                "id": entry.get("id"),
                "questions": [
                    {
                        "header": q.get("header"),
                        "question": q.get("question"),
                        "options": [
                            o.get("label")
                            for o in (q.get("options") or [])
                            if isinstance(o, dict)
                        ],
                        "multiple": q.get("multiple"),
                    }
                    for q in (entry.get("questions") or [])
                    if isinstance(q, dict)
                ],
            }
        )
    return out


def _error_summary(error: Any) -> dict | None:
    if not isinstance(error, dict):
        return None
    return {"name": error.get("name"), "data": error.get("data")}


def _is_running(statuses: Any, session_id: str) -> bool:
    """True mientras la sesion sigue trabajando.

    opencode >= 1.18 responde {"ses_x": {"type": "busy"}} y OMITE la sesion del
    mapa cuando esta idle; versiones anteriores usaban {"running": true}.
    """
    entry = statuses.get(session_id) if isinstance(statuses, dict) else None
    if not isinstance(entry, dict):
        return False
    return entry.get("type") == "busy" or bool(entry.get("running", False))


def _session_id(session: Any) -> str:
    if isinstance(session, dict):
        for key in ("id", "sessionID", "sessionId"):
            if session.get(key):
                return str(session[key])
    raise ValueError(f"Unexpected session payload from server: {session!r}")


def _verify_session_location(session: Any, directory: str) -> None:
    """Fail loudly when the new v2 session is not in the requested folder.

    v2 create echoes back the ``location.directory`` it bound, verbatim
    (measured: trailing slash preserved, no canonicalization), so a mismatch
    means the whole delegation would read and write another project. Skip
    when the serve reports no location (older builds / v1-shaped payloads).
    """
    if not isinstance(session, dict):
        return
    location = session.get("location")
    actual = location.get("directory") if isinstance(location, dict) else None
    if not isinstance(actual, str) or not actual:
        return
    if os.path.normpath(actual) != os.path.normpath(directory):
        raise BridgeError(
            f"opencode created session {_session_id(session)!r} in {actual!r}, not in the "
            f"requested project_dir {directory!r}: refusing to delegate into the wrong folder."
        )


def _latest_assistant_message(messages: Any) -> dict | None:
    """Ultimo mensaje del asistente CON contenido.

    Sin el filtro de rol, mientras el asistente no ha escrito nada se devolvia
    el propio prompt del usuario como "summary"; y un turno nuevo sin respuesta
    reutilizaba el texto del turno anterior (para eso va reply_message_id).
    """
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = (msg.get("info") or {}).get("role")
        if role and role != "assistant":
            continue
        if msg.get("parts"):
            return msg
    return None


def _message_text(msg: dict) -> str:
    parts = msg.get("parts") or []
    texts = [
        p.get("text", "")
        for p in parts
        if isinstance(p, dict) and p.get("type") == "text"
    ]
    return "\n".join(texts).strip()


def _diff_files(diff: Any) -> list:
    if not isinstance(diff, list):
        return []
    files = []
    for entry in diff:
        if not isinstance(entry, dict):
            continue
        for key in ("path", "file", "filePath"):
            if entry.get(key):
                files.append(entry[key])
                break
    return sorted(set(files))


def _diff_text(diff: Any) -> str:
    if not isinstance(diff, list):
        return ""
    patches = [
        e.get("patch", "")
        for e in diff
        if isinstance(e, dict) and isinstance(e.get("patch"), str)
    ]
    return "\n".join(patches)
