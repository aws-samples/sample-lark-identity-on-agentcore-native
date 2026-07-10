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

# Run as a non-root user (least privilege; the app needs no root capability).
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

# Liveness: same /ping the AgentCore platform probes. Python-only (no curl in slim base).
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/ping', timeout=2).status==200 else 1)"]

# AgentCore health check hits GET /ping on 8080. Run under opentelemetry-instrument
# so container stdout/traces are collected into CloudWatch (required by AgentCore).
CMD ["opentelemetry-instrument", "python", "server.py"]
