#!/usr/bin/env bash
# Tear down everything deploy.sh created, in dependency order. Override the
# target with PROFILE=... REGION=... env vars (same as deploy.sh).
#
# The AgentCore Gateway (+ its targets) and Runtime are created out-of-band by
# the control-plane CLI, NOT by CloudFormation — so `cdk destroy` alone can't
# remove them. This script deletes them first, then destroys the CDK stacks.
#
# Order: gateway targets → gateway → runtime → CDK stacks (reverse-dependency).
# Everything is discovered dynamically by name (no hardcoded ids), so this is
# safe to re-run — already-gone resources are skipped.
#
# Usage: [PROFILE=p REGION=r] scripts/destroy.sh [--yes]
#   --yes   skip the interactive confirmation
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Deployment target: command-line env vars win over .env, which wins over defaults.
_CLI_PROFILE="${PROFILE:-}" _CLI_REGION="${REGION:-}"
[ -f .env ] && { set -a; . ./.env; set +a; }
PROFILE="${_CLI_PROFILE:-${PROFILE:-}}"   # empty -> ambient creds (instance role / env)
REGION="${_CLI_REGION:-${REGION:-us-west-2}}"
PREFIX="lark-agent"
export AWS_REGION="$REGION"
# Credentials already in the environment outrank .env's profile.
[ -n "${AWS_ACCESS_KEY_ID:-}" ] || { [ -n "$PROFILE" ] && export AWS_PROFILE="$PROFILE"; } || true

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
export CDK_DEFAULT_ACCOUNT="$ACCOUNT" CDK_DEFAULT_REGION="$REGION"
CDK="npx --yes aws-cdk@2"
# Both CLI-created runtimes: the agent and the lark-cli MCP server.
RUNTIME_NAMES=("${PREFIX//-/_}_agent" "${PREFIX//-/_}_mcp")
GW_NAME="${PREFIX}-gw"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }

# --- confirmation --------------------------------------------------------
if [ "${1:-}" != "--yes" ]; then
  warn "This will DELETE all $PREFIX resources in account $ACCOUNT / $REGION:"
  warn "  AgentCore Gateways ($GW_NAME in $REGION, ${PREFIX}-websearch-gw in us-east-1)"
  warn "  + their targets, Runtimes (${RUNTIME_NAMES[*]}),"
  warn "  and CDK stacks (gateway, router, agentcore, observability, security)."
  warn "  Secrets include your Lark credentials ($PREFIX/channels/lark) — re-seedable"
  warn "  from .env via scripts/setup-lark.sh. Lark console app config is NOT touched."
  warn "  The OAuth provider goes too, so every user's vaulted token is purged and the"
  warn "  next deploy issues a NEW callbackUrl to register in the Lark console."
  read -r -p "Type the account id ($ACCOUNT) to proceed: " reply
  [ "$reply" = "$ACCOUNT" ] || { echo "aborted."; exit 1; }
fi

# --- 1. gateway targets + gateways (CLI-created) -------------------------
# Two possible gateways: one in the main region from earlier versions, and the
# Web Search one in us-east-1 (the only region offering that connector).
log "Gateways — delete targets then the gateway"
delete_gateway() {  # name region
  local name="$1" gw_region="$2" gid tid left
  gid="$(aws bedrock-agentcore-control list-gateways --region "$gw_region" \
    --query "items[?name=='$name'].gatewayId" --output text 2>/dev/null || true)"
  if [ -z "$gid" ] || [ "$gid" = "None" ]; then
    echo "  no gateway named $name in $gw_region — skipping"
    return 0
  fi
  # Targets must go before the gateway.
  for tid in $(aws bedrock-agentcore-control list-gateway-targets --region "$gw_region" \
                 --gateway-identifier "$gid" --query "items[].targetId" --output text 2>/dev/null || true); do
    echo "  deleting target $tid"
    aws bedrock-agentcore-control delete-gateway-target --region "$gw_region" \
      --gateway-identifier "$gid" --target-id "$tid" >/dev/null 2>&1 || warn "  (target $tid delete failed)"
  done
  # Target deletion is async — the gateway delete is rejected while any target
  # still lingers. Wait until the target list is empty before deleting.
  for _ in $(seq 1 30); do
    left="$(aws bedrock-agentcore-control list-gateway-targets --region "$gw_region" \
              --gateway-identifier "$gid" --query "length(items)" --output text 2>/dev/null || echo 0)"
    [ "$left" = "0" ] || [ "$left" = "None" ] && break
    echo "  waiting for $left target(s) to finish deleting…"; sleep 5
  done
  echo "  deleting gateway $gid ($gw_region)"
  aws bedrock-agentcore-control delete-gateway --region "$gw_region" \
    --gateway-identifier "$gid" >/dev/null 2>&1 \
    || warn "  (gateway delete failed — re-run once targets are fully gone)"
}
delete_gateway "$GW_NAME" "$REGION"
delete_gateway "${PREFIX}-websearch-gw" "us-east-1"

# --- 2. runtimes (CLI-created) -------------------------------------------
log "Runtimes — delete the AgentCore Runtimes (agent + lark-cli MCP server)"
for rname in "${RUNTIME_NAMES[@]}"; do
  rid="$(aws bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='$rname'].agentRuntimeId" \
    --output text 2>/dev/null | head -1 || true)"
  if [ -n "$rid" ] && [ "$rid" != "None" ]; then
    echo "  deleting runtime $rname ($rid)"
    aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$rid" >/dev/null 2>&1 \
      || warn "  (runtime $rname delete failed)"
  else
    echo "  no runtime named $rname — skipping"
  fi
done

# --- 2b. Memory, ECR, CodeBuild (created by the AgentCore CLI / build) ----
# None of these belong to a stack, and leaving them behind is not harmless: the
# Memory holds every user's conversation history and keeps billing for stored
# events, and the ECR repo keeps paying for image storage. A redeploy creates a
# new Memory rather than adopting this one, so it would just accumulate.
log "Memory / ECR / CodeBuild — CLI-created leftovers"
MEM_PREFIX="${PREFIX//-/_}"
for mid in $(aws bedrock-agentcore-control list-memories --max-results 100 \
               --query "memories[?starts_with(id,'$MEM_PREFIX')].id" --output text 2>/dev/null || true); do
  echo "  deleting memory $mid (conversation history)"
  aws bedrock-agentcore-control delete-memory --memory-id "$mid" >/dev/null 2>&1 \
    || warn "  (memory $mid delete failed)"
done

for repo in $(aws ecr describe-repositories \
                --query "repositories[?starts_with(repositoryName,'$PREFIX')].repositoryName" \
                --output text 2>/dev/null || true); do
  echo "  deleting ECR repo $repo (with images)"
  aws ecr delete-repository --repository-name "$repo" --force >/dev/null 2>&1 \
    || warn "  (ECR repo $repo delete failed)"
done

for proj in $(aws codebuild list-projects --query "projects[?starts_with(@,'$PREFIX')]" \
                --output text 2>/dev/null || true); do
  echo "  deleting CodeBuild project $proj"
  aws codebuild delete-project --name "$proj" >/dev/null 2>&1 \
    || warn "  (CodeBuild project $proj delete failed)"
done

# The CodeBuild source bucket is shared by every AgentCore project in the account
# (its name carries no project prefix), so delete only our own source keys and
# leave the bucket alone.
SRC_BUCKET="bedrock-agentcore-codebuild-sources-$ACCOUNT-$REGION"
if aws s3api head-bucket --bucket "$SRC_BUCKET" >/dev/null 2>&1; then
  for key in "$PREFIX-mcp/source.zip" "${PREFIX//-/_}_agent/source.zip"; do
    aws s3api delete-object --bucket "$SRC_BUCKET" --key "$key" >/dev/null 2>&1 \
      && echo "  removed s3://$SRC_BUCKET/$key" || true
  done
fi

# --- 3. CDK stacks (reverse dependency order) ----------------------------
log "CDK — destroy stacks"
# gateway/router depend on agentcore+security; destroy dependents first.
$CDK destroy \
  "$PREFIX-shim" "$PREFIX-gateway" "$PREFIX-router" \
  "$PREFIX-agentcore" "$PREFIX-observability" "$PREFIX-security" \
  --force

# --- 4. AgentCore Identity (CLI-created, region-scoped) ------------------
# Deleting the provider purges every user's vaulted token, so a redeploy means
# everyone consents again — and the new provider gets a NEW callbackUrl that must
# be registered in the Lark console. Both are region-scoped: leaving them behind
# is what litters an account after moving regions.
log "AgentCore Identity — OAuth provider + workload identity"
if aws bedrock-agentcore-control get-oauth2-credential-provider --name "$PREFIX-3lo" >/dev/null 2>&1; then
  aws bedrock-agentcore-control delete-oauth2-credential-provider --name "$PREFIX-3lo" >/dev/null 2>&1 \
    && echo "  deleted provider $PREFIX-3lo (vaulted tokens purged)" \
    || warn "  (provider $PREFIX-3lo delete failed)"
else
  echo "  no provider named $PREFIX-3lo — skipping"
fi
if aws bedrock-agentcore-control get-workload-identity --name "$PREFIX-wl" >/dev/null 2>&1; then
  aws bedrock-agentcore-control delete-workload-identity --name "$PREFIX-wl" >/dev/null 2>&1 \
    && echo "  deleted workload identity $PREFIX-wl" \
    || warn "  (workload $PREFIX-wl delete failed — service-linked ones can't be deleted)"
else
  echo "  no workload identity named $PREFIX-wl — skipping"
fi

# --- 5. leftover local state --------------------------------------------
log "Local — clear the AgentCore CLI config so a fresh deploy reconfigures"
[ -f .bedrock_agentcore.yaml ] && { rm -f .bedrock_agentcore.yaml; echo "  removed .bedrock_agentcore.yaml"; }  # safe-rm-ok
rm -rf .bedrock_agentcore 2>/dev/null || true  # safe-rm-ok

# .cdk-state.json holds this deployment's ids; a stale one would point a fresh
# deploy at resources that no longer exist.
[ -f .cdk-state.json ] && { rm -f .cdk-state.json; echo "  removed .cdk-state.json"; }  # safe-rm-ok

log "Done. Lark console app config is untouched; re-deploy with ./deploy.sh."
warn "Re-register the new provider callbackUrl (./deploy.sh urls prints it)."
warn "Note: CLI-created runtime/gateway are gone; CDK-managed secrets were destroyed"
warn "with their stack. If any secret lingers (deletion is scheduled), it will purge"
warn "after the recovery window, or force-delete with: aws secretsmanager delete-secret"
warn "--secret-id $PREFIX/... --force-delete-without-recovery"
