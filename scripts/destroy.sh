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

PROFILE="${PROFILE:-default}"
REGION="${REGION:-us-west-2}"
PREFIX="lark-agent"
export AWS_PROFILE="$PROFILE" AWS_REGION="$REGION"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
export CDK_DEFAULT_ACCOUNT="$ACCOUNT" CDK_DEFAULT_REGION="$REGION"
CDK="npx --yes aws-cdk@2"
RUNTIME_NAME="${PREFIX//-/_}_agent"
GW_NAME="${PREFIX}-gw"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }

# --- confirmation --------------------------------------------------------
if [ "${1:-}" != "--yes" ]; then
  warn "This will DELETE all lark-agent resources in account $ACCOUNT / $REGION:"
  warn "  AgentCore Gateway ($GW_NAME) + targets, Runtime ($RUNTIME_NAME),"
  warn "  and CDK stacks (webui, gateway, router, agentcore, observability, security)."
  warn "  Secrets include your Lark credentials ($PREFIX/channels/lark) — re-seedable"
  warn "  from .env via scripts/setup-lark.sh. Lark console app config is NOT touched."
  read -r -p "Type the account id ($ACCOUNT) to proceed: " reply
  [ "$reply" = "$ACCOUNT" ] || { echo "aborted."; exit 1; }
fi

# --- 1. gateway targets + gateway (CLI-created) --------------------------
log "Gateway — delete targets then the gateway"
gid="$(aws bedrock-agentcore-control list-gateways \
  --query "items[?name=='$GW_NAME'].gatewayId" --output text 2>/dev/null || true)"
if [ -n "$gid" ] && [ "$gid" != "None" ]; then
  # Targets must go before the gateway.
  for tid in $(aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$gid" \
                 --query "items[].targetId" --output text 2>/dev/null || true); do
    echo "  deleting target $tid"
    aws bedrock-agentcore-control delete-gateway-target \
      --gateway-identifier "$gid" --target-id "$tid" >/dev/null 2>&1 || warn "  (target $tid delete failed)"
  done
  # Target deletion is async — the gateway delete is rejected while any target
  # still lingers. Wait until the target list is empty before deleting the gateway.
  for _ in $(seq 1 30); do
    left="$(aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$gid" \
              --query "length(items)" --output text 2>/dev/null || echo 0)"
    [ "$left" = "0" ] || [ "$left" = "None" ] && break
    echo "  waiting for $left target(s) to finish deleting…"; sleep 5
  done
  echo "  deleting gateway $gid"
  aws bedrock-agentcore-control delete-gateway --gateway-identifier "$gid" >/dev/null 2>&1 \
    || warn "  (gateway delete failed — re-run once targets are fully gone)"
else
  echo "  no gateway named $GW_NAME — skipping"
fi

# --- 2. runtime (CLI-created) --------------------------------------------
log "Runtime — delete the AgentCore Runtime"
rid="$(aws bedrock-agentcore-control list-agent-runtimes \
  --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].agentRuntimeId" \
  --output text 2>/dev/null | head -1 || true)"
if [ -n "$rid" ] && [ "$rid" != "None" ]; then
  echo "  deleting runtime $rid"
  aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$rid" >/dev/null 2>&1 \
    || warn "  (runtime delete failed)"
else
  echo "  no runtime named $RUNTIME_NAME — skipping"
fi

# --- 3. CDK stacks (reverse dependency order) ----------------------------
log "CDK — destroy stacks"
# webui/gateway/router depend on agentcore+security; destroy dependents first.
$CDK destroy \
  "$PREFIX-webui" "$PREFIX-gateway" "$PREFIX-router" \
  "$PREFIX-agentcore" "$PREFIX-observability" "$PREFIX-security" \
  --force

# --- 4. dynamic per-user secrets (NOT managed by any stack) --------------
# web_api creates {prefix}/user-tokens/{open_id} at runtime when a user
# authorizes, so cdk destroy never removes them — clean them up explicitly.
log "Secrets — remove dynamic per-user token secrets ($PREFIX/user-tokens/*)"
for name in $(aws secretsmanager list-secrets \
    --query "SecretList[?starts_with(Name,'$PREFIX/user-tokens/')].Name" --output text 2>/dev/null || true); do
  echo "  deleting $name"
  aws secretsmanager delete-secret --secret-id "$name" \
    --force-delete-without-recovery >/dev/null 2>&1 || warn "  ($name delete failed)"
done

# --- 5. leftover local state --------------------------------------------
log "Local — clear the AgentCore CLI config so a fresh deploy reconfigures"
[ -f .bedrock_agentcore.yaml ] && { rm -f .bedrock_agentcore.yaml; echo "  removed .bedrock_agentcore.yaml"; }  # safe-rm-ok
rm -rf .bedrock_agentcore 2>/dev/null || true  # safe-rm-ok

log "Done. Lark console app config is untouched; re-deploy with scripts/deploy.sh."
warn "Note: CLI-created runtime/gateway are gone; CDK-managed secrets were destroyed"
warn "with their stack. If any secret lingers (deletion is scheduled), it will purge"
warn "after the recovery window, or force-delete with: aws secretsmanager delete-secret"
warn "--secret-id $PREFIX/... --force-delete-without-recovery"
