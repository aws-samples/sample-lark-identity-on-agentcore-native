"""Gateway stack — IAM service role for the MCP Gateway.

The AgentCore Gateway itself is NOT a CloudFormation resource. It is created by
scripts/deploy.sh via `aws bedrock-agentcore-control create-gateway` with a
customJWTAuthorizer (Cognito) inbound and an mcpServer target (the lark-mcp
Runtime) outbound. This stack only provisions the Gateway's service role.

Identity variant: no interceptor, no tool Lambda. Downstream tools are the
official lark-mcp server on AgentCore Runtime; per-user Lark tokens are injected
by AgentCore Identity (3LO Token Vault) into the outbound Authorization: Bearer.
The gateway role needs InvokeAgentRuntime on the lark-mcp Runtime (granted in
deploy.sh once the runtime id is known, to avoid a hard dependency here).
"""

from aws_cdk import (
    CfnOutput,
    Stack,
    aws_iam as iam,
)
from constructs import Construct


class GatewayStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        prefix = self.node.try_get_context("resource_prefix") or "lark-agent"
        agentcore_principal = iam.ServicePrincipal("bedrock-agentcore.amazonaws.com")

        # Gateway service role — assumed by the Gateway to sign SigV4 calls to its
        # mcpServer target on Runtime. The specific InvokeAgentRuntime resource is
        # attached by deploy.sh after the lark-mcp runtime exists.
        self.gateway_role = iam.Role(
            self, "GatewayRole",
            role_name=f"{prefix}-gateway-role-{region}",
            assumed_by=agentcore_principal,
        )

        # Web Search connector. Its gateway lives in us-east-1 — the only region
        # offering the connector — while this role is global, so both regions are
        # allowed here. InvokeWebSearch targets a service-owned ARN: the account
        # segment is the literal "aws", not this account.
        account = Stack.of(self).account
        ws_region = "us-east-1"
        self.gateway_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeGateway"],
                resources=[
                    f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/*",
                    f"arn:aws:bedrock-agentcore:{ws_region}:{account}:gateway/*",
                ],
            )
        )
        self.gateway_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeWebSearch"],
                resources=[f"arn:aws:bedrock-agentcore:{ws_region}:aws:tool/web-search.v1"],
            )
        )

        CfnOutput(self, "GatewayRoleArn", value=self.gateway_role.role_arn)
