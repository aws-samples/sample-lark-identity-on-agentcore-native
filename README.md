# Lark Identity on AgentCore — AgentCore Identity

A reference implementation of enterprise identity on Amazon Bedrock AgentCore, using **Lark (Feishu) as the identity provider**. A simple agent is reachable from **Lark bot chat**; every message resolves to a `lark:{open_id}` identity, and downstream MCP tools **act as that user against Lark** with the user's own token — so they reach only what that user can, and Lark itself adjudicates access. The agent inherits both *who you are* and *what you're allowed to do*, adding nothing of its own.

This is the **AgentCore Identity** variant: per-user Lark tokens live in the **AgentCore Identity Token Vault** (OAuth 3LO), which stores, refreshes, and injects each user's token natively — no custom interceptor, no self-managed token store. The sibling repo [lark-identity-on-agentcore-interceptor](https://github.com/aleck31/lark-identity-on-agentcore-interceptor) achieves the same guarantees with a Gateway Request Interceptor and self-managed vaulting; the two differ only in how the downstream hop resolves per-user credentials.

## Architecture

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
                          agent-side 3LO: fetch THIS user's │ token from the vault
                                        ▼
                              ┌───────────────────────┐   ┌─────────────────────────┐
                              │  agent_core.lark_3lo  │◀──┤   AgentCore Identity    │
                              │ GetResourceOauth2Token│   │  Token Vault (3LO):     │
                              │  (USER_FEDERATION)    │   │  stores / refreshes /   │
                              └───────────┬───────────┘   │  THIS user's            │
                                          │ SigV4 + token │  Lark user_access_token │
                                          │ in custom hdr └────────────┬────────────┘
                                          ▼                            │ RFC-6749 token calls
                              ┌───────────────────────┐   ┌────────────▼────────────┐
                              │   Lark MCP server     │   │     Lark OAuth shim     │
                              │  (AgentCore Runtime,  │   │  (Lambda + API GW)      │
                              │   lark-cli engine)    │   │  form ⇄ JSON translate, │
                              └───────────┬───────────┘   │  code!=0 → 4xx          │
                                          │ lark-cli as   └────────────┬────────────┘
                                          │ user_access_token          │ JSON, code:0 envelope
                                          ▼                            ▼
                              ┌─────────────────────────────────────────────────────┐
                              │  Lark REST API  →  returns only what THIS user can  │
                              └─────────────────────────────────────────────────────┘

  Identity: every message resolves to  lark:{open_id}.
  First-time consent (consent-wait): the agent has no vaulted token → the router posts a
  clickable "点击授权" link, holds and polls the vault, and re-invokes on approval — so the
  user gets the answer WITHOUT re-sending. Later turns fetch the token silently.
```

See **[docs/architecture.md](docs/architecture.md)** for the full flow, per-hop auth, and the consent-wait sequence; **[docs/agentcore-behavior.md](docs/agentcore-behavior.md)** and **[docs/native-3lo-builtin-vendor.md](docs/native-3lo-builtin-vendor.md)** for why 3LO is agent-side (the Gateway can't do per-user 3LO for a `CustomOauth2` provider like Lark — agentcore-samples#1424) and how to add other downstream systems.

## Layout

| Path | What |
|---|---|
| `app.py`, `cdk.json` | CDK app (uv-managed deps) — 6 stacks |
| `stacks/` | security, agentcore, router, shim, gateway, observability |
| `agent/` | Strands agent container: HTTP contract + AgentCore Memory + agent-side 3LO (`lark_3lo`) + MCP client to the lark-cli server |
| `lambda/router/` | Lark webhook: verify/decrypt/tenant-token/send + 3LO consent-wait (poll vault, re-invoke) |
| `lambda/shim/` | Lark OAuth RFC-6749 façade + 3LO return endpoint (`CompleteResourceTokenAuth`) |
| `mcp-server/` | Lark MCP server (AgentCore Runtime, lark-cli engine) — calls Lark as the user |
| `scripts/` | deploy / setup-lark / manage-allowlist / test / destroy |
| `docs/architecture.md` | full architecture (core flow updated to the native path; some sections marked legacy) |
| `docs/agentcore-behavior.md` | measured AgentCore Gateway/Runtime behavior + the CustomOauth2 3LO gap (#1424) |
| `docs/native-3lo-builtin-vendor.md` | reusable agent-driven 3LO reference (built-in vendors **and** CustomOauth2) — how to add a downstream system |

## Deploy

Prereqs: `uv`, Docker, the AgentCore CLI (`npm i -g @aws/agentcore`), and AWS credentials. Scripts default to the `default` profile / `us-west-2`; override with `PROFILE=... REGION=...`. Resources deploy under the `lark-id` prefix (independent of the sibling's `lark-agent`).

```bash
cp .env.example .env          # fill in Lark appId/appSecret/encryptKey/token + your open_id
scripts/deploy.sh --base      # CDK base stacks (security, agentcore, router, shim, gateway, observability)
scripts/build-mcp.sh          # build the lark-cli MCP server image (CodeBuild ARM64) + create its Runtime
scripts/deploy.sh --runtime   # build the agent image (CodeBuild ARM64) + deploy the agent Runtime (wires it to the MCP server)
scripts/setup-lark.sh         # read .env → Secrets Manager; print webhook URL; allowlist you
```

The `lark-id-3lo` OAuth credential provider (Lark behind the RFC-6749 shim) is registered separately — see `docs/native-3lo-builtin-vendor.md`. `scripts/deploy.sh --gateway` exists but is **not** on this variant's tool path (3LO is agent-side; the Gateway can't do per-user 3LO for a CustomOauth2 provider).

### Tear down

```bash
scripts/destroy.sh            # delete everything deploy.sh created (asks for confirmation)
# or: scripts/destroy.sh --yes  # skip the prompt
```

Deletes in dependency order — Gateway + targets → both CLI-created Runtimes (the agent and the lark-cli MCP server) → the CDK stacks. Idempotent: re-running skips already-gone resources. The OAuth credential provider (`lark-id-3lo`) and your Lark console app config are **not** touched; re-seed credentials from `.env` via `scripts/setup-lark.sh` on the next deploy.

## Lark console setup

1. **Add features**: enable **Bot**.
2. **Permissions & Scopes**: `im:message`, `im:message:readonly`, **`im:message.p2p_msg:readonly`** (required for single-chat), `im:message:send_as_bot`, `im:resource`, `contact:user.base:readonly`. Under **User Token Scopes** (needs admin approval): `drive:drive`, `docx:document`, `offline_access`.
3. **Events & Callbacks**: Request URL = the webhook URL from deploy output; enable Encryption; add `im.message.receive_v1`.
4. **Security Settings → Redirect URLs**: add the OAuth credential provider's `callbackUrl` (`https://bedrock-agentcore.<region>.amazonaws.com/identities/oauth2/callback/<uuid>`, from `get-oauth2-credential-provider --name lark-id-3lo`). This is where AgentCore Identity receives the 3LO code — not the shim URL.
5. **Publish** a version (re-publish after any scope/event change).

## Test

```bash
scripts/test.sh              # agent + router unit suites
```

### Notes & limitations

- **3LO is agent-side, not Gateway-mediated.** The Gateway does not do per-user 3LO for a `CustomOauth2` provider like Lark (AWS gap, agentcore-samples#1424), so the agent drives 3LO itself and delivers the vaulted token to the lark-cli MCP server in a custom passthrough header. See `docs/agentcore-behavior.md`.
- **Consent-wait is time-bounded.** On first use the router posts the consent link, then holds and polls the vault up to `AUTH_WAIT_SECONDS` (45s) before falling back to "re-send after approving". A user who takes longer than that to approve just re-sends once; the token is already vaulted by then.
- **Chat-only.** This variant has no web UI, no Cognito, and no Gateway on the tool path — the sibling `lark-agentcore-interceptor` is the Gateway/web-UI variant.
- **Two Runtimes.** The agent and the lark-cli MCP server are separate AgentCore Runtimes, both built via CodeBuild (ARM64) and created out-of-band by the CLI.
