# Lark MCP server for AgentCore Runtime (ARM64), backed by the official lark-cli
# as a stateless per-request execution engine.
#
# Each tools/call reads the caller's user_access_token from a custom passthrough
# header and spawns `lark-cli` with LARKSUITE_CLI_USER_ACCESS_TOKEN set — which
# bypasses lark-cli's local AuthStore and calls Lark AS that user, per request.
# (Official lark-mcp can't do this: it validates the token against a local store
# populated only by `lark-mcp login`, rejecting externally-vaulted tokens.)
#
# MCP contract: streamable HTTP on 0.0.0.0:8000/mcp. No sidecar — server.js reads
# the custom header directly. lark-cli install follows ddpie/lark-mcp-on-agentcore
# (the `install` subcommand + ca-certificates, not plain `npm install -g`, which
# can't fetch the binary in a clean container).
FROM public.ecr.aws/docker/library/node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Official Lark CLI (pinned). The `install` subcommand fetches the platform binary;
# fall back to -g. Provides `lark-cli api <method> <path>` for every Lark endpoint,
# with per-invocation token via env var.
ARG LARK_CLI_VERSION=1.0.68
RUN npx -y @larksuite/cli@${LARK_CLI_VERSION} install 2>/dev/null || npm install -g @larksuite/cli@${LARK_CLI_VERSION}
RUN npx -y skills add larksuite/cli -y -g 2>/dev/null || true

WORKDIR /app
COPY server.js /app/server.js

EXPOSE 8000

# Run as a non-root user (least privilege). HOME must be writable for lark-cli.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser
ENV HOME=/home/appuser

# Liveness: any GET returns 200 "ok" (server.js). curl is installed above.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/ || exit 1

# app-id/secret + brand from env; PORT defaults to 8000. Runs as PID 1.
CMD ["node", "/app/server.js"]
