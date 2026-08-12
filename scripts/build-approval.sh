#!/usr/bin/env bash
# Build the Lark Approval MCP server image natively on ARM64 via CodeBuild, push to
# ECR, then create/update its Runtime. Separate from the lark-cli server on purpose:
# approve/reject accept only a tenant token, and the decision guards have to be bound
# to specific tools rather than a generic passthrough. See .dev/adr/0006.
# Prints the image URI, then creates/updates the MCP Runtime (./deploy.sh mcp).
#
# Local QEMU cross-builds aren't reliable for ARM64 native artifacts, so use CodeBuild's aarch64 image to build natively.
# Provisions its own CodeBuild role; reuses the source bucket the agentcore CLI created in this account.
#
# Usage: [PROFILE=p REGION=r] scripts/build-mcp.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"

# Deployment target: command-line env vars win over .env, which wins over defaults.
_CLI_PROFILE="${PROFILE:-}" _CLI_REGION="${REGION:-}"
[ -f .env ] && { set -a; . ./.env; set +a; }
PROFILE="${_CLI_PROFILE:-${PROFILE:-}}"   # empty -> ambient creds
REGION="${_CLI_REGION:-${REGION:-us-west-2}}"
PREFIX="lark-agent"
export AWS_REGION="$REGION"
# Credentials already in the environment outrank .env's profile.
[ -n "${AWS_ACCESS_KEY_ID:-}" ] || { [ -n "$PROFILE" ] && export AWS_PROFILE="$PROFILE"; } || true

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REPO="$PREFIX-mcp-approval"
ECR="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO"
LARK_CLI_VERSION="${LARK_CLI_VERSION:-1.0.68}"   # engine = official lark-cli (not lark-mcp)
TAG="cli-$LARK_CLI_VERSION"
PROJECT="$PREFIX-mcp-builder"
SRC_BUCKET="bedrock-agentcore-codebuild-sources-$ACCOUNT-$REGION"
CB_ROLE_NAME="$PREFIX-mcp-builder-role"
RUNTIME_NAME="${PREFIX//-/_}_approval"   # AgentCore runtime names use underscores
LARK_API_DOMAIN="${LARK_API_DOMAIN:-https://open.larksuite.com}"   # CN: open.feishu.cn
: "${LARK_APP_ID:?set LARK_APP_ID in .env}"
: "${LARK_APP_SECRET:?set LARK_APP_SECRET in .env}"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

log "CodeBuild service role"
if ! aws iam get-role --role-name "$CB_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$CB_ROLE_NAME" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
fi
aws iam put-role-policy --role-name "$CB_ROLE_NAME" --policy-name build \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
    {\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"arn:aws:logs:$REGION:$ACCOUNT:log-group:/aws/codebuild/$PROJECT*\"},
    {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:GetObjectVersion\"],\"Resource\":\"arn:aws:s3:::$SRC_BUCKET/*\"},
    {\"Effect\":\"Allow\",\"Action\":\"ecr:GetAuthorizationToken\",\"Resource\":\"*\"},
    {\"Effect\":\"Allow\",\"Action\":[\"ecr:BatchCheckLayerAvailability\",\"ecr:PutImage\",\"ecr:InitiateLayerUpload\",\"ecr:UploadLayerPart\",\"ecr:CompleteLayerUpload\",\"ecr:BatchGetImage\",\"ecr:GetDownloadUrlForLayer\"],\"Resource\":\"arn:aws:ecr:$REGION:$ACCOUNT:repository/$REPO\"}]}" >/dev/null
CB_ROLE_ARN="$(aws iam get-role --role-name "$CB_ROLE_NAME" --query 'Role.Arn' --output text)"
sleep 8  # let the new role/policy propagate before CodeBuild validates it

log "ECR repo"
aws ecr describe-repositories --repository-names "$REPO" >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "$REPO" >/dev/null
echo "  $ECR"

log "Upload build source to S3"
# The agentcore CLI normally creates this bucket, but it may not have run yet in 
# the target region — create it on demand so the first build in a fresh region works.
aws s3api head-bucket --bucket "$SRC_BUCKET" >/dev/null 2>&1 || \
  aws s3api create-bucket --bucket "$SRC_BUCKET" \
    --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
SRC_KEY="$PREFIX-approval/source.zip"
TMPZIP="$(mktemp -u).zip"   # -u: name only, let zip create it (zip rejects a pre-existing empty file)
( cd mcp-servers/approval && zip -qr "$TMPZIP" . -x '*.pyc' '__pycache__/*' )  # Dockerfile + proxy.py + any other source
aws s3 cp "$TMPZIP" "s3://$SRC_BUCKET/$SRC_KEY" >/dev/null
rm -f "$TMPZIP"  # safe-rm-ok
echo "  s3://$SRC_BUCKET/$SRC_KEY"

log "Create/update CodeBuild project ($PROJECT, ARM64 native)"
# Build the whole project definition in Python so buildspec embedding is valid JSON.
PROJ_FILE="$(mktemp).json"
ECR="$ECR" TAG="$TAG" LARK_CLI_VERSION="$LARK_CLI_VERSION" PROJECT="$PROJECT" \
SRC_BUCKET="$SRC_BUCKET" SRC_KEY="$SRC_KEY" CB_ROLE_ARN="$CB_ROLE_ARN" \
python3 <<'PY' > "$PROJ_FILE"
import json, os
e = os.environ
ecr, tag = e["ECR"], e["TAG"]
buildspec = "\n".join([
    "version: 0.2",
    "phases:",
    "  pre_build:",
    "    commands:",
    f"      - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin {ecr}",
    "  build:",
    "    commands:",
    f"      - docker build --build-arg LARK_CLI_VERSION={e['LARK_CLI_VERSION']} -t {ecr}:{tag} .",
    "  post_build:",
    "    commands:",
    f"      - docker push {ecr}:{tag}",
])
print(json.dumps({
    "name": e["PROJECT"],
    "source": {"type": "S3", "location": f"{e['SRC_BUCKET']}/{e['SRC_KEY']}", "buildspec": buildspec},
    "artifacts": {"type": "NO_ARTIFACTS"},
    "environment": {"type": "ARM_CONTAINER", "image": "aws/codebuild/amazonlinux2-aarch64-standard:3.0",
                    "computeType": "BUILD_GENERAL1_MEDIUM", "privilegedMode": True},
    "serviceRole": e["CB_ROLE_ARN"],
}))
PY
if aws codebuild batch-get-projects --names "$PROJECT" --query 'projects[0].name' --output text 2>/dev/null | grep -q "$PROJECT"; then
  aws codebuild update-project --cli-input-json "file://$PROJ_FILE" >/dev/null
else
  aws codebuild create-project --cli-input-json "file://$PROJ_FILE" >/dev/null
fi
rm -f "$PROJ_FILE"  # safe-rm-ok

log "Start build"
BID="$(aws codebuild start-build --project-name "$PROJECT" --query 'build.id' --output text)"
echo "  build: $BID"
ph=""
while :; do
  ph="$(aws codebuild batch-get-builds --ids "$BID" --query 'builds[0].buildStatus' --output text)"
  if [ "$ph" != "IN_PROGRESS" ]; then
    echo "  build status: $ph"
    break
  fi
  sleep 10
done
if [ "$ph" != "SUCCEEDED" ]; then
  echo "build failed — see CodeBuild logs for $BID"
  exit 1
fi

log "MCP server Runtime ($RUNTIME_NAME)"
# The per-user Lark token reaches the container in a custom passthrough header —
# it is stripped at the edge unless listed in requestHeaderAllowlist.
ROLE_ARN="$(aws cloudformation describe-stacks --stack-name "$PREFIX-agentcore" \
  --query "Stacks[0].Outputs[?OutputKey=='ExecutionRoleArn'].OutputValue" --output text 2>/dev/null)"
[ -n "$ROLE_ARN" ] && [ "$ROLE_ARN" != "None" ] || {
  echo "execution role not found — run ./deploy.sh base first"; exit 1; }

ARTIFACT="{\"containerConfiguration\":{\"containerUri\":\"$ECR:$TAG\"}}"
HEADERS='{"requestHeaderAllowlist":["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Lark-Token"]}'
# This runtime needs a real appSecret, unlike the lark-cli server — approve/reject
# mint a tenant token, and the container has no AWS SDK to fetch it from Secrets
# Manager (adding one to reach a single value costs more than it returns). So the
# secret is injected as an environment variable, which widens where it lives: an
# operator who can read this Runtime's config can read it. Worth knowing given what
# the tenant token can do here — complete any approval in the tenant (.dev/adr/0006).
# AGENT_DECIDE_* are the code-enforced limits on what the agent may decide; an empty
# allow-list decides nothing (fail closed) until set deliberately in .env.
# Built as JSON, not the shorthand key=value,key=value form: an allow-list holds
# several approval codes separated by commas, and the shorthand parser would split on
# those and silently mangle the whole map. Failure mode was an empty allow-list, i.e.
# the guards refusing everything with no error anywhere.
ENVVARS="$(LARK_APP_ID="$LARK_APP_ID" LARK_APP_SECRET="$LARK_APP_SECRET" \
  LARK_API_DOMAIN="$LARK_API_DOMAIN" \
  AGENT_DECIDE_APPROVAL_CODES="${AGENT_DECIDE_APPROVAL_CODES:-}" \
  AGENT_DECIDE_MAX_AMOUNT="${AGENT_DECIDE_MAX_AMOUNT:-1000}" \
  uv run python -c '
import json, os
print(json.dumps({
    "APP_ID": os.environ["LARK_APP_ID"],
    "APP_SECRET": os.environ["LARK_APP_SECRET"],
    "LARK_DOMAIN": os.environ.get("LARK_API_DOMAIN", ""),
    "AGENT_DECIDE_APPROVAL_CODES": os.environ.get("AGENT_DECIDE_APPROVAL_CODES", ""),
    "AGENT_DECIDE_MAX_AMOUNT": os.environ.get("AGENT_DECIDE_MAX_AMOUNT", "1000"),
}))')"

# Surface what the guards will actually allow — an empty allow-list is a silent
# "decide nothing", which is safe but confusing if it was not intended.
if [ -z "${AGENT_DECIDE_APPROVAL_CODES:-}" ]; then
  printf '\033[1;33m%s\033[0m\n' "  AGENT_DECIDE_APPROVAL_CODES is empty — the agent will decline every decision"
else
  echo "  agent may decide on: $AGENT_DECIDE_APPROVAL_CODES (max amount ${AGENT_DECIDE_MAX_AMOUNT:-1000})"
fi

RID="$(aws bedrock-agentcore-control list-agent-runtimes \
  --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].agentRuntimeId" \
  --output text 2>/dev/null | head -1)"
if [ -n "$RID" ] && [ "$RID" != "None" ]; then
  aws bedrock-agentcore-control update-agent-runtime --agent-runtime-id "$RID" \
    --agent-runtime-artifact "$ARTIFACT" --role-arn "$ROLE_ARN" \
    --network-configuration '{"networkMode":"PUBLIC"}' \
    --protocol-configuration '{"serverProtocol":"MCP"}' \
    --environment-variables "$ENVVARS" \
    --request-header-configuration "$HEADERS" >/dev/null
  echo "  updated $RUNTIME_NAME ($RID)"
else
  RID="$(aws bedrock-agentcore-control create-agent-runtime --agent-runtime-name "$RUNTIME_NAME" \
    --agent-runtime-artifact "$ARTIFACT" --role-arn "$ROLE_ARN" \
    --network-configuration '{"networkMode":"PUBLIC"}' \
    --protocol-configuration '{"serverProtocol":"MCP"}' \
    --environment-variables "$ENVVARS" \
    --request-header-configuration "$HEADERS" \
    --query agentRuntimeId --output text)"
  echo "  created $RUNTIME_NAME ($RID)"
fi

log "Done"
echo "IMAGE_URI=$ECR:$TAG"
echo "RUNTIME=$RUNTIME_NAME ($RID) — next: ./deploy.sh 3lo"
