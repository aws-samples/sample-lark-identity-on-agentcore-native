#!/usr/bin/env bash
# Deploy this sample end to end.
#
#   ./deploy.sh              everything, in dependency order (the normal case)
#   ./deploy.sh <step>       one step: base | mcp | approval | 3lo | gateway | runtime | lark
#   ./deploy.sh urls         reprint the two URLs you must register in Lark
#   ./deploy.sh preflight    check tools, credentials and .env without deploying
#
# Target comes from .env (PROFILE / REGION / MODEL_ID); command-line env vars win:
#   REGION=ap-northeast-1 ./deploy.sh
#
# Nothing here waits for human input: the two values you have to paste into the
# Lark console don't block the deploy, they just gate the bot at runtime, so they
# are collected and printed at the end. Implementations live in scripts/ — this
# file owns the order, and every step is idempotent, so re-running is safe.
set -euo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

PREFIX="lark-agent"
# .env holds the config, but an env var given on the command line has to win —
# sourcing with `set -a` would otherwise clobber what the caller just asked for.
_CLI_PROFILE="${PROFILE:-}" _CLI_REGION="${REGION:-}" _CLI_WEB_SEARCH="${WEB_SEARCH:-}"
[ -f .env ] && { set -a; . ./.env; set +a; }
PROFILE="${_CLI_PROFILE:-${PROFILE:-}}"
REGION="${_CLI_REGION:-${REGION:-us-west-2}}"
export AWS_REGION="$REGION"
# Credentials already in the environment outrank .env's profile.
[ -n "${AWS_ACCESS_KEY_ID:-}" ] || { [ -n "$PROFILE" ] && export AWS_PROFILE="$PROFILE"; } || true
# Each step re-sources .env, so an override has to be exported to reach them.
[ -n "$_CLI_WEB_SEARCH" ] && export WEB_SEARCH="$_CLI_WEB_SEARCH"

step() { printf '\n\033[1;36m### %s\033[0m\n' "$*"; }
note() { printf '\033[1;33m%s\033[0m\n' "$*"; }

run_preflight() { step "preflight";                       scripts/preflight.sh; }
run_base()    { step "base — CDK stacks";                scripts/provision.sh --base; }
run_mcp()     { step "mcp — Lark MCP server";            scripts/build-mcp.sh; }
run_approval(){ step "approval — Approval MCP server";   scripts/build-approval.sh; }
run_3lo()     { step "3lo — Identity + OAuth provider";  scripts/setup-3lo.sh; }
run_gateway() { step "gateway — Web Search (optional)";  scripts/provision.sh --gateway; }
run_runtime() { step "runtime — agent";                  scripts/provision.sh --runtime; }
run_lark()    { step "lark — credentials + allowlist";   scripts/setup-lark.sh; }

# What the user still has to do by hand, gathered from deployed state rather than
# scraped from the logs above.
print_urls() {
  local webhook callback
  webhook="$(aws cloudformation describe-stacks --stack-name "$PREFIX-router" \
    --query "Stacks[0].Outputs[?OutputKey=='WebhookLarkUrl'].OutputValue" \
    --output text 2>/dev/null || true)"
  callback="$(aws bedrock-agentcore-control get-oauth2-credential-provider \
    --name "$PREFIX-3lo" --query callbackUrl --output text 2>/dev/null || true)"

  printf '\n\033[1;32m=== Register these in the Lark developer console ===\033[0m\n'
  note "Events & Callbacks → Request URL:"
  echo "  ${webhook:-<not deployed yet>}"
  note "Security Settings → Redirect URLs:"
  echo "  ${callback:-<not deployed yet>}"
  echo
  echo "Then publish a version. See README.md → 'Lark console setup' for the"
  echo "permission scopes and the im.message.receive_v1 event subscription."
}

usage() { sed -n '2,14p' "$0" | cut -c3-; }

case "${1:-all}" in
  base)    run_base ;;
  mcp)     run_mcp ;;
  approval) run_approval ;;
  3lo)     run_3lo ;;
  gateway) run_gateway ;;
  runtime) run_runtime ;;
  lark)    run_lark ;;
  urls)    print_urls ;;
  preflight) run_preflight ;;
  all|"")
    run_preflight
    # 3lo and gateway precede runtime: the agent is deployed with the provider,
    # workload and gateway URL baked into its environment.
    run_base; run_mcp; run_3lo; run_gateway; run_runtime; run_lark
    # approval is opt-in: it provisions another Runtime (its own cold start and
    # memory-time) and does nothing until AGENT_DECIDE_APPROVAL_CODES names an approval
    # definition. Run `./deploy.sh approval` when demoing that scenario.
    print_urls
    ;;
  -h|--help) usage ;;
  *) echo "unknown step: $1"; echo; usage; exit 1 ;;
esac
