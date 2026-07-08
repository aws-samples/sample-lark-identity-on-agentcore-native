#!/usr/bin/env python3
"""lark-agent on AgentCore — CDK application entry point.

A single-tenant-friendly PoC that runs a simple Python agent on Bedrock
AgentCore Runtime, reachable from two Lark entrypoints (webhook messages +
desktop-client embedded web UI), with Lark as the identity provider and
per-user identity forwarded to downstream MCP tools via an AgentCore Gateway
Request Interceptor.

Deployment is hybrid:
  Phase 1 (CDK):  Security, AgentCore base (Role/ECR/S3), Router, WebUI,
                  Gateway, Observability
  Phase 2 (CLI):  AgentCore Runtime + Gateway created/updated by deploy.sh
                  (control-plane APIs), IDs fed back into cdk.json context
"""

import os

import aws_cdk as cdk

from stacks.security_stack import SecurityStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.router_stack import RouterStack
from stacks.webui_stack import WebUiStack
from stacks.gateway_stack import GatewayStack
from stacks.observability_stack import ObservabilityStack

app = cdk.App()
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

# --- Router: Lark webhook ingestion (HTTP API + Lambda + DynamoDB identity) ---
router = RouterStack(
    app,
    f"{prefix}-router",
    runtime_arn=agentcore.runtime_arn,
    runtime_endpoint_qualifier=ctx("runtime_endpoint_id") or "DEFAULT",
    lark_secret_name=security.lark_secret.secret_name,
    env=env,
)

# --- WebUI: Lark-embedded SPA + auth/session API (JWT authorizer) ---
webui = WebUiStack(
    app,
    f"{prefix}-webui",
    runtime_arn=agentcore.runtime_arn,
    runtime_endpoint_qualifier=ctx("runtime_endpoint_id") or "DEFAULT",
    identity_table_name=router.identity_table_name,
    identity_table_arn=router.identity_table_arn,
    lark_secret_name=security.lark_secret.secret_name,
    cognito_user_pool_id=security.user_pool_id,
    cognito_user_pool_arn=security.user_pool_arn,
    cognito_client_id=security.user_pool_client_id,
    cognito_issuer_url=security.cognito_issuer_url,
    cognito_password_secret_name=security.cognito_password_secret.secret_name,
    env=env,
)

# --- Gateway: MCP Request Interceptor (per-user credential injection) + demo tool ---
gateway = GatewayStack(
    app,
    f"{prefix}-gateway",
    tool_keys_secret_prefix=security.tool_keys_secret_prefix,
    env=env,
)

# --- Observability: dashboard + alarms ---
observability = ObservabilityStack(app, f"{prefix}-observability", env=env)

app.synth()
