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
