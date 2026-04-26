# Cronometer MCP image, wrapped with supergateway.
#
# Contract (same for every MCP hosted by this gateway):
#   - Exposes MCP streamable-HTTP on 0.0.0.0:$PORT at /mcp
#   - Health endpoint at /healthz
#   - stdio-speaking MCP process is spawned by supergateway as a child;
#     supergateway handles protocol translation, session binding, and
#     server->client SSE notifications.
#
# The gateway reverse-proxies HTTP into this container. No stdio, Docker
# attach, or EOF games are required anywhere.
#
# Base image: supercorp/supergateway:uvx (Alpine + Node 20 + uv).
# We install Python 3.14 via uv (cronometer-api-mcp requires >=3.14).

FROM supercorp/supergateway:uvx

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_TOOL_DIR=/opt/uv-tools \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    PORT=8080

WORKDIR /app

# Copy the package source. Uses the .dockerignore alongside this file.
COPY pyproject.toml README.md ./
COPY src ./src

# Pre-install Python 3.14 into a deterministic location, then install the MCP
# as a uv tool so its console entry point (`cronometer-api-mcp`) lands in
# /usr/local/bin on $PATH.
RUN uv python install 3.14 \
    && uv tool install --python 3.14 . \
    && cronometer-api-mcp --help >/dev/null 2>&1 || true

# supergateway wraps the stdio MCP; Nomad (or docker run -p) remaps $PORT.
# --stateful enables Mcp-Session-Id semantics per the MCP streamable-HTTP spec.
# --sessionTimeout is unused here because gateway owns reaping via Nomad;
#   we still set a generous value so a forgotten session eventually self-heals.
ENTRYPOINT ["/bin/sh", "-c", "exec supergateway \
  --stdio 'cronometer-api-mcp' \
  --outputTransport streamableHttp \
  --stateful \
  --streamableHttpPath /mcp \
  --healthEndpoint /healthz \
  --port \"${PORT}\" \
  --sessionTimeout 3600000 \
  --logLevel info"]
