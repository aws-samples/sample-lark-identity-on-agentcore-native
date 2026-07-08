"""AgentCore stack — execution role + container image for the simple agent.

Scope is deliberately small (this is a from-scratch simple agent, not OpenClaw):
  - Execution role: Bedrock invoke, Cognito admin-auth (mint per-user JWT for
    MCP calls), Secrets read, CloudWatch logs, ECR pull.
  - Container image: built from ./agent via DockerImageAsset (ARM64), pushed to
    the CDK assets ECR repo.

The Runtime itself is NOT a CloudFormation resource in this account/region
(verified: no AWS::BedrockAgentCore::Runtime type). It is created out-of-band by
scripts/deploy.sh via `aws bedrock-agentcore-control create-agent-runtime`, using
this stack's execution role ARN and the image URI below. The resulting runtime_id
is written back into cdk.json context, from which runtime_arn is derived here for
dependent stacks (Router, WebUI).
"""

from aws_cdk import (
    CfnOutput,
    Stack,
    RemovalPolicy,
    Duration,
    aws_iam as iam,
    aws_s3 as s3,
    aws_ecr_assets as ecr_assets,
)
from constructs import Construct


class AgentCoreStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cognito_user_pool_id: str,
        cognito_client_id: str,
        cognito_issuer_url: str,
        cognito_password_secret_name: str,
        lark_secret_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        prefix = self.node.try_get_context("resource_prefix") or "lark-agent"

        # --- Execution role: what the agent container may do -----------------
        execution_role_name = f"{prefix}-execution-role-{region}"
        self.execution_role = iam.Role(
            self,
            "ExecutionRole",
            role_name=execution_role_name,
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                iam.ServicePrincipal("bedrock.amazonaws.com"),
            ),
        )

        # Bedrock model invocation (foundation models + inference profiles for
        # the global.* Sonnet 5 cross-region profile).
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/*",
                    "arn:aws:bedrock:*::inference-profile/*",
                ],
            )
        )

        # Cognito admin auth — mint a per-user JWT (username = lark:{open_id})
        # to attach as Bearer on outbound MCP/Gateway calls.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminInitiateAuth",
                    "cognito-idp:AdminGetUser",
                ],
                resources=[
                    f"arn:aws:cognito-idp:{region}:{account}:userpool/{cognito_user_pool_id}",
                ],
            )
        )

        # Secrets read — Cognito password salt + Lark creds.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:{prefix}/*",
                ],
            )
        )

        # CloudWatch logs
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:/{prefix}/*",
                    f"arn:aws:logs:{region}:{account}:log-group:/{prefix}/*:*",
                ],
            )
        )

        # AgentCore Memory (STM) — conversation history read/write. Scoped to the
        # project's memory resources (the toolkit creates <prefix>_..._mem-*).
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListActors",
                    "bedrock-agentcore:ListSessions",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{region}:{account}:memory/*",
                ],
            )
        )

        # ECR pull (agent image lives in the CDK assets repo).
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                    "ecr:BatchCheckLayerAvailability",
                ],
                resources=[
                    f"arn:aws:ecr:{region}:{account}:repository/cdk-*",
                    f"arn:aws:ecr:{region}:{account}:repository/{prefix}-*",
                    # Starter Toolkit pushes to repos named bedrock-agentcore-<agent>
                    f"arn:aws:ecr:{region}:{account}:repository/bedrock-agentcore-*",
                ],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )

        # --- Optional S3 for per-user files (kept minimal; agent may use it) --
        self.user_files_bucket = s3.Bucket(
            self,
            "UserFilesBucket",
            bucket_name=f"{prefix}-user-files-{account}-{region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,  # PoC
            auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(365))],
        )
        self.user_files_bucket.grant_read_write(self.execution_role)

        # --- Container image (ARM64) ------------------------------------------
        # Built from ./agent. deploy.sh reads this URI to create/update the runtime.
        self.agent_image = ecr_assets.DockerImageAsset(
            self,
            "AgentImage",
            directory="agent",
            platform=ecr_assets.Platform.LINUX_ARM64,
        )

        # --- Runtime ARN derived from context (populated by deploy.sh) --------
        runtime_id = self.node.try_get_context("runtime_id") or "PLACEHOLDER"
        self.runtime_arn = (
            f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}"
        )

        CfnOutput(self, "ExecutionRoleArn", value=self.execution_role.role_arn)
        CfnOutput(self, "AgentImageUri", value=self.agent_image.image_uri)
        CfnOutput(self, "UserFilesBucketName", value=self.user_files_bucket.bucket_name)
