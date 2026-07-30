#!/usr/bin/env bash
# Read Lark credentials from .env and write them to Secrets Manager. Optionally
# allowlist your open_id. Override target with PROFILE=... REGION=... env vars.
#
# Prereqs (Lark developer console):
#   1. Self-built app with "Bot" capability (chat-only variant — no web app).
#   2. Permissions: im:message, im:message:send_as_bot, im:message.content:readonly,
#      im:chat:readonly, im:resource, contact:user.base:readonly
#      + User Token Scopes for 3LO: drive:drive, docx:document, offline_access
#   3. Event subscription: subscribe im.message.receive_v1; set Request URL to the
#      webhook URL printed below; enable encryption and note the Encrypt Key.
#   4. Security settings: register the OAuth provider's callbackUrl
#      (.../identities/oauth2/callback/<uuid>) as a redirect URL for the 3LO flow.
#   5. Publish the app.
#
# Fill .env (see .env.example) BEFORE running this.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

[ -f .env ] || { echo "missing .env — copy .env.example to .env and fill it in"; exit 1; }
# Deployment target: command-line env vars win over .env, which wins over defaults.
_CLI_PROFILE="${PROFILE:-}" _CLI_REGION="${REGION:-}"
set -a; . ./.env; set +a
PROFILE="${_CLI_PROFILE:-${PROFILE:-}}"   # empty -> ambient creds (instance role / env)
REGION="${_CLI_REGION:-${REGION:-us-west-2}}"
PREFIX="lark-agent"
export AWS_REGION="$REGION"
# Credentials already in the environment outrank .env's profile.
[ -n "${AWS_ACCESS_KEY_ID:-}" ] || { [ -n "$PROFILE" ] && export AWS_PROFILE="$PROFILE"; } || true
TABLE="$PREFIX-identity"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

: "${LARK_APP_ID:?set LARK_APP_ID in .env}"
: "${LARK_APP_SECRET:?set LARK_APP_SECRET in .env}"
: "${LARK_ENCRYPT_KEY:?set LARK_ENCRYPT_KEY in .env}"
: "${LARK_VERIFICATION_TOKEN:?set LARK_VERIFICATION_TOKEN in .env}"

WEBHOOK="$(aws cloudformation describe-stacks --stack-name "$PREFIX-router" \
  --query "Stacks[0].Outputs[?OutputKey=='WebhookLarkUrl'].OutputValue" --output text 2>/dev/null || true)"

log "Register these in the Lark developer console (if not done):"
echo "  Event Request URL : ${WEBHOOK:-<deploy router first>}"

log "Writing credentials to Secrets Manager ($PREFIX/channels/lark)"
TMPF="$(mktemp)"
cat > "$TMPF" <<JSON
{"appId":"$LARK_APP_ID","appSecret":"$LARK_APP_SECRET","verificationToken":"$LARK_VERIFICATION_TOKEN","encryptKey":"$LARK_ENCRYPT_KEY"}
JSON
aws secretsmanager put-secret-value --secret-id "$PREFIX/channels/lark" \
  --secret-string "file://$TMPF" >/dev/null
rm -f "$TMPF"
echo "  stored."

if [ -n "${LARK_ADMIN_OPEN_ID:-}" ]; then
  log "Allowlisting lark:$LARK_ADMIN_OPEN_ID"
  aws dynamodb put-item --table-name "$TABLE" --item "$(cat <<JSON
{"PK":{"S":"ALLOW#lark:$LARK_ADMIN_OPEN_ID"},"SK":{"S":"ALLOW"},"channelKey":{"S":"lark:$LARK_ADMIN_OPEN_ID"}}
JSON
)"
  echo "  allowlisted."
else
  echo "  (LARK_ADMIN_OPEN_ID blank — skipping allowlist; add later via scripts/manage-allowlist.sh)"
fi

log "Done."
