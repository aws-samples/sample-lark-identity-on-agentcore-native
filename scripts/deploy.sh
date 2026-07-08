#!/usr/bin/env bash
# Deploy lark-agent. Override the target with PROFILE=... REGION=... env vars.
#
# Steps (idempotent — re-runnable independently):
#   --base      CDK base stacks (security, agentcore, router, gateway, observability)
#   --runtime   create/update the AgentCore Runtime from the built image (CLI)
#   --gateway   create/update the MCP Gateway + demo target (CLI)
#   (no arg)    run all steps in order
#
# Usage: [PROFILE=p REGION=r] scripts/deploy.sh [--base|--runtime|--gateway]
set -euo pipefail

PROFILE="${PROFILE:-}"   # empty -> use ambient creds (instance role / env), no named profile
REGION="${REGION:-us-west-2}"
PREFIX="lark-id"
export AWS_REGION="$REGION" UV_LINK_MODE=copy
[ -n "$PROFILE" ] && export AWS_PROFILE="$PROFILE"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
export CDK_DEFAULT_ACCOUNT="$ACCOUNT" CDK_DEFAULT_REGION="$REGION"
CDK="npx --yes aws-cdk@2"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

cfn_out() { # stack, output-key
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null
}

ctx_set() { # key value  — persist an id back into cdk.json context
  uv run python - "$1" "$2" <<'PY'
import json, sys
k, v = sys.argv[1], sys.argv[2]
with open("cdk.json") as f: d = json.load(f)
d["context"][k] = v
with open("cdk.json", "w") as f: json.dump(d, f, indent=2); f.write("\n")
print(f"cdk.json: {k} = {v}")
PY
}

base_cdk_stacks() {
  log "Base — CDK stacks"
  $CDK deploy "$PREFIX-security" "$PREFIX-agentcore" "$PREFIX-router" \
             "$PREFIX-gateway" "$PREFIX-shim" "$PREFIX-observability" \
             --require-approval never --outputs-file cdk.out/outputs.json
}

phase2_runtime() {
  # The Runtime image must be ARM64 and built via CodeBuild.
  # We use the AgentCore CLI, which runs CodeBuild in the cloud and creates/updates the runtime.
  # Requires `npm i -g @aws/agentcore`.
  log "Runtime — build (CodeBuild ARM64) + deploy via AgentCore CLI"
  command -v agentcore >/dev/null || { echo "agentcore CLI not found: npm i -g @aws/agentcore"; exit 1; }
  export AGENTCORE_SUPPRESS_RECOMMENDATION=1

  local role model pool client gw
  role="$(cfn_out "$PREFIX-agentcore" ExecutionRoleArn)"
  model="$(uv run python -c "import json;print(json.load(open('cdk.json'))['context']['default_model_id'])")"
  pool="$(cfn_out "$PREFIX-security" UserPoolId)"
  client="$(cfn_out "$PREFIX-security" UserPoolClientId)"
  gw="$(uv run python -c "import json;print(json.load(open('cdk.json'))['context'].get('gateway_url',''))")"
  [ -n "$role" ] || { echo "missing execution role output — run --base first"; exit 1; }

  # Configure once (idempotent; writes .bedrock_agentcore.yaml). Custom Dockerfile
  # in agent/ is respected.
  if [ ! -f .bedrock_agentcore.yaml ]; then
    agentcore configure -e agent/server.py -n "${PREFIX//-/_}_agent" \
      --execution-role "$role" -dt container -p HTTP -r "$REGION" --non-interactive
  fi

  agentcore deploy --auto-update-on-conflict \
    --env "BEDROCK_MODEL_ID=$model" \
    --env "COGNITO_USER_POOL_ID=$pool" \
    --env "COGNITO_CLIENT_ID=$client" \
    --env "COGNITO_PASSWORD_SECRET_ID=$PREFIX/cognito-password-secret" \
    --env "GATEWAY_URL=$gw"

  # Persist the runtime id back into cdk.json for the dependent stacks.
  local rid
  rid="$(aws bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='${PREFIX//-/_}_agent'].agentRuntimeId" \
    --output text 2>/dev/null | head -1)"
  [ -n "$rid" ] && [ "$rid" != "None" ] && ctx_set runtime_id "$rid"
}

phase3_gateway() {
  log "Phase 3 — MCP Gateway (mcpServer target wired below)"
  local issuer client grole gid
  issuer="$(cfn_out "$PREFIX-security" CognitoIssuerUrl)"
  client="$(cfn_out "$PREFIX-security" UserPoolClientId)"
  grole="$(cfn_out "$PREFIX-gateway" GatewayRoleArn)"

  gid="$(aws bedrock-agentcore-control list-gateways \
    --query "items[?name=='${PREFIX}-gw'].gatewayId" --output text 2>/dev/null || true)"

  if [ -z "$gid" ] || [ "$gid" = "None" ]; then
    log "creating gateway"
    # Identity variant: no interceptor. sessionConfiguration is REQUIRED for warm
    # microVM reuse (measured — without it every tool call cold-starts). Protocol
    # 2025-11-25 for the 3LO elicitation flow (lark-mcp negotiates it — measured).
    gid="$(aws bedrock-agentcore-control create-gateway \
      --name "${PREFIX}-gw" \
      --protocol-type MCP \
      --protocol-configuration '{"mcp":{"supportedVersions":["2025-11-25"],"sessionConfiguration":{"sessionTimeoutInSeconds":3600}}}' \
      --role-arn "$grole" \
      --authorizer-type CUSTOM_JWT \
      --authorizer-configuration "{\"customJWTAuthorizer\":{\"discoveryUrl\":\"$issuer/.well-known/openid-configuration\",\"allowedClients\":[\"$client\"]}}" \
      --query gatewayId --output text)"
  fi
  ctx_set gateway_id "$gid"

  local gurl
  gurl="$(aws bedrock-agentcore-control get-gateway --gateway-identifier "$gid" \
    --query gatewayUrl --output text 2>/dev/null || true)"
  [ -n "$gurl" ] && ctx_set gateway_url "$gurl"

  # TODO(Phase 3): create the mcpServer target -> lark-mcp Runtime, bound to a 3LO
  # AUTHORIZATION_CODE OAuth credential provider (the Lark shim). Endpoint must be
  # the URL-encoded runtime ARN; outbound auth = OAUTH (not GATEWAY_IAM_ROLE, since
  # SigV4 would occupy the Authorization header the vaulted Lark token needs).
  echo "  gateway ready ($gid); mcpServer target + 3LO provider wired in Phase 3"
}

case "${1:-all}" in
  --base|--phase1) base_cdk_stacks ;;  # --phase1 kept as a back-compat alias
  --runtime)  phase2_runtime ;;
  --gateway)  phase3_gateway ;;
  all|"")     base_cdk_stacks; phase2_runtime; phase3_gateway
              log "Webhook URL (register in Lark): $(cfn_out "$PREFIX-router" WebhookLarkUrl)" ;;
  *) echo "usage: [PROFILE=p REGION=r] $0 [--base|--runtime|--gateway]"; exit 1 ;;
esac
