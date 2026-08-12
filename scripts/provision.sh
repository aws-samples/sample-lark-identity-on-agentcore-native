#!/usr/bin/env bash
# Deploy lark-agent. Override the target with PROFILE=... REGION=... env vars.
#
# Steps (idempotent — re-runnable independently):
#   --base      CDK base stacks (security, agentcore, router, gateway, observability)
#   --runtime   create/update the AgentCore Runtime from the built image (CLI)
#   --gateway   Web Search gateway in us-east-1 (only when WEB_SEARCH=true)
#   (no arg)    run all steps in order
#
# Step implementation — normally invoked through ./deploy.sh in the repo root,
# which owns the ordering. Callable directly when iterating on one phase:
# Usage: [PROFILE=p REGION=r] scripts/provision.sh [--base|--runtime|--gateway]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Deployment target: command-line env vars win over .env, which wins over defaults.
_CLI_PROFILE="${PROFILE:-}" _CLI_REGION="${REGION:-}" _CLI_WEB_SEARCH="${WEB_SEARCH:-}"
[ -f .env ] && { set -a; . ./.env; set +a; }
PROFILE="${_CLI_PROFILE:-${PROFILE:-}}"   # empty -> ambient creds (instance role / env)
REGION="${_CLI_REGION:-${REGION:-us-west-2}}"
WEB_SEARCH="${_CLI_WEB_SEARCH:-${WEB_SEARCH:-false}}"
PREFIX="lark-agent"
export AWS_REGION="$REGION" UV_LINK_MODE=copy
# Credentials already in the environment outrank .env's profile.
[ -n "${AWS_ACCESS_KEY_ID:-}" ] || { [ -n "$PROFILE" ] && export AWS_PROFILE="$PROFILE"; } || true

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
export CDK_DEFAULT_ACCOUNT="$ACCOUNT" CDK_DEFAULT_REGION="$REGION"
CDK="npx --yes aws-cdk@2"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

cfn_out() { # stack, output-key
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null
}

ctx_set() { # key value  — persist a deployment id into .cdk-state.json (gitignored)
  uv run python - "$1" "$2" <<'PY'
import json, os, sys
k, v = sys.argv[1], sys.argv[2]
f = ".cdk-state.json"
d = json.load(open(f)) if os.path.isfile(f) else {}
d[k] = v
with open(f, "w") as fh: json.dump(d, fh, indent=2); fh.write("\n")
print(f".cdk-state.json: {k} = {v}")
PY
}

base_cdk_stacks() {
  # First deploy in a region needs the CDK bootstrap stack; it creates an assets
  # bucket/ECR repo and deploy roles, so this step needs broader IAM permissions.
  aws cloudformation describe-stacks --stack-name CDKToolkit >/dev/null 2>&1 || {
    log "CDK bootstrap — first deploy in $REGION"
    $CDK bootstrap "aws://$ACCOUNT/$REGION"
  }

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

  local role model memory shim mcp_arn mcp_url pool client pwsecret ws_url approval_url approval_arn
  role="$(cfn_out "$PREFIX-agentcore" ExecutionRoleArn)"
  # Cognito lets the agent mint the access token the search Gateway authorises with.
  pool="$(cfn_out "$PREFIX-security" UserPoolId)"
  client="$(cfn_out "$PREFIX-security" UserPoolClientId)"
  pwsecret="$PREFIX/cognito-password-secret"
  ws_url="$(uv run python -c "import json,os;f='.cdk-state.json';print((json.load(open(f)) if os.path.isfile(f) else {}).get('websearch_gateway_url',''))" 2>/dev/null)"
  # MODEL_ID from .env wins; else cdk.json's default_model_id.
  model="${MODEL_ID:-$(uv run python -c "import json;print(json.load(open('cdk.json'))['context']['default_model_id'])")}"
  memory="$(uv run python -c "import json,os;f='.cdk-state.json';print((json.load(open(f)) if os.path.isfile(f) else {}).get('memory_id',''))" 2>/dev/null)"
  shim="$(cfn_out "$PREFIX-shim" ShimReturnUrl)"
  # lark-cli MCP server runtime → its SigV4 MCP invocations URL (URL-encoded ARN).
  mcp_arn="$(aws bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='lark_agent_mcp'].agentRuntimeArn" --output text 2>/dev/null | head -1)"
  mcp_url="https://bedrock-agentcore.$REGION.amazonaws.com/runtimes/$(uv run python -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$mcp_arn")/invocations?qualifier=DEFAULT"
  # Approval MCP server, if ./deploy.sh approval ran. Optional by design: left empty
  # the agent simply has no approval tools, same as web search.
  approval_arn="$(aws bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='${PREFIX//-/_}_approval'].agentRuntimeArn" --output text 2>/dev/null | head -1)"
  if [ -n "$approval_arn" ] && [ "$approval_arn" != "None" ]; then
    approval_url="https://bedrock-agentcore.$REGION.amazonaws.com/runtimes/$(uv run python -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$approval_arn")/invocations?qualifier=DEFAULT"
    echo "  approval tools: enabled"
  else
    approval_url=""
    echo "  approval tools: not deployed (./deploy.sh approval to add them)"
  fi

  [ -n "$role" ] || { echo "missing execution role output — run --base first"; exit 1; }
  [ -n "$mcp_arn" ] && [ "$mcp_arn" != "None" ] || { echo "lark_agent_mcp runtime not found — deploy the MCP server first (scripts/build-mcp.sh + create runtime)"; exit 1; }

  # Configure once (idempotent; writes .bedrock_agentcore.yaml). Custom Dockerfile
  # in agent/ is respected. Allow the agent's own execution role to fetch per-user
  # tokens and invoke the lark-cli MCP runtime (granted out-of-band / in agentcore stack).
  if [ ! -f .bedrock_agentcore.yaml ]; then
    agentcore configure -e agent/server.py -n "${PREFIX//-/_}_agent" \
      --execution-role "$role" -dt container -p HTTP -r "$REGION" --non-interactive
  fi

  agentcore deploy --auto-update-on-conflict \
    --env "BEDROCK_MODEL_ID=$model" \
    --env "BEDROCK_AGENTCORE_MEMORY_ID=$memory" \
    --env "LARK_MCP_URL=$mcp_url" \
    --env "SHIM_RETURN_URL=$shim" \
    --env "LARK_OAUTH_PROVIDER=lark-agent-3lo" \
    --env "AGENT_WORKLOAD_NAME=lark-agent-wl" \
    --env "LARK_SCOPES=drive:drive docx:document offline_access" \
    --env "LARK_SECRET_ID=$PREFIX/channels/lark" \
    --env "LARK_API_DOMAIN=$(uv run python -c "import json;print(json.load(open('cdk.json'))['context']['lark_api_domain'])")" \
    --env "COGNITO_USER_POOL_ID=$pool" \
    --env "COGNITO_CLIENT_ID=$client" \
    --env "COGNITO_PASSWORD_SECRET_ID=$pwsecret" \
    --env "WEBSEARCH_GATEWAY_URL=$ws_url" \
    --env "APPROVAL_MCP_URL=$approval_url"

  # Persist the runtime id into .cdk-state.json for the dependent stacks.
  local rid
  rid="$(aws bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='${PREFIX//-/_}_agent'].agentRuntimeId" \
    --output text 2>/dev/null | head -1)"
  [ -n "$rid" ] && [ "$rid" != "None" ] && ctx_set runtime_id "$rid"

  # The router's AGENTCORE_RUNTIME_ARN was synthesised before the runtime existed
  # (a PLACEHOLDER), so re-deploy it now that the real id is known — otherwise the
  # webhook invokes a non-existent runtime.
  log "Router — re-deploy with the real runtime ARN"
  $CDK deploy "$PREFIX-router" --require-approval never

  # AgentCore keeps serving existing sessions from the OLD container instance, so
  # stored session ids would pin users to the previous image. Drop them: the next
  # message starts a new session on the just-deployed version.
  log "Sessions — drop stored ids so users land on the new version"
  local n=0
  for pk in $(aws dynamodb scan --table-name "$PREFIX-identity" \
      --filter-expression "SK = :s" \
      --expression-attribute-values '{":s":{"S":"SESSION"}}' \
      --projection-expression "PK" --query 'Items[].PK.S' --output text 2>/dev/null); do
    aws dynamodb delete-item --table-name "$PREFIX-identity" \
      --key "{\"PK\":{\"S\":\"$pk\"},\"SK\":{\"S\":\"SESSION\"}}" >/dev/null 2>&1 && n=$((n+1))
  done
  echo "  dropped $n session(s)"
}

phase3_gateway() {
  # The Web Search connector is only offered in us-east-1, while everything else
  # here runs in $REGION — so this gateway lives there and the agent calls it
  # cross-region. That is fine: web search needs no end-user identity, so
  # it uses GATEWAY_IAM_ROLE outbound auth and never touches the per-user 3LO path
  # that forced the Lark tools off the Gateway in the first place.
  if [ "${WEB_SEARCH:-false}" != "true" ]; then
    log "Gateway — skipped (WEB_SEARCH is not true)"
    return 0
  fi
  # Not configurable: us-east-1 is the only region offering the connector.
  local ws_region="us-east-1"
  log "Gateway — Web Search connector in $ws_region"

  local issuer client grole gid
  issuer="$(cfn_out "$PREFIX-security" CognitoIssuerUrl)"
  client="$(cfn_out "$PREFIX-security" UserPoolClientId)"
  grole="$(cfn_out "$PREFIX-gateway" GatewayRoleArn)"
  [ -n "$issuer" ] && [ "$issuer" != "None" ] || { echo "security stack outputs missing — run --base first"; exit 1; }

  # IAM roles are global, so the role from the main region is reused as-is.
  gid="$(aws bedrock-agentcore-control list-gateways --region "$ws_region" \
    --query "items[?name=='${PREFIX}-websearch-gw'].gatewayId" --output text 2>/dev/null || true)"
  if [ -z "$gid" ] || [ "$gid" = "None" ]; then
    echo "  creating gateway"
    gid="$(aws bedrock-agentcore-control create-gateway --region "$ws_region" \
      --name "${PREFIX}-websearch-gw" \
      --protocol-type MCP \
      --protocol-configuration '{"mcp":{"supportedVersions":["2025-11-25"],"sessionConfiguration":{"sessionTimeoutInSeconds":3600}}}' \
      --role-arn "$grole" \
      --authorizer-type CUSTOM_JWT \
      --authorizer-configuration "{\"customJWTAuthorizer\":{\"discoveryUrl\":\"$issuer/.well-known/openid-configuration\",\"allowedClients\":[\"$client\"]}}" \
      --query gatewayId --output text)"
    # A gateway is briefly CREATING; targets can't be added until it settles.
    for _ in $(seq 1 30); do
      [ "$(aws bedrock-agentcore-control get-gateway --region "$ws_region" \
           --gateway-identifier "$gid" --query status --output text 2>/dev/null)" = "READY" ] && break
      sleep 3
    done
  fi
  echo "  gateway: $gid"

  # The tool name must be WebSearch and parameterValues must be present even when
  # empty ({} = no domain filter); the API rejects a config entry without it.
  local tid
  tid="$(aws bedrock-agentcore-control list-gateway-targets --region "$ws_region" \
    --gateway-identifier "$gid" --query "items[?name=='web-search-tool'].targetId" \
    --output text 2>/dev/null || true)"
  if [ -z "$tid" ] || [ "$tid" = "None" ]; then
    echo "  creating web-search target"
    tid="$(aws bedrock-agentcore-control create-gateway-target --region "$ws_region" \
      --gateway-identifier "$gid" --name web-search-tool \
      --target-configuration '{"mcp":{"connector":{"source":{"connectorId":"web-search"},"configurations":[{"name":"WebSearch","parameterValues":{}}]}}}' \
      --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' \
      --query targetId --output text)"
  fi
  echo "  target: $tid"

  local gurl
  gurl="$(aws bedrock-agentcore-control get-gateway --region "$ws_region" \
    --gateway-identifier "$gid" --query gatewayUrl --output text 2>/dev/null || true)"
  ctx_set websearch_gateway_id "$gid"
  [ -n "$gurl" ] && [ "$gurl" != "None" ] && ctx_set websearch_gateway_url "$gurl"
  echo "  url: $gurl"
  echo "  next: ./deploy.sh runtime  (injects the URL into the agent)"
}

case "${1:-all}" in
  --base|--phase1) base_cdk_stacks ;;  # --phase1 kept as a back-compat alias
  --runtime)  phase2_runtime ;;
  --gateway)  phase3_gateway ;;
  all|"")     base_cdk_stacks; phase2_runtime; phase3_gateway
              log "Webhook URL (register in Lark): $(cfn_out "$PREFIX-router" WebhookLarkUrl)" ;;
  *) echo "usage: [PROFILE=p REGION=r] $0 [--base|--runtime|--gateway]"; exit 1 ;;
esac
