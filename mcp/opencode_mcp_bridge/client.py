"""Thin async HTTP client for the opencode serve API."""

from __future__ import annotations

import os
from typing import Any

import httpx

from .config import BridgeConfig

# Payload budgets (borrowed from hermes-opencode-mcp-bridge parser limits).
MAX_TEXT_BYTES = 100 * 1024

# Tokens que permiten escapar del endpoint previsto al construir la URL.
_UNSAFE_PATH_TOKENS = ("..", "\\", "?", "#", "//", "%", " ")


class BridgeError(RuntimeError):
    """Raised when the opencode server returns an error or is unreachable."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def check_path(path: str) -> None:
    """Rechaza rutas que permiten alcanzar otro endpoint del mismo servidor.

    httpx normaliza los segmentos `..` al construir la URL, y varias
    herramientas interpolan un `id` del cliente dentro de la ruta. Medido
    contra el serve: `permission_respond(permissionID='../../../instance/dispose')`
    acaba en POST /instance/dispose (saltandose su gate) y
    `session_delete(id='../../session/ses_otra')` borra sesiones ajenas.

    Excepcion: `GET /api/fs/read/<fichero>` (opencode v2) incrusta la ruta del
    fichero como segmentos, con percent-encoding (`%20`, ...) generado por la
    propia herramienta. Ninguna ruta v1 empieza por ese prefijo, asi que este
    caso no puede alterar el comportamiento v1: aqui se permite `%` pero se
    siguen rechazando `..`, `\\`, `?`, `#`, `//` y espacios (la herramienta
    ademas rechaza `..` antes de codificar, y codificar convierte un `%`
    literal del nombre en `%25`, inofensivo tras una sola decodificacion).
    Como las rutas v2 se relativizan a la base (`v2_path`: bajo una base
    `.../api` el prefijo viajado es `/fs/read/` y httpx reune el `/api`),
    la exencion cubre ambas formas.
    """
    if not path.startswith("/"):
        raise BridgeError(f"Internal error: request path must start with '/': {path!r}")
    if path.startswith(("/api/fs/read/", "/fs/read/")):
        suffix = path.split("/fs/read/", 1)[1]
        for token in ("..", "\\", "?", "#", "//", " "):
            if token in suffix:
                raise BridgeError(
                    f"Refusing to send unsafe request path {path!r}: identifiers must not "
                    f"contain '..', '\\\\', '?', '#', '//' or spaces."
                )
        return
    for token in _UNSAFE_PATH_TOKENS:
        if token in path:
            raise BridgeError(
                f"Refusing to send unsafe request path {path!r}: identifiers must not "
                f"contain '..', '%', '\\\\', '?', '#', '//' or spaces."
            )


class OpencodeClient:
    def __init__(
        self, config: BridgeConfig, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.config = config
        # Local bridge: nunca enrutar por HTTP_PROXY/NO_PROXY del entorno. Medido
        # con HTTP_PROXY=http://vpn:8888 y el serve fuera de no_proxy: las
        # peticiones normales pasan por el proxy, pero el SSE de /event
        # (event_poll) no llega; con trust_env=False funciona.
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            auth=config.auth,
            timeout=config.timeout,
            transport=transport,
            trust_env=False,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        content: bytes | None = None,
        headers: dict | None = None,
    ) -> Any:
        check_path(path)
        try:
            resp = await self._http.request(
                method, path, json=json, params=params, content=content, headers=headers
            )
        except httpx.ConnectError as exc:
            raise BridgeError(
                f"Cannot reach opencode server at {self.config.base_url} "
                f"(is `opencode serve` running?): {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise BridgeError(
                f"opencode server timed out after {self.config.timeout}s: {exc}"
            ) from exc
        if resp.status_code >= 400:
            hint = (
                " (unauthorized: check OPENCODE_SERVER_PASSWORD and "
                "OPENCODE_SERVER_USERNAME; Basic auth applies to opencode v2 only)"
                if resp.status_code == 401
                else ""
            )
            raise BridgeError(
                f"opencode {method} {path} failed with HTTP {resp.status_code}{hint}: "
                f"{truncate_text(resp.text)}",
                status=resp.status_code,
                body=resp.text,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        if "text/html" in resp.headers.get("content-type", ""):
            # El serve responde 200 con la SPA en rutas que no existen: sin esto,
            # un endpoint mal escrito "funcionaba" devolviendo HTML.
            raise BridgeError(
                f"opencode {method} {path} returned HTML, not API JSON: that endpoint "
                f"probably does not exist. {truncate_text(resp.text, 400)}",
                status=resp.status_code,
                body=resp.text,
            )
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def event_poll(
        self, limit: int = 20, timeout: float = 5.0, path: str = "/event"
    ) -> list[dict]:
        """Read at most `limit` SSE events from GET /event, then disconnect.

        MCP has no streaming, so this is a one-shot poll, not a subscription.
        `path` exists for opencode v2 (`/api/event`); the default keeps v1 identical.
        """
        if limit <= 0:
            return []
        check_path(path)
        events: list[dict] = []
        try:
            async with self._http.stream("GET", path, timeout=timeout) as resp:
                if resp.status_code >= 400:
                    raise BridgeError(f"GET {path} failed with HTTP {resp.status_code}")
                current: dict[str, str] = {}
                async for line in resp.aiter_lines():
                    if line.startswith(":"):
                        continue
                    if line == "":
                        if current:
                            events.append(_parse_sse_event(current))
                            current = {}
                            if len(events) >= limit:
                                break
                        continue
                    if ":" in line:
                        key, _, value = line.partition(":")
                        current[key.strip()] = value.strip()
        except BridgeError:
            raise
        except Exception as exc:  # timeouts / disconnects just end the poll
            if not events:
                raise BridgeError(f"event poll failed: {exc}") from exc
        return events


def model_ref(
    providerID: str | None, modelID: str | None, *, use_default: bool = True
) -> dict | None:
    """Payload `model` para prompt/prompt_async/session_create, o None.

    Un valor suelto es un error: antes, `providerID` sin `modelID` (o al reves)
    se descartaba en silencio y el llamante creia haber elegido modelo.

    Con `use_default=True` (por defecto) y sin argumentos se aplica el pin del
    entorno (OPENCODE_DEFAULT_PROVIDER_ID + OPENCODE_DEFAULT_MODEL_ID, ambos
    obligatorios). Un par explicito siempre gana. `use_default=False` devuelve
    None sin pin: es lo que necesitan los call sites v2 donde el cuerpo no
    puede llevar modelo (el pin va en la creacion de la sesion).
    """
    if bool(providerID) != bool(modelID):
        raise ValueError("providerID and modelID must be provided together")
    if providerID and modelID:
        return {"providerID": providerID, "modelID": modelID}
    if not use_default:
        return None
    # ponytail: env leido aqui en vez de pasarar BridgeConfig por todos los
    # call sites (v1, v2, delegate) — un proceso del bridge = un servidor MCP.
    # Upgrade: inyectar BridgeConfig.default_model_ref si algun dia hacen falta
    # dos pines distintos en el mismo proceso.
    default_provider = os.environ.get("OPENCODE_DEFAULT_PROVIDER_ID")
    default_model = os.environ.get("OPENCODE_DEFAULT_MODEL_ID")
    if default_provider and default_model:
        return {"providerID": default_provider, "modelID": default_model}
    return None


def _parse_sse_event(raw: dict[str, str]) -> dict:
    data = raw.get("data", "")
    try:
        import json as _json

        parsed = _json.loads(data)
        return {"event": raw.get("event", "message"), "data": parsed}
    except (ValueError, TypeError):
        return {"event": raw.get("event", "message"), "data": data}


def truncate_text(value: Any, max_bytes: int = MAX_TEXT_BYTES) -> str:
    s = value if isinstance(value, str) else str(value)
    raw = s.encode("utf-8")
    if len(raw) > max_bytes:
        # Corta por bytes UTF-8 reales: con len(s) el payload podia superar el
        # presupuesto anunciado (el texto no ASCII ocupa 2-4 bytes por caracter).
        return (
            raw[:max_bytes].decode("utf-8", "ignore")
            + f"... [truncated at {max_bytes} bytes]"
        )
    return s
