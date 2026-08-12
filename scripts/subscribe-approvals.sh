#!/usr/bin/env bash
# Subscribe the app to approval events for each definition the agent may decide.
#
# Ticking the event in the developer console is necessary but NOT sufficient: Lark
# delivers approval events only for definitions also subscribed through this API, per
# https://open.feishu.cn/document/server-docs/approval-v4/event/subscription-steps
# ("在应用后台订阅审批事件后，仍需要再次通过审批开放接口订阅指定的审批定义").
#
# Idempotent — an already-subscribed definition is reported and left alone.
#
# Usage: scripts/subscribe-approvals.sh [--unsubscribe]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

[ -f .env ] && { set -a; . ./.env; set +a; }

ACTION="subscribe"
[ "${1:-}" = "--unsubscribe" ] && ACTION="unsubscribe"

: "${LARK_APP_ID:?set LARK_APP_ID in .env}"
: "${LARK_APP_SECRET:?set LARK_APP_SECRET in .env}"
DOMAIN="${LARK_API_DOMAIN:-https://open.larksuite.com}"

if [ -z "${AGENT_DECIDE_APPROVAL_CODES:-}" ]; then
  echo "AGENT_DECIDE_APPROVAL_CODES is empty — nothing to $ACTION."
  echo "The agent decides nothing without it, so no approval event is worth receiving."
  exit 0
fi

TOKEN="$(curl -sS -X POST "$DOMAIN/open-apis/auth/v3/tenant_access_token/internal" \
  -H 'Content-Type: application/json' \
  -d "{\"app_id\":\"$LARK_APP_ID\",\"app_secret\":\"$LARK_APP_SECRET\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tenant_access_token",""))')"
[ -n "$TOKEN" ] || { echo "could not mint a tenant token — check LARK_APP_ID/SECRET" >&2; exit 1; }

# Read into an array rather than piping into a loop: a `while read` in a pipeline runs
# in a subshell, where the failure flag set below would be discarded.
IFS=',' read -r -a CODES <<<"$AGENT_DECIDE_APPROVAL_CODES"

rc=0
for code in "${CODES[@]}"; do
  code="$(echo "$code" | xargs)"   # trims the space a human leaves after the comma
  [ -n "$code" ] || continue
  resp="$(curl -sS -X POST "$DOMAIN/open-apis/approval/v4/approvals/$code/$ACTION" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' || echo '{}')"
  RESP="$resp" CODE="$code" ACTION="$ACTION" python3 -c '
import json, os
code, action, raw = os.environ["CODE"], os.environ["ACTION"], os.environ["RESP"]
try:
    b = json.loads(raw)
except Exception:
    print(f"  {code}: unparseable response: {raw[:120]}"); raise SystemExit(1)
c, msg = b.get("code"), b.get("msg", "")
if c == 0:
    print(f"  {code}: {action}d")
elif "already" in msg.lower() or "subscription" in msg.lower():
    # Already in the requested state is success here: this is meant to be re-runnable
    # after every deploy.
    print(f"  {code}: already {action}d ({msg})")
else:
    print(f"  {code}: FAILED code={c} msg={msg}")
    raise SystemExit(1)
' || rc=1
done

echo
if [ "$ACTION" = "subscribe" ]; then
  echo "Approval events for these definitions now reach the router's webhook."
  echo "Also required, in the developer console: Event Subscriptions -> 审批任务状态变更"
  echo "(approval_task), plus the approval:approval scope."
fi
exit $rc
