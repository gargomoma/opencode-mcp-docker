# opencode + MCP in one container

> [!CAUTION]
> This project has been generated with AI.

[github.com/gargomoma/opencode-mcp-docker](https://github.com/gargomoma/opencode-mcp-docker)

Run [opencode](https://opencode.ai) and expose it to any MCP client from a single
`docker compose up`. The MCP endpoint speaks **streamable-http** and is protected by the
same password as the opencode web UI.

```
:4096  opencode serve — web UI + HTTP API
:8123  MCP endpoint — http://<host>:8123/mcp
```

## Quick start

```sh
git clone https://github.com/gargomoma/opencode-mcp-docker.git
cd opencode-mcp-docker
cp .env.example .env
$EDITOR .env                       # set OPENCODE_SERVER_PASSWORD
docker compose up -d --build
```

Then, in any MCP client:

| Setting | Value |
|---|---|
| URL | `http://<host>:8123/mcp` |
| Transport | streamable-http |
| Header | `Authorization: Bearer <OPENCODE_SERVER_PASSWORD>` |

Smoke test — `401` without the password, `200` with it:

```sh
make test
```

## Client config

Hermes (`config.yaml`):

```yaml
mcp_servers:
  opencode:
    url: http://<host>:8123/mcp
    headers:
      Authorization: "Bearer ${OPENCODE_SERVER_PASSWORD}"
    connect_timeout: 90.0
    enabled: true
```

Claude Desktop / other stdio-only clients can bridge it locally:

```sh
npx -y supergateway --streamableHttp http://<host>:8123/mcp \
  --header "Authorization: Bearer <password>"
```

## What is inside

| File | Role |
|---|---|
| `Dockerfile` | `debian:trixie-slim` + opencode CLI + python3 + the bridge |
| `mcp/entrypoint.sh` | runs `opencode serve --service` and the MCP server; if either dies the container exits and the restart policy revives both |
| `mcp/serve_http.py` | FastMCP HTTP transport + password check (Bearer or Basic) |
| `mcp/opencode_mcp_bridge/` | the bridge itself — maps the opencode HTTP API to ~70 MCP tools |
| `docker-compose.yml` | ports, volumes, env |
| `Makefile` | `up`, `build`, `down`, `logs`, `sh`, `test`, `clean` |

The bridge is vendored from
[gargomoma/opencode-mcp-bridge](https://github.com/gargomoma/opencode-mcp-bridge)
(MIT), pinned at the commit that was verified in this image.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `OPENCODE_SERVER_PASSWORD` | — (required) | protects the web UI **and** the MCP endpoint |
| `SERVICE_PORT` | `4096` | opencode's port |
| `MCP_PORT` | `8123` | MCP port |
| `UID` / `GID` | `1000` | container user; match your host user so `./workspace` files stay yours |
| `OPENCODE_DEFAULT_PROVIDER_ID` / `..._MODEL_ID` | empty | defaults for the bridge's `delegate_*` tools |
| `TZ` | `UTC` | container timezone |

## Security

The MCP endpoint can **run shell commands** through opencode. With
`OPENCODE_SERVER_PASSWORD` set it answers `401` to anything without the header; with it
unset the endpoint is open (the entrypoint says so in the log). Publish the port only on
a network you trust — a tailnet, a LAN, or behind a reverse proxy with TLS.

## Updating the vendored bridge

```sh
rm -rf mcp/opencode_mcp_bridge
git clone --depth 1 https://github.com/gargomoma/opencode-mcp-bridge /tmp/b
cp -r /tmp/b/src/opencode_mcp_bridge mcp/
docker compose up -d --build
```

## Notes / gotchas

- **Pin `mcp` and `fastmcp` together.** `fastmcp 4.x` needs `mcp 2.x`; a stale `mcp 1.x`
  fails at boot with `No module named 'mcp.server.request_state'`.
- **`httpx` must be installed explicitly** — the bridge imports it directly.
- **Don't use `wait -n` here.** Compose runs the entrypoint with `/bin/sh` (dash), which
  lacks it; the poll loop in `entrypoint.sh` is the portable equivalent.
- `docker compose up --build` does **not** upgrade opencode on its own (the installer layer is
  cached) — use `make update`, see above.

## Updating opencode

Docker caches the layer that runs the official installer, so a plain
`docker compose up --build` reuses the **old** CLI. Force it:

```sh
make update          # = docker compose build --build-arg OPENCODE_REFRESH=$(date +%s) && up -d
make version         # what the container is actually running now
```

`OPENCODE_REFRESH` only busts that one layer, so the apt/pip layers stay cached and the
rebuild is quick. If you also want a newer Debian base, add `--pull`.

The bridge is unaffected by an opencode upgrade — but a new opencode can change the tool
surface it exposes (78 → 69 between 2.0.19 and 2.3.2), so re-check your client's tool list
after updating.

## License

This project is MIT — see [LICENSE](LICENSE).

[opencode](https://github.com/sst/opencode) — MIT.

Issues and PRs: <https://github.com/gargomoma/opencode-mcp-docker/issues>
