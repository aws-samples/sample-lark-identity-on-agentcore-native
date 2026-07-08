#!/usr/bin/env bash
# Deploy lark-agent. Override the target with PROFILE=... REGION=... env vars.
#
# Steps (idempotent — re-runnable independently):
#   --base      CDK base stacks (security, agentcore, router, gateway, observability)
#   --runtime   create/update the AgentCore Runtime from the built image (CLI)
#   --gateway   create/update the MCP Gateway + interceptor + demo target (CLI)
#   --frontend  deploy the WebUI stack, inject SPA config, upload the SPA
#   (no arg)    run all steps in order
#
# Usage: [PROFILE=p REGION=r] scripts/deploy.sh [--base|--runtime|--gateway|--frontend]
set -euo pipefail

PROFILE="${PROFILE:-default}"
REGION="${REGION:-us-west-2}"
PREFIX="lark-agent"
export AWS_PROFILE="$PROFILE" AWS_REGION="$REGION" UV_LINK_MODE=copy
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
             "$PREFIX-gateway" "$PREFIX-observability" \
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
  log "Phase 3 — MCP Gateway + interceptor + demo target"
  local issuer client interceptor tool grole gid
  issuer="$(cfn_out "$PREFIX-security" CognitoIssuerUrl)"
  client="$(cfn_out "$PREFIX-security" UserPoolClientId)"
  interceptor="$(cfn_out "$PREFIX-gateway" InterceptorFnArn)"
  tool="$(cfn_out "$PREFIX-gateway" ToolFnArn)"
  grole="$(cfn_out "$PREFIX-gateway" GatewayRoleArn)"

  gid="$(aws bedrock-agentcore-control list-gateways \
    --query "items[?name=='${PREFIX}-gw'].gatewayId" --output text 2>/dev/null || true)"

  if [ -z "$gid" ] || [ "$gid" = "None" ]; then
    log "creating gateway"
    gid="$(aws bedrock-agentcore-control create-gateway \
      --name "${PREFIX}-gw" \
      --protocol-type MCP \
      --role-arn "$grole" \
      --authorizer-type CUSTOM_JWT \
      --authorizer-configuration "{\"customJWTAuthorizer\":{\"discoveryUrl\":\"$issuer/.well-known/openid-configuration\",\"allowedClients\":[\"$client\"]}}" \
      --interceptor-configurations "[{\"interceptor\":{\"lambda\":{\"arn\":\"$interceptor\"}},\"interceptionPoints\":[\"REQUEST\"],\"inputConfiguration\":{\"passRequestHeaders\":true}}]" \
      --query gatewayId --output text)"
  fi
  ctx_set gateway_id "$gid"

  local gurl
  gurl="$(aws bedrock-agentcore-control get-gateway --gateway-identifier "$gid" \
    --query gatewayUrl --output text 2>/dev/null || true)"
  [ -n "$gurl" ] && ctx_set gateway_url "$gurl"

  # demo tool target (idempotent create). Pass JSON via file:// to avoid shell
  # quote mangling of the nested schema.
  if ! aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$gid" \
       --query "items[?name=='demo-whoami']" --output text 2>/dev/null | grep -q demo-whoami; then
    log "creating demo tool target"
    local tgt_file; tgt_file="$(mktemp)"
    cat > "$tgt_file" <<JSON
{"mcp":{"lambda":{"lambdaArn":"$tool","toolSchema":{"inlinePayload":[{"name":"whoami","description":"Report the calling end-user identity injected by the gateway","inputSchema":{"type":"object","properties":{}}}]}}}}
JSON
    aws bedrock-agentcore-control create-gateway-target \
      --gateway-identifier "$gid" \
      --name demo-whoami \
      --target-configuration "file://$tgt_file" \
      --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' \
      >/dev/null && echo "  demo-whoami target created" || echo "(target create failed — check output)"
    rm -f "$tgt_file"
  fi

  # list_my_docs target (same Lambda) — proves permission inheritance: lists the
  # calling user's Lark docs, scoped to that user's own Lark permissions.
  if ! aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$gid" \
       --query "items[?name=='demo-docs']" --output text 2>/dev/null | grep -q demo-docs; then
    log "creating list_my_docs target"
    local docs_file; docs_file="$(mktemp)"
    cat > "$docs_file" <<JSON
{"mcp":{"lambda":{"lambdaArn":"$tool","toolSchema":{"inlinePayload":[{"name":"list_my_docs","description":"List the calling user's Lark cloud documents, scoped to that user's own Lark permissions (the agent never holds the user's token). Returns one folder level; pass folder_token (from a folder entry in a prior result) to descend into a subfolder","inputSchema":{"type":"object","properties":{"folder_token":{"type":"string","description":"optional; a folder's token to list its contents. Omit for the drive root"}}}}]}}}}
JSON
    aws bedrock-agentcore-control create-gateway-target \
      --gateway-identifier "$gid" \
      --name demo-docs \
      --target-configuration "file://$docs_file" \
      --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' \
      >/dev/null && echo "  demo-docs target created" || echo "(target create failed — check output)"
    rm -f "$docs_file"
  fi

  # doc write tools (same Lambda) — create/edit/delete Lark docs AS the user.
  if ! aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$gid" \
       --query "items[?name=='demo-doc-write']" --output text 2>/dev/null | grep -q demo-doc-write; then
    log "creating doc-write target"
    local w_file; w_file="$(mktemp)"
    cat > "$w_file" <<'JSON'
{"mcp":{"lambda":{"lambdaArn":"__TOOL_ARN__","toolSchema":{"inlinePayload":[
  {"name":"create_doc","description":"Create a new Lark doc owned by the calling user","inputSchema":{"type":"object","properties":{"title":{"type":"string","description":"document title"},"content":{"type":"string","description":"optional initial text content"}},"required":["title"]}},
  {"name":"edit_doc","description":"Append text content to an existing Lark doc the user can edit","inputSchema":{"type":"object","properties":{"document_id":{"type":"string"},"content":{"type":"string"}},"required":["document_id","content"]}},
  {"name":"delete_doc","description":"Delete a Lark doc/file the user owns (moves to trash)","inputSchema":{"type":"object","properties":{"document_id":{"type":"string","description":"file token"},"type":{"type":"string","description":"docx|file|sheet…, default docx"}},"required":["document_id"]}}
]}}}}
JSON
    sed -i "s|__TOOL_ARN__|$tool|" "$w_file"
    aws bedrock-agentcore-control create-gateway-target \
      --gateway-identifier "$gid" \
      --name demo-doc-write \
      --target-configuration "file://$w_file" \
      --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' \
      >/dev/null && echo "  demo-doc-write target created" || echo "(target create failed — check output)"
    rm -f "$w_file"
  fi
}

phase4_frontend() {
  log "Phase 4 — re-deploy dependent stacks + publish SPA"
  # webui depends on runtime ARN (from runtime_id) — deploy now that it's set
  $CDK deploy "$PREFIX-webui" --require-approval never --outputs-file cdk.out/outputs.json

  local api_base app_id bucket
  api_base="$(cfn_out "$PREFIX-webui" ApiUrl)"
  bucket="$(cfn_out "$PREFIX-webui" SiteBucketName)"
  # larkAppId comes from .env (single config source), not Secrets Manager.
  [ -f .env ] && { set -a; . ./.env; set +a; }
  app_id="${LARK_APP_ID:-}"

  log "rendering web-ui/config.js from template (apiBase=$api_base appId=${app_id:-<empty>})"
  sed -e "s|REPLACE_API_BASE|${api_base%/}|" -e "s|REPLACE_LARK_APP_ID|${app_id}|" \
    web-ui/config.js.example > web-ui/config.js

  log "uploading SPA to s3://$bucket"
  # exclude the template + docs; config.js (generated) IS uploaded
  aws s3 sync web-ui/ "s3://$bucket/" --delete \
    --exclude "*.md" --exclude "*.example" --cache-control "no-cache"

  log "Done. Site: $(cfn_out "$PREFIX-webui" SiteUrl)"
  log "Webhook URL (register in Lark): $(cfn_out "$PREFIX-router" WebhookLarkUrl)"
}

case "${1:-all}" in
  --base|--phase1) base_cdk_stacks ;;  # --phase1 kept as a back-compat alias
  --runtime)  phase2_runtime ;;
  --gateway)  phase3_gateway ;;
  --frontend) phase4_frontend ;;
  all|"")     base_cdk_stacks; phase2_runtime; phase3_gateway; phase4_frontend ;;
  *) echo "usage: [PROFILE=p REGION=r] $0 [--base|--runtime|--gateway|--frontend]"; exit 1 ;;
esac
