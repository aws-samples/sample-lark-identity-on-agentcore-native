#!/usr/bin/env bash
# Register the AgentCore Identity pieces the agent-side 3LO flow needs:
#   1. workload identity  — the agent's identity for GetWorkloadAccessTokenForUserId,
#                           with the shim's /return URL on its allowlist (exact-match).
#   2. OAuth2 credential provider (CustomOauth2) — Lark behind the RFC-6749 shim; the
#                           Token Vault stores/refreshes each user's Lark token here.
#
# Idempotent: existing resources are updated, not recreated. Run after ./deploy.sh base
# (needs the shim stack outputs) and before the first Lark message.
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

: "${LARK_APP_ID:?set LARK_APP_ID in .env}"
: "${LARK_APP_SECRET:?set LARK_APP_SECRET in .env}"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }

cfn_out() { # stack, output-key
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null
}

# --- shim endpoints (the provider's authorization server) -----------------
SHIM_ISSUER="$(cfn_out "$PREFIX-shim" ShimIssuer)"
SHIM_AUTHORIZE="$(cfn_out "$PREFIX-shim" ShimAuthorizeUrl)"
SHIM_TOKEN="$(cfn_out "$PREFIX-shim" ShimTokenUrl)"
SHIM_RETURN="$(cfn_out "$PREFIX-shim" ShimReturnUrl)"
[ -n "$SHIM_ISSUER" ] && [ "$SHIM_ISSUER" != "None" ] || {
  echo "shim stack outputs not found — run ./deploy.sh base first"; exit 1; }

# --- 1. workload identity -------------------------------------------------
# The return URL allowlist is exact-match: the URL passed at token time must
# equal a registered entry byte for byte (no extra query string).
log "Workload identity ($WORKLOAD)"
if aws bedrock-agentcore-control get-workload-identity --name "$WORKLOAD" >/dev/null 2>&1; then
  aws bedrock-agentcore-control update-workload-identity --name "$WORKLOAD" \
    --allowed-resource-oauth2-return-urls "$SHIM_RETURN" >/dev/null
  echo "  updated (return URL: $SHIM_RETURN)"
else
  aws bedrock-agentcore-control create-workload-identity --name "$WORKLOAD" \
    --allowed-resource-oauth2-return-urls "$SHIM_RETURN" >/dev/null
  echo "  created (return URL: $SHIM_RETURN)"
fi

# --- 2. OAuth2 credential provider ---------------------------------------
# Lark is not standard OIDC (no discovery doc), so we pass authorizationServerMetadata.
# clientAuthenticationMethod goes on the provider config — setting the legacy
# tokenEndpointAuthMethods in the metadata too is a validation error.
log "OAuth2 credential provider ($PROVIDER)"
CFG="$(SHIM_ISSUER="$SHIM_ISSUER" SHIM_AUTHORIZE="$SHIM_AUTHORIZE" SHIM_TOKEN="$SHIM_TOKEN" \
       LARK_APP_ID="$LARK_APP_ID" LARK_APP_SECRET="$LARK_APP_SECRET" python3 <<'PY'
import json, os
e = os.environ
print(json.dumps({
    "customOauth2ProviderConfig": {
        "oauthDiscovery": {
            "authorizationServerMetadata": {
                "issuer": e["SHIM_ISSUER"],
                "authorizationEndpoint": e["SHIM_AUTHORIZE"],
                "tokenEndpoint": e["SHIM_TOKEN"],
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
echo "Lark authorize host in use: $LARK_AUTH_HOST"
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
