#!/usr/bin/env python3
"""lark-agent on AgentCore — CDK application entry point.

A single-tenant-friendly PoC that runs a simple Python agent on Bedrock
AgentCore Runtime, reachable from Lark bot chat (webhook messages), with Lark
as the identity provider. Per-user Lark tokens live in the AgentCore Identity
Token Vault (3LO) and are injected by the Gateway into an MCP-server target —
the agent never holds a downstream credential.

Deployment is hybrid:
  Phase 1 (CDK):  Security, AgentCore base (Role/ECR/S3), Router,
                  Gateway, Observability
  Phase 2 (CLI):  AgentCore Runtime + Gateway created/updated by deploy.sh
                  (control-plane APIs), IDs fed back into cdk.json context
"""

import json
import os
import pathlib

import aws_cdk as cdk

from stacks.security_stack import SecurityStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.router_stack import RouterStack
from stacks.gateway_stack import GatewayStack
from stacks.shim_stack import ShimStack
from stacks.observability_stack import ObservabilityStack

app = cdk.App()

# Per-deployment state (runtime/gateway ids) lives outside version control —
# deploy.sh writes it, we inject it so stacks read it via try_get_context as usual.
_state = pathlib.Path(__file__).parent / ".cdk-state.json"
if _state.is_file():
    for k, v in json.loads(_state.read_text()).items():
        if v:
            app.node.set_context(k, v)

ctx = app.node.try_get_context

env = cdk.Environment(
    account=ctx("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=ctx("region") or os.environ.get("CDK_DEFAULT_REGION") or "us-west-2",
)

prefix = ctx("resource_prefix") or "lark-agent"

# --- Security: Cognito user pool + Secrets Manager slots ---
security = SecurityStack(app, f"{prefix}-security", env=env)

# --- AgentCore base: execution role, ECR image, S3 user files ---
# Runtime itself is created out-of-band by deploy.sh; runtime_arn is derived
# from cdk.json context (runtime_id) once it exists.
agentcore = AgentCoreStack(
    app,
    f"{prefix}-agentcore",
    cognito_user_pool_id=security.user_pool_id,
    cognito_client_id=security.user_pool_client_id,
    cognito_issuer_url=security.cognito_issuer_url,
    cognito_password_secret_name=security.cognito_password_secret.secret_name,
    lark_secret_name=security.lark_secret.secret_name,
    env=env,
)

# --- Shim: Lark OAuth RFC-6749 façade + 3LO return-url ---
# Created before the router so the router can consume its return_url for the
# consent-wait vault check.
shim = ShimStack(
    app,
    f"{prefix}-shim",
    lark_api_domain=ctx("lark_api_domain") or "https://open.larksuite.com",
    lark_secret_name=security.lark_secret.secret_name,
    env=env,
)

# --- Router: Lark webhook ingestion (HTTP API + Lambda + DynamoDB identity) ---
router = RouterStack(
    app,
    f"{prefix}-router",
    runtime_arn=agentcore.runtime_arn,
    runtime_endpoint_qualifier=ctx("runtime_endpoint_id") or "DEFAULT",
    lark_secret_name=security.lark_secret.secret_name,
    shim_return_url=shim.return_url,
    env=env,
)

# --- Gateway: demo tool Lambda + Gateway IAM (mcpServer target wired in Phase 3) ---
gateway = GatewayStack(app, f"{prefix}-gateway", env=env)

# --- Observability: dashboard + alarms ---
observability = ObservabilityStack(app, f"{prefix}-observability", env=env)

app.synth()
