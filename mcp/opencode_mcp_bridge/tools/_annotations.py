"""Annotations MCP por clase de herramienta.

Las derivadas del metodo HTTP se aplican con scripts (ver oc_review_probes/annotate_codemod.py);
estas constantes son la fuente unica de lo que significa cada clase.
"""

from mcp.types import ToolAnnotations

# Solo lee del serve (GET): un cliente puede auto-aprobar estas.
READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
# Solo lee, y ni siquiera del serve (estado local del puente).
READ_LOCAL = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# Escribe (POST/PUT/PATCH) pero de forma aditiva: manda prompts, crea sesiones...
WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
# Borra o mata algo sin vuelta atras (DELETE, run_shell, dispose).
DESTRUCT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)

__all__ = ["DESTRUCT", "READ", "READ_LOCAL", "WRITE"]
