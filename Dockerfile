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

# Pinned by digest so the bundled Node SDK pairing is a deliberate, reproducible
# choice rather than drift under a floating tag. See the SDK upgrade below:
# supergateway proxies the MCP `initialize` exchange verbatim, so its
# HTTP-transport SDK must support every protocol version the Python side can
# negotiate or spec-compliant clients (e.g. claude.ai on 2025-11-25) connect and
# then get "no tools available" when the next request is rejected.
FROM supercorp/supergateway:uvx@sha256:2ffee900c18d8375096b392c8be15cd344535a05b753a5b182da227ab6306a15

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_TOOL_DIR=/opt/uv-tools \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    PORT=8080

WORKDIR /app

# Copy the package source (plus uv.lock). Uses the .dockerignore alongside this file.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Install from uv.lock with `uv sync --frozen`: exact locked versions, and the
# build fails on a stale lock rather than re-resolving. Entry point lands in /app/.venv/bin.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH
RUN uv python install 3.14 \
    && uv sync --frozen --no-dev --compile-bytecode --python 3.14 \
    && cronometer-api-mcp --help >/dev/null 2>&1 || true

# supergateway owns the HTTP listener and validates the Mcp-Protocol-Version
# header against its bundled @modelcontextprotocol/sdk. The base image's pinned
# SDK (1.19.1) tops out at 2025-06-18, so a client that negotiates a newer
# version at `initialize` has its follow-up requests rejected. Upgrade the SDK
# in supergateway's own node_modules to a release that covers the versions the
# Python `mcp` package negotiates.
#
# NOTE: this is a per-image mitigation, not the durable fix. supergateway
# proxies `initialize` verbatim but then validates the negotiated version
# against its own (possibly older) SDK, so the skew recurs whenever the wrapped
# server's SDK is newer. The upstream fix is to clamp/reject the negotiation at
# `initialize` inside supergateway itself:
#   https://github.com/supercorp-ai/supergateway/issues/117
RUN cd /usr/local/lib/node_modules/supergateway \
    && npm install @modelcontextprotocol/sdk@^1.29.0 --no-save --no-audit --no-fund

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
