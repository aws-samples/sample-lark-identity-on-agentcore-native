# AgentCore Gateway + Runtime — behavior notes (measured)

Facts about how AgentCore Gateway and Runtime actually behave for an MCP-server target, established by direct measurement against a deployed probe MCP server (a minimal FastMCP server whose `whoami` reports a process-start instance id, a per-process call counter, and the request headers it received). These are behaviors the AWS docs leave implicit; recording them so they aren't rediscovered the hard way. Account-specific ids and raw run data live in the team's local working notes, not here.

## Session & microVM lifecycle

- **A microVM is per session, 1:1 — and the isolation is real, but only one kind of evidence proves it.** Same session across calls lands on the same microVM; different sessions get different ones, concurrently as well as serially.
  - **Identifiers cannot prove this.** Anything captured in the image or snapshot is copied to every restore. Measured on three *concurrent* sessions: the kernel's `boot_id` was **identical** for all three, and `pid` was `1` in each. Neither means they share a machine — a self-generated uuid, a boot id, a pid: all reproduce identically across restores of one image.
  - **Post-start writes do prove it.** Each of the three sessions wrote a marker file to `/tmp` and then listed the directory: each saw only its own. Runtime state cannot be copied from a snapshot, so this establishes separate filesystems — consistent with the documented dedicated-microVM-per-session guarantee. Use this shape of test, not identifier comparison.
  - A serial test is not enough either way: it can't distinguish "dedicated" from "one microVM reassigned after the previous session ended".

- **Gateway sessions are opt-in.** Unless the gateway is created with `protocolConfiguration.mcp.sessionConfiguration.sessionTimeoutInSeconds` (900–28800), the `initialize` response carries **no** `Mcp-Session-Id` header, and every `tools/call` cold-starts a fresh downstream microVM. With it configured, `initialize` returns an `Mcp-Session-Id`; the client must echo it on every later request to get warm-microVM reuse.

- **`time.time()` inside the microVM is NOT wall-clock elapsed time — do not build timing on it.** Measured: a process reported 777 s of age (`time.time()` at module import vs. now) inside a kernel that had been up for **25 s**. A process cannot outlive its kernel, so the wall clock's base is inherited from the image the microVM restores from, not set at boot. This one flaw produced a chain of wrong conclusions here (a "22-minute-old process serving a one-minute-old session", and from it an invented warm pool) before `/proc/uptime` exposed it. AWS's devguide mentions no pre-warming at all — checked `runtime-lifecycle-settings`, `runtime-how-it-works`, `runtime-sessions`, 25–39 kB each, zero hits for "warm pool"/"pre-warm"/"warm instance".
  - **Use `time.monotonic()` for durations and `/proc/uptime` for the microVM's age.** Both were verified to advance with real time (70 s of waiting → +70 s on each). After switching, process age became sane (5 s → 75 s across a 70 s gap).
  - Three distinct figures, easy to confuse — `agent/server.py` reports all three and `/status` shows the last two: process age (`monotonic`, when this process started), **microVM age** (`/proc/uptime`, the only answer to "how long has this compute been running"), and **session age** (from the first request bearing this session id — independent of when the microVM booted).

- **The session id reaches the container.** AgentCore sends `x-amzn-bedrock-agentcore-runtime-session-id` on every `/invocations` request (verified by echoing headers back). That is what lets a process report per-session age rather than only its own.

- **Cold start is measurable, with caveats.** `microVM age − session age` is how long the compute was up before your request arrived: near zero means it started for you. AgentCore's own provisioning time stays invisible (no instance API), so only X-Ray segments (untested here) or end-to-end deltas can bound it. A fresh session id does **not** imply a cold start. To make cold starts repeatable, drop `idleRuntimeSessionTimeout` to its 60 s minimum for the test.

- **Cold start is ~19 s, and essentially none of it is ours.** Measured on this deployment by timing the module's own imports against `/proc/uptime` and logging the split at start-up (`agent/server.py:_log_startup_breakdown`, log-only): `microVM=19.6s process=0.5s imports=0.5s before-our-code=19.1s`, reproduced across two cold starts within 0.1 s. So ~97% is AgentCore booting the microVM and pulling the image before our code runs; trimming imports (strands + boto3 + mcp) would buy back half a second. Optimising cold start is not an application-layer problem here.
  - AWS documents none of this. The only statement on the subject: without a consistent session id "each request may be routed to a new microVM, which may result in additional latency due to cold starts" (`runtime-sessions`) — no phases, no figures.
  - **X-Ray cannot see it either.** Its trace root is the agent's own segment, so everything before OTel initialises (kernel boot, image pull, imports) is outside the trace — verified against a real trace, whose only child was the agent's Cognito call. Contrast Lambda, which splits INIT/INVOKE server-side and emits an `Initialization` subsegment. So `microVM age − session age` bounds the total, and only in-container instrumentation breaks it down.

- **Session create/tear-down returns a retryable 409.** "While the service provisions or tears down a session, a second operation targeting that same session returns a retryable HTTP 409 `RetryableConflictException`... Retry with short exponential backoff." Relevant here because the router disables botocore retries outright (a timeout must not replay a whole turn), which also means nothing retries a 409.

- **Probing a session id is not a passive read.** Calling `status` on an id with no live microVM provisions one — measured at 1.4 s for a fresh id. `/status` therefore materialises part of what it reports.

- **Within a session, the microVM and its MCP server process are reused — not cold-started per call.** Measured: 5 consecutive calls on one session hit the same instance id with a per-process counter incrementing 1→5. The first call for a target pays the cold start + MCP `initialize`; later same-session calls skip both. The two lifecycle limits govern different things, and only one is resettable:
  - `idleRuntimeSessionTimeout` (default 900 s / 15 min) — **session inactivity**. Reset on every invocation, so it only fires after a quiet stretch. This is the one `HealthyBusy` defers.
  - `maxLifetime` (default 28800 s / 8 h) — **wall-clock age of the microVM from creation**, not the session. It never resets *for that microVM*, so a continuously busy turn is still cut off at 8 h and `HealthyBusy` does not help. This is the hard ceiling on a single background task. It is not a ceiling on the session, though: once the microVM is gone the next invocation gets a new one "with the same lifecycle configuration (i.e. `idleRuntimeSessionTimeout` and `maxLifetime` that can be up to **another 8 hours**)" — so a session can outlive many 8-hour compute lifetimes. Still leave margin on any one turn: we could not establish whether a microVM's clock starts before it is assigned to a session.

Both are configurable 60–28800 s via `lifecycleConfiguration` and left at defaults on every Runtime here. On either limit the `runtimeSessionId` survives — the next call provisions a fresh microVM (sanitized memory, cold start), and the session stays valid until the Runtime ARN is deleted. Since memory bills continuously across a session including idle, a shorter idle timeout is the direct lever on idle cost.
  - Sources: <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html> (timer semantics: idle resets per invocation, `maxLifetime` "cannot be reset"), <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html> (Stopped → Active with new compute), <https://aws.amazon.com/bedrock/agentcore/pricing/> (billing spans boot → idle → termination; memory continuous, CPU only during active processing).

- **Two deployment artifacts; both run in microVMs.** `CreateAgentRuntime.agentRuntimeArtifact` accepts either `containerConfiguration` (an ECR image URI) or `codeConfiguration` (source in S3 + `runtime` + `entryPoint`; runtime enum as of 2026-08: `PYTHON_3_10`…`PYTHON_3_14`, `NODE_22`). "Container" is the packaging; the microVM is the isolation boundary, and the Runtime docs never use "container" for the isolation unit. This deployment uses `containerConfiguration`. Read from the live `bedrock-agentcore-control` API model and verified against the deployed agent Runtime.

- **`runtimeSessionId` ≠ user.** AgentCore does not enforce a session-to-user mapping; the caller owns it. When a **Gateway** is the caller, it derives the session from the inbound JWT identity, so per-user separation happens there.

## Per-user isolation

- **Two different end users → two different gateway sessions → two different downstream microVMs**, each stable within its own session. The Gateway keys the session off the inbound JWT identity. So per-user isolation needs no extra work as long as each user's calls carry that user's own (Cognito) JWT to the Gateway.

## Outbound credential injection to an mcpServer target

- **`GATEWAY_IAM_ROLE` (SigV4) delivers no `Authorization` header to the container.** The SigV4 signing rides the AWS `InvokeAgentRuntime` channel; the downstream container sees `authorization: <none>`. A server that needs a per-request bearer token (e.g. to act as the calling user) therefore **cannot** get it under IAM outbound auth.
- **Per-user tokens must come via OAuth 3LO (`AUTHORIZATION_CODE`) outbound**, which places the vaulted per-user token in `Authorization: Bearer` on each outbound call. This is per-request: the token is fresh each call, so a reused warm process never carries a stale/foreign token.
- **A gateway target holds exactly one credential provider** (`credentialProviderConfigurations` is fixed at 1 item); `GATEWAY_IAM_ROLE` and `OAUTH` are mutually exclusive on one target, and SigV4 and a bearer token cannot share the `Authorization` header anyway.

## mcpServer target wiring gotchas

- **The target endpoint must be the full URL-encoded runtime ARN** (`https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<urlencoded-arn>/invocations?qualifier=DEFAULT`), not the bare runtime id — otherwise target creation FAILS with "accountID is required".
- **The target caches the downstream MCP handshake at creation.** After upgrading the server image, delete and recreate the target so the gateway re-handshakes.
- **The downstream MCP server must speak the gateway's locked MCP protocol version.** A gateway pinned to `2025-11-25` rejects a server that only negotiates older versions; `update-gateway` cannot add protocol versions to an existing gateway (recreate it). For the Python `mcp` SDK, `2025-11-25` requires `mcp >= 1.23.0`.

## Client contract for calling a Gateway MCP endpoint directly

- Send `MCP-Protocol-Version: <version>` on **every** request after `initialize` (the gateway is otherwise stateless about it and falls back to an older version, 400 `-32042`).
- Tool names are target-prefixed: `<targetName>___<toolName>`.

## How the workload access token is obtained — and why inbound auth decides the trust model

Every per-user token fetch starts with a workload access token (WAT). There are **two ways to get one, and they have completely different security properties**. Which one applies is decided by the Runtime's *inbound* auth, not by anything in the agent.

**Manual path — `GetWorkloadAccessTokenForUserId(workloadName, userId)`** (what this sample uses, because it is invoked with SigV4 and the identity arrives as a payload string):

- **It does not check whether the caller "owns" the `userId`** (measured: our agent passes whatever `actorId` the router put in the payload, for arbitrary users, and it succeeds). So the calling code's choice of `userId` *is* the identity it acts as. Any user who has consented once is reachable, with nobody present.
- Consequence: with SigV4 inbound, per-user safety rests on the agent only ever passing the `actorId` it was given — **code discipline, not a cryptographic constraint**. That matters because the agent is the component processing untrusted input.

**Automatic path — Runtime does it for you when inbound auth is `CUSTOM_JWT`** (from the [devguide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/get-workload-access-token.html); **documented, not measured by us**):

> "Runtime automatically delivers workload access tokens to agent execution instances as payload headers, eliminating the need for manual token management in most scenarios."

The documented sequence: Runtime validates the inbound OAuth token (issuer, signature) → extracts `iss`/`sub` → fetches the agent's workload identity → calls `GetWorkloadAccessTokenForJWT` → passes the WAT to agent code as a payload header. The same page adds the property that makes this the stronger model:

> "Runtime-managed agent identities cannot retrieve workload access tokens directly, preventing token extraction and misuse."

Three practical notes:

- **`CUSTOM_JWT` and SigV4 invocation are mutually exclusive.** Turning it on means the caller stops using the `InvokeAgentRuntime` SDK call and sends `Authorization: Bearer` to the Runtime endpoint instead — a breaking cutover, not an additive hardening. This is why this sample still uses SigV4.
- **Removing the manual call from agent code is not enforcement.** `GetWorkloadAccessTokenForUserId` remains callable by anything holding the execution role, so closing the impersonation surface requires **denying that action in IAM**, not just not calling it.
- `authorizerConfiguration` on a Runtime has exactly one shape, `customJWTAuthorizer` (`discoveryUrl` + `allowedAudience`/`allowedClients`/`allowedScopes`, plus optional custom claim matching) — verified from the `create-agent-runtime` API model. `UpdateAgentRuntime` accepts `authorizerConfiguration`, so switching does not require rebuilding the runtime.

## 3LO (per-user authorization) with a custom OAuth2 provider — the important one

**Rule of thumb, verified @2026-08-18: `CustomOauth2` gets a target-level federation, never a per-user one.** Two different things wear the same name:

| | Works? | What it is |
|---|---|---|
| **Target-level federation** | ✅ | One consent, completed by an operator at target creation. The `userId` is `{gatewayId}_{targetId}_{random}` — Gateway's own, not a real user's. Every later call shares that token |
| **Per-user dispatch at `tools/call`** | ❌ | `An internal error occurred. Please retry later.`, no elicitation, no token |

The per-user failure held across all three configurations, with the IAM-permission confound eliminated: `mcpServer` + dynamic discovery, `mcpServer` + `mcpToolSchema`, and OpenAPI. Callers who had never consented and callers whose token was already vaulted got the same error. So per-user 3LO must be driven from the agent, whatever the target type.

> Two wordings preceded this one. The first blamed the `CustomOauth2` vendor on second-hand evidence. The second, after seeing a target-level federation complete, over-stated it as "3LO works" — it does, but only for that synthetic target-level identity. Both are left described rather than deleted so the change is traceable.

Registering a non-standard IdP (e.g. behind an RFC-6749 shim) yields a `CustomOauth2` credential provider. What the experiments established:

- **A target-level federation completes.** `mcpServer` target on our lark-cli Runtime, `grantType: AUTHORIZATION_CODE`, no `mcpToolSchema`: real `authorizationUrl` → a real user consented via the shim → `CompleteResourceTokenAuth` succeeded → target **`READY`**, `statusReasons: null`. But the `userId` it federated is `{gatewayId}_{targetId}_{random}`, documented as "defined by AgentCore Gateway" — so it is the Gateway's own identity, one per target, not the caller's.
  - **Why that federation exists at all: dynamic tool discovery.** `mcpToolSchema` is documented as "supported only when the credential provider is configured with an authorization code grant type. Dynamic tool discovery/synchronization will be disabled when target is configured with mcpToolSchema." Supply the schema and the pre-auth disappears — verified: the target went straight to `READY` with **no `authorizationData`**. The Gateway needed a credential to list tools at creation time, when no user exists; that is what the synthetic identity is for. (Format gotcha: `inlinePayload` is a JSON **string** containing `{"tools":[…]}` — a bare array fails with `mcpToolSchema must be an object with 'tools' array`.)
  - **But that does not buy per-user dispatch.** With `mcpToolSchema` in place, the gateway role holding `InvokeAgentRuntime`, `CUSTOM_JWT` inbound, and the client declaring the `elicitation` capability, a `tools/call` from a caller who had never consented still returned `An internal error occurred. Please retry later.` — no elicitation, no token.
  - **`Authorization error when sending message` means a missing IAM permission, not a 3LO gap.** One variable at a time: OAuth outbound + no `InvokeAgentRuntime` → that error; SigV4 + no permission → *same* error; SigV4 + permission → `READY`; OAuth + permission → `READY`. The Web Search gateway role carries only `InvokeGateway` + `InvokeWebSearch`, which is how we walked into it. **Check the role before suspecting 3LO.**
  - **#1424** (<https://github.com/awslabs/agentcore-samples/issues/1424>, closed `not_planned` by the reporter, no AWS response) — its CloudTrail note is second-hand; we could not reproduce the conclusion drawn from it.
  - **`-32042` is not in AWS docs**, though these notes long treated it as *the* signal. Documented codes: `-32600`, `-32601`, `-32021`; the OAuth path raises `URLElicitationRequiredError` carrying a URL — [gateway-mcp-elicitation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-mcp-elicitation.html). Elicitation works **only for MCP server targets**, and the client must declare the capability.
  - **No vendor restriction is documented.** The API model defines `AUTHORIZATION_CODE` as "Authorization with a token that is specific to an individual end user", and the outbound-auth guide's own authorization-code example uses `CustomOAuth2`.
- **But no token reaches the Runtime container.** With that `READY` target, a `tools/call` arrived carrying only `x-amzn-requestid, accept, baggage, content-length, content-type, mcp-method, mcp-protocol-version, x-amzn-bedrock-agentcore-runtime-session-id, x-amzn-trace-id, host` — and `authorization=(none)`. Our server answered its own "no user token". Same wall as `GATEWAY_IAM_ROLE` (below): the AWS transport owns `Authorization` on a Runtime hop, so a per-request bearer has nowhere to ride.
- **An OpenAPI target behaves the same way.** Same provider and grant, target replaced by `mcp.openApiSchema.inlinePayload` (one endpoint, `authen/v1/user_info` — its answer names the token's owner): straight to `READY` with no `authorizationData`, and `tools/call` → the same `An internal error occurred. Please retry later.`, no consent prompt, for both an unconsented caller and one whose token was already vaulted. CloudTrail showed no `AccessDenied` and no `GetResourceOauth2Token` — data-plane calls may not be logged, so that proves little.
  - **Why the failure is upstream of the target.** It reproduces identically whether the target is a Runtime MCP server, a schema-declared MCP server, or an external HTTPS endpoint — the target is never contacted. So it sits in the token-fetch stage, and hosting the MCP server outside AgentCore is unlikely to change it (untested).
  - **One ambiguity left open:** whether the fetch fails on an identity-key mismatch (the agent flow vaults under `lark:{open_id}`, while the Gateway keys on the inbound JWT's `iss`/`sub` and our Cognito `sub` is a UUID) or because the dispatch is unwired for `CustomOauth2`. To separate: make the JWT's `sub`, or a custom claim, equal `lark:{open_id}`.
- **3LO outbound requires `CUSTOM_JWT` inbound.** On an `AWS_IAM` gateway it is refused outright: `ValidationException: 3LO Auth is not supported when gateway authorizer type is AWS_IAM`. Per-user outbound needs an inbound user identity to key on, and SigV4 carries none — so inbound and outbound are **not** freely combinable, contrary to what these notes implied.
- **Two credential provider types we had missed:** `CredentialProviderType` is `['GATEWAY_IAM_ROLE','OAUTH','API_KEY','CALLER_IAM_CREDENTIALS','JWT_PASSTHROUGH']`; the last two need no config struct. `JWT_PASSTHROUGH` (forward the caller's own JWT downstream) is worth evaluating where the downstream validates end-user JWTs directly — untested.
- **Operational trap:** a target stuck in `CREATE_PENDING_AUTH` (nobody completed the consent before the `request_uri` expired) **cannot be deleted** — `DeleteGatewayTarget can't be performed on target when it is in Create_Pending_Auth state`. The way out is `UpdateGatewayTarget`, which issues a fresh `authorizationUrl` (and a fresh synthetic `userId`).
- **The agent-side SDK path DOES work end-to-end for `CustomOauth2`** — you drive 3LO yourself instead of relying on the Gateway:
  1. `GetWorkloadAccessTokenForUserId(workloadName, userId)` → workload access token.
  2. `GetResourceOauth2Token(workloadIdentityToken, resourceCredentialProviderName, scopes, oauth2Flow=USER_FEDERATION, resourceOauth2ReturnUrl=..., customState=base64url(userId))` → `{authorizationUrl, sessionUri}` when no token is vaulted.
  3. User consents (shim → Lark), then `CompleteResourceTokenAuth(sessionUri, userIdentifier={userId})` vaults the token.
  Verified: a real Lark `user_access_token` (JWT) landed in the managed Token Vault and is retrievable per-user. So the workaround for the Gateway gap is: drive 3LO from the agent (not the Gateway), and deliver the vaulted token to the MCP server yourself (custom passthrough header). See the finding below.

## FINDING: AgentCore native 3LO WORKS end-to-end for a non-standard IdP (Lark) via the agent-driven path — measured

**Bottom line:** Lark, as a `CustomOauth2` provider behind an RFC-6749 shim, **does** complete native per-user 3LO and vault the token. Verified: a real user consented once through the shim → Lark → AgentCore, the return page showed "Authorized", and `GetResourceOauth2Token` then returned a real Lark `user_access_token` (a ~1529-char JWT) from the managed Token Vault. So the "fully native managed Token Vault for the user token" thesis holds for Lark too — **no self-managed store needed after all.**

**Revised @2026-07 after re-running it:** a prior version of this finding said CustomOauth2 3LO completion was "defective" (CompleteResourceTokenAuth → "Invalid or expired session"). That was a **misdiagnosis** caused by our own operational noise, not an AWS defect. Three self-inflicted issues, each independently confirmed and fixed, produced that error:
1. **Server-side "generate-and-verify" curl consumed the single-use `request_uri`** before the user's browser could — after which the browser hit an already-spent session (surfacing as "Invalid request" / "Invalid or expired session"). Fix: never touch the authorizationUrl server-side; hand it straight to the user.
2. **A `:` in `customState`** (raw `lark:userAAA`) made AgentCore reject the authorize request with a misleading "requestUri regex" error. Fix: base64url-encode the userId into state; decode at the return endpoint.
3. **URL mangling in transit** (double-encoded `%3A`→`%253A` / stray whitespace from copy-paste). Fix: hand the URL as one unbroken line.

With all three avoided (base64url state, no server-side curl, clean URL) the identical flow succeeds — first proven with `GoogleOauth2` (built-in), then reproduced with `lark-agent-3lo` (CustomOauth2). So the agent-driven path (`GetWorkloadAccessTokenForUserId` → `GetResourceOauth2Token USER_FEDERATION` → surface URL → `CompleteResourceTokenAuth`) works for **both** built-in and custom providers.

**Superseded (2026-08-18):** this section used to end by naming the Gateway-mediated per-user elicitation as "the one real remaining AWS gap" for `CustomOauth2`. Running it showed otherwise — the Gateway completes 3LO for a custom provider and the target reaches `READY`; what it will not do is deliver the token to an **AgentCore Runtime** target's container. The reason to drive 3LO from the agent is that delivery gap, not a vendor gap. See the findings above, verified @2026-08-18.

### Refinement (2026-08, measured directly): the Gateway *can* be configured for `AUTHORIZATION_CODE` — but what you get is target-level, not per-user

The account above rests on the second-hand CloudTrail evidence in #1424. Configuring it ourselves gave a more precise, and more dangerous, picture — the failure is not that it refuses, it is that **it appears to work**:

- An `mcpServer` target with `oauthCredentialProvider` + `grantType: AUTHORIZATION_CODE` on a `CustomOauth2` provider **creates successfully**, reaches status `CREATE_PENDING_AUTH`, and returns a real `authorizationUrl` that 302s to our shim with proper PKCE parameters. Nothing about it looks broken.
- But the accompanying `userId` is of the form `{gatewayId}_{targetId}_{random}`, and the API documentation defines that field as "The user identifier associated with the OAuth2 authorization session that is **defined by AgentCore Gateway**". So it is a **synthetic, target-level, one-time federation**: an operator consents once, and every subsequent call shares that single token.
- **Mistaking this for per-user injection is worse than not using it at all** — per-user identity is silently lost (the effect degrades to 2LO) while everything continues to appear correct. The direction of the original conclusion stands; the wording needed fixing: not "you cannot configure it", but "what you configure is not per-user".
- One self-inflicted trap found along the way: **`grantType` is a top-level field of `oauthCredentialProvider`**, not something inside `customParameters`. Putting it in `customParameters` silently falls back to `CLIENT_CREDENTIALS`, which then fails against a shim that implements no 2LO — and the resulting error reads like "3LO is unsupported". We lost a round to this.

**What works (measured):**
- Shim translating Lark's non-standard OAuth (JSON body / `code:"0"` envelope / HTTP-200-on-error / PKCE `code_verifier`) into RFC-6749.
- `CustomOauth2` provider registration, Gateway `mcpServer` target (via `mcpToolSchema`), lark-mcp on Runtime, Gateway→lark-mcp forwarding, per-user microVM isolation.
- Agent-driven 3LO end to end: `GetResourceOauth2Token(USER_FEDERATION)` → consent → `CompleteResourceTokenAuth` → **Lark token vaulted and retrievable per-user.**

**Consequence for the design:** the user-token leg is **fully native** — the managed Token Vault holds and refreshes each user's Lark token; no self-managed Secrets Manager store is needed. The only thing you must not rely on for a `CustomOauth2` provider is the Gateway *auto-eliciting* consent at `tools/call`; instead the agent drives 3LO explicitly (initiate → surface URL in chat → complete on the return endpoint). Operationally, respect the three rules above (single-use request_uri, colon-free base64url state, unbroken URL) or the flow appears to fail with misleading errors.

- **`CompleteResourceTokenAuth` requires both `sessionUri` and `userIdentifier`** (a struct `{userId}` or `{userToken}`), and the OAuth authorization code is short-lived (~5 min) — the completion must happen automatically on the return-URL callback, not via a delayed manual step.

## Built-in OAuth2 vendors vs CustomOauth2 — which downstream systems need the shim + agent-side workaround

`create-oauth2-credential-provider` accepts a `credentialProviderVendor`. The full set (from the control-plane API schema) is 24 built-in vendors plus `CustomOauth2`:

`GoogleOauth2, GithubOauth2, SlackOauth2, SalesforceOauth2, MicrosoftOauth2, AtlassianOauth2, LinkedinOauth2, XOauth2, OktaOauth2, OneLoginOauth2, PingOneOauth2, FacebookOauth2, YandexOauth2, RedditOauth2, ZoomOauth2, TwitchOauth2, SpotifyOauth2, DropboxOauth2, NotionOauth2, HubspotOauth2, CyberArkOauth2, FusionAuthOauth2, Auth0Oauth2, CognitoOauth2` — and `CustomOauth2`.

Two things vary across them, and they are independent:

**Integration depth (from the config schema):** only seven have a dedicated config sub-key with endpoints baked in — Google, Github, Slack, Salesforce, Microsoft, Atlassian, Linkedin (you supply just client id/secret). The rest of the built-ins (Okta, PingOne, Auth0, Zoom, Notion, …) use `includedOauth2ProviderConfig`, where you still supply the `authorizationEndpoint`/`tokenEndpoint`/`issuer` yourself. `CustomOauth2` is fully self-described (via `authorizationServerMetadata` or a discovery URL).

**Whether Gateway-mediated per-user consent works (note: `-32042` below is a code we carried from community reports; official docs list `-32600`/`-32601`/`-32021` plus `URLElicitationRequiredError`):**
- **Publicly confirmed working:** `GithubOauth2`, `LinkedinOauth2` (AWS samples/workshops).
- **Confirmed NOT working:** `CustomOauth2` (issue #1424).
- **Listed but unverified in public material:** the other 22 built-ins — very likely fine (the gap is `CustomOauth2`-specific), but not proven; verify per-vendor before relying on it.

**Routing guidance for adding a downstream business system:**
- If the system is a built-in vendor (Salesforce, Microsoft/M365, Atlassian/Jira, Slack, Zoom, HubSpot, Okta, Google, …), register it directly as that vendor — no shim needed, and the Gateway-native 3LO likely works (verify the elicitation once).
- If it is NOT one of the built-ins (e.g. **Lark/Feishu**, and most China-region or in-house IdPs), it must be `CustomOauth2` — which means: (a) if its OAuth is non-standard, front it with an RFC-6749 shim to register at all; and (b) drive 3LO agent-side (`GetResourceOauth2Token`), because Gateway `tools/call` elicitation won't fire. This project's Lark path is the reference implementation of that "non-built-in IdP" pattern.
