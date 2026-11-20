# opencode + MCP endpoint in one image.
#
# Minimal: Debian trixie-slim + the official opencode installer + python3 for the
# MCP bridge. Nothing else — add what your deployment needs on top.
FROM debian:trixie-slim

ARG UID=1000
ARG GID=1000

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 python3-pip bash git curl ca-certificates tar && \
    rm -rf /var/lib/apt/lists/*

# opencode CLI, normalized to /usr/local/bin so PATH never depends on the installer.
# OPENCODE_REFRESH forces this layer to re-run (Docker caches RUN forever otherwise):
#   docker compose build --build-arg OPENCODE_REFRESH=$(date +%s)
# `make update` does exactly that.
ARG OPENCODE_REFRESH=0
RUN echo "opencode install refresh: ${OPENCODE_REFRESH}" && \
    curl -fsSL https://opencode.ai/v2/install | bash && \
    mv /root/.opencode/bin/opencode /usr/local/bin/opencode && \
    opencode --version

# MCP bridge dependencies. Pinned: the mcp/fastmcp pair matters (fastmcp 4.x needs
# mcp 2.x; mixing them fails with `No module named 'mcp.server.request_state'`).
RUN pip install --break-system-packages --no-cache-dir \
        "mcp==2.2.0" "fastmcp==4.0.5" httpx uvicorn

# The bridge package (vendored) + its HTTP wrapper.
COPY mcp/ /opt/mcp/
ENV PYTHONPATH=/opt/mcp

RUN groupadd -g "$GID" dev && \
    useradd -u "$UID" -g "$GID" -m -d /home/dev -s /bin/bash dev && \
    mkdir -p /home/dev/.local/share/opencode /home/dev/.local/state/opencode \
             /home/dev/.config/opencode /workspace && \
    chown -R dev:dev /home/dev /workspace

ENV HOME=/home/dev \
    SERVICE_PORT=4096 \
    MCP_PORT=8123 \
    MCP_HOST=0.0.0.0

USER dev
WORKDIR /workspace

EXPOSE 4096 8123
ENTRYPOINT ["/bin/sh", "/opt/mcp/entrypoint.sh"]
