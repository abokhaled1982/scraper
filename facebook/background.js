const WEBSOCKET_URL = "ws://localhost:8080";
let socket = null;
let reconnectTimer = null;

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
  };

  socket.onmessage = (event) => {
    try {
        const data = JSON.parse(event.data);

        // --- HEARTBEAT CHECK ---
        // Wenn es nur ein Ping ist, nichts tun (aber die Verbindung bleibt dadurch wach)
        if (data.type === 'ping') {
            // Optional: console.log("❤️ Ping empfangen"); 
            return;
        }

        // Ab hier nur noch echte Posts verarbeiten
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
            console.log("Kein Facebook-Tab gefunden!");
          }
        });

    } catch (e) {
        console.error("Fehler beim Parsen der Nachricht:", e);
    }
  };

  socket.onclose = () => {
    console.log("❌ Verbindung getrennt. Versuche in 5 Sekunden erneut...");
    socket = null; // Wichtig: Socket nullen
    reconnectTimer = setTimeout(connectWebSocket, 5000);
  };

  socket.onerror = (error) => {
    console.error("❌ WebSocket Fehler:", error);
    socket.close();
  };
}

// Start
connectWebSocket();

// Keep-Alive Check (Client-seitig)
// Falls der Service Worker aufwacht und merkt, dass er keine Verbindung hat
setInterval(() => {
  if (!socket || socket.readyState === WebSocket.CLOSED) {
    connectWebSocket();
  }
}, 10000);