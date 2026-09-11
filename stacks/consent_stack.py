"""Consent stack — the 3LO return endpoint on Lambda + HTTP API.

One route: GET /return, where AgentCore Identity sends the browser after Lark
consent so the flow can be completed and the parked turn resumed. The OAuth
authorize/token endpoints are Lark's own (v3 implements RFC 6749), registered
directly on the credential provider — see lambda/consent/index.py.

The API's execute-api URL is only the return-url base — no custom domain.
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


class ConsentStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, lark_api_domain: str,
                 lark_secret_name: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("resource_prefix") or "lark-agent"
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30

        log_group = logs.LogGroup(
            self, "ConsentLog",
            log_group_name=f"/{prefix}/lambda/consent",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )
        # The router's function name is deterministic ({prefix}-router), so this stack
        # can address it without a cross-stack reference — which matters because the
        # consent stack is created BEFORE the router (the router consumes its return
        # URL). Referencing the router construct here would be a dependency cycle.
        router_function = f"{prefix}-router"
        self.consent_fn = _lambda.Function(
            self, "ConsentFn",
            function_name=f"{prefix}-consent",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda/consent"),  # stdlib + boto3 only, no deps
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={"LARK_API_DOMAIN": lark_api_domain,
                         # Lets /return DM the user once consent completes.
                         "LARK_SECRET_ID": lark_secret_name,
                         # /return pokes this to replay the message that hit the wall.
                         "ROUTER_FUNCTION_NAME": router_function},
            log_group=log_group,
        )
        # Invoke the router for consent-resume. Scoped to that one function by ARN
        # built from the deterministic name — no router-stack reference needed.
        self.consent_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[f"arn:aws:lambda:{Stack.of(self).region}:{Stack.of(self).account}"
                       f":function:{router_function}"],
        ))
        # The return-url handler completes the 3LO auth session.
        self.consent_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:CompleteResourceTokenAuth"],
            resources=["*"],
        ))
        # CompleteResourceTokenAuth reads the provider's Identity-managed client
        # secret AS THE CALLER, so this role needs GetSecretValue on it.
        self.consent_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
            resources=[
                f"arn:aws:secretsmanager:{Stack.of(self).region}:{Stack.of(self).account}"
                ":secret:bedrock-agentcore-identity!default/oauth2/*",
                # Lark app credentials — used to DM the user after consent.
                f"arn:aws:secretsmanager:{Stack.of(self).region}:{Stack.of(self).account}"
                f":secret:{prefix}/*",
            ],
        ))

        integration = apigwv2_integrations.HttpLambdaIntegration("ConsentIntegration", handler=self.consent_fn)
        self.http_api = apigwv2.HttpApi(self, "ConsentApi", api_name=f"{prefix}-consent")
        self.http_api.add_routes(path="/return", methods=[apigwv2.HttpMethod.GET], integration=integration)

        base = (self.http_api.url or "").rstrip("/")
        self.return_url = f"{base}/return"  # consumed by the router (consent-wait)
        CfnOutput(self, "ConsentReturnUrl", value=f"{base}/return")
