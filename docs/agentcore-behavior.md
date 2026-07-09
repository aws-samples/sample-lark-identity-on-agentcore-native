# AgentCore Gateway + Runtime — behavior notes (measured)

Facts about how AgentCore Gateway and Runtime actually behave for an MCP-server target, established by direct measurement against a deployed probe MCP server (a minimal FastMCP server whose `whoami` reports a process-start instance id, a per-process call counter, and the request headers it received). These are behaviors the AWS docs leave implicit; recording them so they aren't rediscovered the hard way. Account-specific ids and raw run data live in the team's local working notes, not here.

## Session & microVM lifecycle

- **A microVM is per MCP session, 1:1.** Reusing one session across calls always lands on the same microVM; a different session gets a different microVM. Isolation is real and physical (separate CPU/memory/filesystem).
- **Gateway sessions are opt-in.** Unless the gateway is created with `protocolConfiguration.mcp.sessionConfiguration.sessionTimeoutInSeconds` (900–28800), the `initialize` response carries **no** `Mcp-Session-Id` header, and every `tools/call` cold-starts a fresh downstream microVM. With it configured, `initialize` returns an `Mcp-Session-Id`; the client must echo it on every later request to get warm-microVM reuse.
- **Within a session, the microVM and its MCP server process are reused — not cold-started per call.** Measured: 5 consecutive calls on one session hit the same instance id with a per-process counter incrementing 1→5. The first call for a target pays the cold start + MCP `initialize`; later same-session calls skip both. Idle ~15 min before reclaim, 8 h absolute max.
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

## 3LO (per-user authorization) with a custom OAuth2 provider — the important one

Registering a non-standard IdP (e.g. behind an RFC-6749 shim) yields a `CustomOauth2` credential provider. Two facts govern what works:

- **The Gateway's per-user 3LO elicitation at `tools/call` does NOT fire for `CustomOauth2` providers.** When the calling user has no vaulted token, instead of the documented `-32042` elicitation (auth URL), the Gateway returns a tool result `{isError:true, "An internal error occurred. Please retry later."}` and never even calls Identity. This elicitation path is wired for built-in vendors (GithubOauth2, LinkedinOauth2 are the publicly confirmed ones) but not for `CustomOauth2`. It is an AWS-side gap (see awslabs/agentcore-samples issue #1424, closed "not planned"). A shim makes the IdP *standard*, but not a *built-in vendor* — so it cannot dodge this.
- **The agent-side SDK path gets FURTHER but still cannot vault the token for `CustomOauth2`.** Driving 3LO explicitly avoids the `tools/call` elicitation gap:
  1. `GetWorkloadAccessTokenForUserId(workloadName, userId)` → workload access token.
  2. `GetResourceOauth2Token(workloadIdentityToken, resourceCredentialProviderName, scopes, oauth2Flow=USER_FEDERATION, resourceOauth2ReturnUrl=..., customState=userId)` → returns `{authorizationUrl, sessionUri}` when no token is vaulted.
  This part works: the user opens the URL, the shim 302s to Lark, the user consents, Lark returns a code, and the shim's `/token` successfully exchanges it (Lark issues a real `user_access_token`). But the final **vaulting step fails**: `CompleteResourceTokenAuth(sessionUri, userIdentifier={userId})` returns `AccessDeniedException: "Invalid or expired session"`, and the token never lands in the vault. See the dedicated finding below.

## FINDING: AgentCore native 3LO WORKS end-to-end for a non-standard IdP (Lark) via the agent-driven path — measured

**Bottom line:** Lark, as a `CustomOauth2` provider behind an RFC-6749 shim, **does** complete native per-user 3LO and vault the token. Verified: a real user consented once through the shim → Lark → AgentCore, the return page showed "Authorized", and `GetResourceOauth2Token` then returned a real Lark `user_access_token` (a ~1529-char JWT) from the managed Token Vault. So the "fully native managed Token Vault for the user token" thesis holds for Lark too — **no self-managed store needed after all.**

**Correction of an earlier wrong conclusion:** a prior version of this finding said CustomOauth2 3LO completion was "defective" (CompleteResourceTokenAuth → "Invalid or expired session"). That was a **misdiagnosis** caused by our own operational noise, not an AWS defect. Three self-inflicted issues, each independently confirmed and fixed, produced that error:
1. **Server-side "generate-and-verify" curl consumed the single-use `request_uri`** before the user's browser could — after which the browser hit an already-spent session (surfacing as "Invalid request" / "Invalid or expired session"). Fix: never touch the authorizationUrl server-side; hand it straight to the user.
2. **A `:` in `customState`** (raw `lark:userAAA`) made AgentCore reject the authorize request with a misleading "requestUri regex" error. Fix: base64url-encode the userId into state; decode at the return endpoint.
3. **URL mangling in transit** (double-encoded `%3A`→`%253A` / stray whitespace from copy-paste). Fix: hand the URL as one unbroken line.

With all three avoided (base64url state, no server-side curl, clean URL) the identical flow succeeds — first proven with `GoogleOauth2` (built-in), then reproduced with `lark-id-3lo` (CustomOauth2). So the agent-driven path (`GetWorkloadAccessTokenForUserId` → `GetResourceOauth2Token USER_FEDERATION` → surface URL → `CompleteResourceTokenAuth`) works for **both** built-in and custom providers.

**The one real, remaining AWS gap:** the **Gateway-mediated** per-user elicitation at `tools/call` still returns `{isError:true,"An internal error occurred"}` instead of `-32042` for `CustomOauth2` (awslabs/agentcore-samples #1424, "not planned"). That only means you cannot rely on the *Gateway auto-prompting* the user for a custom provider — you drive the 3LO from the agent instead (which we do). Vaulting itself is fine.

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

**Whether Gateway per-user `-32042` elicitation works (the thing that matters for end-user 3LO):**
- **Publicly confirmed working:** `GithubOauth2`, `LinkedinOauth2` (AWS samples/workshops).
- **Confirmed NOT working:** `CustomOauth2` (issue #1424).
- **Listed but unverified in public material:** the other 22 built-ins — very likely fine (the gap is `CustomOauth2`-specific), but not proven; verify per-vendor before relying on it.

**Routing guidance for adding a downstream business system:**
- If the system is a built-in vendor (Salesforce, Microsoft/M365, Atlassian/Jira, Slack, Zoom, HubSpot, Okta, Google, …), register it directly as that vendor — no shim needed, and the Gateway-native 3LO likely works (verify the elicitation once).
- If it is NOT one of the built-ins (e.g. **Lark/Feishu**, and most China-region or in-house IdPs), it must be `CustomOauth2` — which means: (a) if its OAuth is non-standard, front it with an RFC-6749 shim to register at all; and (b) drive 3LO agent-side (`GetResourceOauth2Token`), because Gateway `tools/call` elicitation won't fire. This project's Lark path is the reference implementation of that "non-built-in IdP" pattern.
