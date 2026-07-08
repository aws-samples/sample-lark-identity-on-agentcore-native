#!/usr/bin/env bash
# Build the Lark MCP server image natively on ARM64 via CodeBuild, then push to ECR.
# Prints the image URI. runtime creation is a separate step (deploy.sh --mcp).
#
# Local QEMU cross-builds aren't reliable for ARM64 native artifacts, so use CodeBuild's aarch64 image to build natively.
# Provisions its own CodeBuild role; reuses the source bucket the agentcore CLI created in this account.
#
# Usage: [PROFILE=p REGION=r] scripts/build-mcp.sh
set -euo pipefail

PROFILE="${PROFILE:-}"   # empty -> ambient creds
REGION="${REGION:-us-west-2}"
PREFIX="lark-id"
export AWS_REGION="$REGION"
[ -n "$PROFILE" ] && export AWS_PROFILE="$PROFILE"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REPO="$PREFIX-mcp-server"
ECR="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO"
LARK_MCP_VERSION="${LARK_MCP_VERSION:-0.5.1}"
TAG="$LARK_MCP_VERSION"
PROJECT="$PREFIX-mcp-builder"
SRC_BUCKET="bedrock-agentcore-codebuild-sources-$ACCOUNT-$REGION"
CB_ROLE_NAME="$PREFIX-mcp-builder-role"

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
SRC_KEY="$PREFIX-mcp/source.zip"
TMPZIP="$(mktemp -u).zip"   # -u: name only, let zip create it (zip rejects a pre-existing empty file)
( cd mcp-server && zip -qr "$TMPZIP" Dockerfile )
aws s3 cp "$TMPZIP" "s3://$SRC_BUCKET/$SRC_KEY" >/dev/null
rm -f "$TMPZIP"  # safe-rm-ok
echo "  s3://$SRC_BUCKET/$SRC_KEY"

log "Create/update CodeBuild project ($PROJECT, ARM64 native)"
# Build the whole project definition in Python so buildspec embedding is valid JSON.
PROJ_FILE="$(mktemp).json"
ECR="$ECR" TAG="$TAG" LARK_MCP_VERSION="$LARK_MCP_VERSION" PROJECT="$PROJECT" \
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
    f"      - docker build --build-arg LARK_MCP_VERSION={e['LARK_MCP_VERSION']} -t {ecr}:{tag} .",
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
while :; do
  ph="$(aws codebuild batch-get-builds --ids "$BID" --query 'builds[0].buildStatus' --output text)"
  [ "$ph" = "IN_PROGRESS" ] || { echo "  build status: $ph"; break; }
  sleep 10
done
[ "$ph" = "SUCCEEDED" ] || { echo "build failed — see CodeBuild logs for $BID"; exit 1; }

log "Done"
echo "IMAGE_URI=$ECR:$TAG"
