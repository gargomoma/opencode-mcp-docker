#!/bin/sh
# Two processes, one container:
#   * opencode serve --service   (agent server + web UI, SERVICE_PORT)
#   * serve_http.py              (MCP over streamable-http, MCP_PORT, same password)
#
# `wait -n` is deliberately NOT used: compose runs this with /bin/sh (dash) and dash
# has no `wait -n`. The poll loop below exits as soon as either child dies, so the
# restart policy brings both back together.
set -eu

: "${SERVICE_PORT:=4096}"
: "${SERVICE_HOSTNAME:=0.0.0.0}"
: "${MCP_PORT:=8123}"
: "${MCP_HOST:=0.0.0.0}"

echo "entrypoint: opencode=$(opencode --version 2>&1 | head -1) port=$SERVICE_PORT mcp=$MCP_PORT"

# Seed the V2 service password from the environment. `opencode service set` cannot be
# used here: with no init it tries to stop its own parent (PID 1) and hangs.
if [ -n "${OPENCODE_SERVER_PASSWORD:-}" ]; then
  python3 - "$HOME/.config/opencode/service.json" <<'EOF'
import json, os, sys
path = sys.argv[1]
pw = os.environ["OPENCODE_SERVER_PASSWORD"]
try:
    with open(path) as f:
        cfg = json.load(f)
except (FileNotFoundError, ValueError):
    cfg = {}
if cfg.get("password") != pw:
    cfg["password"] = pw
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cfg, f)
    os.chmod(path, 0o600)
    print("entrypoint: service password seeded from env")
else:
    print("entrypoint: service password already matches env")
EOF
else
  echo "entrypoint: OPENCODE_SERVER_PASSWORD empty — web UI and MCP are UNAUTHENTICATED" >&2
fi

export OPENCODE_BASE_URL="http://127.0.0.1:${SERVICE_PORT}/api"
export MCP_PORT MCP_HOST

opencode serve --service --hostname "$SERVICE_HOSTNAME" --port "$SERVICE_PORT" & OC_PID=$!
python3 /opt/mcp/serve_http.py & MCP_PID=$!

trap 'kill $OC_PID $MCP_PID 2>/dev/null || true; exit 0' TERM INT
while kill -0 "$OC_PID" 2>/dev/null && kill -0 "$MCP_PID" 2>/dev/null; do sleep 2; done
kill $OC_PID $MCP_PID 2>/dev/null || true
exit 1
