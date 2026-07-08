#!/usr/bin/env bash
# Manage the user allowlist in the DynamoDB identity table.
# Usage:
#   scripts/manage-allowlist.sh add lark:ou_xxx
#   scripts/manage-allowlist.sh remove lark:ou_xxx
#   scripts/manage-allowlist.sh list
set -euo pipefail

PROFILE="${PROFILE:-default}"
REGION="${REGION:-us-west-2}"
TABLE="lark-agent-identity"
export AWS_PROFILE="$PROFILE" AWS_REGION="$REGION"

cmd="${1:-list}"
key="${2:-}"

case "$cmd" in
  add)
    [ -n "$key" ] || { echo "usage: $0 add lark:ou_xxx"; exit 1; }
    aws dynamodb put-item --table-name "$TABLE" \
      --item "{\"PK\":{\"S\":\"ALLOW#$key\"},\"SK\":{\"S\":\"ALLOW\"},\"channelKey\":{\"S\":\"$key\"}}"
    echo "added $key" ;;
  remove)
    [ -n "$key" ] || { echo "usage: $0 remove lark:ou_xxx"; exit 1; }
    aws dynamodb delete-item --table-name "$TABLE" \
      --key "{\"PK\":{\"S\":\"ALLOW#$key\"},\"SK\":{\"S\":\"ALLOW\"}}"
    echo "removed $key" ;;
  list)
    aws dynamodb scan --table-name "$TABLE" \
      --filter-expression "SK = :s" --expression-attribute-values '{":s":{"S":"ALLOW"}}' \
      --query "Items[].channelKey.S" --output table ;;
  *) echo "usage: $0 [add|remove|list] [lark:ou_xxx]"; exit 1 ;;
esac
