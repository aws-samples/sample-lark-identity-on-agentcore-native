# Official Lark MCP server for AgentCore Runtime (ARM64).
# Streamable HTTP on 0.0.0.0:8000/mcp (AgentCore MCP contract). NEVER --oauth:
# per-request Authorization: Bearer passthrough is what carries each user's token.
FROM public.ecr.aws/docker/library/node:20-slim

WORKDIR /app

# Pin the version so the (undocumented) per-request Bearer passthrough behavior is stable.
ARG LARK_MCP_VERSION=0.5.1
RUN npm install -g @larksuiteoapi/lark-mcp@${LARK_MCP_VERSION}

EXPOSE 8000

# app-id/app-secret come from env (APP_ID/APP_SECRET), injected at deploy.
# --host 0.0.0.0 -p 8000 to match the Runtime contract; default tool preset.
CMD ["sh", "-c", "lark-mcp mcp -m streamable --host 0.0.0.0 -p 8000 -a \"$APP_ID\" -s \"$APP_SECRET\""]
