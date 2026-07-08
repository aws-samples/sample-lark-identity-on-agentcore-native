#!/usr/bin/env bash
# Run all unit tests. Each suite runs in its own process because the three
# Lambda/agent dirs each define an `index.py`/`identity.py` — running them in a
# single pytest session would cross-import the wrong module.
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

echo "== agent =="
uv run --with boto3 --with aiohttp --with pytest python -m pytest agent/test_agent.py -q

echo "== router =="
uv run --with cryptography --with boto3 --with pytest python -m pytest lambda/router/test_router.py -q

echo "== web_api =="
uv run --with boto3 --with pytest python -m pytest lambda/web_api/test_web_api.py -q

echo "== all green =="
