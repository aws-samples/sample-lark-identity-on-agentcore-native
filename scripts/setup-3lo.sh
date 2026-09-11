#!/usr/bin/env bash
# Register the AgentCore Identity pieces the agent-side 3LO flow needs:
#   1. workload identity  — the agent's identity for GetWorkloadAccessTokenForUserId,
#                           with the /return URL on its allowlist (exact-match).
#   2. OAuth2 credential provider (CustomOauth2) — Lark's own OAuth endpoints,
#                           registered directly: the v3 token endpoint implements
#                           RFC 6749 (form-encoded, standard errors), so no
#                           translation façade sits in between. The Token Vault
#                           stores/refreshes each user's Lark token here.
#
# Idempotent: existing resources are updated, not recreated. Run after ./deploy.sh base
# (needs the consent stack outputs) and before the first Lark message.
#
# Prints the provider callbackUrl — register it in the Lark console under
# Security Settings → Redirect URLs, or 3LO consent will be rejected.
#
# Usage: [PROFILE=p REGION=r] scripts/setup-3lo.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

[ -f .env ] || { echo "missing .env — copy .env.example to .env and fill it in"; exit 1; }
# Deployment target: command-line env vars win over .env, which wins over defaults.
_CLI_PROFILE="${PROFILE:-}" _CLI_REGION="${REGION:-}"
set -a; . ./.env; set +a
PROFILE="${_CLI_PROFILE:-${PROFILE:-}}"   # empty -> ambient creds (instance role / env)
REGION="${_CLI_REGION:-${REGION:-us-west-2}}"
PREFIX="lark-agent"
export AWS_REGION="$REGION"
# Credentials already in the environment outrank .env's profile.
[ -n "${AWS_ACCESS_KEY_ID:-}" ] || { [ -n "$PROFILE" ] && export AWS_PROFILE="$PROFILE"; } || true

WORKLOAD="$PREFIX-wl"
PROVIDER="$PREFIX-3lo"
LARK_AUTH_HOST="${LARK_AUTH_HOST:-https://accounts.larksuite.com}"   # CN: accounts.feishu.cn
# Lark's OAuth endpoints, registered directly — nothing of ours in the OAuth path.
# authorize was always a standard redirect. Token: v2 for now, NOT v3 — Lark's own
# authorize doc states PKCE must "暂时搭配 v2 换取 Token；后续将尽快支持最新的 Token 端点",
# and AgentCore always drives PKCE. Measured: v3 returns 20049 (PKCE code challenge
# failed) even with a correct verifier; v2 accepts form-encoded requests (undocumented
# but probed) and completes the PKCE-initiated flow. Flip LARK_TOKEN to
# "$LARK_AUTH_HOST/oauth/v3/token" once Lark wires PKCE into v3.
LARK_AUTHORIZE="$LARK_AUTH_HOST/open-apis/authen/v1/authorize"
LARK_TOKEN="${LARK_API_DOMAIN:-https://open.larksuite.com}/open-apis/authen/v2/oauth/token"

: "${LARK_APP_ID:?set LARK_APP_ID in .env}"
: "${LARK_APP_SECRET:?set LARK_APP_SECRET in .env}"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }

cfn_out() { # stack, output-key
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null
}

# --- consent endpoint (the 3LO return-url) ---------------------------------
RETURN_URL="$(cfn_out "$PREFIX-consent" ConsentReturnUrl)"
[ -n "$RETURN_URL" ] && [ "$RETURN_URL" != "None" ] || {
  echo "consent stack outputs not found — run ./deploy.sh base first"; exit 1; }

# --- 1. workload identity -------------------------------------------------
# The return URL allowlist is exact-match: the URL passed at token time must
# equal a registered entry byte for byte (no extra query string).
log "Workload identity ($WORKLOAD)"
if aws bedrock-agentcore-control get-workload-identity --name "$WORKLOAD" >/dev/null 2>&1; then
  aws bedrock-agentcore-control update-workload-identity --name "$WORKLOAD" \
    --allowed-resource-oauth2-return-urls "$RETURN_URL" >/dev/null
  echo "  updated (return URL: $RETURN_URL)"
else
  aws bedrock-agentcore-control create-workload-identity --name "$WORKLOAD" \
    --allowed-resource-oauth2-return-urls "$RETURN_URL" >/dev/null
  echo "  created (return URL: $RETURN_URL)"
fi

# --- 2. OAuth2 credential provider ---------------------------------------
# Lark publishes no OIDC discovery doc, so we pass authorizationServerMetadata —
# but the endpoints are Lark's own: the v3 token endpoint implements RFC 6749
# (verified: form-encoded and client_secret_post/basic both accepted, errors are
# non-2xx with standard error/error_description). Nothing of ours in the OAuth path.
# clientAuthenticationMethod goes on the provider config — setting the legacy
# tokenEndpointAuthMethods in the metadata too is a validation error.
log "OAuth2 credential provider ($PROVIDER) — Lark endpoints, direct"
CFG="$(LARK_AUTH_HOST="$LARK_AUTH_HOST" LARK_AUTHORIZE="$LARK_AUTHORIZE" LARK_TOKEN="$LARK_TOKEN" \
       LARK_APP_ID="$LARK_APP_ID" LARK_APP_SECRET="$LARK_APP_SECRET" python3 <<'PY'
import json, os
e = os.environ
print(json.dumps({
    "customOauth2ProviderConfig": {
        "oauthDiscovery": {
            "authorizationServerMetadata": {
                "issuer": e["LARK_AUTH_HOST"],
                "authorizationEndpoint": e["LARK_AUTHORIZE"],
                "tokenEndpoint": e["LARK_TOKEN"],
                "responseTypes": ["code"],
            }
        },
        "clientId": e["LARK_APP_ID"],
        "clientSecret": e["LARK_APP_SECRET"],
        "clientAuthenticationMethod": "CLIENT_SECRET_POST",
    }
}))
PY
)"

if aws bedrock-agentcore-control get-oauth2-credential-provider --name "$PROVIDER" >/dev/null 2>&1; then
  aws bedrock-agentcore-control update-oauth2-credential-provider \
    --name "$PROVIDER" --credential-provider-vendor CustomOauth2 \
    --oauth2-provider-config-input "$CFG" >/dev/null
  echo "  updated"
else
  aws bedrock-agentcore-control create-oauth2-credential-provider \
    --name "$PROVIDER" --credential-provider-vendor CustomOauth2 \
    --oauth2-provider-config-input "$CFG" >/dev/null
  echo "  created"
fi

CALLBACK="$(aws bedrock-agentcore-control get-oauth2-credential-provider \
  --name "$PROVIDER" --query callbackUrl --output text)"

# --- summary --------------------------------------------------------------
log "Done — register this in the Lark developer console"
echo
warn "Security Settings → Redirect URLs — add exactly:"
echo "  $CALLBACK"
echo
echo "Also confirm under Permissions & Scopes → User Token Scopes (admin approval needed):"
echo "  drive:drive, docx:document, offline_access"
echo
echo "Lark endpoints in use (registered directly, nothing of ours in between):"
echo "  authorize: $LARK_AUTHORIZE"
echo "  token    : $LARK_TOKEN"
echo "  (set LARK_AUTH_HOST=https://accounts.feishu.cn in .env for the CN edition)"

# Registry consumed by the router/agent so /auth can be per-IdP. Add an entry per
# downstream system as you register more providers.
log "IdP registry (injected into the router and agent at deploy time)"
IDP_REGISTRY="$(PROVIDER="$PROVIDER" python3 -c '
import json, os
print(json.dumps([{"key": "lark", "provider": os.environ["PROVIDER"],
                   "scopes": ["drive:drive", "docx:document", "offline_access"],
                   "label": "Lark"}]))')"
echo "  $IDP_REGISTRY"
ctx_file=".cdk-state.json"
python3 - "$ctx_file" "$IDP_REGISTRY" <<'PY'
import json, os, sys
f, reg = sys.argv[1], sys.argv[2]
d = json.load(open(f)) if os.path.isfile(f) else {}
d["idp_registry"] = reg
with open(f, "w") as fh: json.dump(d, fh, indent=2); fh.write("\n")
print(f"  saved to {f}")
PY
