# Lark Approval MCP server for AgentCore Runtime (ARM64). Calls the Lark approval
# API directly — no CLI, no SDK.
#
# Two identities in one server: approve/reject/transfer run on the app tenant token
# (Lark accepts no user token there), while add_sign needs the caller user token from
# the custom passthrough header. See .dev/adr/0006 for why, and for the limits.
# Decision limits (AGENT_DECIDE_MAX_AMOUNT / AGENT_DECIDE_APPROVAL_CODES) are enforced
# in code, never by the model — an approval form is untrusted input.
#
# MCP contract: streamable HTTP on 0.0.0.0:8000/mcp. No sidecar — server.js reads
# the custom header directly. No lark-cli either: the approval endpoints are plain
# REST and every tool was a passthrough, so this calls Lark with fetch (built in
# since Node 18). That keeps the image to the base plus one file.
# node 24 (current LTS). The sibling lark-cli server is pinned to 20 for that CLI's
# sake; this one has no such constraint, and matching the version the guard tests run
# on locally means they exercise the same runtime that ships.
FROM public.ecr.aws/docker/library/node:24-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*


WORKDIR /app
COPY server.js /app/server.js

EXPOSE 8000

# Run as a non-root user (least privilege).
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser
ENV HOME=/home/appuser

# Liveness: any GET returns 200 "ok" (server.js). curl is installed above.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/ || exit 1

# app-id/secret + LARK_DOMAIN from env; PORT defaults to 8000. Runs as PID 1.
CMD ["node", "/app/server.js"]
