# Lark Identity on AgentCore — Gateway Interceptor

A reference implementation of enterprise identity on Amazon Bedrock AgentCore, using **Lark (Feishu) as the identity provider**. A simple agent is reachable from **two Lark entrypoints** — chat messages and a desktop-client-embedded web UI — that both resolve to the same `lark:{open_id}` identity. That identity is **forwarded to downstream MCP tools** through an AgentCore Gateway Request Interceptor (the agent never holds a downstream credential), and the tools then **act as the user against Lark** with the user's own token, so they reach only what that user can — Lark itself decides. In short, the agent inherits both *who you are* and *what you're allowed to do*, adding nothing of its own.

This is the **Gateway Interceptor** variant (downstream tools are Lambda targets; a custom interceptor forwards identity and injects the per-user credential).

## Architecture

```
  Lark message  ────▶  Router Lambda ─┐        Lark desktop web UI ──▶ SPA (S3/CloudFront)
  (webhook)            verify/decrypt  │          h5sdk requestAccess ─▶ login code
                       resolve user    │                                  │
                                       │                                  ▼
                                       │                        web_api Lambda
                                       │      POST /api/lark/auth  code ─▶ Cognito JWT (Lark is IdP)
                                       │      POST /api/session    JWT  ─▶ presigned WSS URL
                                       │                                  │
             InvokeAgentRuntime (SigV4)│                                  │ browser opens WSS
             payload carries actorId   ▼                                  ▼ (platform bridges to /ws)
                             ┌─────────────────────────────────────────────────────┐
                             │  Agent container (ARM64, AgentCore Runtime)         │
                             │    :8080  /ping  /invocations(POST) /ws(WebSocket)  │
                             │    Strands Agent + AgentCore Memory (per-user STM)  │
                             └───────────────────────┬─────────────────────────────┘
                                       │ MCP call, Bearer = user's Cognito ACCESS token
                                       ▼
                             ┌───────────────────┐
                             │ AgentCore Gateway │  MCP server; customJWTAuthorizer (Cognito)
                             │  + Interceptor λ  │  passRequestHeaders=true; injects end-user id
                             └─────────┬─────────┘  (Gateway invokes its Lambda target, not Lark)
                                       ▼
                             ┌───────────────────┐  whoami — identity proof
                             │   Tool Lambda     │  list_my_docs / create / edit / delete — act AS the
                             └─────────┬─────────┘  user with THIS user's Lark user_access_token (by open_id)
                                       │ HTTPS, Bearer = user_access_token
                                       ▼
                                  Lark REST API  → returns only what THIS user can see

  Identity: both entrypoints resolve to  lark:{open_id}  (shared session/memory).
  Lark is NOT standard OIDC → web_api exchanges the login code for a Cognito JWT.
  Pass-through (whoami) proves WHO; inheritance (list_my_docs, user_access_token)
  means the agent can only reach what the user can — Lark adjudicates.
```

See **[docs/architecture.md](docs/architecture.md)** for the full layered design, per-hop auth matrix, and sequence diagrams.

## Layout

| Path | What |
|---|---|
| `app.py`, `cdk.json` | CDK app (uv-managed deps) — 6 stacks |
| `stacks/` | security, agentcore, router, webui, gateway, observability |
| `agent/` | Strands agent container (HTTP contract + WS + AgentCore Memory + MCP Gateway client) |
| `lambda/router/` | Lark webhook: verify/decrypt/tenant-token/send |
| `lambda/web_api/` | Lark login exchange + session bootstrap (presigned WSS) |
| `lambda/interceptor/` | Gateway Request Interceptor (per-user credential injection) |
| `lambda/tools/` | MCP tool targets: `whoami` (identity proof) + `list_my_docs`/`create_doc`/`edit_doc`/`delete_doc` (all act as the user against Lark) |
| `web-ui/` | Lark-embedded SPA (no build step; renders agent replies as Markdown via marked + DOMPurify) |
| `scripts/` | deploy / setup-lark / manage-allowlist / test |
| `docs/architecture.md` | Full architecture: layers, per-hop auth, sequence diagrams |

## Deploy

Prereqs: `uv`, Docker, the AgentCore CLI (`npm i -g @aws/agentcore`), and AWS credentials. 
Scripts default to the `default` profile / `us-west-2`; override with `PROFILE=... REGION=...`. 
The AgentCore Runtime and Gateway have no CloudFormation resources in this region: 
 the **Gateway** is created by `deploy.sh` via the control-plane CLI (IDs fed back into `cdk.json`),
 the **Runtime image** is built (ARM64, CodeBuild) and deployed with the AgentCore CLI.

```bash
cp .env.example .env          # fill in Lark appId/appSecret/encryptKey/token + your open_id
scripts/deploy.sh --base      # CDK base stacks (security, agentcore, router, gateway, observability)
scripts/deploy.sh --gateway   # create the MCP Gateway + interceptor + demo target
scripts/deploy.sh --runtime   # build ARM64 image (CodeBuild) + deploy the Runtime (AgentCore CLI)
scripts/setup-lark.sh         # read .env → Secrets Manager; print webhook/SPA URLs; allowlist you
scripts/deploy.sh --frontend  # deploy WebUI stack, inject SPA config, upload SPA
# or: scripts/deploy.sh       # run every step in order
```

Config in `cdk.json`: `default_model_id` (`global.anthropic.claude-sonnet-5`),
`lark_api_domain` (`https://open.larksuite.com` international / `open.feishu.cn`),
`registration_open` (false = allowlist only), `presigned_url_expires`.

## Lark console setup (do this once, in order)

The webhook/SPA URLs come from `deploy.sh` output. In the Lark developer console:

1. **Add features**: enable **Bot** + **Web app**.
2. **Permissions & Scopes** — add all of these, then note that **subscribing to the message event requires the p2p scope** or single-chat messages are never pushed:
   - `im:message`, `im:message:readonly`
   - **`im:message.p2p_msg:readonly`** ← required for **single-chat** messages
   - `im:message:send_as_bot` (reply), `im:resource` (images)
   - `contact:user.base:readonly` (login → open_id)

   For **permission inheritance** (the doc tools act as the user), also add these under the **User Token Scopes** tab — they are user-identity scopes and usually need **admin approval** before they take effect; the web app's `tt.requestAccess` `scopeList` must match them exactly or it fails with **20027**:
   - `drive:drive` (list/create/manage the user's Drive files), `docx:document` (create/edit docx), `offline_access` (refresh_token — the user_access_token lives only ~2h)
3. **Events & Callbacks**: subscription mode = *Send to developer's server*; set Request URL to the webhook URL; enable **Encryption** (note the Encrypt Key); add event **`im.message.receive_v1`**.
4. **Security Settings**: add the SPA URL to **Redirect URLs** and **H5 trusted domains**. Leave IP allowlist empty (Lambda egress IPs are dynamic).
5. **Web app**: set Desktop + Mobile homepage to the SPA URL.
6. **Version Management & Release**: create a version and **publish**. Any change to scopes/events requires a **re-publish** to take effect.

Then `scripts/setup-lark.sh` stores credentials and allowlists your open_id. 
New users: they message the bot, get a rejection with their `lark:ou_...` id, and an admin runs `scripts/manage-allowlist.sh add lark:ou_...` (or set
`registration_open: true` to let anyone in).

## Test

```bash
scripts/test.sh              # agent (8) + router (7) + web_api (4)
```

## Deployment status

All four goals verified end-to-end on an AWS account:

- ✅ **Lark chat**: a real single-chat message → Router (verify + AES decrypt) → resolve `lark:{open_id}` → AgentCore Runtime → model reply back in Lark.
  `/whoami` in Lark reports the caller's identity.
- ✅ **Desktop embed**: opening the Web app inside the Lark client runs h5sdk 免登 → `POST /api/lark/auth` (code → Cognito JWT) → `POST /api/session`
  (presigned WSS) → the AgentCore platform bridges the browser's WSS to the
  agent's `/ws` on port 8080 → streaming reply. Shows the user's display name.
  The minted JWT is cached in `sessionStorage`, so a refresh reuses it and skips `requestAccess` — otherwise Lark re-prompts the consent popup on every load (it has no silent login-state reuse; the SPA must do it).
- ✅ **Unified identity**: both entrypoints resolve to the same `lark:{open_id}` (session/workspace shared).
- ✅ **MCP identity pass-through**: the agent mints the user's Cognito **access** token → AgentCore Gateway (`customJWTAuthorizer`) → Request Interceptor reads
  the identity and injects the per-tenant downstream key → the `whoami` tool reports the real end-user id and that a credential was injected, **while the agent never holds the key**.
- ✅ **Conversation memory**: the agent is a Strands agent with an AgentCore Memory (STM) session manager keyed by `(actor_id, session)`.
  Verified across two *different* runtime sessions for the same user: it recalls a fact stated earlier — so memory persists across reconnects and both entrypoints (30-day event retention).
- ✅ **Permission inheritance**: after the user authorizes in the web app, the doc tools act *as* that user — each loads the user's Lark `user_access_token` (stored by open_id, refreshed on expiry) and calls the Lark API, so access is **scoped to what that user can see/do in Lark**, adjudicated by Lark, and the agent never holds the token. Verified end-to-end: `list_my_docs` returned the user's real folder and, given a `folder_token`, descended into it to list the nested docs; `create_doc`/`edit_doc`/`delete_doc` created a doc (real document_id), appended content, and trashed it — all as the user.
- ✅ **Markdown rendering**: agent replies (bold, lists, tables, code) render as sanitized HTML in the web chat — `marked` parses, `DOMPurify` strips anything unsafe (agent output is untrusted); streaming re-renders each delta; plain-text fallback if the CDN scripts fail.

### Build & deploy notes (learned the hard way)

- **Runtime image must be ARM64 and built via CodeBuild.** 
  A QEMU cross-build on an x86 host produces an image that fails to start on Graviton (500 with no logs). 
  The runtime is built + deployed with the AgentCore CLI (`agentcore configure` / `agentcore deploy`), which runs CodeBuild in the cloud. 
  The agent image includes `aws-opentelemetry-distro` and runs under `opentelemetry-instrument` (required for AgentCore log/trace collection).
- **Runtime container logs**:
  `aws logs tail /aws/bedrock-agentcore/runtimes/<runtime_id>-DEFAULT --since 15m`.
- **WebSocket endpoint** 
  `/ws` on **port 8080** (same app as `/ping` + `/invocations`), matching the AgentCore SDK contract — not a separate port.
- **Gateway auth**: 
  send the Cognito **access token** (has the `client_id` claim that `allowedClients` validates); 
  an ID token 403s with `insufficient_scope`.
- **Errors from the agent** are returned as HTTP 200 `{error: ...}` — AgentCore wraps non-2xx as `RuntimeClientError` and drops the body.

### Notes & limitations

- **Session granularity**: one long conversation thread per user (the Memory `session_id` is derived from `actor_id`). All of a user's turns — webhook, web UI, and reconnects — share one context. There is no "new chat" affordance yet.
- **Memory is STM-only**: the Memory resource stores raw turns (30-day retention), no long-term extraction/summarization strategies are configured.
- **`whoami` reports the `lark:{open_id}`**, not a display name — the tool only sees what the Gateway interceptor injects (identity from the JWT), and the JWT carries no name claim.
