// background.js
const WEBSOCKET_URL = "ws://localhost:8080";
let socket = null;
let reconnectTimer = null;
let keepAliveInterval = null;

// --- 1. VERBINDUNGSAUFBAU ---
function connectWebSocket() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }

  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  console.log("🔄 Starte Verbindung zum Node.js Server...");
  socket = new WebSocket(WEBSOCKET_URL);

  socket.onopen = () => {
    console.log("✅ Verbunden mit Node.js Server");
    // Wenn verbunden, Keep-Alive starten
    startKeepAlive();
  };

  socket.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);

      // Heartbeat ignorieren (aber loggen, dass wir leben)
      if (data.type === "ping") {
        return;
      }

      // Facebook Tab suchen und Nachricht senden
      chrome.tabs.query({ url: "*://*.facebook.com/*" }, (tabs) => {
        if (tabs.length > 0) {
          const targetTab = tabs[0];
          console.log("Sende Post an Tab ID:", targetTab.id);

          chrome.tabs.sendMessage(
            targetTab.id,
            {
              command: "remote_post",
              text: data.text,
              image: data.image,
              comment: data.comment,
            },
            (response) => {
              if (chrome.runtime.lastError) {
                console.log("Fehler beim Senden an Tab: ", chrome.runtime.lastError.message);
              } else {
                console.log("Erfolgreich an Content-Script gesendet.");
              }
            }
          );
        } else {
          console.log("⚠️ Kein Facebook-Tab gefunden! Bitte Facebook öffnen.");
        }
      });
    } catch (e) {
      console.error("Fehler beim Parsen der Nachricht:", e);
    }
  };

  socket.onclose = () => {
    console.log("❌ Verbindung getrennt. Versuche in 5 Sekunden erneut...");
    socket = null;
    stopKeepAlive(); // Keep-Alive stoppen, da Socket tot
    reconnectTimer = setTimeout(connectWebSocket, 5000);
  };

  socket.onerror = (error) => {
    console.error("❌ WebSocket Fehler:", error);
    socket.close();
  };
}

// --- 2. START-EVENTS (Wichtig für Autostart) ---

// Wird gefeuert, wenn Chrome startet
chrome.runtime.onStartup.addListener(() => {
  console.log("🚀 Chrome gestartet - Initialisiere WebSocket...");
  connectWebSocket();
});

// Wird gefeuert, wenn das Addon installiert/aktualisiert wird
chrome.runtime.onInstalled.addListener(() => {
  console.log("📦 Addon installiert/geladen - Initialisiere WebSocket...");
  connectWebSocket();
});

// --- 3. KEEP-ALIVE MECHANISMUS (Gegen Service Worker Schlafmodus) ---

function startKeepAlive() {
  if (keepAliveInterval) clearInterval(keepAliveInterval);
  
  // Alle 20 Sekunden einen "Dummy"-Aufruf machen, damit Chrome den Worker nicht tötet
  keepAliveInterval = setInterval(() => {
    
    // 1. WebSocket prüfen und ggf. neu verbinden
    if (!socket || socket.readyState === WebSocket.CLOSED) {
      console.log("💓 Keep-Alive: Socket war tot, verbinde neu...");
      connectWebSocket();
    } else {
      // 2. Chrome API aufrufen (Reset des Idle-Timers)
      chrome.runtime.getPlatformInfo((info) => {
         // Dieser Aufruf ist sinnlos, aber er zwingt Chrome, den Service Worker wach zu halten
      });
    }
    
  }, 20000); // 20 Sekunden (Chrome tötet nach ca. 30s Inaktivität)
}

function stopKeepAlive() {
  if (keepAliveInterval) {
    clearInterval(keepAliveInterval);
    keepAliveInterval = null;
  }
}

// Initialer Aufruf (falls das Skript direkt geladen wird)
connectWebSocket();