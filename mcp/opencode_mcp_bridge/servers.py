"""B2 multi-project routing: resolve a working folder to an opencode serve instance.

Resolution order for a project_dir:
1. Explicit map (OPENCODE_SERVERS_JSON) -> pre-started serve instance.
2. Reuse an already-spawned/managed instance for that directory.
3. Spawn `opencode serve --port <free> --hostname 127.0.0.1` with cwd=project_dir
   and wait until /global/health responds.

Spawned children inherit this process's environment, so OPENCODE_SERVER_PASSWORD
automatically protects them too, and the bridge authenticates with the same
credentials it uses for the default server.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable

from .client import BridgeError, OpencodeClient
from .config import BridgeConfig, normalize_dir
from .v2 import v2_path

SPAWN_ATTEMPTS = 3


class ServerManager:
    def __init__(
        self,
        config: BridgeConfig,
        client_factory: Callable[[str], OpencodeClient] | None = None,
    ):
        self.config = config
        self._client_factory = client_factory or (
            lambda base_url: OpencodeClient(_with_base_url(config, base_url))
        )
        # Normalize here too: configs built directly (not via from_env) skip normalization.
        self._static = {
            normalize_dir(k): v.rstrip("/") for k, v in (config.servers or {}).items()
        }
        self._clients: dict[str, OpencodeClient] = {}  # base_url -> client
        self._by_dir: dict[str, str] = {}  # normalized dir -> base_url
        self._spawned: dict[str, subprocess.Popen] = {}  # base_url -> process
        self._log_paths: dict[str, str] = {}  # base_url -> child log file
        self._registry: dict[str, dict] = {}  # session_id -> {base_url, project_dir}
        # ponytail: lock por proceso. Dos procesos del puente sobre el mismo directorio
        # nuevo todavia podrian lanzar dos serves; para eso haria falta un lock de
        # fichero (como el del registro) y no compensa para un caso raro.
        self._spawn_lock = asyncio.Lock()
        self._load_registry()
        atexit.register(self.shutdown)

    # -- routing -----------------------------------------------------------
    async def resolve(self, project_dir: str) -> OpencodeClient:
        """Return a client for the serve instance owning project_dir."""
        key = normalize_dir(project_dir)
        # 1. Mapeo explicito: la clave configurada manda aunque no exista aqui.
        #    Un serve remoto (OPENCODE_SERVERS_JSON) resuelve la ruta en SU
        #    filesystem, asi que un isdir local la rechazaria por error.
        if key in self._static:
            return self._client_for(self._static[key])
        # 2. Subcarpeta de una carpeta mapeada: reutiliza el serve de la carpeta
        # padre más específica. Exige separador de ruta para que `/a/bc` no
        # cuente como dentro de `/a/b`. No exige isdir: una subcarpeta de un
        # directorio mapeado remoto tampoco existe en local.
        best: str | None = None
        for parent in self._static:
            prefix = parent if parent.endswith(os.sep) else parent + os.sep
            if key.startswith(prefix) and (best is None or len(parent) > len(best)):
                best = parent
        if best is not None:
            return self._client_for(self._static[best])
        # 3. Sin mapeo: aqui si tiene que existir (o se lanzaria un serve con
        # cwd inexistente).
        if not os.path.isdir(key):
            raise BridgeError(
                f"project_dir does not exist or is not a directory: {project_dir}"
            )
        if key in self._by_dir:
            return self._client_for(self._by_dir[key])
        async with self._spawn_lock:
            # Dos delegate_task concurrentes al mismo directorio nuevo lanzaban dos
            # serves (uno quedaba huerfano hasta el atexit): recomprobar aqui dentro.
            if key in self._by_dir:
                return self._client_for(self._by_dir[key])
            base_url = await self._spawn(key)
            self._by_dir[key] = base_url
        return self._client_for(base_url)

    def default_client(self) -> OpencodeClient:
        return self._client_for(self.config.base_url)

    def client_for_session(self, session_id: str) -> OpencodeClient:
        entry = self._registry.get(session_id)
        if not isinstance(entry, dict):
            raise BridgeError(
                f"Unknown session '{session_id}': it was not created via delegate_task "
                f"(or the registry was lost). Use session_list on the right server, or delegate again."
            )
        base_url = entry.get("base_url")
        # El registro puede apuntar a un puerto efimero de un serve que ya no
        # existe (medido: una entrada 127.0.0.1:4200 muerta ganaba al mapa vivo).
        # Si el directorio esta mapeado o ya resuelto, esa es la URL buena.
        # ponytail: solo corrige el caso "el serve bueno ya se conoce"; un serve
        # lanzado a demanda y muerto sigue necesitando un delegate_task nuevo.
        project_dir = entry.get("project_dir")
        if project_dir:
            key = normalize_dir(str(project_dir))
            live = self._static.get(key) or self._by_dir.get(key)
            if live:
                base_url = live
        if not isinstance(base_url, str) or not base_url:
            raise BridgeError(
                f"Session '{session_id}' has no usable server URL in "
                f"{self.config.session_registry}: delete that entry or delegate again."
            )
        return self._client_for(base_url)

    def session_info(self, session_id: str) -> dict | None:
        return self._registry.get(session_id)

    def remember_session(
        self, session_id: str, base_url: str, project_dir: str
    ) -> None:
        entry = {"base_url": base_url, "project_dir": project_dir}
        with self._registry_lock():
            # Relee antes de escribir: otro proceso del puente puede haber
            # añadido sus propias sesiones entretanto.
            data = self._read_registry()
            data[session_id] = entry
            self._registry = data
            self._save_registry()

    def managed_servers(self) -> list[dict]:
        out = []
        for directory, base_url in sorted(self._by_dir.items()):
            entry = {
                "project_dir": directory,
                "base_url": base_url,
                "spawned_by_bridge": base_url in self._spawned,
            }
            if base_url in self._log_paths:
                entry["log"] = self._log_paths[base_url]
            out.append(entry)
        for directory, base_url in sorted(self._static.items()):
            out.append(
                {
                    "project_dir": directory,
                    "base_url": base_url,
                    "spawned_by_bridge": False,
                }
            )
        return out

    async def aclose(self) -> None:
        """Cierra los clientes HTTP (los hijos los mata el atexit/shutdown)."""
        for client in list(self._clients.values()):
            with contextlib.suppress(Exception):
                await client.close()
        self._clients.clear()

    # -- internals ----------------------------------------------------------
    def _client_for(self, base_url: str) -> OpencodeClient:
        base_url = base_url.rstrip("/")
        if base_url not in self._clients:
            self._clients[base_url] = self._client_factory(base_url)
        return self._clients[base_url]

    async def _spawn(self, project_dir: str) -> str:
        """Lanza `opencode serve` con reintento.

        `_free_port()` cierra el socket antes del Popen, asi que otro proceso puede
        quedarse ese puerto en el hueco y el hijo muere al arrancar. Un fallo que no
        sea del puerto (binario ausente, cwd invalido) se propaga igual: solo cuesta
        el retardo.
        """
        last_error: Exception | None = None
        for attempt in range(SPAWN_ATTEMPTS):
            try:
                return await self._spawn_once(project_dir)
            except BridgeError as exc:
                text = str(exc)
                if "not found" in text or "Cannot spawn" in text:
                    raise
                last_error = exc
                if attempt + 1 < SPAWN_ATTEMPTS:
                    await asyncio.sleep(0.3 * (attempt + 1))
        raise last_error or BridgeError(
            f"Cannot spawn `opencode serve` for {project_dir}"
        )

    async def _spawn_once(self, project_dir: str) -> str:
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        cmd = [
            self.config.opencode_bin,
            "serve",
            "--port",
            str(port),
            "--hostname",
            "127.0.0.1",
        ]
        # Salida del hijo a un fichero: con DEVNULL, un "exited early" no decia por que.
        log_path = self._log_path(base_url)
        log = _open_child_log(log_path)
        try:
            proc = subprocess.Popen(  # noqa: ASYNC220 - spawn is fast/non-blocking; health wait is async
                cmd,
                cwd=project_dir,
                stdout=log or subprocess.DEVNULL,
                stderr=subprocess.STDOUT if log else subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise BridgeError(
                f"Cannot spawn `opencode serve`: binary '{self.config.opencode_bin}' not found. "
                f"Install opencode or set OPENCODE_BIN / pre-start a server and map it via OPENCODE_SERVERS_JSON."
            ) from exc
        except OSError as exc:
            raise BridgeError(
                f"Cannot spawn `opencode serve` in {project_dir}: {exc}"
            ) from exc
        finally:
            if log is not None:
                log.close()
        self._spawned[base_url] = proc
        if log is not None:
            self._log_paths[base_url] = log_path
        client = self._client_for(base_url)
        try:
            await self._wait_healthy(client, project_dir)
        except Exception:
            self._kill(base_url)
            raise
        return base_url

    def _log_path(self, base_url: str) -> str:
        port = base_url.rsplit(":", 1)[-1]
        folder = os.path.dirname(self.config.session_registry) or "."
        return os.path.join(folder, f"spawn-{port}.log")

    async def _wait_healthy(self, client: OpencodeClient, project_dir: str) -> None:
        base_url = client.config.base_url
        deadline = None
        if self.config.spawn_timeout is not None:
            deadline = asyncio.get_event_loop().time() + self.config.spawn_timeout
        last_error: Exception | None = None
        while True:
            proc = self._spawned.get(base_url)
            if proc is not None and proc.poll() is not None:
                raise BridgeError(
                    f"`opencode serve` for {project_dir} exited early (code {proc.returncode}). "
                    f"Output: {self._log_paths.get(base_url, 'not captured')}"
                )
            try:
                # v1 health first (identical path as before); on 404 fall back
                # to the v2 info endpoint so on-demand v2 serves also become
                # ready. Auth rides the client's configured credentials.
                try:
                    await client.request("GET", "/global/health")
                except BridgeError as exc:
                    if exc.status != 404:
                        raise
                    await client.request("GET", v2_path(client, "/api/info"))
                return
            except BridgeError as exc:
                last_error = exc
            if deadline is not None and asyncio.get_event_loop().time() >= deadline:
                raise BridgeError(
                    f"`opencode serve` for {project_dir} did not become healthy within "
                    f"{self.config.spawn_timeout}s: {last_error} "
                    f"(child log: {self._log_paths.get(base_url, 'not captured')})"
                ) from last_error
            await asyncio.sleep(0.5)

    def _kill(self, base_url: str) -> None:
        proc = self._spawned.pop(base_url, None)
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - best-effort cleanup during shutdown
            try:
                proc.kill()
            except Exception:  # noqa: BLE001, S110 - process may already be gone
                pass

    def shutdown(self) -> None:
        for base_url in list(self._spawned):
            self._kill(base_url)

    # -- session registry persistence ----------------------------------------
    def _load_registry(self) -> None:
        self._registry = self._read_registry()

    def _read_registry(self) -> dict[str, dict]:
        """Lee y valida: fuera entradas que no sean dict o sin base_url usable.

        Una entrada dict sin `base_url` reventaba con KeyError (no BridgeError)
        al consultar su sesion.
        """
        try:
            with open(self.config.session_registry, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(k): v
            for k, v in data.items()
            if isinstance(v, dict)
            and isinstance(v.get("base_url"), str)
            and v["base_url"]
        }

    def _save_registry(self) -> None:
        """Escritura atomica (tmp + os.replace) y permisos de usuario."""
        path = self.config.session_registry
        tmp = f"{path}.tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._registry, fh, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            # stderr: stdout es el protocolo MCP.
            print(
                f"[opencode-mcp-bridge] cannot persist session registry {path}: {exc}",
                file=sys.stderr,
            )
            return
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    @contextlib.contextmanager
    def _registry_lock(self):
        """Lock entre procesos del mismo host, best-effort.

        fcntl no existe en Windows (el README lo documenta) y el puente no
        necesita serializar nada remoto: si no hay fcntl se sigue sin lock.
        """
        try:
            import fcntl
        except ImportError:
            yield
            return
        path = f"{self.config.session_registry}.lock"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            handle = open(path, "a+", encoding="utf-8")  # noqa: SIM115 - vive mientras dura el lock
        except OSError:
            yield
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(Exception):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _with_base_url(config: BridgeConfig, base_url: str) -> BridgeConfig:
    return BridgeConfig(
        base_url=base_url,
        username=config.username,
        password=config.password,
        timeout=config.timeout,
        enable_tui_tools=config.enable_tui_tools,
        enable_destructive=config.enable_destructive,
        opencode_bin=config.opencode_bin,
        servers=config.servers,
        spawn_timeout=config.spawn_timeout,
        session_registry=config.session_registry,
    )


def _open_child_log(log_path: str):
    """Log del hijo en append, o None si no se puede crear.

    Una apertura trivial en el hilo del evento (no hay E/S real: nadie escribe
    todavia) y el hijo hereda el fd, que es justo lo que hace falta antes del Popen.
    """
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        return open(log_path, "ab")  # lo cierra _spawn_once tras lanzar el hijo
    except OSError:
        return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


__all__ = ["ServerManager"]
