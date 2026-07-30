#!/usr/bin/env bash
# Check that a deploy can succeed, and print the target it would hit.
#
# Runs before the first slow step: a CodeBuild image takes minutes, so finding out
# afterwards that a Lark credential was blank means paying for that wait twice.
# Read-only — safe to run any time.
#
# Usage: [PROFILE=p REGION=r] scripts/preflight.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Command-line env vars win over .env, which wins over defaults (same as deploy.sh).
_CLI_PROFILE="${PROFILE:-}" _CLI_REGION="${REGION:-}" _CLI_WEB_SEARCH="${WEB_SEARCH:-}"
[ -f .env ] && { set -a; . ./.env; set +a; }
PROFILE="${_CLI_PROFILE:-${PROFILE:-}}"
REGION="${_CLI_REGION:-${REGION:-us-west-2}}"
WEB_SEARCH="${_CLI_WEB_SEARCH:-${WEB_SEARCH:-false}}"
export AWS_REGION="$REGION"
# Credentials already in the environment outrank .env's profile — otherwise this
# would report (and check) an identity other than the one the caller supplied.
if [ -n "${AWS_ACCESS_KEY_ID:-}" ]; then
  PROFILE=""
elif [ -n "$PROFILE" ]; then
  export AWS_PROFILE="$PROFILE"
fi

fail() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

# --- tools ---------------------------------------------------------------
missing=""
for t in uv docker agentcore aws npx python3; do
  command -v "$t" >/dev/null || missing="$missing $t"
done
[ -z "$missing" ] || fail "missing tools:$missing
  uv: https://docs.astral.sh/uv/   agentcore: npm i -g @aws/agentcore"

# Both runtime images are built from Docker, so a stopped daemon fails the deploy
# several minutes in rather than here.
docker info >/dev/null 2>&1 || fail "Docker isn't running — both runtime images are built from it."

# --- credentials ---------------------------------------------------------
IDENT="$(aws sts get-caller-identity --output json 2>/dev/null)" \
  || fail "AWS credentials not usable${PROFILE:+ for profile '$PROFILE'} — check .env, or run aws configure."
ACCOUNT="$(printf '%s' "$IDENT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Account"])')"

# The agent can't run without the model, and access is per-region opt-in.
MODEL="${MODEL_ID:-global.anthropic.claude-sonnet-4-6}"
aws bedrock get-foundation-model --model-identifier "${MODEL#global.}" >/dev/null 2>&1 \
  || printf '\033[1;33m%s\033[0m\n' "  warning: can't confirm $MODEL in $REGION — enable model access if the agent 403s"

# --- permissions ---------------------------------------------------------
# One harmless read per service the deploy writes to. This catches the case that
# actually bites — a policy or SCP that doesn't reach the service at all — without
# the false confidence of SimulatePrincipalPolicy, which can't see SCPs or
# permission boundaries and needs its own permission to call. A read passing does
# not prove the matching write is allowed, so these only warn.
denied=""
probe() {  # label, command…
  local label="$1"; shift
  "$@" >/dev/null 2>&1 || denied="$denied $label"
}
probe cloudformation aws cloudformation list-stacks --max-items 1
probe agentcore      aws bedrock-agentcore-control list-agent-runtimes --max-results 1
probe secretsmanager aws secretsmanager list-secrets --max-items 1
probe dynamodb       aws dynamodb list-tables --max-items 1
probe ecr            aws ecr describe-repositories --max-items 1
probe codebuild      aws codebuild list-projects
probe cognito        aws cognito-idp list-user-pools --max-results 1
probe iam            aws iam list-roles --max-items 1
[ "$WEB_SEARCH" != "true" ] || probe "agentcore(us-east-1)" \
  aws bedrock-agentcore-control list-gateways --region us-east-1 --max-results 1
[ -z "$denied" ] || printf '\033[1;33m%s\033[0m\n' "  warning: no read access to:$denied
  The deploy writes to these — expect it to fail unless that is just a read-only denial."

# --- config --------------------------------------------------------------
[ -f .env ] || fail ".env is missing — start from: cp .env.example .env"
blank=""
for k in LARK_APP_ID LARK_APP_SECRET LARK_ENCRYPT_KEY LARK_VERIFICATION_TOKEN LARK_ADMIN_OPEN_ID; do
  [ -n "${!k:-}" ] || blank="$blank $k"
done
[ -z "$blank" ] || fail "unset in .env:$blank
  Get these from the Lark developer console (README → Lark console setup)."

echo "  account : $ACCOUNT"
echo "  region  : $REGION${PROFILE:+   profile: $PROFILE}"
echo "  model   : $MODEL"
echo "  search  : $WEB_SEARCH$([ "$WEB_SEARCH" = "true" ] && echo '  (+ a gateway in us-east-1)')"
