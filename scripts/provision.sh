#!/usr/bin/env bash
# Deploy lark-agent. Override the target with PROFILE=... REGION=... env vars.
#
# Steps (idempotent — re-runnable independently):
#   --base      CDK base stacks (security, agentcore, router, shim, observability)
#   --runtime   create/update the AgentCore Runtime from the built image (CLI)
#   (no arg)    run all steps in order
#
# Step implementation — normally invoked through ./deploy.sh in the repo root,
# which owns the ordering. Callable directly when iterating on one phase:
# Usage: [PROFILE=p REGION=r] scripts/provision.sh [--base|--runtime]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Deployment target: command-line env vars win over .env, which wins over defaults.
_CLI_PROFILE="${PROFILE:-}" _CLI_REGION="${REGION:-}"
[ -f .env ] && { set -a; . ./.env; set +a; }
PROFILE="${_CLI_PROFILE:-${PROFILE:-}}"   # empty -> ambient creds (instance role / env)
REGION="${_CLI_REGION:-${REGION:-us-west-2}}"
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
             "$PREFIX-shim" "$PREFIX-observability" \
             --require-approval never --outputs-file cdk.out/outputs.json
}

phase2_runtime() {
  # The Runtime image must be ARM64 and built via CodeBuild.
  # We use the AgentCore CLI, which runs CodeBuild in the cloud and creates/updates the runtime.
  # Requires `npm i -g @aws/agentcore`.
  log "Runtime — build (CodeBuild ARM64) + deploy via AgentCore CLI"
  command -v agentcore >/dev/null || { echo "agentcore CLI not found: npm i -g @aws/agentcore"; exit 1; }
  export AGENTCORE_SUPPRESS_RECOMMENDATION=1

  local role model memory shim mcp_arn mcp_url pool client pwsecret
  role="$(cfn_out "$PREFIX-agentcore" ExecutionRoleArn)"
  pool="$(cfn_out "$PREFIX-security" UserPoolId)"
  client="$(cfn_out "$PREFIX-security" UserPoolClientId)"
  pwsecret="$PREFIX/cognito-password-secret"
  # MODEL_ID from .env wins; else cdk.json's default_model_id.
  model="${MODEL_ID:-$(uv run python -c "import json;print(json.load(open('cdk.json'))['context']['default_model_id'])")}"
  memory="$(uv run python -c "import json,os;f='.cdk-state.json';print((json.load(open(f)) if os.path.isfile(f) else {}).get('memory_id',''))" 2>/dev/null)"
  shim="$(cfn_out "$PREFIX-shim" ShimReturnUrl)"
  # lark-cli MCP server runtime → its SigV4 MCP invocations URL (URL-encoded ARN).
  mcp_arn="$(aws bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='lark_agent_mcp'].agentRuntimeArn" --output text 2>/dev/null | head -1)"
  mcp_url="https://bedrock-agentcore.$REGION.amazonaws.com/runtimes/$(uv run python -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$mcp_arn")/invocations?qualifier=DEFAULT"
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
    --env "COGNITO_PASSWORD_SECRET_ID=$pwsecret"

  # Persist the runtime id into .cdk-state.json for the dependent stacks.
  local rid
  rid="$(aws bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='${PREFIX//-/_}_agent'].agentRuntimeId" \
    --output text 2>/dev/null | head -1)"
  [ -n "$rid" ] && [ "$rid" != "None" ] && ctx_set runtime_id "$rid"

  # Inbound auth: CUSTOM_JWT, so the platform verifies who the caller is and hands the
  # agent a workload token derived from that identity. The AgentCore CLI has no flag for
  # this, hence a follow-up update.
  #
  # UpdateAgentRuntime REPLACES the runtime rather than patching it: anything omitted is
  # cleared, and it silently dropped every environment variable the first time round
  # (SHIM_RETURN_URL going missing surfaced as a ValidationException from
  # GetResourceOauth2Token). So read the current config back and resend all of it.
  #
  # This is also what makes SigV4 invocation stop working ("Authorization method
  # mismatch"), so the router deploy above and this step belong to the same cutover.
  if [ -n "$rid" ] && [ "$rid" != "None" ]; then
    log "Runtime — inbound auth: CUSTOM_JWT (Cognito)"
    local issuer client cur art net envvars
    issuer="$(cfn_out "$PREFIX-security" CognitoIssuerUrl)"
    client="$(cfn_out "$PREFIX-security" UserPoolClientId)"
    cur="$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$rid" --output json)"
    _rt_get() { printf '%s' "$cur" | uv run python -c "import json,sys;print(json.dumps(json.load(sys.stdin).get('$1') or {}))"; }
    art="$(_rt_get agentRuntimeArtifact)"
    net="$(_rt_get networkConfiguration)"
    envvars="$(_rt_get environmentVariables)"
    # Fail loudly rather than deploy a runtime stripped of its configuration.
    [ "$envvars" = "{}" ] && { echo "  aborting: runtime reports no environment variables to preserve"; exit 1; }
    aws bedrock-agentcore-control update-agent-runtime --agent-runtime-id "$rid" \
      --agent-runtime-artifact "$art" --role-arn "$role" --network-configuration "$net" \
      --environment-variables "$envvars" \
      --authorizer-configuration "{\"customJWTAuthorizer\":{\"discoveryUrl\":\"$issuer/.well-known/openid-configuration\",\"allowedClients\":[\"$client\"]}}" \
      --query 'status' --output text
    for _ in $(seq 1 40); do
      [ "$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$rid" \
           --query status --output text 2>/dev/null)" = "READY" ] && break
      sleep 5
    done
    echo "  inbound: CUSTOM_JWT ($client); env vars preserved: $(printf '%s' "$envvars" | uv run python -c 'import json,sys;print(len(json.load(sys.stdin)))')"
  fi

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

case "${1:-all}" in
  --base|--phase1) base_cdk_stacks ;;  # --phase1 kept as a back-compat alias
  --runtime)  phase2_runtime ;;
  all|"")     base_cdk_stacks; phase2_runtime
              log "Webhook URL (register in Lark): $(cfn_out "$PREFIX-router" WebhookLarkUrl)" ;;
  *) echo "usage: [PROFILE=p REGION=r] $0 [--base|--runtime]"; exit 1 ;;
esac
