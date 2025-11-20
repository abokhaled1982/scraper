const WEBSOCKET_URL = "ws://localhost:8080";
let socket = null;
let reconnectTimer = null;

function connectWebSocket() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return; // Bereits verbunden oder verbindet sich
  }

  // Lösche vorherigen Timer, falls vorhanden
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
    const data = JSON.parse(event.data);

    // Die Logik zum Senden an den Content Script bleibt unverändert
    chrome.tabs.query({ url: "*://*.facebook.com/*" }, (tabs) => {
      if (tabs.length > 0) {
        const targetTab = tabs[0];
        console.log("Sende an Tab ID:", targetTab.id);

        chrome.tabs.sendMessage(
          targetTab.id,
          {
            command: "remote_post",
            text: data.text,
            image: data.image,
          },
          (response) => {
            if (chrome.runtime.lastError) {
              console.log("Fehler beim Senden: ", chrome.runtime.lastError.message);
              // Im Fehlerfall muss die Verbindung nicht unbedingt wiederhergestellt werden,
              // da der Service Worker noch aktiv ist.
            } else {
              console.log("Erfolgreich an Content-Script gesendet.");
            }
          }
        );
      } else {
        console.log("Kein Facebook-Tab gefunden! Bitte öffne Facebook.");
      }
    });
  };

  socket.onclose = () => {
    console.log("❌ Verbindung getrennt. Versuche in 5 Sekunden erneut...");
    // Starte den Wiederverbindungsprozess
    reconnectTimer = setTimeout(connectWebSocket, 5000);
  };

  socket.onerror = (error) => {
    console.error("❌ WebSocket Fehler:", error);
    // Schließt normalerweise automatisch, aber um sicherzugehen:
    socket.close();
  };
}

// Beim Start des Service Workers (dieser Code wird ausgeführt)
connectWebSocket();

// Optional: Hinzufügen eines Keep-Alive Mechanismus
// Dies sendet alle 20 Sekunden ein "Ping" an sich selbst, um den Service Worker aktiv zu halten.
// Alternativ kann ein Server-seitiger Ping verwendet werden, aber dieser ist einfacher.
setInterval(() => {
  if (socket && socket.readyState === WebSocket.CLOSED) {
    // Verbindet sich sofort wieder, falls der Service Worker beendet wurde
    // und wieder gestartet wird (aber der Timer war noch aktiv).
    connectWebSocket();
  }
}, 20000);
