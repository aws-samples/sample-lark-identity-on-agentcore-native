"""Gateway stack — demo tool Lambda + IAM for the MCP Gateway.

The AgentCore Gateway itself is NOT a CloudFormation resource in this region
(verified). It is created by scripts/deploy.sh via
`aws bedrock-agentcore-control create-gateway` with:
  - authorizerConfiguration.customJWTAuthorizer -> Cognito discovery + client
  - a Lambda target -> the demo tool Lambda

Identity variant: no interceptor. Per-user identity/credentials are handled
natively by AgentCore Identity (Token Vault + OAuth provider); this stack will
be reworked in Phase 3 to the mcpServer-target shape. The tool Lambda below is
transitional — its Lark logic moves into the MCP server in Phase 2.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
)
from constructs import Construct

from stacks import retention_days


class GatewayStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        prefix = self.node.try_get_context("resource_prefix") or "lark-agent"
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30

        agentcore_principal = iam.ServicePrincipal("bedrock-agentcore.amazonaws.com")
        gateway_source_arn = f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/*"

        # --- Demo tool Lambda (Gateway target) ---
        tool_log = logs.LogGroup(
            self, "ToolLog",
            log_group_name=f"/{prefix}/lambda/tools",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )
        lark_api_domain = self.node.try_get_context("lark_api_domain") or "https://open.larksuite.com"
        self.tool_fn = _lambda.Function(
            self, "ToolFn",
            function_name=f"{prefix}-demo-tool",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda/tools"),
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={
                "RESOURCE_PREFIX": prefix,
                "LARK_API_DOMAIN": lark_api_domain,
                "LARK_SECRET_ID": f"{prefix}/channels/lark",
            },
            log_group=tool_log,
        )
        self.tool_fn.add_permission(
            "AllowGatewayInvokeTool",
            principal=agentcore_principal,
            action="lambda:InvokeFunction",
            source_arn=gateway_source_arn,
        )
        # list_my_docs acts as the end-user: read their Lark token + app creds,
        # and refresh (PutSecretValue) when the user_access_token expires.
        self.tool_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[
                f"arn:aws:secretsmanager:{region}:{account}:secret:{prefix}/user-tokens/*",
                f"arn:aws:secretsmanager:{region}:{account}:secret:{prefix}/channels/lark-*",
            ],
        ))
        self.tool_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:PutSecretValue"],
            resources=[f"arn:aws:secretsmanager:{region}:{account}:secret:{prefix}/user-tokens/*"],
        ))

        # --- Gateway service role (assumed by the Gateway to call the target) ---
        self.gateway_role = iam.Role(
            self, "GatewayRole",
            role_name=f"{prefix}-gateway-role-{region}",
            assumed_by=agentcore_principal,
        )
        self.gateway_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[self.tool_fn.function_arn],
        ))

        CfnOutput(self, "ToolFnArn", value=self.tool_fn.function_arn)
        CfnOutput(self, "GatewayRoleArn", value=self.gateway_role.role_arn)
