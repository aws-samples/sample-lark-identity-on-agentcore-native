"""Shim stack — Lark OAuth shim (RFC-6749 façade) on Lambda + HTTP API.

Fronts Lark's non-standard token endpoint so AgentCore Identity can register it
as a custom OAuth2 provider (Phase 3). Also hosts the 3LO return-url that calls
CompleteResourceTokenAuth (Phase 3b). See lambda/shim/index.py.

The API's execute-api URL is the provider's issuer/authorization/token base —
no custom domain for the PoC.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
)
from constructs import Construct

from stacks import retention_days


class ShimStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, lark_api_domain: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("resource_prefix") or "lark-id"
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30

        log_group = logs.LogGroup(
            self, "ShimLog",
            log_group_name=f"/{prefix}/lambda/shim",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.shim_fn = _lambda.Function(
            self, "ShimFn",
            function_name=f"{prefix}-oauth-shim",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda/shim"),  # stdlib + boto3 only, no deps
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={"LARK_API_DOMAIN": lark_api_domain},
            log_group=log_group,
        )
        # The return-url handler completes the 3LO auth session.
        self.shim_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:CompleteResourceTokenAuth"],
            resources=["*"],
        ))

        integration = apigwv2_integrations.HttpLambdaIntegration("ShimIntegration", handler=self.shim_fn)
        self.http_api = apigwv2.HttpApi(self, "ShimApi", api_name=f"{prefix}-oauth-shim")
        self.http_api.add_routes(path="/authorize", methods=[apigwv2.HttpMethod.GET], integration=integration)
        self.http_api.add_routes(path="/token", methods=[apigwv2.HttpMethod.POST], integration=integration)
        self.http_api.add_routes(path="/return", methods=[apigwv2.HttpMethod.GET], integration=integration)

        base = (self.http_api.url or "").rstrip("/")
        self.return_url = f"{base}/return"  # consumed by the router (consent-wait)
        CfnOutput(self, "ShimIssuer", value=base)
        CfnOutput(self, "ShimAuthorizeUrl", value=f"{base}/authorize")
        CfnOutput(self, "ShimTokenUrl", value=f"{base}/token")
        CfnOutput(self, "ShimReturnUrl", value=f"{base}/return")
