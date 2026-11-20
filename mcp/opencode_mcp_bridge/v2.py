"""opencode v2 compatibility (sin romper v1).

- Detection per serve, cached with TTL: probe v1 ``GET /global/health``;
  404/401/403 -> confirm with ``GET /api/info`` (2xx con cuerpo objeto JSON,
  o bien 401/403 -> v2). Never raises to the caller (defaults to v1,
  uncached on transport errors).
- Auth: the base ``OpencodeClient`` carries ``auth=config.auth`` at the httpx
  level (production behavior, kept intact), so v2 requests authenticate
  automatically whenever ``OPENCODE_SERVER_PASSWORD`` is configured. This
  module intentionally adds NO parallel auth plumbing.
- v2 routes verified against https://opencode.ai/v2/docs/api (live docs)
  plus SPEC.md candidates. The local openapi.json referenced in SPEC.md
  (``/opt/data/cache/scratch/opencode-v2-openapi.json``) does not exist in
  this environment, so live docs are the source of truth (see EVIDENCE_V2.md).
- v2 list/object envelopes ``{"data": ...}`` are unwrapped via :func:`unwrap`;
  :func:`unwrap_list` additionally preserves siblings (location/pagination).
- :func:`bridge_request` is the single dispatch helper for tools whose
  v1/v2 bodies and params are identical; tools with differing bodies keep
  explicit branches using :func:`map_prompt_body` / :func:`map_session_create`,
  which fail loudly instead of silently dropping fields.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

from .client import model_ref

V2_PREFIX = "/api"
V2_NOT_AVAILABLE = "not available in opencode v2"

# How long a detected version is trusted before re-probing.
VERSION_TTL = 300.0

# base_url (rstripped) -> (version, monotonic timestamp of detection)
_version_cache: dict[str, tuple[str, float]] = {}


def cache_key(base_url: str) -> str:
    return (base_url or "").rstrip("/")


def _base_declares_v2(base_url: str) -> bool:
    """True when the configured base URL itself declares v2 (path is /api).

    SPEC defines v2 exactly this way ("base que termina en `/api`"). An
    explicit `/api` suffix wins over probing: opencode 2.x answers
    `GET /global/health` (compat shim, measured live on 2.0.18), so a
    2xx probe alone cannot prove v1 — trusting it routes health/path/
    project_current down v1 paths that 404 on the live serve.
    """
    try:
        path = urlsplit(base_url or "").path.rstrip("/")
    except Exception:
        return False
    return path == "/api" or path.endswith("/api")


def clear_cache() -> None:
    _version_cache.clear()


def v2_path(client_or_base: Any, route: str) -> str:
    """Relativize a canonical v2 route to the configured base URL.

    Canonical routes are written from the server root (``/api/session``,
    matching the OpenAPI docs and EVIDENCE_V2.md). But httpx *appends*
    request paths to the base URL path, so under a base that already ends
    with ``/api`` (SPEC's v2 setup, e.g. ``http://host:4096/api``) a
    literal ``/api/session`` would join to ``/api/api/session`` -> 404
    (measured live). When the base declares v2, the ``/api`` prefix is
    therefore stripped (``/session`` joins back to ``/api/session``);
    otherwise the canonical route is returned unchanged.
    """
    base = getattr(
        getattr(client_or_base, "config", client_or_base), "base_url", client_or_base
    )
    if isinstance(base, str) and _base_declares_v2(base) and route.startswith("/api/"):
        return route[len("/api") :]
    return route


def peek_version(base_url: str) -> str | None:
    """Last detected version for a serve, or None if never detected.

    Synchronous and never probes: for display where an extra RTT is unwanted.
    """
    hit = _version_cache.get(cache_key(base_url))
    return hit[0] if hit is not None else None


def _confirm_is_v2(resp: Any) -> bool:
    """Decide whether a ``GET /api/info`` response proves a v2 serve.

    401/403 mean something guards the /api tree (a v2 stack asking for
    credentials — the probe itself is unauthenticated only when no password
    is configured). A 2xx must carry a JSON object body; bare text/HTML
    (proxies, static files, captive portals, the v1 SPA fallback) must NOT
    flip a serve to v2.
    """
    if resp.status_code in (401, 403):
        return True
    if resp.status_code >= 400:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict)


async def get_version(client: Any) -> str:
    """Return 'v1' or 'v2' for this client's serve; cached per base_url + TTL.

    A base URL whose path is/ends with ``/api`` declares v2 outright (SPEC's
    definition) and short-circuits: no probe is sent. Otherwise probes
    ``GET /global/health`` via the raw httpx client (the status code
    IS the signal; ``OpencodeClient.request()`` would raise on >=400). 2xx
    means v1. A 404/401/403 MAY mean v2, but v1 test doubles and unrelated
    origins also answer 404, so it is confirmed with ``GET /api/info`` (see
    :func:`_confirm_is_v2`): only then is it v2, otherwise v1.
    Any transport error -> 'v1' without caching and without raising.
    """
    key = cache_key(getattr(client.config, "base_url", ""))
    hit = _version_cache.get(key)
    if hit is not None:
        ver, ts = hit
        if time.monotonic() - ts < VERSION_TTL:
            return ver
    if _base_declares_v2(key):
        # Explicit operator config beats inference: no probe is even sent,
        # so a compat shim on /global/health cannot misroute us to v1.
        _version_cache[key] = ("v2", time.monotonic())
        return "v2"
    try:
        resp = await client._http.get("/global/health")
    except Exception:
        return "v1"
    if resp.status_code < 400:
        ver = "v1"
    elif resp.status_code not in (404, 401, 403):
        ver = "v1"
    else:
        # Possible v2: confirm so plain v1 mocks (404 for everything) and
        # non-JSON origins stay v1.
        try:
            confirm = await client._http.get("/api/info")
        except Exception:
            return "v1"
        ver = "v2" if _confirm_is_v2(confirm) else "v1"
    _version_cache[key] = (ver, time.monotonic())
    return ver


def unwrap(payload: Any) -> Any:
    """Unwrap v2 ``{"data": ...}`` envelopes (with or without ``location``)."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def unwrap_list(payload: Any) -> tuple[Any, dict]:
    """Unwrap a v2 list envelope, preserving siblings (location/pagination).

    Returns ``(items, extra)`` where ``extra`` holds every envelope key
    besides ``data``. Non-list-data payloads pass through as
    ``(payload, {})`` so v1 shapes (plain lists) are untouched.
    """
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        extra = {k: v for k, v in payload.items() if k != "data"}
        return payload["data"], extra
    return payload, {}


def unwrap_obj(payload: Any) -> tuple[Any, dict]:
    """Unwrap a v2 single-object envelope, preserving siblings."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        extra = {k: v for k, v in payload.items() if k != "data"}
        return payload["data"], extra
    return payload, {}


async def bridge_request(
    client: Any,
    method: str,
    v1_path: str,
    route_v2: str,
    *,
    json: Any = None,
    params: dict | None = None,
    unwrap_v2: bool = True,
) -> Any:
    """Single dispatch for the common version-branched request shape.

    v1: ``request(method, v1_path, …)`` — byte-identical to pre-v2 code
    (``json=None, params=None`` serialize exactly like omitted arguments).
    v2: ``request(method, route_v2, …)`` + :func:`unwrap` unless ``unwrap_v2``
    is False (for endpoints whose v1 branch also returns the raw payload).
    Auth rides the client's configured credentials on both paths (base
    behavior). Only for tools whose v1/v2 bodies and params are identical;
    anything with body mapping keeps an explicit branch (which must wrap
    its route with :func:`v2_path` too).
    """
    if await get_version(client) == "v2":
        resp = await client.request(
            method, v2_path(client, route_v2), json=json, params=params
        )
        return unwrap(resp) if unwrap_v2 else resp
    return await client.request(method, v1_path, json=json, params=params)


def map_prompt_body(
    text: str,
    *,
    model: dict | None = None,
    agent: str | None = None,
    system: str | None = None,
    noReply: bool = False,
    format: dict | None = None,
    files: list[str] | None = None,
    delivery: str | None = None,
) -> dict:
    """Build a v2 ``POST /api/session/{id}/prompt`` body, failing loudly.

    The v2 prompt schema carries ``{text, files, agents, skills, metadata,
    delivery, resume}`` — no model pin, no agent switch, no system injection,
    no noReply/format. Silently dropping those would misbill or misbehave
    with no error, so any set field raises a clean MCP isError instead.
    Pair validation (lone providerID/modelID) stays in ``model_ref``, called
    by the tool before this mapper, exactly as on v1.
    ``files`` is a list of URIs -> ``[PromptInput.FileAttachment{uri}]`` and
    ``delivery`` is the enum ``steer|queue`` (schema: Session.Inbox.Delivery).
    """
    problems: list[str] = []
    if model is not None:
        problems.append("providerID/modelID")
    if agent is not None:
        problems.append("agent")
    if system is not None:
        problems.append("system")
    if noReply:
        problems.append("noReply")
    if format is not None:
        problems.append("format")
    if problems:
        raise v2_error(f"prompt({', '.join(problems)})")
    if delivery is not None and delivery not in ("steer", "queue"):
        raise ValueError('delivery must be "steer" or "queue"')
    body: dict = {"text": text}
    if files:
        body["files"] = [{"uri": uri} for uri in files]
    if delivery:
        body["delivery"] = delivery
    return body


def map_session_create(
    *,
    title: str | None = None,
    providerID: str | None = None,
    modelID: str | None = None,
    agent: str | None = None,
    system: str | None = None,
    directory: str | None = None,
) -> dict:
    """Build a v2 ``POST /api/session`` body.

    v2 create accepts ``{id, title, agent, model, location, metadata,
    permissions}`` — ``additionalProperties: false`` and NO query parameters,
    so ``?directory=`` is silently ignored (measured on v2.0.19: the session
    landed in the serve default ``/home/dev``). The project folder therefore
    rides in the body as ``location: {directory}``.

    ``model`` is ``Model.Ref`` = ``{providerID, id}``: the v1 key ``modelID``
    is rejected with ``400 Missing key at ["model"]["id"]`` (measured).
    Pair validation reuses ``model_ref`` (same ``ValueError`` as v1 on a lone
    value); ``system`` has no v2 equivalent and raises instead of being
    dropped.
    """
    body: dict = {}
    if title:
        body["title"] = title
    if agent:
        body["agent"] = agent
    model = model_ref(providerID, modelID)
    if model:
        body["model"] = {"providerID": model["providerID"], "id": model["modelID"]}
    if directory:
        body["location"] = {"directory": directory}
    if system is not None:
        raise v2_error("delegate_task(system)")
    return body


def v2_error(tool: str) -> RuntimeError:
    """Clean MCP isError for tools/fields with no v2 equivalent (no traceback leak)."""
    return RuntimeError(f"{tool} {V2_NOT_AVAILABLE}")


# -- request bodies (live openapi.json of v2.0.19) --------------------------

PERMISSION_DECISIONS = ("once", "always", "reject")


def map_command_body(
    command: str, arguments: str = "", agent: str | None = None
) -> dict:
    """v2 ``POST /api/session/{id}/command`` body: ``{name, text}``.

    v1 sent ``{command, arguments, agent}``; v2 renamed the first two (the
    required one is ``name``) and has no ``agent`` field, with
    ``additionalProperties: false`` a set agent would 400 — so it raises.
    """
    if agent is not None:
        raise v2_error("run_command(agent)")
    body: dict = {"name": command}
    if arguments:
        body["text"] = arguments
    return body


def map_shell_body(command: str, agent: str | None = None) -> dict:
    """v2 ``POST /api/session/{id}/shell`` body is ``{command}`` only."""
    if agent is not None:
        raise v2_error("run_shell(agent)")
    return {"command": command}


def map_permission_reply(response: str) -> dict:
    """v2 ``POST .../permission/{requestID}/reply`` body: ``{decision}``.

    Same enum values as the v1 ``response`` field (once/always/reject), but the
    key changed, so sending ``{response}`` to v2 fails the schema.
    """
    if response not in PERMISSION_DECISIONS:
        raise ValueError(
            f"response must be one of {', '.join(PERMISSION_DECISIONS)}: {response!r}"
        )
    return {"decision": response}


def map_fork_body(messageID: str | None = None) -> dict:
    """v2 ``POST /api/session/{id}/fork`` takes ``before``, not ``messageID``."""
    return {"before": messageID} if messageID else {}


def fs_location_params(path: str) -> tuple[dict, str]:
    """Map an fs path to the v2 deepObject ``location`` query params.

    The v2 filesystem routes (``style: deepObject``) resolve a path relative to
    ``location.directory``, which defaults to the serve's own dir. Measured live
    on v2.0.19: ``GET /api/fs/read/opt/.../root.json`` without a location is a
    404, while the same read with ``location[directory]=/`` is a 200. Absolute
    paths therefore split into ``location[directory]=/`` + the path without its
    leading slash; relative paths keep v1 behaviour (no location, the serve
    resolves them against its default).

    Returns ``(params, path_without_leading_slash)``.
    """
    if path.startswith("/"):
        return {"location[directory]": "/"}, path.lstrip("/")
    return {}, path


def map_form_answer(fields: Any, answers: Any) -> dict:
    """v1 question answers -> v2 ``{fieldKey: Form.Value}``.

    The v1 tool takes one entry per question, in order, each a list of chosen
    option LABELS; v2 forms are keyed by ``field.key`` and expect the option
    ``value`` (Form.Option carries both). Unanswered fields are omitted (the
    server enforces its own required fields).
    """
    if not isinstance(fields, list) or not isinstance(answers, list):
        raise TypeError("form fields and answers must both be lists")
    answer: dict = {}
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            continue
        key = field.get("key")
        if not key:
            continue
        chosen = answers[index] if index < len(answers) else []
        if isinstance(chosen, str):
            chosen = [chosen]
        if not isinstance(chosen, list):
            raise TypeError(f"answers[{index}] must be a string or a list of strings")
        values = [_form_option_value(item, field.get("options")) for item in chosen]
        values = [value for value in values if value is not None]
        if not values:
            continue
        answer[key] = values if field.get("type") == "multiselect" else values[0]
    if not answer:
        raise ValueError("no answer could be mapped: `answers` carried no usable value")
    return answer


def _form_option_value(label: Any, options: Any) -> Any:
    """Map one chosen label to the option's ``value`` (free-form text passes through)."""
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, dict):
                continue
            if option.get("label") == label or option.get("value") == label:
                return option.get("value")
    return label
