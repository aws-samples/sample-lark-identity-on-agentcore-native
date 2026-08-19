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
- **OAuth 3LO outbound cannot substitute for it on a Runtime target** — the Bearer displaces the SigV4 signature the Runtime's own front door requires, so the call never reaches the container. Verified @2026-08-19; see "A Runtime `mcpServer` target cannot receive the per-user token" below. On an **OpenAPI** (external HTTPS) target OAuth 3LO outbound does inject the vaulted per-user token, fresh per request.
- **A gateway target holds exactly one credential provider** (`credentialProviderConfigurations` is fixed at 1 item); `GATEWAY_IAM_ROLE` and `OAUTH` are mutually exclusive on one target, and SigV4 and a bearer token cannot share the `Authorization` header anyway. For a Runtime target that single slot is already spoken for by the transport, which is what makes the two options above exhaustive rather than merely inconvenient.

## mcpServer target wiring gotchas

- **The target endpoint must be the full URL-encoded runtime ARN** (`https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<urlencoded-arn>/invocations?qualifier=DEFAULT`), not the bare runtime id — otherwise target creation FAILS with "accountID is required".
- **The target caches the downstream MCP handshake at creation.** After upgrading the server image, delete and recreate the target so the gateway re-handshakes.
- **The downstream MCP server must speak the gateway's locked MCP protocol version.** A gateway pinned to `2025-11-25` rejects a server that only negotiates older versions; `update-gateway` cannot add protocol versions to an existing gateway (recreate it). For the Python `mcp` SDK, `2025-11-25` requires `mcp >= 1.23.0`.

## Client contract for calling a Gateway MCP endpoint directly

- Send `MCP-Protocol-Version: <version>` on **every** request after `initialize` (the gateway is otherwise stateless about it and falls back to an older version, 400 `-32042`).
- Tool names are target-prefixed: `<targetName>___<toolName>`.

## How the workload access token is obtained — and why inbound auth decides the trust model

Every per-user token fetch starts with a workload access token (WAT). There are **two ways to get one, and they have completely different security properties**. Which one applies is decided by the Runtime's *inbound* auth, not by anything in the agent.

**Manual path — `GetWorkloadAccessTokenForUserId(workloadName, userId)`** (what this sample used until the `CUSTOM_JWT` cutover, and what any SigV4-invoked deployment is left with, since the identity arrives as a payload string):

- **It does not check whether the caller "owns" the `userId`** (measured: our agent passes whatever `actorId` the router put in the payload, for arbitrary users, and it succeeds). So the calling code's choice of `userId` *is* the identity it acts as. Any user who has consented once is reachable, with nobody present.
- Consequence: with SigV4 inbound, per-user safety rests on the agent only ever passing the `actorId` it was given — **code discipline, not a cryptographic constraint**. That matters because the agent is the component processing untrusted input. This sample no longer relies on it: inbound is `CUSTOM_JWT` and both by-name APIs are IAM-denied on the execution role.

**Automatic path — Runtime does it for you when inbound auth is `CUSTOM_JWT`** (documented in the [devguide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/get-workload-access-token.html), and **measured @2026-08-19** — this is now what this sample runs):

> "Runtime automatically delivers workload access tokens to agent execution instances as payload headers, eliminating the need for manual token management in most scenarios."

The documented sequence: Runtime validates the inbound OAuth token (issuer, signature) → extracts `iss`/`sub` → fetches the agent's workload identity → calls `GetWorkloadAccessTokenForJWT` → passes the WAT to agent code as a payload header. The same page adds the property that makes this the stronger model:

> "Runtime-managed agent identities cannot retrieve workload access tokens directly, preventing token extraction and misuse."

Measured details, beyond what the devguide states:

- **The WAT arrives in three header aliases, same value** (2911 bytes in our case): `x-amzn-bedrock-agentcore-runtime-workload-accesstoken`, `x-amz-bedrock-agentcore-identity-wat`, `workloadaccesstoken`. Read whichever is present; do not assume one name.
- **`Authorization` reaches the container too**, but only if the Runtime's `requestHeaderConfiguration.requestHeaderAllowlist` includes it. The WAT headers arrive regardless.
- **`CUSTOM_JWT` and SigV4 invocation are mutually exclusive, and the error says so plainly**: `AccessDeniedException: Authorization method mismatch. The agent is configured for a different authorization method than what was used`. So the caller's switch to `Authorization: Bearer` and the authorizer change are one cutover, not an additive hardening.
- **Removing the manual call from agent code is not enforcement.** `GetWorkloadAccessTokenForUserId` remains callable by anything holding the execution role, so closing the impersonation surface requires **denying that action in IAM**, not just not calling it. Deny `GetWorkloadAccessTokenForJWT` as well: possessing any user's JWT is itself enough to exchange for their vaulted token.
- **The vault namespace follows the token's `sub`, and consent completion must match it.** Cognito's `sub` is a UUID while the username is `lark:{open_id}`, which is why the JWT-derived namespace is disjoint from `ForUserId`. A consent started from a JWT-derived WAT can only be completed with `userIdentifier={"userToken": <JWT>}`; passing `{"userId": <string>}` fails with `AccessDeniedException: Invalid or expired session` — a misleading message for what is an identity mismatch, not expiry.
- **Migrating an existing deployment costs one re-consent per user.** Measured on a real user with a live grant: `ForUserId` returned the vaulted token, the platform-delivered WAT for the same person returned none plus an `authorizationUrl`. Nothing errors, so the only symptom is users being asked to authorise again.
- **`UpdateAgentRuntime` replaces rather than patches.** Setting the authorizer without resending `environmentVariables` silently cleared all of them; the symptom was a `ValidationException` from `GetResourceOauth2Token` about a missing `ResourceOauth2ReturnUrl`. Read the current config back and resend artifact, role, network and env together.
- `authorizerConfiguration` on a Runtime has exactly one shape, `customJWTAuthorizer` (`discoveryUrl` + `allowedAudience`/`allowedClients`/`allowedScopes`, plus optional custom claim matching) — verified from the `create-agent-runtime` API model. `UpdateAgentRuntime` accepts `authorizerConfiguration`, so switching does not require rebuilding the runtime.

## 3LO (per-user authorization) with a custom OAuth2 provider — the important one

**Rule of thumb, verified @2026-08-19 end to end: the Gateway DOES do per-user 3LO for a `CustomOauth2` provider. Every failure we chased was the gateway role missing IAM permissions.** The symptom is a single opaque string, `An internal error occurred. Please retry later.`, which reads exactly like a missing capability.

The working sequence, measured on an OpenAPI target (Lark REST) with `grantType: AUTHORIZATION_CODE`:

```
tools/call            -> {"code":-32042,"message":"This request requires more information.",
                          "data":{"elicitations":[{"mode":"url","url":"https://bedrock-agentcore…/authorize?request_uri=…",
                                                   "message":"Please login to this URL for authorization."}]}}
user consents         -> CompleteResourceTokenAuth
tools/call (retry)    -> Gateway fetches that user's token, injects Authorization: Bearer, calls Lark
                      -> {"code":0,"data":{"en_name":"…","open_id":"ou_…"},"msg":"success"}
```

The agent never sees the access token.

### The permissions, because this is the whole story

Both groups are required on the **gateway execution role**; removing either returns the opaque error:

| Permission | Why |
|---|---|
| `bedrock-agentcore:GetResourceOauth2Token`, `bedrock-agentcore:GetWorkloadAccessToken*` | the Gateway fetches the token on the caller's behalf |
| `secretsmanager:GetSecretValue` on `bedrock-agentcore-identity!default/oauth2/*` | fetching reads the provider's Identity-managed secret **as the caller** |

One variable at a time: both groups → success; only `bedrock-agentcore:*` → opaque error; only `secretsmanager` → opaque error; neither → opaque error. (Not narrowed *within* the `bedrock-agentcore` group.) A Runtime target additionally needs `bedrock-agentcore:InvokeAgentRuntime` — a separate permission with the same opaque symptom, which is how we first walked into this: the gateway role auto-created for the Web Search connector carries only `InvokeGateway` + `InvokeWebSearch`.

**Debug order: suspect the gateway role before suspecting the feature.** [agentcore-samples #1424](https://github.com/awslabs/agentcore-samples/issues/1424) reports this exact symptom and is very likely the same permission gap; it was closed `not_planned` by the reporter with no AWS response, and these notes leaned on it for months.

**`-32042` is real.** It is the elicitation code the Gateway actually emits, with the URL at `error.data.elicitations[0].url`. AWS's own devguide does not list that number (it describes `URLElicitationRequiredError` and lists `-32600`/`-32601`/`-32021`), so doc-searching for it finds nothing — but the community reports that cite it are correct.

> Four wordings preceded this one, and the sequence is worth keeping as a caution. (1) "the Gateway cannot do per-user 3LO for `CustomOauth2`" — second-hand, from #1424. (2) after seeing a target-level federation complete, over-corrected to "3LO works". (3) narrowed to "target-level works, per-user does not" after three target configurations all returned the opaque error. (4) this one, after adding the Identity permissions and getting real user data back. Steps 1–3 all failed to rule out permissions first, **including after a permission problem had already been diagnosed once in the same investigation**. The generalisable lesson is in the debug order above.

### Other things established along the way

- **3LO outbound requires `CUSTOM_JWT` inbound.** On an `AWS_IAM` gateway the target is refused outright: `ValidationException: 3LO Auth is not supported when gateway authorizer type is AWS_IAM`. Per-user outbound needs an inbound user identity to key on, and SigV4 carries none — so inbound and outbound are **not** freely combinable.
- **`ForUserId` and `ForJWT` are separate vault key spaces.** Measured both directions on one user: a `ForUserId`-derived WAT retrieved the vaulted token, while a `ForJWT`-derived WAT (Cognito `sub` is a UUID) returned no token and an `authorizationUrl`; after consenting once under the `ForJWT` key, that path retrieved a token too — a *different* grant. **Consequence: migrating an existing `ForUserId` deployment to a JWT-keyed path (including the Gateway's) does not inherit consents. Users must re-authorise, and the failure mode is silent — repeated consent prompts, no error.**
- **`CompleteResourceTokenAuth` accepts `userIdentifier={"userToken": <JWT>}`**, not only `{"userId": <string>}` — that is how you bind a consent to a verified JWT identity.
- **Consent windows are short.** `request_uri` is single-use and lasts ~10 min; the authorization code is shorter (~5 min). Completion must happen automatically on the return-url callback — a delayed manual `CompleteResourceTokenAuth` gets `AccessDeniedException: Invalid or expired session`.
- **A target stuck in `CREATE_PENDING_AUTH` cannot be deleted** (`DeleteGatewayTarget can't be performed on target when it is in Create_Pending_Auth state`). Use `UpdateGatewayTarget`, which issues a fresh `authorizationUrl` and a fresh synthetic `userId`.
- **`mcpToolSchema` exists to remove the creation-time credential problem.** Documented as "supported only when the credential provider is configured with an authorization code grant type. Dynamic tool discovery/synchronization will be disabled when target is configured with mcpToolSchema." Without it, a `mcpServer` + `AUTHORIZATION_CODE` target enters `CREATE_PENDING_AUTH` and needs one operator consent under a synthetic `{gatewayId}_{targetId}_{random}` userId — that federation exists only so the Gateway can list tools when no user is present. Supply the schema and the pre-auth disappears (verified: straight to `READY`, no `authorizationData`). Format gotcha: `inlinePayload` is a JSON **string** containing `{"tools":[…]}`; a bare array fails with `mcpToolSchema must be an object with 'tools' array`.
- **`grantType` is a top-level field of `oauthCredentialProvider`**, not something inside `customParameters`. Misplacing it silently falls back to `CLIENT_CREDENTIALS`, which then fails against a shim implementing no 2LO — and the error reads like "3LO is unsupported".
- **Two credential provider types worth knowing:** `CredentialProviderType` is `['GATEWAY_IAM_ROLE','OAUTH','API_KEY','CALLER_IAM_CREDENTIALS','JWT_PASSTHROUGH']`; the last two need no config struct. `JWT_PASSTHROUGH` (forward the caller's own JWT downstream) suits a downstream that validates end-user JWTs directly — untested.
- **OBO (`TOKEN_EXCHANGE`) is configurable only on `CustomOauth2`.** `onBehalfOfTokenExchangeConfig` appears on `CustomOauth2ProviderConfigInput` only — not on `includedOauth2ProviderConfig` (which is how Okta, Auth0, PingOne, Cognito etc. are registered) and not on the seven dedicated vendor sub-keys. Cross-checked @2026-08-19 against both the local SDK model and botocore master. One exception on the *preconfigured* side: the devguide's OBO page says the built-in **Microsoft** provider ships with OBO baked in (`JWT_AUTHORIZATION_GRANT` with `requested_token_use=on_behalf_of`, not adjustable) — custom exchange parameters still require `CustomOauth2`. Grants offered: `TOKEN_EXCHANGE` (RFC 8693) and `JWT_AUTHORIZATION_GRANT` (RFC 7523). Note OBO's real precondition is the entrypoint, not the vendor: it exchanges *an inbound user token*, and a bot/webhook entrypoint carries none.

### A Runtime `mcpServer` target cannot receive the per-user token — verified @2026-08-19

Single-variable experiment: **one** gateway (`CUSTOM_JWT` inbound), **one** synthetic identity, **one** permission set, three targets differing only in type and outbound credential. Same `CustomOauth2` provider, same scopes, same `grantType`, same return URL on both OAuth targets.

| Target | Outbound | Gateway → downstream | Container saw | User token |
|---|---|---|---|---|
| OpenAPI (Lark REST) | OAuth 3LO | ✅ | n/a | ✅ real user data returned |
| Runtime `mcpServer` | OAuth 3LO | ❌ `MCP initialization failed: Authorization error when sending message` | nothing (only platform health-check pings) | ❌ |
| Runtime `mcpServer`, same runtime | SigV4 | ✅ full handshake + `tools/call` | `auth=(none) token=no` | ❌ |

**Mechanism:** the Runtime `/invocations` endpoint authenticates the transport itself and owns the `Authorization` header. With SigV4 outbound the header is spent on the signature and stripped before the container; with OAuth outbound the Bearer replaces the signature and the front door rejects the call. Either way the container cannot be handed a per-user bearer token by the Gateway.

Two controls, because permissions had already produced four wrong conclusions in this same investigation:

- The **SigV4 target on the same runtime succeeded**, returning our own server's `no user token (authorize first)` string — so `InvokeAgentRuntime` and the Gateway→Runtime path were both working.
- Re-run with `bedrock-agentcore:*`, `secretsmanager:*`, `bedrock:*`, `kms:*`, `sts:*` on `Resource: "*"`: **byte-identical failure**, while the OpenAPI target still succeeded. The result is permission-independent.

Also note the OAuth Runtime target's error *changed* from `-32042` to the authorization failure once consent was completed — the Gateway did acquire the token; the loss is at the Runtime hop, not at the vault.

**Consequence for this repo: the agent-side 3LO path stays.** A Runtime-hosted MCP server can only be reached over SigV4, so the per-user Lark token has to arrive some other way — here, the custom passthrough header. The managed Gateway path becomes available only by moving the MCP server off Runtime onto an addressable HTTPS endpoint (ALB / API Gateway / Fargate), which trades Runtime's session and scaling model for it.

**Incidental finding, scoped narrowly: on the Gateway path, one consent did not cover a second target.** The two OAuth targets shared provider, scopes, grant and return URL, yet completing consent on one still left the other emitting `-32042` until separately authorised. Observed once, on Gateway targets only, and not narrowed further — treat it as a thing to check when planning a Gateway rollout, not an established rule.

**This does not apply to the agent-driven path, where one vaulted token serves every downstream server.** There are no targets there: the agent fetches a single token per (workload, provider, scopes) and passes it to each MCP server in a header. Verified in real use @2026-08-19 — one consent, and the same turn used both the lark-cli and the approval MCP server, with later turns needing no re-consent.

## FINDING: AgentCore native 3LO WORKS end-to-end for a non-standard IdP (Lark) via the agent-driven path — measured

**Bottom line:** Lark, as a `CustomOauth2` provider behind an RFC-6749 shim, **does** complete native per-user 3LO and vault the token. Verified: a real user consented once through the shim → Lark → AgentCore, the return page showed "Authorized", and `GetResourceOauth2Token` then returned a real Lark `user_access_token` (a ~1529-char JWT) from the managed Token Vault. So the "fully native managed Token Vault for the user token" thesis holds for Lark too — **no self-managed store needed after all.**

**Revised @2026-07 after re-running it:** a prior version of this finding said CustomOauth2 3LO completion was "defective" (CompleteResourceTokenAuth → "Invalid or expired session"). That was a **misdiagnosis** caused by our own operational noise, not an AWS defect. Three self-inflicted issues, each independently confirmed and fixed, produced that error:
1. **Server-side "generate-and-verify" curl consumed the single-use `request_uri`** before the user's browser could — after which the browser hit an already-spent session (surfacing as "Invalid request" / "Invalid or expired session"). Fix: never touch the authorizationUrl server-side; hand it straight to the user.
2. **A `:` in `customState`** (raw `lark:userAAA`) made AgentCore reject the authorize request with a misleading "requestUri regex" error. Fix: base64url-encode the userId into state; decode at the return endpoint.
3. **URL mangling in transit** (double-encoded `%3A`→`%253A` / stray whitespace from copy-paste). Fix: hand the URL as one unbroken line.

With all three avoided (base64url state, no server-side curl, clean URL) the identical flow succeeds — first proven with `GoogleOauth2` (built-in), then reproduced with `lark-agent-3lo` (CustomOauth2). So the agent-driven path (`GetWorkloadAccessTokenForUserId` → `GetResourceOauth2Token USER_FEDERATION` → surface URL → `CompleteResourceTokenAuth`) works for **both** built-in and custom providers.

**Superseded @2026-08-19:** this section used to name the Gateway-mediated per-user elicitation as "the one real remaining AWS gap" for `CustomOauth2`. It is not a gap — see the top of this document. The agent-driven path below remains correct and useful (it is what this repo runs), but it is a choice, not a workaround for a missing feature.

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
