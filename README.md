# Lark Identity on AgentCore — AgentCore Identity

A reference implementation of enterprise identity on Amazon Bedrock AgentCore, using **Lark (Feishu) as the identity provider**. A simple agent is reachable from **Lark bot chat**; every message resolves to a `lark:{open_id}` identity, and downstream MCP tools **act as that user against Lark** with the user's own token — so they reach only what that user can, and Lark itself adjudicates access. The agent inherits both *who you are* and *what you're allowed to do*, adding nothing of its own.

This is the **AgentCore Identity** variant: per-user Lark tokens live in the **AgentCore Identity Token Vault** (OAuth 3LO), which stores, refreshes, and injects each user's token natively — no custom interceptor, no self-managed token store. The sibling repo (`lark-agentcore-interceptor`) achieves the same guarantees with a Gateway Request Interceptor and self-managed vaulting; the two differ only in how the downstream hop resolves per-user credentials.

## Architecture (target)

```
                     ┌──────────────────────┐      InvokeAgentRuntime (SigV4)
                     │    Router Lambda     │      payload carries actorId
  Lark message ────▶ │  verify / decrypt /  ├────────────────┐
  (webhook)          │     resolve user     │                │
                     └──────────────────────┘                ▼
                              ┌───────────────────────────────────────────────┐
                              │  Agent container (ARM64, AgentCore Runtime)   │
                              │   :8080  /ping  /invocations(POST)            │
                              │   Strands Agent + AgentCore Memory (per-user) │
                              └───────────────────────┬───────────────────────┘
                                        │ MCP call, Bearer = user's Cognito ACCESS token
                                        ▼
                              ┌───────────────────────┐   ┌─────────────────────────┐
                              │   AgentCore Gateway   │◀──┤   AgentCore Identity    │
                              │  customJWTAuthorizer  │   │  Token Vault (3LO):     │
                              │   (Cognito, inbound)  │   │  stores / refreshes /   │
                              └───────────┬───────────┘   │  injects THIS user's    │
                                          │ per-user      │  Lark user_access_token │
                                          │ token injected└────────────┬────────────┘
                                          ▼                            │ RFC-6749 token calls
                              ┌───────────────────────┐   ┌────────────▼────────────┐
                              │    Lark MCP server    │   │     Lark OAuth shim     │
                              │  (AgentCore Runtime,  │   │  (Lambda + API GW)      │
                              │    mcpServer target)  │   │  form ⇄ JSON translate, │
                              └───────────┬───────────┘   │  code!=0 → 4xx          │
                                          │ HTTPS, Bearer └────────────┬────────────┘
                                          │ = user_access_token        │ JSON, code:0 envelope
                                          ▼                            ▼
                              ┌─────────────────────────────────────────────────────┐
                              │  Lark REST API  →  returns only what THIS user can  │
                              └─────────────────────────────────────────────────────┘

  Identity: every message resolves to  lark:{open_id}  (Cognito JWT minted by the agent).
  First-time consent: no vaulted token yet → the Gateway answers the tool call with an
  MCP elicitation (-32042) carrying an authorization URL → the agent sends that URL as a
  chat message → the user authorizes on Lark's own consent page (via the shim) → Identity
  vaults the token → the retried call succeeds. Later calls inject silently; the agent
  never holds the Lark token.
```

Status: being converted from the interceptor baseline — see the phase plan below. The interceptor's Lambda-target tools still exist transitionally and are replaced by the MCP server + Token Vault in Phases 1–3.

## Layout

| Path | What |
|---|---|
| `app.py`, `cdk.json` | CDK app (uv-managed deps) — 5 stacks |
| `stacks/` | security, agentcore, router, gateway, observability |
| `agent/` | Strands agent container (HTTP contract + AgentCore Memory + MCP Gateway client) |
| `lambda/router/` | Lark webhook: verify/decrypt/tenant-token/send |
| `lambda/tools/` | transitional Lambda tools — replaced by the MCP server in Phase 2 |
| `scripts/` | deploy / setup-lark / manage-allowlist / test / destroy |
| `docs/architecture.md` | full architecture (being updated to the native flow) |

## Deploy

Prereqs: `uv`, Docker, the AgentCore CLI (`npm i -g @aws/agentcore`), and AWS credentials. Scripts default to the `default` profile / `us-west-2`; override with `PROFILE=... REGION=...`. Resources deploy under the `lark-id` prefix (independent of the sibling's `lark-agent`).

```bash
cp .env.example .env          # fill in Lark appId/appSecret/encryptKey/token + your open_id
scripts/deploy.sh --base      # CDK base stacks (security, agentcore, router, gateway, observability)
scripts/deploy.sh --gateway   # create the MCP Gateway (protocol 2025-11-25, 3LO-capable)
scripts/deploy.sh --runtime   # build ARM64 image (CodeBuild) + deploy the Runtime (AgentCore CLI)
scripts/setup-lark.sh         # read .env → Secrets Manager; print webhook URL; allowlist you
```

## Lark console setup

1. **Add features**: enable **Bot**.
2. **Permissions & Scopes**: `im:message`, `im:message:readonly`, **`im:message.p2p_msg:readonly`** (required for single-chat), `im:message:send_as_bot`, `im:resource`, `contact:user.base:readonly`. Under **User Token Scopes** (needs admin approval): `drive:drive`, `docx:document`, `offline_access`.
3. **Events & Callbacks**: Request URL = the webhook URL from deploy output; enable Encryption; add `im.message.receive_v1`.
4. **Security Settings**: add the OAuth shim's redirect URL (printed in Phase 3) to Redirect URLs.
5. **Publish** a version (re-publish after any scope/event change).

## Test

```bash
scripts/test.sh              # agent + router unit suites
```
