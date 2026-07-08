"""WebUI stack — Lark-embedded SPA + auth/session API.

Components:
  - web_api Lambda (Lark login exchange + session bootstrap)
  - HTTP API v2 with a Cognito JWT authorizer:
      POST /api/lark/auth   -> NO authorizer (pre-login: code -> JWT)
      POST /api/session     -> JWT-authorized
      GET  /api/session     -> JWT-authorized
  - S3 + CloudFront to host the SPA (the page URL is registered as a Lark
    redirect/safe domain so h5sdk免登 works inside the Lark desktop client)

Reuses the identity table from the Router stack (passed by name/arn) so both
entrypoints share one user store.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_authorizers as apigwv2_auth,
    aws_apigatewayv2_integrations as apigwv2_int,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
)
from constructs import Construct

from stacks import retention_days


class WebUiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        runtime_arn: str,
        runtime_endpoint_qualifier: str,
        identity_table_name: str,
        identity_table_arn: str,
        lark_secret_name: str,
        cognito_user_pool_id: str,
        cognito_user_pool_arn: str,
        cognito_client_id: str,
        cognito_issuer_url: str,
        cognito_password_secret_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        prefix = self.node.try_get_context("resource_prefix") or "lark-agent"
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30
        registration_open = str(self.node.try_get_context("registration_open") or "false").lower()
        lark_api_domain = self.node.try_get_context("lark_api_domain") or "https://open.larksuite.com"
        presigned_expires = str(self.node.try_get_context("presigned_url_expires") or "300")

        # --- web_api Lambda (boto3/botocore only — no bundling needed) ---
        log_group = logs.LogGroup(
            self, "WebApiLogGroup",
            log_group_name=f"/{prefix}/lambda/web-api",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.web_api_fn = _lambda.Function(
            self, "WebApiFn",
            function_name=f"{prefix}-web-api",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda/web_api"),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                "AGENTCORE_RUNTIME_ARN": runtime_arn,
                "AGENTCORE_QUALIFIER": runtime_endpoint_qualifier,
                "IDENTITY_TABLE_NAME": identity_table_name,
                "LARK_SECRET_ID": lark_secret_name,
                "LARK_API_DOMAIN": lark_api_domain,
                "COGNITO_USER_POOL_ID": cognito_user_pool_id,
                "COGNITO_CLIENT_ID": cognito_client_id,
                "COGNITO_PASSWORD_SECRET_ID": cognito_password_secret_name,
                "REGISTRATION_OPEN": registration_open,
                "PRESIGNED_URL_EXPIRES": presigned_expires,
                "RESOURCE_PREFIX": prefix,
            },
            log_group=log_group,
        )

        # --- IAM for web_api ---
        self.web_api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "bedrock-agentcore:InvokeAgentRuntime",
                "bedrock-agentcore:InvokeAgentRuntimeForUser",
                # the browser's presigned WSS URL is signed with this role, and
                # the WebSocket upgrade authorizes this dedicated action
                "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
            ],
            resources=[runtime_arn, f"{runtime_arn}/*"],
        ))
        # presign requires signing creds; the WSS connect is authorized by the
        # runtime — grant explicit connect too.
        self.web_api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
            resources=[f"arn:aws:secretsmanager:{region}:{account}:secret:{prefix}/*"],
        ))
        # Store per-user Lark tokens (permission inheritance) under user-tokens/*.
        # CreateSecret can't be resource-scoped; Put/Tag are scoped to the path.
        self.web_api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:CreateSecret"], resources=["*"],
        ))
        self.web_api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:PutSecretValue", "secretsmanager:TagResource"],
            resources=[f"arn:aws:secretsmanager:{region}:{account}:secret:{prefix}/user-tokens/*"],
        ))
        self.web_api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "cognito-idp:AdminCreateUser", "cognito-idp:AdminSetUserPassword",
                "cognito-idp:AdminInitiateAuth", "cognito-idp:AdminGetUser",
            ],
            resources=[cognito_user_pool_arn],
        ))
        # DynamoDB identity table (imported from Router stack by ARN)
        identity_table = dynamodb.Table.from_table_arn(self, "IdentityTable", identity_table_arn)
        identity_table.grant_read_write_data(self.web_api_fn)

        # --- HTTP API + Cognito JWT authorizer ---
        user_pool = cognito.UserPool.from_user_pool_id(self, "UserPool", cognito_user_pool_id)
        user_pool_client = cognito.UserPoolClient.from_user_pool_client_id(
            self, "UserPoolClient", cognito_client_id)
        jwt_authorizer = apigwv2_auth.HttpUserPoolAuthorizer(
            "JwtAuthorizer", user_pool,
            user_pool_clients=[user_pool_client],
        )

        integration = apigwv2_int.HttpLambdaIntegration("WebApiIntegration", handler=self.web_api_fn)
        self.http_api = apigwv2.HttpApi(
            self, "WebApi", api_name=f"{prefix}-web-api",
            description="Lark web UI auth + session API",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigwv2.CorsHttpMethod.GET, apigwv2.CorsHttpMethod.POST,
                               apigwv2.CorsHttpMethod.OPTIONS],
                allow_headers=["authorization", "content-type"],
            ),
        )
        # login exchange — no JWT yet
        self.http_api.add_routes(
            path="/api/lark/auth", methods=[apigwv2.HttpMethod.POST], integration=integration,
        )
        # session routes — JWT required
        self.http_api.add_routes(
            path="/api/session", methods=[apigwv2.HttpMethod.POST, apigwv2.HttpMethod.GET],
            integration=integration, authorizer=jwt_authorizer,
        )

        # --- SPA hosting: S3 + CloudFront ---
        self.site_bucket = s3.Bucket(
            self, "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        self.distribution = cloudfront.Distribution(
            self, "SiteDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403, response_http_status=200,
                    response_page_path="/index.html"),
            ],
        )

        CfnOutput(self, "ApiUrl", value=self.http_api.url or "")
        CfnOutput(self, "SiteBucketName", value=self.site_bucket.bucket_name)
        CfnOutput(self, "SiteUrl", value=f"https://{self.distribution.distribution_domain_name}")
        CfnOutput(self, "CognitoIssuerUrl", value=cognito_issuer_url)
