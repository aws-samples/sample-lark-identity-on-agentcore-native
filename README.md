# Lark Identity on AgentCore — AgentCore Identity

A reference implementation of enterprise identity on Amazon Bedrock AgentCore, using **Lark (Feishu) as the identity provider**. A simple agent is reachable from **Lark bot chat**; every message resolves to a `lark:{open_id}` identity, and downstream MCP tools **act as that user against Lark** with the user's own token — so they reach only what that user can, and Lark itself adjudicates access. The agent inherits both *who you are* and *what you're allowed to do*, adding nothing of its own.

This is the **AgentCore Identity** variant: per-user Lark tokens live in the **AgentCore Identity Token Vault** (OAuth 3LO), which stores, refreshes, and injects each user's token natively — no custom interceptor, no self-managed token store. The sibling repo [lark-agententity-on-agentcore-interceptor](https://github.com/aws-samples/sample-lark-agententity-on-agentcore-interceptor) achieves the same guarantees with a Gateway Request Interceptor and self-managed vaulting; the two differ only in how the downstream hop resolves per-user credentials.

## Architecture

```
                                            ┌──────────── AgentCore Identity ────────────┐
                                            │  Token Vault: stores / refreshes / returns │
                                            │  THIS user's Lark token (3LO)              │
                                            └───────────────────┬────────────────────────┘
                                                                │ the user's own token
  Lark    ──webhook──▶  Router Lambda  ──▶  Agent (AgentCore Runtime)  ──▶  Lark MCP server
  bot chat              verify/decrypt      Strands + Memory                acts AS the user
     ▲                  resolve identity    fetches that user's token                │
     │                                                                               ▼
     └──────────────── the answer, posted when the turn finishes ───────────  Lark REST API
                                                                        returns only what
                                                                        THIS user can see

  Every message resolves to lark:{open_id}. That identity picks the token, and Lark — not
  our code — decides what the tools may reach.

  Delivery is asynchronous: the agent accepts the turn, returns at once, and posts the answer
  when it's ready. Real tasks outlast any request/response window, and cutting one off is
  worse than waiting — the work has usually already succeeded.

  First use needs consent: with no vaulted token the router posts a 点击授权 link, waits for
  approval, and continues on its own, so the user never re-sends.
```

See **[docs/architecture.md](docs/architecture.md)** for the full flow, per-hop auth, and the consent-wait sequence; **[docs/agentcore-behavior.md](docs/agentcore-behavior.md)** and **[docs/native-3lo-builtin-vendor.md](docs/native-3lo-builtin-vendor.md)** for why 3LO is agent-side (a Gateway hop to a Runtime-hosted MCP server delivers no user token to the container — measured) and how to add other downstream systems.

## Layout

| Path | What |
|---|---|
| `app.py`, `cdk.json` | CDK app (uv-managed deps) — 6 stacks. Deployment state goes to `.cdk-state.json`, not here |
| `.env` | Deployment target (`PROFILE`/`REGION`/`MODEL_ID`) + Lark credentials — gitignored, read by every script |
| `stacks/` | security, agentcore, router, shim, gateway, observability |
| `agent/` | Strands agent container: HTTP contract + AgentCore Memory + agent-side 3LO (`lark_3lo`) + MCP clients for the lark-cli server and, optionally, web search (`websearch`); runs turns in the background and streams answers back into a card (`lark_notify`) |
| `lambda/router/` | Lark webhook: verify/decrypt/tenant-token/send + 3LO consent-wait + the chat commands |
| `lambda/shim/` | Lark OAuth RFC-6749 façade + 3LO return endpoint (`CompleteResourceTokenAuth`, then DMs the user) |
| `mcp-servers/` | One directory per MCP server, one Runtime each: `lark-cli/` acts as the user against Lark, `approval/` runs approval decisions on the app identity. Each declares its own build/runtime config in `runtime.env`, so adding a server needs no script change |
| `deploy.sh` | the deploy entry point — orders the steps in `scripts/` |
| `scripts/` | step implementations: preflight / provision (base/runtime/gateway) / build-mcp / setup-3lo / setup-lark / subscribe-approvals / manage-allowlist / destroy |
| `tests/` | `run.sh` (all unit suites) + e2e smoke tests that need a deployed stack |
| `docs/architecture.md` | full architecture (core flow updated to the native path; some sections marked legacy) |
| `docs/agentcore-behavior.md` | measured AgentCore Gateway/Runtime behavior, incl. why a Gateway hop can't carry a per-user token to a Runtime target |
| `docs/native-3lo-builtin-vendor.md` | reusable agent-driven 3LO reference (built-in vendors **and** CustomOauth2) — how to add a downstream system |

## Deploy

Prereqs: `uv`, Docker, the AgentCore CLI (`npm i -g @aws/agentcore`), and AWS credentials. The deployment target lives in `.env` (`PROFILE`, `REGION`, `MODEL_ID`); command-line env vars override it (`REGION=... ./deploy.sh`). Resources deploy under the `lark-agent` prefix. The first deploy into a region runs `cdk bootstrap` automatically.

```bash
cp .env.example .env          # deployment target (PROFILE/REGION/MODEL_ID) + Lark appId/appSecret/encryptKey/token + your open_id
./deploy.sh                   # everything, in order — ends by printing the two URLs to register in Lark
```

That's the whole deploy. It runs six steps in dependency order and nothing stops to ask you anything: the two values you must paste into the Lark console don't block the deploy, they only gate the bot at runtime, so they're printed together at the end (`./deploy.sh urls` reprints them).

Individual steps, for iterating — each is idempotent, so re-running any of them is safe:

| Step | What |
|---|---|
| `./deploy.sh base` | CDK stacks (security, agentcore, router, shim, gateway, observability) |
| `./deploy.sh mcp` | build every MCP server under `mcp-servers/` (CodeBuild ARM64) + create/update a Runtime each. `./deploy.sh mcp approval` for just one |
| `./deploy.sh 3lo` | workload identity + the `lark-agent-3lo` OAuth credential provider |
| `./deploy.sh gateway` | Web Search gateway in us-east-1 — skipped unless `WEB_SEARCH=true` |
| `./deploy.sh runtime` | build the agent image + deploy the agent Runtime |
| `./deploy.sh lark` | seed Lark credentials to Secrets Manager + allowlist your `open_id` |
| `./deploy.sh approvals` | subscribe to Lark approval events for each `AGENT_DECIDE_APPROVAL_CODES` definition — no-op when that is empty. Ticking the event in the console is **not** sufficient; Lark delivers approval events only for definitions also subscribed through the API |

Order matters in one place: `3lo` and `gateway` precede `runtime`, because the agent Runtime is created with the provider name and the gateway URL baked into its environment. `deploy.sh` handles that; the underlying implementations are in `scripts/`.

`setup-3lo.sh` registers the OAuth credential provider (Lark behind the RFC-6749 shim) plus the agent's workload identity, and prints the provider `callbackUrl` — register that in the Lark console (step 4 below) before the first 3LO consent. See `docs/native-3lo-builtin-vendor.md` to add other downstream systems.

Per-deployment ids (runtime/gateway) go to `.cdk-state.json` (gitignored), so `cdk.json` stays free of environment state.

Optional web search: set `WEB_SEARCH=true` in `.env` before deploying (or run `./deploy.sh gateway` then `./deploy.sh runtime`). That provisions a Gateway fronting AgentCore's built-in Web Search connector — **in us-east-1, the only region offering it**, so the agent calls it cross-region. Search carries no end-user identity, so it uses `GATEWAY_IAM_ROLE` and never touches the per-user 3LO path the Lark tools have to avoid. Off by default; the agent just runs without the tool.

### Tear down

```bash
scripts/destroy.sh            # delete everything deploy.sh created (asks for confirmation)
# or: scripts/destroy.sh --yes  # skip the prompt
```

Deletes in dependency order — gateway targets → gateways (including the Web Search one in us-east-1) → both CLI-created Runtimes → the CDK stacks → the OAuth credential provider and workload identity. Idempotent: re-running skips already-gone resources.

Two consequences worth knowing: deleting the provider **purges every user's vaulted token**, so everyone consents again after a redeploy — and the new provider gets a **new `callbackUrl`** that must be registered in the Lark console (`scripts/setup-3lo.sh` prints it). Your Lark console app config is otherwise untouched; re-seed credentials from `.env` via `scripts/setup-lark.sh`.

## Lark console setup

1. **Add features**: enable **Bot**.
2. **Permissions & Scopes** — two groups, and the split is the whole point of this sample: the bot speaks with its own identity, while anything touching a user's data acts as that user.

   **Tenant token scopes** (the bot acting as itself — receiving webhooks, replying, reacting):

   | Scope | Used for |
   |---|---|
   | `im:message` | receive events, send replies, and the in-progress emoji reaction (no separate reaction scope needed) |
   | `im:message:readonly` | read message content |
   | `im:message.p2p_msg:readonly` | **required for single (p2p) chats** — without it the bot never sees direct messages |
   | `im:message.group_at_msg:readonly` | see @mentions in group chats (the router strips the mention before passing the text on) |
   | `im:message:send_as_bot` | post as the bot |
   | `im:resource` | download images the user sends |
   | `contact:user.base:readonly` | resolve the sender's basic profile |
   | `cardkit:card:write` | create and update the streaming reply card ("Create and update cards") |

   **User token scopes** (the *user's* identity, via 3LO — this is what the Lark MCP server uses, so tools reach only what that user can). Needs admin approval:

   | Scope | Used for |
   |---|---|
   | `drive:drive` | list and read the user's Drive |
   | `docx:document` | read/write the user's documents |
   | `offline_access` | issue a refresh token, so the vaulted grant survives without re-consent |

   Nothing in the first group can read a user's documents, and nothing in the second is ever used to speak as the bot — `LARKSUITE_CLI_DEFAULT_AS=user` keeps the MCP server on the user's token exclusively.
3. **Events & Callbacks**: Request URL = the webhook URL from deploy output; enable Encryption; add `im.message.receive_v1`. For the approval demo also add **审批任务状态变更** (`approval_task`) — and note that ticking it here is not enough on its own, see below.
4. **Security Settings → Redirect URLs**: add the OAuth credential provider's `callbackUrl` (`https://bedrock-agentcore.<region>.amazonaws.com/identities/oauth2/callback/<uuid>`, from `get-oauth2-credential-provider --name lark-agent-3lo`). This is where AgentCore Identity receives the 3LO code — not the shim URL.
5. **Publish** a version (re-publish after any scope/event change).

### Optional: the approval demo

Off by default. It shows what to do when a downstream API *refuses* to accept the user's identity — Lark's approval endpoints take only an app token, so a decision is made by the app with a `user_id` saying whose name to record it under. Read [docs/architecture.md](docs/architecture.md#a-third-path-approvals-where-the-users-identity-cannot-be-passed-through) before switching it on: the limits are self-imposed, and what they can and cannot prevent is the point of the demo.

To enable:

1. Add the approval scopes and the `approval_task` event from step 3 above, then re-publish. The console lists these by display name, so both are given here:

   | Scope | Type | Display name | Used for |
   |---|---|---|---|
   | `approval:approval` | tenant | View, create, update, and delete info of Approval app | making decisions (approve/reject/transfer) |
   | `approval:approval:readonly` | tenant | Access Approval | reading instances and queues |

   The `approval_task` event accepts **either** of those two (the console shows "any one suffices"), so nothing extra is needed to receive events.
2. Set the limits in `.env` — the agent decides nothing until you do:
   ```
   AGENT_DECIDE_APPROVAL_CODES="<definitionCode>, ..."   # empty = decide nothing
   AGENT_DECIDE_MAX_AMOUNT=1000                          # 0 = kill switch
   ```
   The definition code is the `definitionCode=` query parameter in the URL of a form's edit page in the Lark approval admin.
3. `./deploy.sh mcp approval` (builds the approval Runtime — gated on that variable so it costs nothing when unused), then `./deploy.sh approvals` to subscribe. **Both the console tick and this API subscription are required**; Lark delivers approval events only for definitions subscribed through the API.
4. Authorize as the approver (`/auth lark` in the bot chat). The server refuses to decide for anyone without their own grant on record, so an approver who never consented gets a 点击授权 card instead — after which the turn resumes on its own.

One tool is deliberately left unusable: `approval_add_sign` (加签) is the single approval endpoint that takes the *user's* token instead of the app's, but the vaulted token carries only the scopes `LARK_SCOPES` requests (`drive:drive docx:document offline_access`), and the only user-token approval scope on offer is `approval:approval:readonly` — a read scope, while add_sign writes. So it fails on permissions by construction. It stays exposed because that boundary is the lesson: Lark's approval API admits a user identity for exactly one operation, and not one this sample can reach.

Then submit an approval assigned to that approver. Both outcomes are worth trying: within the limits the agent decides and comments `[AI 自动处理]`; over the amount ceiling it refuses to decide and hands the case back.

### Letting more people in

The bot answers only allowlisted users; `./deploy.sh lark` adds you and nobody else. An unlisted user who messages the bot is told their own id, which is the easiest way to collect one:

```bash
PROFILE=... REGION=... scripts/manage-allowlist.sh add lark:ou_...
scripts/manage-allowlist.sh list
scripts/manage-allowlist.sh remove lark:ou_...
```

The allowlist gates *conversations*. It also gates whether an approval event is acted on at all: an approver who isn't listed is left alone silently. Note the asymmetry — deciding for someone needs their own 3LO grant, but DMing them does not (that runs on the app's token), so the allowlist is the only thing standing between an approval event and a stranger's chat window.

## Chat commands

Send these to the bot instead of a question. `/help` lists them in-chat.

| Command | What it does |
|---|---|
| `/auth` | Authorization status per IdP — one OAuth provider per downstream system |
| `/auth <idp>` | Authorize (or re-authorize) that IdP; always starts a fresh 3LO flow, so it is idempotent |
| `/status` | Identity, session routing key, the microVM serving it (id + age + how long it has served this session), Memory thread id, message count, last activity |
| `/new` | New Memory thread **and** new runtime instance — a fully fresh start |
| `/reset` | New Memory thread, same runtime instance — history starts over, old events kept |
| `/clear` | Actually delete this thread's events (unlike `/reset`, which just stops reading them) |
| `/reconnect` | New runtime instance, same Memory thread — shows that memory outlives the container |

The three session commands exist because **the runtime session and the Memory thread are independent ids**: the first decides which microVM serves you, the second decides which conversation history the agent reads. The router owns both (DynamoDB `SESSION` / `MEMSESSION` items) and rotates them in the three meaningful combinations — rotating is just "switch to a new id", so all of them are instant. `/clear` is the only one that deletes data.

`/status` is written for developers evaluating AgentCore rather than for end users, so it deliberately exposes the compute layer: the session id is a routing key that outlives any one microVM, and AgentCore replaces the microVM underneath it on idle or lifetime limits without the id changing. Showing the microVM's own id alongside two durations — its age, and how long it has served this session — makes that turnover visible. Both are in seconds so they can be compared directly: equal values mean this microVM started for you, a much larger age means an existing one took over. See `docs/agentcore-behavior.md` for how these are obtained and, more importantly, which seemingly obvious signals are *not* trustworthy.

Authorization is a third, orthogonal dimension: the vaulted Lark token is keyed to `lark:{open_id}`, not to any session — so `/new` does **not** require re-authorizing. Token Vault refreshes the access token automatically; users only re-authorize if the refresh chain lapses, they revoke access in Lark, or the provider is recreated.

## Test

```bash
tests/run.sh                 # unit suites (agent, router, shim) — no AWS needed
```

Unit tests sit next to the code they cover and mock AWS; `tests/run.sh` walks them, one process per suite (several modules share filenames, so a single pytest session would import the wrong one). The e2e smoke tests in `tests/` exercise a **deployed** stack and stay skipped unless you point them at it — see `tests/README.md`.

## Cost

This deploys billable AWS resources. All the always-on pieces are consumption- or per-unit-priced (no fixed reservation), so an idle single-user demo in us-west-2 is on the order of a couple USD/month before model usage; the variable cost is dominated by the agent's Bedrock calls. Verify current rates on the AWS pricing pages — figures below are as researched, not a quote.

- **Bedrock model invocations** — the main usage-sensitive line; priced per input/output token on the model in `default_model_id`. A chatty demo is cents-to-dollars; a load test is not.
- **AgentCore Runtime ×2–3** — the agent and the lark-cli MCP server always, plus the approval MCP server if `AGENT_DECIDE_APPROVAL_CODES` is set. Each is metered per-second: CPU (`~$0.0895/vCPU-hour`) only during active processing, memory (`~$0.00945/GB-hour`) continuously while the microVM is alive. So each extra MCP server adds idle memory-time even when nothing calls it — which is why the approval one is gated on that variable rather than always deployed.
- **AgentCore Identity Token Vault (3LO)** — stores/refreshes/injects each user's Lark token natively. No separate per-user Secrets Manager charge (unlike the interceptor variant) — this is the main cost-structure difference between the two.
- **AgentCore Memory (STM)** — billed per event *written* (`~$0.25 per 1,000` create-event calls), **not** for retention duration.
- **Lambda + API Gateway** — router (webhook) + shim (OAuth RFC-6749 façade, a backend web service); effectively free at demo volume.
- **Secrets Manager** — `$0.40/secret/month` each, and only two static secrets: the Lark credentials (`{prefix}/channels/lark`) and the Cognito password salt. This variant does **not** create dynamic per-user secrets.
- **Web search (optional)** — only when `WEB_SEARCH=true`: an AgentCore Gateway plus per-query connector charges. The gateway sits in us-east-1, so its traffic is cross-region.
- **Cognito, DynamoDB (on-demand)** — the identity/state plane; negligible at demo volume. (A `user-files` S3 bucket is provisioned but not used on the current tool path — near-zero cost.)

`scripts/destroy.sh` removes everything `deploy.sh` created, including the OAuth credential provider and both gateways. Costs are usage-driven; an idle deployment still accrues the two microVMs' memory-time and the two static per-secret charges.

## Security considerations

This is a **reference implementation, not production-ready as-is**. Before any real use:

- **Per-user Lark tokens live in the AgentCore Identity Token Vault**, not in application code or a self-managed store. The agent fetches a user's token at call time (agent-side 3LO) and passes it to the lark-cli MCP server in a custom header; it holds no long-lived credential of its own. Treat the account hosting the vault as sensitive.
- **The MCP server calls Lark strictly as the user.** `LARKSUITE_CLI_DEFAULT_AS=user` — the lark-cli engine always acts with the vaulted `user_access_token`, never the bot identity, so access is scoped to what that user can do in Lark and Lark adjudicates it.
- **Command execution is injection-safe.** The MCP server spawns lark-cli via `execFile` (no shell) with arguments passed as an array, and the user token via an environment variable — never interpolated into a command line.
- **Web search sees no user data.** It runs on Amazon's index with `GATEWAY_IAM_ROLE`, carries no user token, and queries stay inside AWS. It does mean model output can include fetched web content — treat that as untrusted input like any other tool result. `parameterValues.domainFilter` can restrict which domains are searched.
- **IAM is scoped but a sample.** Re-review least-privilege for your account before production.
- **Webhook verification is fail-closed** — a missing/invalid signature or a timestamp outside the replay window is rejected before decryption. Don't relax this.
- **AES-CBC webhook decryption** is Lark's fixed scheme (not our choice); authenticity is guaranteed by the upstream signature check, not by the cipher mode.
- **No secrets in this repo** — Lark credentials come from `.env` → Secrets Manager via `scripts/setup-lark.sh`; `.env` is git-ignored.

### Notes & limitations

- **3LO is agent-side, not Gateway-mediated.** The Gateway does not do per-user 3LO for a `CustomOauth2` provider like Lark (AWS gap, agentcore-samples#1424), so the agent drives 3LO itself and delivers the vaulted token to the lark-cli MCP server in a custom passthrough header. See `docs/agentcore-behavior.md`.
- **Answers arrive asynchronously.** A turn that researches a topic and writes a document takes longer than any request/response window allows — `InvokeAgentRuntime` and the router's Lambda both cap out, and a turn cut off mid-way is the worst outcome, because the work often completed while the user was told it failed. So the agent accepts the work, returns immediately, and types the answer into a CardKit streaming card as it is produced — a placeholder appears at once (the first token takes several seconds: session assembly, MCP handshake, model latency), then fills in. If CardKit is unavailable the answer is posted as plain text instead, so it is never lost. Its `/ping` reports `HealthyBusy` while a turn is running, which defers *idle* reclamation (`idleRuntimeSessionTimeout`, a session-inactivity timer). It does **not** defer `maxLifetime` — the microVM's wall-clock age cap (default 8 h, configurable) that never resets — so that is the hard ceiling on a single background turn. Consent is the exception and stays synchronous, since the router drives the wait-and-retry loop around it.
- **Consent-wait is time-bounded.** On first use the router posts the consent link, then holds and polls the vault up to `AUTH_WAIT_SECONDS` (45s) before falling back to "re-send after approving". A user who takes longer than that to approve just re-sends once; the token is already vaulted by then.
- **Chat-only.** This variant has no web UI — the sibling `lark-agentcore-interceptor` is the web-UI variant. The Lark tools don't go through the Gateway either — a Gateway hop to a Runtime-hosted MCP server arrives with no user token in any header (measured; see `docs/agentcore-behavior.md`), so the agent fetches each user's token itself. The Gateway is used only for web search, where no user identity is involved.
- **One Runtime per MCP server.** `protocolConfiguration.serverProtocol` is a single value and a container exposes one MCP endpoint, so each server under `mcp-servers/` gets its own Runtime — the agent's is a third. All are built via CodeBuild (ARM64) and created out-of-band by the CLI. Each server declares its own build/runtime config in `runtime.env`, including an optional gate so it is skipped when unconfigured.
- **A new image doesn't reach existing users by itself.** AgentCore keeps serving stored sessions from the old container, so `./deploy.sh runtime` drops the saved session ids — the next message lands on the new version.
- **Message counts are approximate.** `/status` reads one page of Memory events (100) and reports `100+` beyond that; it counts only `conversational` payloads, since Strands also writes session/agent state events. `/clear` deletes at most `CLEAR_EVENT_LIMIT` (200) per run — deletion is one API call per event.
- **Token Vault exposes no metadata.** `GetResourceOauth2Token` returns just the token (or a consent URL) — no issued-at, expiry, or granted scopes — so `/auth` reports presence only.
