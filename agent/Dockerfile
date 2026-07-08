# Simple Lark agent for Bedrock AgentCore Runtime (ARM64).
# Uses uv's multi-arch base image so the uv binary matches the target platform
# (a plain `COPY --from=uv:latest` copies the build-host arch and fails on arm64).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Install deps first for layer caching
COPY requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt

COPY *.py /app/

EXPOSE 8080 18789

# AgentCore health check hits GET /ping on 8080. Run under opentelemetry-instrument
# so container stdout/traces are collected into CloudWatch (required by AgentCore).
CMD ["opentelemetry-instrument", "python", "server.py"]
