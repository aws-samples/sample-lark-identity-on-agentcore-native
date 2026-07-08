/* Lark-embedded agent chat client.
 *
 * Flow:
 *   1. tt.requestAccess (Lark h5sdk) -> short-lived login code
 *   2. POST {apiBase}/api/lark/auth {code} -> Cognito idToken (Lark is the IdP)
 *   3. POST {apiBase}/api/session (Bearer idToken) -> {wsUrl, sessionId}
 *   4. WebSocket(wsUrl); send {type:chat,...}; render {type:delta}/{type:final}
 *
 * The WSS layer is the primary path (streaming). If the presigned-WSS bridge is
 * unavailable the code surfaces a clear error rather than silently degrading;
 * the SSE fallback lives server-side and can be enabled without client changes.
 */
(function () {
  "use strict";

  var cfg = window.LARK_AGENT_CONFIG || {};
  var API = (cfg.apiBase || "").replace(/\/$/, "");

  var elChat = document.getElementById("chat");
  var elStatus = document.getElementById("status");
  var elInput = document.getElementById("input");
  var elSend = document.getElementById("send");
  var elForm = document.getElementById("composer");

  var state = { idToken: null, actorId: null, displayName: null, ws: null, currentBot: null, botRaw: "" };

  // Login-state cache (sessionStorage). Lark's requestAccess re-prompts every time the page calls it, 
  // so we cache the minted JWT and reuse it across refreshes — only re-authenticating (which re-triggers the Lark consent popup) 
  // when there is no valid token. The JWT's own `exp` claim decides validity.
  var AUTH_KEY = "larkAgentAuth";
  function jwtExpMs(tok) {
    try {
      var p = JSON.parse(atob(tok.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
      return (p.exp || 0) * 1000;
    } catch (e) { return 0; }
  }
  function saveAuth(auth) {
    try {
      sessionStorage.setItem(AUTH_KEY, JSON.stringify({
        idToken: auth.idToken, actorId: auth.actorId,
        name: auth.name || auth.actorId, exp: jwtExpMs(auth.idToken),
      }));
    } catch (e) { /* private mode / quota — fall back to in-memory only */ }
  }
  function loadAuth() {
    try {
      var a = JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null");
      // 60s skew so we never hand a token that expires mid-request.
      if (a && a.idToken && a.exp - 60000 > nowMs()) return a;
    } catch (e) { /* ignore */ }
    return null;
  }
  function clearAuth() { try { sessionStorage.removeItem(AUTH_KEY); } catch (e) { /* ignore */ } }
  function nowMs() { return new Date().getTime(); }

  function setStatus(text, cls) {
    elStatus.textContent = text;
    elStatus.className = "status" + (cls ? " " + cls : "");
  }
  // Markdown for agent replies only; sanitize since agent output is untrusted.
  // Falls back to plain text if marked/DOMPurify didn't load.
  var mdReady = window.marked && window.DOMPurify;
  function renderMarkdown(el, raw) {
    if (!mdReady) { el.textContent = raw; return; }
    el.innerHTML = window.DOMPurify.sanitize(window.marked.parse(raw));
  }
  function addMsg(text, kind) {
    var d = document.createElement("div");
    d.className = "msg " + kind;
    if (kind === "bot" && mdReady) { d.classList.add("md"); renderMarkdown(d, text); }
    else d.textContent = text;
    elChat.appendChild(d);
    elChat.scrollTop = elChat.scrollHeight;
    return d;
  }
  function enableInput(on) {
    elInput.disabled = !on;
    elSend.disabled = !on;
    if (on) { setWaiting(false); elInput.focus(); }
  }

  // Waiting state: the composer is disabled while the agent works, so make it
  // explicit this is "awaiting a reply" (not a broken/frozen UI). A CSS spinner
  // replaces the button label in place (fixed width, no reflow); status echoes it.
  function setWaiting(on) {
    elSend.classList.toggle("waiting", on);
    if (on) setStatus("agent is thinking…", "");
  }

  // --- step 1: get a Lark login code -------------------------------------
  function getLarkCode() {
    return new Promise(function (resolve, reject) {
      if (!window.h5sdk || !window.tt) {
        return reject(new Error("NOT_IN_LARK"));
      }
      window.h5sdk.ready(function () {
        window.tt.requestAccess({
          appID: cfg.larkAppId,
          // Scopes the user consents to; the resulting user_access_token can only
          // reach these, further narrowed by the user's own Lark permissions.
          // Must already be granted to the app in the Lark console. offline_access
          // yields a refresh_token (user_access_token lives only ~2h).
          // Must match the app's User Token Scopes in the Lark console exactly,
          // or requestAccess fails with 20027. Read-write here (create/edit docs
          // + manage drive files) per the configured scopes.
          scopeList: cfg.scopeList || [
            "drive:drive",       // view/comment/edit/manage My Space files
            "docx:document",     // create and edit docx
            "offline_access",    // refresh_token for token renewal
          ],
          success: function (res) { resolve(res.code); },
          fail: function (err) { reject(new Error("requestAccess failed: " + JSON.stringify(err))); },
        });
      });
      window.h5sdk.error(function (err) { reject(new Error("h5sdk error: " + JSON.stringify(err))); });
    });
  }

  // --- step 2: Lark login code -> fresh Cognito JWT ----------------------
  // Reusable so reconnect can re-mint a JWT (the Cognito idToken expires in ~1h;
  // Lark 免登 is silent since the user is already signed into the Lark client).
  function authenticate() {
    return getLarkCode()
      .then(function (code) {
        return fetch(API + "/api/lark/auth", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code: code }),
        });
      })
      .then(function (r) { if (!r.ok) throw new Error("auth failed " + r.status); return r.json(); })
      .then(function (auth) {
        state.idToken = auth.idToken;
        state.actorId = auth.actorId;
        state.displayName = auth.name || auth.actorId;
        saveAuth(auth);   // cache so a refresh reuses it and skips the Lark popup
        return auth;
      });
  }

  // Reuse a cached, still-valid JWT (no Lark popup) or fall back to full auth.
  function ensureAuth() {
    var a = loadAuth();
    if (!a) return authenticate();
    state.idToken = a.idToken;
    state.actorId = a.actorId;
    state.displayName = a.name;
    return Promise.resolve(a);
  }

  // --- step 3: create/refresh a session (WSS URL) ------------------------
  function createSession() {
    return fetch(API + "/api/session", {
      method: "POST", headers: { Authorization: "Bearer " + state.idToken },
    }).then(function (r) {
      if (!r.ok) throw new Error("session failed " + r.status);
      return r.json();
    });
  }

  // Get a fresh WSS URL for reconnect. If the stored JWT has expired (401),
  // silently re-authenticate via Lark and retry once.
  function refreshSession() {
    return fetch(API + "/api/session", {
      method: "GET", headers: { Authorization: "Bearer " + state.idToken },
    }).then(function (r) {
      if (r.status === 401) {
        clearAuth();                                 // stale cached token → drop it
        return authenticate().then(createSession);   // re-login (may re-prompt)
      }
      if (!r.ok) throw new Error("session refresh failed " + r.status);
      return r.json();
    });
  }

  // --- step 4: WebSocket chat --------------------------------------------
  function connectWs(wsUrl) {
    return new Promise(function (resolve, reject) {
      var ws = new WebSocket(wsUrl);
      var opened = false;
      ws.onopen = function () { opened = true; state.ws = ws; resolve(ws); };
      ws.onerror = function () { if (!opened) reject(new Error("WSS connect failed")); };
      // On idle disconnect keep the composer usable — the next send reconnects.
      ws.onclose = function () {
        state.ws = null;
        setStatus("idle 💤", "");
        enableInput(true);
      };
      ws.onmessage = function (ev) {
        var frame;
        try { frame = JSON.parse(ev.data); } catch (e) { return; }
        if (frame.type === "delta") {
          if (!state.currentBot) {
            state.currentBot = addMsg("", "bot"); state.botRaw = "";
            setStatus("replying…", "");   // first token arrived — agent is streaming
          }
          state.botRaw += frame.text;
          // Re-render the accumulated markdown each delta (plain-text fallback inside).
          if (state.currentBot.classList.contains("md")) renderMarkdown(state.currentBot, state.botRaw);
          else state.currentBot.textContent = state.botRaw;
          elChat.scrollTop = elChat.scrollHeight;
        } else if (frame.type === "final") {
          state.currentBot = null;
          enableInput(true);
          setStatus("connected as " + state.displayName, "ok");
        } else if (frame.type === "error") {
          addMsg("⚠️ " + frame.message, "note");
          state.currentBot = null;
          enableInput(true);
        }
      };
    });
  }

  function deliver(text) {
    state.currentBot = null;
    state.ws.send(JSON.stringify({ type: "chat", actorId: state.actorId, message: text }));
  }

  function sendMessage(text) {
    addMsg(text, "me");
    enableInput(false);
    setWaiting(true);

    if (state.ws && state.ws.readyState === 1) {
      deliver(text);
      return;
    }
    // Reconnect lazily: fresh presigned URL → new socket → then send.
    setStatus("reconnecting…");
    refreshSession()
      .then(function (session) { return connectWs(session.wsUrl); })
      .then(function () {
        setStatus("connected as " + state.displayName, "ok");
        deliver(text);
      })
      .catch(function (err) {
        addMsg("⚠️ reconnect failed: " + err.message, "note");
        setStatus("disconnected", "err");
        enableInput(true);
      });
  }

  elForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = elInput.value.trim();
    if (!text) return;
    elInput.value = "";
    sendMessage(text);
  });

  // --- bootstrap ----------------------------------------------------------
  function boot() {
    if (!API || API.indexOf("REPLACE_") === 0) {
      setStatus("misconfigured", "err");
      addMsg("Config not injected (apiBase). Run the deploy script.", "note");
      return;
    }
    setStatus("authenticating…");
    ensureAuth()
      .then(createSession)
      .then(function (session) {
        setStatus("connected as " + state.displayName, "ok");
        return connectWs(session.wsUrl);
      })
      .then(function () { enableInput(true); addMsg("Connected. Say hello 👋", "note"); })
      .catch(function (err) {
        if (err.message === "NOT_IN_LARK") {
          setStatus("open in Lark", "err");
          addMsg("Please open this page inside the Lark desktop client to sign in.", "note");
        } else {
          setStatus("error", "err");
          addMsg("⚠️ " + err.message, "note");
        }
      });
  }

  boot();
})();
