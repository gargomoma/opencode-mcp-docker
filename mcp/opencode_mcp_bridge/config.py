"""Configuration from environment variables and CLI flags."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass
class BridgeConfig:
    base_url: str = "http://127.0.0.1:4096"
    username: str = "opencode"
    password: str | None = None
    # None = sin timeout (es lo que acepta httpx para no limitar). Un `--timeout 0`
    # o OPENCODE_TIMEOUT=0 significa exactamente eso: httpx interpreta timeout=0
    # como timeout inmediato, no como "sin límite".
    timeout: float | None = 60.0
    enable_tui_tools: bool = False
    enable_destructive: bool = False
    # B2 multi-project routing: opencode binary used to spawn per-directory
    # `opencode serve` instances on demand.
    opencode_bin: str = "opencode"
    # Explicit project_dir -> base_url map for pre-started serve instances.
    # Via OPENCODE_SERVERS_JSON, e.g. {"C:/repos/foo": "http://127.0.0.1:4097"}.
    servers: dict = field(default_factory=dict)
    # Seconds to wait for an on-demand spawned server to become healthy
    # (None = sin límite: se espera a que responda o a que el hijo muera).
    spawn_timeout: float | None = 30.0
    # Where delegated session_id -> server mappings persist (best-effort).
    session_registry: str = os.path.join(
        os.path.expanduser("~"), ".config", "opencode-mcp-bridge", "sessions.json"
    )

    @classmethod
    def from_env(cls, overrides: dict | None = None) -> BridgeConfig:
        env = os.environ
        over = overrides or {}
        cfg = cls(
            base_url=over.get("base_url")
            or env.get("OPENCODE_BASE_URL", "http://127.0.0.1:4096"),
            username=over.get("username")
            or env.get("OPENCODE_SERVER_USERNAME", "opencode"),
            password=over.get("password") or env.get("OPENCODE_SERVER_PASSWORD"),
            timeout=_seconds(
                over["timeout"] if "timeout" in over else env.get("OPENCODE_TIMEOUT"),
                60.0,
            ),
            enable_tui_tools=_as_bool(
                over["enable_tui_tools"]
                if over.get("enable_tui_tools") is not None
                else env.get("OPENCODE_ENABLE_TUI_TOOLS", "0")
            ),
            enable_destructive=_as_bool(
                over["enable_destructive"]
                if over.get("enable_destructive") is not None
                else env.get("OPENCODE_ENABLE_DESTRUCTIVE", "0")
            ),
            opencode_bin=over.get("opencode_bin")
            or env.get("OPENCODE_BIN", "opencode"),
            servers=over["servers"]
            if over.get("servers") is not None
            else _parse_servers_map(env.get("OPENCODE_SERVERS_JSON", "")),
            spawn_timeout=_seconds(
                over["spawn_timeout"]
                if "spawn_timeout" in over
                else env.get("OPENCODE_SPAWN_TIMEOUT"),
                30.0,
            ),
            session_registry=over.get("session_registry")
            or env.get(
                "OPENCODE_SESSION_REGISTRY",
                os.path.join(
                    os.path.expanduser("~"),
                    ".config",
                    "opencode-mcp-bridge",
                    "sessions.json",
                ),
            ),
        )
        cfg.base_url = cfg.base_url.rstrip("/")
        cfg.servers = {normalize_dir(k): v.rstrip("/") for k, v in cfg.servers.items()}
        return cfg

    @property
    def auth(self) -> tuple[str, str] | None:
        if self.password:
            return (self.username, self.password)
        return None


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _seconds(value: object, default: float) -> float | None:
    """Segundos desde entorno/CLI. Sin valor -> default; `0` -> None (sin límite).

    Con truthiness, un `0` explicito se descartaba en silencio y volvia el default.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    seconds = float(value)
    return None if seconds == 0 else seconds


def normalize_dir(path: str) -> str:
    """Absolute, normalized form used as the routing key for project dirs."""
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


def http_dir(path: str) -> str:
    """Ruta absoluta para enviar a opencode y guardar en el registro.

    Preserva mayusculas (normcase las minusculizaria en Windows y opencode no
    resolveria la ruta); normalize_dir() se queda SOLO como clave de routing.
    """
    return os.path.abspath(os.path.expanduser(path))


def _parse_servers_map(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"OPENCODE_SERVERS_JSON is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise TypeError(
            "OPENCODE_SERVERS_JSON must be a JSON object of {project_dir: base_url}"
        )
    return {str(k): str(v) for k, v in parsed.items()}
