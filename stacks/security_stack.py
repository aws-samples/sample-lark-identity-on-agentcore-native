"""Security stack — Cognito user pool + Secrets Manager slots.

Identity model: Lark is the IdP. A user's stable identity is `lark:{open_id}`,
which is used verbatim as the Cognito username. Both entrypoints (webhook
messages and the embedded web UI) resolve to the same identity, so a user's
session/workspace is shared across them.

Cognito's role here is narrow: mint a standard OIDC JWT for a Lark-authenticated
user so that AgentCore / API Gateway JWT authorizers can validate it. Passwords
are HMAC-derived (never user-chosen), so the pool is effectively a token factory.
"""

from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    aws_secretsmanager as secretsmanager,
    aws_cognito as cognito,
)
from constructs import Construct


class SecurityStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("resource_prefix") or "lark-agent"

        # --- Cognito user pool: token factory for Lark-authenticated users ---
        # username == "lark:{open_id}"; passwords are HMAC-derived by the web_api
        # Lambda, so no interactive sign-up / recovery / MFA applies.
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{prefix}-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True),
            password_policy=cognito.PasswordPolicy(
                min_length=16,
                require_lowercase=False,
                require_uppercase=False,
                require_digits=False,
                require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.NONE,
            removal_policy=RemovalPolicy.DESTROY,  # PoC — recreate freely
        )

        self.user_pool_client = self.user_pool.add_client(
            "AppClient",
            user_pool_client_name=f"{prefix}-app",
            auth_flows=cognito.AuthFlow(admin_user_password=True),
            generate_secret=False,
            # id_token_validity left at Cognito default (1h)
        )

        self.user_pool_id = self.user_pool.user_pool_id
        self.user_pool_arn = self.user_pool.user_pool_arn
        self.user_pool_client_id = self.user_pool_client.user_pool_client_id
        self.cognito_issuer_url = (
            f"https://cognito-idp.{Stack.of(self).region}.amazonaws.com/"
            f"{self.user_pool.user_pool_id}"
        )

        # --- Lark app credentials (placeholder — filled by scripts/setup-lark.sh) --
        # JSON shape: {"appId","appSecret","verificationToken","encryptKey"}
        self.lark_secret = secretsmanager.Secret(
            self,
            "LarkSecret",
            secret_name=f"{prefix}/channels/lark",
            description="Lark self-built app credentials "
            "(appId/appSecret/verificationToken/encryptKey)",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- HMAC salt for deriving deterministic Cognito passwords ---
        self.cognito_password_secret = secretsmanager.Secret(
            self,
            "CognitoPasswordSecret",
            secret_name=f"{prefix}/cognito-password-secret",
            description="HMAC salt for deriving Cognito user passwords",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=64,
                exclude_punctuation=True,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Downstream MCP tool keys (per-tenant) for the Gateway Interceptor --
        # The interceptor reads `{prefix}/tool-keys/{tenant}` and injects the key
        # into the outbound tool request. Seed a "default" tenant for the PoC.
        self.tool_keys_secret_prefix = f"{prefix}/tool-keys"
        self.tool_keys_default_secret = secretsmanager.Secret(
            self,
            "ToolKeysDefault",
            secret_name=f"{self.tool_keys_secret_prefix}/default",
            description="Downstream MCP tool API key for the 'default' tenant",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"api_key":"demo-"}',
                generate_string_key="filler",
                password_length=24,
                exclude_punctuation=True,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        CfnOutput(self, "UserPoolId", value=self.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=self.user_pool_client_id)
        CfnOutput(self, "CognitoIssuerUrl", value=self.cognito_issuer_url)
        CfnOutput(self, "LarkSecretName", value=self.lark_secret.secret_name)
