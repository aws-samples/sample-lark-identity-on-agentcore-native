# Native per-user 3LO with a built-in OAuth2 vendor (Google, Salesforce, M365, Slack, …)

> **3LO = 3-Legged OAuth.** The three "legs" are the three parties: the **end user**, **your application/agent**, and the **resource server**. In 3LO the user personally consents on the IdP's page, and the app receives a token that acts **on behalf of that user** — so it inherits that user's own permissions (OAuth `authorization_code` grant; AgentCore calls this `oauth2Flow=USER_FEDERATION`). Contrast **2LO** (2-Legged, `client_credentials`): no user, the app uses its own identity and gets an app-level token. This whole design needs 3LO because the goal is "the agent acts as the individual employee, with that employee's permissions" — 2LO couldn't distinguish per-user access.

A working, fully-native reference for giving an agent per-user delegated access to a downstream system whose IdP is one of AgentCore Identity's **built-in vendors**. Verified end to end on 2026-07-09 with `GoogleOauth2`: a real user consented once and their Google `ya29.*` access token landed in the managed Token Vault, retrievable per-user thereafter — no self-managed token store.

Use this when the downstream IdP is built-in. **A non-standard IdP that can only be `CustomOauth2`** (e.g. Lark/Feishu behind an RFC-6749 shim) works too — the same agent-driven flow below vaulted a real Lark token once the operational gotchas were respected. See the note at the end.

## The built-in vendors

`create-oauth2-credential-provider --credential-provider-vendor` accepts (as of 2026-07): GoogleOauth2, GithubOauth2, SlackOauth2, SalesforceOauth2, MicrosoftOauth2, AtlassianOauth2, LinkedinOauth2, XOauth2, OktaOauth2, OneLoginOauth2, PingOneOauth2, FacebookOauth2, YandexOauth2, RedditOauth2, ZoomOauth2, TwitchOauth2, SpotifyOauth2, DropboxOauth2, NotionOauth2, HubspotOauth2, CyberArkOauth2, FusionAuthOauth2, Auth0Oauth2, CognitoOauth2 — plus `CustomOauth2` for anything else.

Built-in vendors with a dedicated config sub-key (endpoints baked in, you supply only client id/secret): Google, Github, Slack, Salesforce, Microsoft, Atlassian, Linkedin. The rest use `includedOauth2ProviderConfig` (you also supply authorization/token/issuer endpoints). Publicly confirmed to emit the Gateway `-32042` elicitation: Github, Linkedin. The agent-side flow below works for any built-in vendor and is the more portable pattern.

## Setup (Google shown; other built-ins are analogous)

1. **Create the OAuth client at the IdP.** e.g. Google Cloud Console → Credentials → OAuth client ID → "Web application". Leave redirect URI as a placeholder for now; you fill it in step 3.

2. **Register the credential provider.** Built-in vendor → only client id/secret:
   ```
   aws bedrock-agentcore-control create-oauth2-credential-provider \
     --name google-xyz --credential-provider-vendor GoogleOauth2 \
     --oauth2-provider-config-input '{"googleOauth2ProviderConfig":{"clientId":"...","clientSecret":"..."}}'
   ```

3. **Get the provider's callback URL and register it at the IdP.**
   ```
   aws bedrock-agentcore-control get-oauth2-credential-provider --name google-xyz --query callbackUrl
   # → https://bedrock-agentcore.<region>.amazonaws.com/identities/oauth2/callback/<uuid>
   ```
   Put that exact URL in the IdP OAuth client's Authorized redirect URIs.

4. **Create a workload identity for the agent and allowlist your return URL.** The return URL is where AgentCore redirects the browser after it has received the code; your endpoint there calls `CompleteResourceTokenAuth`.
   ```
   aws bedrock-agentcore-control create-workload-identity --name my-agent-wl
   aws bedrock-agentcore-control update-workload-identity --name my-agent-wl \
     --allowed-resource-oauth2-return-urls "https://<your-return-endpoint>/return"
   ```
   The allowlist is **exact-match** — the return URL passed at token time must equal a registered entry byte for byte (no extra query string).

## The per-user 3LO flow (agent-driven, non-blocking)

```
1. GetWorkloadAccessTokenForUserId(workloadName=my-agent-wl, userId=<end-user id>) → workloadAccessToken
2. GetResourceOauth2Token(
     workloadIdentityToken=<that>,
     resourceCredentialProviderName=google-xyz,
     scopes=[...],
     oauth2Flow=USER_FEDERATION,
     resourceOauth2ReturnUrl="https://<your-return-endpoint>/return",   # bare, allowlisted
     customState=<base64url(userId)>,                                    # carries userId back; MUST be colon-free
     forceAuthentication=true)                                           # mint a fresh authorization
   → if a token is already vaulted for this user: returns {accessToken}
   → otherwise: returns {authorizationUrl, sessionUri}
3. Surface authorizationUrl to the user (a chat message / link). The simplest form ends the turn here (the user re-sends after consenting). This project instead has the router post the link, then poll the vault a bounded time and re-invoke on success, so the user needn't re-send — see the "consent-wait" flow in the README / architecture.md.
4. User opens it → AgentCore /authorize sets session cookies (SameSite=Lax) and 302s to the IdP →
   user consents → IdP 302s the code to AgentCore's own /identities/oauth2/callback → AgentCore
   exchanges the code, then 302s the browser to your return URL with ?session_id=<sessionUri>&state=<base64url userId>.
5. Your return endpoint calls:
     CompleteResourceTokenAuth(sessionUri=<session_id>, userIdentifier={userId: <decoded state>})
   → the token is vaulted, keyed to that user.
6. Next turn, step 2 returns {accessToken} directly → use it (e.g. inject as Authorization: Bearer to a downstream MCP server / API).
```

`userIdentifier` uses `{userId}` when the workload token came from `GetWorkloadAccessTokenForUserId`; use `{userToken}` (the original IdP JWT) when it came from `GetWorkloadAccessTokenForJWT`. Never pass the workloadIdentityToken there.

## Operational gotchas (learned the hard way; each cost a failed consent)

- **`request_uri` (the authorizationUrl's session) is single-use and short-lived.** Generate it, hand it straight to the user, and let the browser be the first thing to open it. **Do NOT curl-verify it server-side first — that consumes it,** after which the user's browser gets `{"message":"Invalid request"}`.
- **`customState` must be colon-free.** A `:` in state (e.g. a raw `lark:ou_x` userId) makes AgentCore reject the request with a *misleading* error: `Value at 'requestUri' failed to satisfy ... pattern`. Base64url-encode the userId into state and decode it at the return endpoint.
- **That same "requestUri regex" error is also what a stale/expired/consumed request_uri returns** — it does not mean the URL is malformed. If a byte-correct URL suddenly 400s with it, the session is gone; mint a fresh one.
- **Don't let the URL get mangled in transit.** A double-encoded `%3A`→`%253A` (from copy/paste or line-wrapping) or a decoded bare `:` both fail. Hand the URL as one unbroken line.
- **The return URL allowlist is exact-match** — keep the return URL bare and carry variable data (userId) via `customState`, not query params on the return URL.
- **PKCE:** AgentCore drives PKCE for you on the built-in path (no shim to worry about). For CustomOauth2 with a shim, the shim's token endpoint must forward `code_verifier` to the upstream.

## Verified result (2026-07-09)

`GoogleOauth2` provider, `USER_FEDERATION`, scope `userinfo.email`: real user consented → return page showed "Authorized" → `GetResourceOauth2Token` then returned a real Google `ya29.*` token (340 chars) from the vault. The managed Token Vault stores and returns it per-user with no self-managed storage.

## Also works for CustomOauth2 (Lark/Feishu, in-house IdPs)

The same agent-driven flow vaults a token for a `CustomOauth2` provider too. Verified 2026-07-09:

| provider | type | agent-driven 3LO → token vaulted |
|---|---|---|
| GoogleOauth2 | built-in | ✅ real `ya29.*` vaulted |
| Lark (shim) | CustomOauth2 | ✅ real `user_access_token` (JWT) vaulted |

An earlier note here wrongly claimed CustomOauth2 completion was "defective". That was a misdiagnosis: three operational mistakes (curl-consuming the single-use `request_uri`, a `:` in `customState`, URL mangling) produced misleading `Invalid request` / `Invalid or expired session` errors. Once all three are avoided — exactly the gotchas listed above — the CustomOauth2 flow completes and vaults the token, same as a built-in vendor.

**The one real remaining AWS gap** (unrelated to vaulting): the **Gateway-mediated** per-user elicitation at `tools/call` does not emit `-32042` for `CustomOauth2` (only for built-in vendors) — `agentcore-samples#1424`. That just means you cannot rely on the *Gateway auto-prompting* the user for a custom provider; drive 3LO from the agent instead (as this doc describes). Extra shim requirement for a non-standard token endpoint like Lark's: the shim's `/token` must forward PKCE `code_verifier` to the upstream.
