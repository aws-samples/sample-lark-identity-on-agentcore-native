// Minimal MCP server that executes the official lark-cli per tool call with the
// caller's per-request user_access_token — statelessly (env var, no AuthStore).
//
// Why this instead of official lark-mcp: lark-mcp validates a user token against
// its LOCAL AuthStore (populated only by `lark-mcp login`), so an externally
// vaulted token is rejected as "invalid or expired". lark-cli's env token
// (LARKSUITE_CLI_USER_ACCESS_TOKEN) bypasses that entirely — verified.
//
// Contract: AgentCore Runtime MCP server on 0.0.0.0:8000/mcp (streamable HTTP,
// SSE responses). Per-request user token arrives in the custom passthrough header
// the agent sets (no sidecar needed — we read it directly).

const http = require('http');
const { execFile } = require('child_process');

const PORT = parseInt(process.env.PORT || '8000', 10);
const APP_ID = process.env.APP_ID || '';
const APP_SECRET = process.env.APP_SECRET || '';
const BRAND = process.env.LARK_BRAND || 'lark';           // 'lark' (international) | 'feishu'
const TOKEN_HEADER = 'x-amzn-bedrock-agentcore-runtime-custom-lark-token';  // lowercased by node

// A small, high-signal tool set that proves per-user access end to end, plus a
// generic raw-API escape hatch covering every Lark endpoint. Kept intentionally
// tiny; expand or auto-generate from `lark-cli --help` later.
const TOOLS = [
  {
    name: 'lark_whoami',
    description: "Return the calling Lark user's own profile (name, open_id, email). Proves the agent is acting as that user.",
    inputSchema: { type: 'object', properties: {} },
    cli: () => ['api', 'GET', '/open-apis/authen/v1/user_info'],
  },
  {
    name: 'lark_list_my_docs',
    description: "List the calling user's own Lark cloud-drive files (scoped to what that user can see). Optional folder_token to list a subfolder.",
    inputSchema: { type: 'object', properties: { folder_token: { type: 'string', description: 'optional folder token; omit for drive root' } } },
    cli: (a) => ['api', 'GET', '/open-apis/drive/v1/files' + (a.folder_token ? `?folder_token=${encodeURIComponent(a.folder_token)}` : '')],
  },
  {
    name: 'lark_api',
    description: "Raw Lark OpenAPI passthrough as the calling user. method=GET/POST/... path=/open-apis/... ; optional params (query, JSON string) and data (body, JSON string).",
    inputSchema: {
      type: 'object',
      properties: {
        method: { type: 'string', description: 'HTTP method, e.g. GET or POST' },
        path: { type: 'string', description: 'API path, e.g. /open-apis/im/v1/chats' },
        params: { type: 'string', description: 'optional query params as a JSON string' },
        data: { type: 'string', description: 'optional request body as a JSON string' },
      },
      required: ['method', 'path'],
    },
    cli: (a) => {
      const args = ['api', a.method, a.path];
      if (a.params) args.push('--params', a.params);
      if (a.data) args.push('--data', a.data);
      return args;
    },
  },
];

function runLarkCli(cliArgs, userToken) {
  return new Promise((resolve) => {
    const env = {
      PATH: process.env.PATH,
      HOME: process.env.HOME || '/tmp',
      LARKSUITE_CLI_USER_ACCESS_TOKEN: userToken,
      LARKSUITE_CLI_APP_ID: APP_ID,
      LARKSUITE_CLI_APP_SECRET: APP_SECRET,
      LARKSUITE_CLI_BRAND: BRAND,
      LARKSUITE_CLI_DEFAULT_AS: 'user',   // always act as the user, never the bot
    };
    execFile('lark-cli', cliArgs, { timeout: 30000, maxBuffer: 10 * 1024 * 1024, env },
      (err, stdout, stderr) => {
        if (err && !stdout) resolve({ isError: true, text: `lark-cli error: ${stderr || err.message}` });
        else resolve({ isError: false, text: stdout.trim() || stderr.trim() });
      });
  });
}

function sse(res, obj) {
  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
  res.end(`event: message\ndata: ${JSON.stringify(obj)}\n\n`);
}

const server = http.createServer((req, res) => {
  if (req.method === 'GET') { res.writeHead(200); res.end('ok'); return; }
  let body = '';
  req.on('data', (c) => (body += c));
  req.on('end', async () => {
    let mcp;
    try { mcp = JSON.parse(body); } catch { res.writeHead(400); res.end('bad json'); return; }
    const userToken = req.headers[TOKEN_HEADER] || '';

    if (mcp.method === 'initialize') {
      return sse(res, { jsonrpc: '2.0', id: mcp.id, result: {
        protocolVersion: '2025-11-25',
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: 'lark-cli-mcp', version: '1.0.0' },
      } });
    }
    if (mcp.method === 'notifications/initialized') { res.writeHead(202); res.end(); return; }
    if (mcp.method === 'tools/list') {
      const tools = TOOLS.map((t) => ({ name: t.name, description: t.description, inputSchema: t.inputSchema }));
      return sse(res, { jsonrpc: '2.0', id: mcp.id, result: { tools } });
    }
    if (mcp.method === 'tools/call') {
      const name = mcp.params && mcp.params.name;
      const args = (mcp.params && mcp.params.arguments) || {};
      const tool = TOOLS.find((t) => t.name === name);
      if (!tool) return sse(res, { jsonrpc: '2.0', id: mcp.id, result: { content: [{ type: 'text', text: `unknown tool: ${name}` }], isError: true } });
      if (!userToken) return sse(res, { jsonrpc: '2.0', id: mcp.id, result: { content: [{ type: 'text', text: 'no user token (authorize first)' }], isError: true } });
      const out = await runLarkCli(tool.cli(args), userToken);
      return sse(res, { jsonrpc: '2.0', id: mcp.id, result: { content: [{ type: 'text', text: out.text }], isError: out.isError } });
    }
    return sse(res, { jsonrpc: '2.0', id: mcp.id || null, error: { code: -32601, message: `method not found: ${mcp.method}` } });
  });
});

// Report the engine versions actually running, not the ones the build asked for: the
// image tag records the build argument, which is a different claim.
server.listen(PORT, '0.0.0.0', () => {
  console.log(`lark-cli-mcp on :${PORT} (${TOOLS.length} tools) node=${process.version}`);
  execFile('lark-cli', ['--version'], { timeout: 10000 }, (err, stdout) => {
    console.log(`lark-cli=${err ? `unavailable (${err.message})` : stdout.trim()}`);
  });
});
