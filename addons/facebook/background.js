// background.js — WS-driven state machine v4.1
// Zustand wird vom WebSocket-Backend gesteuert.
// Während "busy": Job-Tab bleibt immer im Fokus.

const WEBSOCKET_URL      = "ws://localhost:8080";
const TARGET_URL         = "https://www.facebook.com/profile.php?id=61584368422265";
const RECONNECT_DELAY_MS = 5000;
const HANDSHAKE_TIMEOUT  = 10000;

let socket          = null;
let reconnectTimer  = null;
let pendingPosts    = [];
let isHandshakeDone = false;
let addonState      = "disconnected"; // disconnected|connecting|handshake|ready|busy|error
let currentActivity = "";
let activeJobTabId  = null; // Tab-ID des laufenden Jobs – nur dieser darf busy→ready schalten

// ── Logger ────────────────────────────────────────────────────────────────────
const MAX_LOG = 200;
let logBuffer = [];

function log(level, msg) {
  logBuffer.push({ ts: new Date().toISOString(), level, msg });
  if (logBuffer.length > MAX_LOG) logBuffer.shift();
  console[level === "error" ? "error" : level === "warn" ? "warn" : "log"](`[BG] ${msg}`);
  broadcastStatus();
}

// ── State ────────────────────────────────────────────────────────────────────
function setState(state, activity = "") {
  addonState      = state;
  currentActivity = activity;
  chrome.storage.session.set({ addonState, currentActivity, activeJobTabId }).catch(() => {});
  broadcastStatus();
}

function broadcastStatus() {
  chrome.runtime.sendMessage({
    type:     "status_update",
    state:    addonState,
    activity: currentActivity,
    wsUrl:    WEBSOCKET_URL,
    logs:     logBuffer.slice(-50),
    pending:  pendingPosts.length,
  }).catch(() => {});
}

// ── WS-Helper ────────────────────────────────────────────────────────────────
function wsSend(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));
  }
}

// ── Tab-Focus-Guard: Job-Tab bleibt immer aktiv während busy ─────────────────
chrome.tabs.onActivated.addListener((activeInfo) => {
  if (addonState !== "busy" || activeJobTabId === null) return;
  if (activeInfo.tabId === activeJobTabId) return;
  chrome.tabs.update(activeJobTabId, { active: true }, () => {
    if (chrome.runtime.lastError) {
      log("warn", `Job-Tab ${activeJobTabId} nicht mehr erreichbar.`);
    } else {
      log("info", "Tab-Wechsel verhindert: Job-Tab wieder fokussiert.");
    }
  });
});

// Job-Tab geschlossen während busy → Fehler
chrome.tabs.onRemoved.addListener((tabId) => {
  if (addonState === "busy" && tabId === activeJobTabId) {
    log("error", "Job-Tab wurde während des Postens geschlossen!");
    activeJobTabId = null;
    setState("error", "Job-Tab geschlossen während Posting");
  }
});

// ── Navigation ────────────────────────────────────────────────────────────────
function ensureOnTargetUrl() {
  return new Promise((resolve) => {
    chrome.tabs.query({ url: ["*://*.facebook.com/*"] }, (tabs) => {
      const target = tabs.find(t => t.url && t.url.startsWith(TARGET_URL));
      if (target) {
        target._wasAlreadyOnTarget = true;
        log("ok", `Profil-Tab bereits offen (Tab ${target.id})`);
        return resolve(target);
      }
      if (tabs.length > 0) {
        log("info", "Navigiere bestehenden FB-Tab zu Profil...");
        chrome.tabs.update(tabs[0].id, { url: TARGET_URL, active: true }, () => {
          const listener = (tabId, info) => {
            if (tabId === tabs[0].id && info.status === "complete") {
              chrome.tabs.onUpdated.removeListener(listener);
              log("ok", "Profil-URL geladen.");
              chrome.tabs.get(tabs[0].id, resolve);
            }
          };
          chrome.tabs.onUpdated.addListener(listener);
        });
      } else {
        log("info", "Öffne neuen Tab mit Profil...");
        chrome.tabs.create({ url: TARGET_URL }, (tab) => {
          const listener = (tabId, info) => {
            if (tabId === tab.id && info.status === "complete") {
              chrome.tabs.onUpdated.removeListener(listener);
              log("ok", "Profil-URL in neuem Tab geladen.");
              resolve(tab);
            }
          };
          chrome.tabs.onUpdated.addListener(listener);
        });
      }
    });
  });
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connectWebSocket() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

  setState("connecting", "Verbinde mit Server...");
  log("info", `Verbinde mit ${WEBSOCKET_URL}...`);
  socket = new WebSocket(WEBSOCKET_URL);

  socket.onopen = () => {
    setState("handshake", "Handshake...");
    log("info", "Verbunden – sende Handshake...");
    socket.send(JSON.stringify({ type: "handshake", version: "4.0", client: "facebook-addon" }));
    socket._hsTimer = setTimeout(() => {
      if (!isHandshakeDone) {
        log("warn", "Handshake Timeout.");
        setState("error", "Handshake Timeout");
        socket.close();
      }
    }, HANDSHAKE_TIMEOUT);
  };

  socket.onmessage = (event) => {
    let data;
    try { data = JSON.parse(event.data); }
    catch (e) { log("error", `JSON-Fehler: ${e.message}`); return; }

    if (data.type === "ping") {
      wsSend({ type: "pong" });
      return;
    }

    // Backend sagt: Verbindung bestätigt → "ready"
    if (data.type === "handshake_ack") {
      isHandshakeDone = true;
      if (socket._hsTimer) clearTimeout(socket._hsTimer);
      log("ok", `Handshake OK. Server: ${data.server || "fb-service"}`);
      if (addonState !== "busy") setState("ready", "Bereit");
      flushPendingPosts();
      return;
    }

    // Auftrag vom Backend (remote_post, reel, post)
    if (data.type === "remote_post" || data.type === "reel" || data.type === "post") {
      const message = { command: "remote_post", text: data.text, image: data.image, video: data.video, comment: data.comment };
      const jobLabel = data.video ? "🎥 Reel" : "📝 Post";

      if (!isHandshakeDone) {
        log("warn", "Auftrag vor Handshake – wird gepuffert.");
        pendingPosts.push(message);
        return;
      }

      if (addonState === "busy") {
        log("warn", `Addon ist busy – Auftrag gepuffert (${pendingPosts.length + 1} in Queue).`);
        pendingPosts.push(message);
        wsSend({ type: "busy", reason: "Addon arbeitet gerade", queued: pendingPosts.length });
        return;
      }

      log("info", `Auftrag: ${jobLabel} – ${(data.text || "").slice(0, 60)}`);
      setState("busy", data.video ? "Navigiere zu Profil (Reel)..." : "Navigiere zu Profil (Post)...");
      wsSend({ type: "addon_status", state: "busy", activity: `Starte ${jobLabel}` });
      dispatchJob(message);
      return;
    }
  };

  socket.onclose = (ev) => {
    isHandshakeDone = false;
    if (addonState !== "busy") setState("disconnected", "Verbindung getrennt – reconnect...");
    log("warn", `WS getrennt (Code ${ev.code}). Reconnect in ${RECONNECT_DELAY_MS / 1000}s...`);
    socket = null;
    reconnectTimer = setTimeout(connectWebSocket, RECONNECT_DELAY_MS);
  };

  socket.onerror = () => {
    log("error", "WebSocket Fehler.");
    setState("error", "Verbindungsfehler");
    socket.close();
  };
}

// ── Job dispatchen ────────────────────────────────────────────────────────────
function dispatchJob(message) {
  ensureOnTargetUrl().then((tab) => {
    const delay = tab._wasAlreadyOnTarget ? 0 : 2000;
    setTimeout(() => sendToTab(tab.id, message), delay);
  }).catch((err) => {
    log("error", `Navigation fehlgeschlagen: ${err}`);
    pendingPosts.push(message);
    setState("error", "Navigation zu Profil fehlgeschlagen");
  });
}

function sendToTab(tabId, message, isRetry = false) {
  log("info", `→ Tab ${tabId}: ${message.video ? "🎥 Reel" : "📝 Post"}${isRetry ? " (Retry)" : ""}`);
  setState("busy", message.video ? "Reel wird hochgeladen..." : "Post wird erstellt...");
  activeJobTabId = tabId;
  chrome.storage.session.set({ activeJobTabId }).catch(() => {});

  // Tab in den Vordergrund holen
  chrome.tabs.update(tabId, { active: true }, () => {});

  chrome.tabs.sendMessage(tabId, message, (response) => {
    if (chrome.runtime.lastError) {
      const errMsg = chrome.runtime.lastError.message;
      if (!isRetry && errMsg.includes("Receiving end does not exist")) {
        log("warn", "Content Script weg – injiziere neu...");
        setState("busy", "Content Script wird neu geladen...");
        chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] }, () => {
          if (chrome.runtime.lastError) {
            log("error", `Injektion fehlgeschlagen: ${chrome.runtime.lastError.message}`);
            pendingPosts.push(message);
            activeJobTabId = null;
            setState("error", "Content Script Injektion fehlgeschlagen");
            return;
          }
          log("ok", "Content Script injiziert – sende erneut...");
          setTimeout(() => sendToTab(tabId, message, true), 800);
        });
      } else {
        log("error", `Senden fehlgeschlagen: ${errMsg}`);
        pendingPosts.push(message);
        activeJobTabId = null;
        setState("error", "Content Script nicht erreichbar");
      }
    } else {
      log("ok", "Auftrag übermittelt – warte auf Fertigmeldung.");
    }
  });
}

function flushPendingPosts() {
  if (!pendingPosts.length || addonState === "busy") return;
  const next = pendingPosts.shift();
  const jobLabel = next.video ? "🎥 Reel" : "📝 Post";
  log("info", `Nächster Auftrag aus Queue: ${jobLabel} (${pendingPosts.length} verbleibend)`);
  setState("busy", next.video ? "Navigiere zu Profil (Reel)..." : "Navigiere zu Profil (Post)...");
  wsSend({ type: "addon_status", state: "busy", activity: `Starte ${jobLabel} (aus Queue)` });
  dispatchJob(next);
}

// ── Nachrichten von Content Script & Popup ────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {

  if (msg.type === "content_status") {
    const fromTabId = sender && sender.tab ? sender.tab.id : null;

    // Während busy: nur Nachrichten vom Job-Tab akzeptieren
    if (addonState === "busy" && activeJobTabId !== null && fromTabId !== activeJobTabId) {
      return; // anderer Tab – ignorieren
    }

    // Log immer schreiben
    if (msg.log) log(msg.logLevel || "info", `[CS] ${msg.log}`);

    // Aktivitätstext immer updaten (Fortschritt im Popup + Server)
    if (msg.activity) {
      currentActivity = msg.activity;
      broadcastStatus();
      wsSend({ type: "addon_status", state: addonState, activity: msg.activity });
    }

    // Fehler vom Job-Tab
    if (msg.state === "error") {
      activeJobTabId = null;
      chrome.storage.session.set({ activeJobTabId: null }).catch(() => {});
      setState("error", msg.activity || "Fehler beim Posten");
      if (msg.result) {
        wsSend({ type: "task_result", success: false, error: msg.result.error || "Unbekannt" });
        chrome.runtime.sendMessage({ type: "task_done", success: false, error: msg.result.error }).catch(() => {});
      }
      // Auto-Recovery: nach 30s Fehler → bereit (wenn WS verbunden)
      setTimeout(() => {
        if (addonState === "error" && isHandshakeDone) {
          log("info", "Auto-Recovery: Fehler → Bereit");
          setState("ready", "Auto-Recovery nach Fehler");
          if (pendingPosts.length > 0) setTimeout(flushPendingPosts, 3000);
        }
      }, 30000);
      return;
    }

    // Job erfolgreich abgeschlossen (result vorhanden)
    if (msg.result && msg.result.success === true) {
      // Task-Result an Python-Backend → Python entsperrt die nächste Datei
      wsSend({ type: "task_result", success: true, error: null });
      log("ok", "task_result: success=true → gesendet an Backend");
      chrome.runtime.sendMessage({ type: "task_done", success: true, error: null }).catch(() => {});

      // Job beendet
      activeJobTabId = null;
      chrome.storage.session.set({ activeJobTabId: null }).catch(() => {});
      setState("ready", "Bereit");

      // Nächsten gepufferten Auftrag nach kurzer Pause verarbeiten
      if (pendingPosts.length > 0) {
        log("info", `Queue hat noch ${pendingPosts.length} Auftrag/Aufträge – starte nächsten in 5s...`);
        setTimeout(flushPendingPosts, 5000);
      }
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

// ── Keepalive ─────────────────────────────────────────────────────────────────
chrome.alarms.create("keepalive", { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== "keepalive") return;
  if (!socket || socket.readyState === WebSocket.CLOSED || socket.readyState === WebSocket.CLOSING) {
    log("warn", "Keepalive: WS tot – reconnecte...");
    connectWebSocket();
  }
  // Während busy: Job-Tab im Fokus halten
  if (addonState === "busy" && activeJobTabId !== null) {
    chrome.tabs.update(activeJobTabId, { active: true }, () => {
      if (chrome.runtime.lastError) {
        log("warn", `Keepalive: Job-Tab ${activeJobTabId} weg.`);
      }
    });
  }
});

// ── Init: Zustand nach SW-Neustart wiederherstellen ──────────────────────────
chrome.storage.session.get(["addonState", "currentActivity", "activeJobTabId"], (saved) => {
  if (saved.addonState && saved.addonState !== "disconnected") {
    addonState      = saved.addonState;
    currentActivity = saved.currentActivity || "";
    activeJobTabId  = saved.activeJobTabId  || null;
    log("info", `SW-Neustart: Status wiederhergestellt → ${addonState} (Job-Tab: ${activeJobTabId})`);
  }
  connectWebSocket();
});

setInterval(() => {
  if (!socket || socket.readyState === WebSocket.CLOSED) connectWebSocket();
  if (isHandshakeDone && pendingPosts.length > 0 && addonState === "ready") flushPendingPosts();
}, 10000);
