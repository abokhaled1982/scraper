// background.js — Professional WebSocket + Handshake + Status Broadcasting
const WEBSOCKET_URL      = "ws://localhost:8080";
const TARGET_URL         = "https://www.facebook.com/profile.php?id=61584368422265";
const RECONNECT_DELAY_MS = 5000;
const HANDSHAKE_TIMEOUT  = 10000; // ms to wait for handshake ack

let socket          = null;
let reconnectTimer  = null;
let pendingPosts    = [];
let isHandshakeDone = false;
let addonState      = "disconnected"; // disconnected | connecting | handshake | ready | busy | error
let currentActivity = "";

// ── Logger ──────────────────────────────────────────────────────────────────
const MAX_LOG = 200;
let logBuffer = [];

function log(level, msg) {
  const entry = { ts: new Date().toISOString(), level, msg };
  logBuffer.push(entry);
  if (logBuffer.length > MAX_LOG) logBuffer.shift();
  const icon = level === "error" ? "❌" : level === "warn" ? "⚠️" : level === "ok" ? "✅" : "ℹ️";
  console[level === "error" ? "error" : level === "warn" ? "warn" : "log"](`[BG] ${icon} ${msg}`);
  broadcastStatus();
}

// ── State Machine ─────────────────────────────────────────────────────────
function setState(state, activity = "") {
  addonState      = state;
  currentActivity = activity;
  broadcastStatus();
}

function broadcastStatus() {
  const status = {
    type:     "status_update",
    state:    addonState,
    activity: currentActivity,
    wsUrl:    WEBSOCKET_URL,
    logs:     logBuffer.slice(-50),
    pending:  pendingPosts.length,
  };
  chrome.runtime.sendMessage(status).catch(() => {}); // popup may be closed
}

// ── URL Navigation ────────────────────────────────────────────────────────
function ensureOnTargetUrl() {
  return new Promise((resolve) => {
    chrome.tabs.query({ url: ["*://*.facebook.com/*"] }, (tabs) => {
      const target = tabs.find(t => t.url && t.url.startsWith("https://www.facebook.com/profile.php?id=61584368422265"));
      if (target) {
        log("ok", `Ziel-URL bereits geöffnet (Tab ${target.id})`);
        target._wasAlreadyOnTarget = true;
        return resolve(target);
      }
      // Not on target URL — navigate existing FB tab or open new one
      if (tabs.length > 0) {
        log("info", `Navigiere zu Deal-Boss Profil...`);
        chrome.tabs.update(tabs[0].id, { url: TARGET_URL, active: true }, (tab) => {
          // Wait for page load
          const listener = (tabId, info) => {
            if (tabId === tabs[0].id && info.status === "complete") {
              chrome.tabs.onUpdated.removeListener(listener);
              log("ok", "Ziel-URL geladen.");
              chrome.tabs.get(tabs[0].id, resolve);
            }
          };
          chrome.tabs.onUpdated.addListener(listener);
        });
      } else {
        log("info", "Öffne neuen Tab mit Deal-Boss Profil...");
        chrome.tabs.create({ url: TARGET_URL }, (tab) => {
          const listener = (tabId, info) => {
            if (tabId === tab.id && info.status === "complete") {
              chrome.tabs.onUpdated.removeListener(listener);
              log("ok", "Ziel-URL in neuem Tab geladen.");
              resolve(tab);
            }
          };
          chrome.tabs.onUpdated.addListener(listener);
        });
      }
    });
  });
}

// ── WebSocket ─────────────────────────────────────────────────────────────
function connectWebSocket() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

  setState("connecting", "Verbinde mit Server...");
  log("info", `Verbinde mit ${WEBSOCKET_URL}...`);
  socket = new WebSocket(WEBSOCKET_URL);

  socket.onopen = () => {
    setState("handshake", "Handshake...");
    log("info", "Verbunden. Sende Handshake...");

    // Send handshake
    socket.send(JSON.stringify({ type: "handshake", version: "3.2", client: "facebook-addon" }));

    // Timeout if no ack
    const hsTimer = setTimeout(() => {
      if (!isHandshakeDone) {
        log("warn", "Handshake Timeout – Server antwortet nicht.");
        setState("error", "Handshake Timeout");
        socket.close();
      }
    }, HANDSHAKE_TIMEOUT);

    // Store timer so we can clear it
    socket._hsTimer = hsTimer;
  };

  socket.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);

      if (data.type === "ping") {
        socket.send(JSON.stringify({ type: "pong" }));
        return;
      }

      if (data.type === "handshake_ack") {
        isHandshakeDone = true;
        if (socket._hsTimer) clearTimeout(socket._hsTimer);
        setState("ready", "Bereit");
        log("ok", `Handshake bestätigt. Server: ${data.server || "fb-service"}`);
        flushPendingPosts();
        return;
      }

      if (data.type === "remote_post" || data.command === "remote_post" || data.text || data.video || data.image) {
        if (!isHandshakeDone) {
          log("warn", "Nachricht vor Handshake empfangen – wird gepuffert.");
          pendingPosts.push({ command: "remote_post", text: data.text, image: data.image, video: data.video, comment: data.comment });
          return;
        }
        const message = { command: "remote_post", text: data.text, image: data.image, video: data.video, comment: data.comment };
        log("info", `Auftrag empfangen: ${data.video ? "🎥 Reel" : "📝 Post"} – ${(data.text || "").slice(0, 60)}`);
        setState("busy", data.video ? "Reel wird hochgeladen..." : "Post wird erstellt...");
        queueOrSendMessage(message);
        return;
      }
    } catch (e) {
      log("error", `Nachricht konnte nicht geparst werden: ${e.message}`);
    }
  };

  socket.onclose = (ev) => {
    isHandshakeDone = false;
    setState("disconnected", "Verbindung getrennt – reconnect...");
    log("warn", `Verbindung getrennt (Code ${ev.code}). Reconnect in ${RECONNECT_DELAY_MS / 1000}s...`);
    socket = null;
    reconnectTimer = setTimeout(connectWebSocket, RECONNECT_DELAY_MS);
  };

  socket.onerror = () => {
    log("error", "WebSocket Fehler aufgetreten.");
    setState("error", "Verbindungsfehler");
    socket.close();
  };
}

function queueOrSendMessage(message) {
  // Always navigate to the target profile page before sending
  setState("busy", message.video ? "Navigiere zu Profil (Reel)..." : "Navigiere zu Profil (Post)...");
  ensureOnTargetUrl().then((tab) => {
    // After navigation give page a moment; after SW restart content.js is gone → re-inject
    const delay = tab._wasAlreadyOnTarget ? 0 : 2000;
    setTimeout(() => sendToTab(tab.id, message), delay);
  }).catch((err) => {
    log("error", `Navigation fehlgeschlagen: ${err}`);
    pendingPosts.push(message);
    setState("error", "Navigation zu Profil fehlgeschlagen");
  });
}

function sendToTab(tabId, message, isRetry = false) {
  log("info", `Sende an Tab ${tabId}: ${message.video ? "🎥 Reel" : "📝 Post"}${isRetry ? " (Retry nach Injektion)" : ""}`);
  setState("busy", message.video ? "Reel wird hochgeladen..." : "Post wird erstellt...");
  chrome.tabs.sendMessage(tabId, message, (response) => {
    if (chrome.runtime.lastError) {
      const errMsg = chrome.runtime.lastError.message;
      if (!isRetry && errMsg.includes("Receiving end does not exist")) {
        // Content script lost after SW restart — re-inject and retry once
        log("warn", "Content Script nicht erreichbar – injiziere neu...");
        setState("busy", "Content Script wird neu geladen...");
        chrome.scripting.executeScript(
          { target: { tabId }, files: ["content.js"] },
          () => {
            if (chrome.runtime.lastError) {
              log("error", `Injektion fehlgeschlagen: ${chrome.runtime.lastError.message}`);
              pendingPosts.push(message);
              setState("error", "Content Script Injektion fehlgeschlagen");
              return;
            }
            log("ok", "Content Script neu injiziert – sende erneut...");
            // Brief pause for the injected script to register its listener
            setTimeout(() => sendToTab(tabId, message, true), 800);
          }
        );
      } else {
        log("error", `Fehler beim Senden an Tab: ${errMsg}`);
        pendingPosts.push(message);
        setState("error", "Content Script nicht erreichbar");
      }
    } else {
      log("ok", "Auftrag an Content Script übermittelt.");
      // State will be updated via content_status events from content script
    }
  });
}

function flushPendingPosts() {
  if (!pendingPosts.length) return;
  log("info", `Sende ${pendingPosts.length} gepufferte Nachricht(en)...`);
  const queue = [...pendingPosts];
  pendingPosts = [];
  queue.forEach(msg => queueOrSendMessage(msg));
}

// ── Content Script Events ─────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "content_status") {
    setState(msg.state || addonState, msg.activity || "");
    if (msg.log) log(msg.logLevel || "info", `[content] ${msg.log}`);
    // Forward real result to Python via WebSocket (only when content script reports a result)
    if (msg.result) {
      const success = msg.result.success === true;
      const error   = msg.result.error || null;
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "task_result", success, error }));
      }
      // Notify popup for last-result display
      chrome.runtime.sendMessage({ type: "task_done", success, error }).catch(() => {});
    }
    return;
  }

  if (msg.type === "get_status") {
    sendResponse({
      type:     "status_update",
      state:    addonState,
      activity: currentActivity,
      wsUrl:    WEBSOCKET_URL,
      logs:     logBuffer.slice(-50),
      pending:  pendingPosts.length,
    });
    return true;
  }

  if (msg.type === "clear_log") {
    logBuffer = [];
    sendResponse({ ok: true });
    return true;
  }
});

// ── MV3 Service Worker Keepalive ─────────────────────────────────────────
// Chrome killt MV3 Service Worker nach ~30s Inaktivität.
// chrome.alarms hält ihn am Leben.
chrome.alarms.create("keepalive", { periodInMinutes: 0.4 }); // alle 24s
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "keepalive") {
    // Re-connect falls WebSocket tot ist
    if (!socket || socket.readyState === WebSocket.CLOSED || socket.readyState === WebSocket.CLOSING) {
      log("warn", "Keepalive: WebSocket tot – reconnecte...");
      connectWebSocket();
    }
  }
});

// ── Init ──────────────────────────────────────────────────────────────────
connectWebSocket();

setInterval(() => {
  if (!socket || socket.readyState === WebSocket.CLOSED) connectWebSocket();
  if (isHandshakeDone) flushPendingPosts();
}, 10000);

// NOTE: duplicate legacy function declarations below have been removed.
// The proper connectWebSocket / queueOrSendMessage / flushPendingPosts
// definitions above (with handshake + state machine) are the active ones.
/* ── LEGACY DUPLICATE START (kept as comment for reference only) ──
function connectWebSocket() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }

  socket.onopen = () => {
    console.log("✅ Verbunden mit Node.js Server");
    flushPendingPosts();
  };

  socket.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);

      // --- HEARTBEAT CHECK ---
      if (data.type === "ping") {
        return;
      }

      const message = {
        command: "remote_post",
        text:    data.text,
        image:   data.image,
        video:   data.video,
        comment: data.comment,
      };

      queueOrSendMessage(message);
    } catch (e) {
      console.error("Fehler beim Parsen der Nachricht:", e);
    }
  };

  socket.onclose = () => {
    console.log("❌ Verbindung getrennt. Versuche in 5 Sekunden erneut...");
    socket = null;
    reconnectTimer = setTimeout(connectWebSocket, 5000);
  };

  socket.onerror = (error) => {
    console.error("❌ WebSocket Fehler:", error);
    socket.close();
  };
}
── LEGACY DUPLICATE END ── */
