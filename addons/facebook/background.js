// background.js
const WEBSOCKET_URL = "ws://localhost:8080";
let socket = null;
let reconnectTimer = null;
let pendingPosts = [];

function connectWebSocket() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }

  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  console.log("Starte Verbindung zum Node.js Server...");
  socket = new WebSocket(WEBSOCKET_URL);

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

function queueOrSendMessage(message) {
  console.log("📨 WebSocket-Nachricht erhalten:", message.type || 'remote_post', message.text ? message.text.slice(0, 80) : '(kein Text)');
  chrome.tabs.query({ url: ["*://*.facebook.com/*", "*://facebook.com/*"] }, (tabs) => {
    if (tabs.length > 0) {
      const targetTab = tabs[0];
      console.log("Sende Post an Tab ID:", targetTab.id, "URL:", targetTab.url);

      chrome.tabs.sendMessage(targetTab.id, message, (response) => {
        if (chrome.runtime.lastError) {
          console.log("Fehler beim Senden an Tab:", chrome.runtime.lastError.message);
          pendingPosts.push(message);
        } else {
          console.log("Erfolgreich an Content-Script gesendet.");
          if (pendingPosts.length > 0) {
            pendingPosts = pendingPosts.filter((item) => item !== message);
          }
        }
      });
    } else {
      console.log("Kein Facebook-Tab gefunden! Nachricht wird zwischengespeichert.");
      pendingPosts.push(message);
    }
  });
}

function flushPendingPosts() {
  if (!pendingPosts.length) return;

  chrome.tabs.query({ url: "*://*.facebook.com/*" }, (tabs) => {
    if (tabs.length > 0) {
      const targetTab = tabs[0];
      console.log(`🔄 Sende ${pendingPosts.length} ausstehende Nachricht(en) an Tab ID: ${targetTab.id}`);
      const queueCopy = [...pendingPosts];
      pendingPosts = [];
      queueCopy.forEach((message) => {
        chrome.tabs.sendMessage(targetTab.id, message, (response) => {
          if (chrome.runtime.lastError) {
            console.log("Fehler beim Senden ausstehender Nachricht:", chrome.runtime.lastError.message);
            pendingPosts.push(message);
          } else {
            console.log("Erfolgreich an Content-Script gesendet.");
          }
        });
      });
    }
  });
}

connectWebSocket();

setInterval(() => {
  if (!socket || socket.readyState === WebSocket.CLOSED) {
    connectWebSocket();
  }
  flushPendingPosts();
}, 10000);
