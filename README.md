# Lark Identity on AgentCore — AgentCore Identity

A reference implementation of enterprise identity on Amazon Bedrock AgentCore, using **Lark (Feishu) as the identity provider**. A simple agent is reachable from **Lark bot chat**; every message resolves to a `lark:{open_id}` identity, and downstream MCP tools **act as that user against Lark** with the user's own token — so they reach only what that user can, and Lark itself adjudicates access. The agent inherits both *who you are* and *what you're allowed to do*, adding nothing of its own.

This is the **AgentCore Identity** variant: per-user Lark tokens live in the **AgentCore Identity Token Vault** (OAuth 3LO), which stores, refreshes, and injects each user's token natively — no custom interceptor, no self-managed token store. The sibling repo [lark-agententity-on-agentcore-interceptor](https://github.com/aws-samples/sample-lark-agententity-on-agentcore-interceptor) achieves the same guarantees with a Gateway Request Interceptor and self-managed vaulting; the two differ only in how the downstream hop resolves per-user credentials.

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
| `app.py`, `cdk.json` | CDK app (uv-managed deps) — 6 stacks. Deployment state goes to `.cdk-state.json`, not here |
| `.env` | Deployment target (`PROFILE`/`REGION`/`MODEL_ID`) + Lark credentials — gitignored, read by every script |
| `stacks/` | security, agentcore, router, shim, gateway, observability |
| `agent/` | Strands agent container: HTTP contract + AgentCore Memory + agent-side 3LO (`lark_3lo`) + MCP client to the lark-cli server |
| `lambda/router/` | Lark webhook: verify/decrypt/tenant-token/send + 3LO consent-wait + the chat commands |
| `lambda/shim/` | Lark OAuth RFC-6749 façade + 3LO return endpoint (`CompleteResourceTokenAuth`, then DMs the user) |
| `mcp-server/` | Lark MCP server (AgentCore Runtime, lark-cli engine) — calls Lark as the user |
| `scripts/` | deploy / build-mcp / setup-3lo / setup-lark / manage-allowlist / test / destroy |
| `docs/architecture.md` | full architecture (core flow updated to the native path; some sections marked legacy) |
| `docs/agentcore-behavior.md` | measured AgentCore Gateway/Runtime behavior + the CustomOauth2 3LO gap (#1424) |
| `docs/native-3lo-builtin-vendor.md` | reusable agent-driven 3LO reference (built-in vendors **and** CustomOauth2) — how to add a downstream system |

## Deploy

Prereqs: `uv`, Docker, the AgentCore CLI (`npm i -g @aws/agentcore`), and AWS credentials. The deployment target lives in `.env` (`PROFILE`, `REGION`, `MODEL_ID`); command-line env vars override it (`REGION=... scripts/deploy.sh`). Resources deploy under the `lark-agent` prefix. The first deploy into a region runs `cdk bootstrap` automatically.

```bash
cp .env.example .env          # deployment target (PROFILE/REGION/MODEL_ID) + Lark appId/appSecret/encryptKey/token + your open_id
scripts/deploy.sh --base      # CDK base stacks (security, agentcore, router, shim, gateway, observability)
scripts/build-mcp.sh          # build the lark-cli MCP server image (CodeBuild ARM64) + create/update its Runtime
scripts/setup-3lo.sh          # workload identity + OAuth credential provider; prints the callbackUrl for the Lark console
scripts/deploy.sh --runtime   # build the agent image (CodeBuild ARM64) + deploy the agent Runtime (wires it to the MCP server)
scripts/setup-lark.sh         # read .env → Secrets Manager; print webhook URL; allowlist you
```

`setup-3lo.sh` registers the `lark-agent-3lo` OAuth credential provider (Lark behind the RFC-6749 shim) plus the agent's workload identity, and prints the provider `callbackUrl` — register that in the Lark console (step 4 below) before the first 3LO consent. See `docs/native-3lo-builtin-vendor.md` to add other downstream systems.

Per-deployment ids (runtime/gateway) go to `.cdk-state.json` (gitignored), so `cdk.json` stays free of environment state. `scripts/deploy.sh --gateway` exists but is **not** on this variant's tool path (3LO is agent-side; the Gateway can't do per-user 3LO for a CustomOauth2 provider).

### Tear down

```bash
scripts/destroy.sh            # delete everything deploy.sh created (asks for confirmation)
# or: scripts/destroy.sh --yes  # skip the prompt
```

Deletes in dependency order — Gateway + targets → both CLI-created Runtimes (the agent and the lark-cli MCP server) → the CDK stacks. Idempotent: re-running skips already-gone resources. The OAuth credential provider (`lark-agent-3lo`) and your Lark console app config are **not** touched; re-seed credentials from `.env` via `scripts/setup-lark.sh` on the next deploy.

## Lark console setup

1. **Add features**: enable **Bot**.
2. **Permissions & Scopes**: `im:message`, `im:message:readonly`, **`im:message.p2p_msg:readonly`** (required for single-chat), `im:message:send_as_bot`, `im:resource`, `contact:user.base:readonly`. Under **User Token Scopes** (needs admin approval): `drive:drive`, `docx:document`, `offline_access`.
3. **Events & Callbacks**: Request URL = the webhook URL from deploy output; enable Encryption; add `im.message.receive_v1`.
4. **Security Settings → Redirect URLs**: add the OAuth credential provider's `callbackUrl` (`https://bedrock-agentcore.<region>.amazonaws.com/identities/oauth2/callback/<uuid>`, from `get-oauth2-credential-provider --name lark-agent-3lo`). This is where AgentCore Identity receives the 3LO code — not the shim URL.
5. **Publish** a version (re-publish after any scope/event change).

## Chat commands

Send these to the bot instead of a question. `/help` lists them in-chat.

| Command | What it does |
|---|---|
| `/auth` | Authorization status per IdP — one OAuth provider per downstream system |
| `/auth <idp>` | Authorize (or re-authorize) that IdP; always starts a fresh 3LO flow, so it is idempotent |
| `/status` | Identity, runtime session id, Memory thread id, message count, last activity |
| `/new` | New Memory thread **and** new runtime instance — a fully fresh start |
| `/reset` | New Memory thread, same runtime instance — history starts over, old events kept |
| `/clear` | Actually delete this thread's events (unlike `/reset`, which just stops reading them) |
| `/reconnect` | New runtime instance, same Memory thread — shows that memory outlives the container |

The three session commands exist because **the runtime session and the Memory thread are independent ids**: the first decides which microVM serves you, the second decides which conversation history the agent reads. The router owns both (DynamoDB `SESSION` / `MEMSESSION` items) and rotates them in the three meaningful combinations — rotating is just "switch to a new id", so all of them are instant. `/clear` is the only one that deletes data.

Authorization is a third, orthogonal dimension: the vaulted Lark token is keyed to `lark:{open_id}`, not to any session — so `/new` does **not** require re-authorizing. Token Vault refreshes the access token automatically; users only re-authorize if the refresh chain lapses, they revoke access in Lark, or the provider is recreated.

## Test

```bash
scripts/test.sh              # agent + router unit suites
```

## Cost

This deploys billable AWS resources. All the always-on pieces are consumption- or per-unit-priced (no fixed reservation), so an idle single-user demo in us-west-2 is on the order of a couple USD/month before model usage; the variable cost is dominated by the agent's Bedrock calls. Verify current rates on the AWS pricing pages — figures below are as researched, not a quote.

- **Bedrock model invocations** — the main usage-sensitive line; priced per input/output token on the model in `default_model_id`. A chatty demo is cents-to-dollars; a load test is not.
- **AgentCore Runtime ×2** — this variant runs **two** Runtimes (the agent and the lark-cli MCP server), each metered per-second: CPU (`~$0.0895/vCPU-hour`) billed only during active processing, memory (`~$0.00945/GB-hour`) accrues continuously while the microVM is alive. Two microVMs means roughly double the interceptor variant's Runtime memory-time at idle.
- **AgentCore Identity Token Vault (3LO)** — stores/refreshes/injects each user's Lark token natively. No separate per-user Secrets Manager charge (unlike the interceptor variant) — this is the main cost-structure difference between the two.
- **AgentCore Memory (STM)** — billed per event *written* (`~$0.25 per 1,000` create-event calls), **not** for retention duration.
- **Lambda + API Gateway** — router (webhook) + shim (OAuth RFC-6749 façade, a backend web service); effectively free at demo volume.
- **Secrets Manager** — `$0.40/secret/month` each, and only two static secrets: the Lark credentials (`{prefix}/channels/lark`) and the Cognito password salt. This variant does **not** create dynamic per-user secrets.
- **Cognito, DynamoDB (on-demand)** — the identity/state plane; negligible at demo volume. (A `user-files` S3 bucket is provisioned but not used on the current tool path — near-zero cost.)

`scripts/destroy.sh` removes everything `deploy.sh` created (Gateway, both Runtimes, the CDK stacks). Note it does **not** delete the `lark-agent-3lo` OAuth credential provider or the tokens vaulted in AgentCore Identity — the vault itself has no standing charge, but re-seed the provider via the docs on the next deploy. Costs are usage-driven; an idle deployment still accrues the two microVMs' memory-time and the two static per-secret charges.

## Security considerations

This is a **reference implementation, not production-ready as-is**. Before any real use:

- **Per-user Lark tokens live in the AgentCore Identity Token Vault**, not in application code or a self-managed store. The agent fetches a user's token at call time (agent-side 3LO) and passes it to the lark-cli MCP server in a custom header; it holds no long-lived credential of its own. Treat the account hosting the vault as sensitive.
- **The MCP server calls Lark strictly as the user.** `LARKSUITE_CLI_DEFAULT_AS=user` — the lark-cli engine always acts with the vaulted `user_access_token`, never the bot identity, so access is scoped to what that user can do in Lark and Lark adjudicates it.
- **Command execution is injection-safe.** The MCP server spawns lark-cli via `execFile` (no shell) with arguments passed as an array, and the user token via an environment variable — never interpolated into a command line.
- **IAM is scoped but a sample.** Re-review least-privilege for your account before production.
- **Webhook verification is fail-closed** — a missing/invalid signature or a timestamp outside the replay window is rejected before decryption. Don't relax this.
- **AES-CBC webhook decryption** is Lark's fixed scheme (not our choice); authenticity is guaranteed by the upstream signature check, not by the cipher mode.
- **No secrets in this repo** — Lark credentials come from `.env` → Secrets Manager via `scripts/setup-lark.sh`; `.env` is git-ignored.

### Notes & limitations

- **3LO is agent-side, not Gateway-mediated.** The Gateway does not do per-user 3LO for a `CustomOauth2` provider like Lark (AWS gap, agentcore-samples#1424), so the agent drives 3LO itself and delivers the vaulted token to the lark-cli MCP server in a custom passthrough header. See `docs/agentcore-behavior.md`.
- **Consent-wait is time-bounded.** On first use the router posts the consent link, then holds and polls the vault up to `AUTH_WAIT_SECONDS` (45s) before falling back to "re-send after approving". A user who takes longer than that to approve just re-sends once; the token is already vaulted by then.
- **Chat-only.** This variant has no web UI, no Cognito, and no Gateway on the tool path — the sibling `lark-agentcore-interceptor` is the Gateway/web-UI variant.
- **Two Runtimes.** The agent and the lark-cli MCP server are separate AgentCore Runtimes, both built via CodeBuild (ARM64) and created out-of-band by the CLI.
- **A new image doesn't reach existing users by itself.** AgentCore keeps serving stored sessions from the old container, so `deploy.sh --runtime` drops the saved session ids — the next message lands on the new version.
- **Message counts are approximate.** `/status` reads one page of Memory events (100) and reports `100+` beyond that; it counts only `conversational` payloads, since Strands also writes session/agent state events. `/clear` deletes at most `CLEAR_EVENT_LIMIT` (200) per run — deletion is one API call per event.
- **Token Vault exposes no metadata.** `GetResourceOauth2Token` returns just the token (or a consent URL) — no issued-at, expiry, or granted scopes — so `/auth` reports presence only.
