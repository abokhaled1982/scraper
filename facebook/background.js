let socket = new WebSocket("ws://localhost:8080");

socket.onopen = () => {
  console.log("Verbunden mit Node.js Server");
};

socket.onmessage = (event) => {
  const data = JSON.parse(event.data);
  const textToPost = data.text;

  console.log("Nachricht vom Server:", textToPost);

  // Suchen nach ALLEN Tabs, die Facebook geöffnet haben
  // (Egal ob das Fenster aktiv ist oder nicht)
  chrome.tabs.query({ url: "*://*.facebook.com/*" }, (tabs) => {
    if (tabs.length > 0) {
      // Wir nehmen einfach den ersten Facebook-Tab, den wir finden
      const targetTab = tabs[0];

      console.log("Sende an Tab ID:", targetTab.id);

      chrome.tabs.sendMessage(
        targetTab.id,        {
          command: "remote_post",
          text: data.text,
          image: data.image, // <--- WICHTIG: Das Bild weiterleiten
        },
        (response) => {
          // Prüfen, ob das Content-Script geantwortet hat (optional)
          if (chrome.runtime.lastError) {
            console.log(
              "Fehler beim Senden: ",
              chrome.runtime.lastError.message
            );
            console.log(
              "Tipp: Hast du die Facebook-Seite nach dem Update der Extension neu geladen?"
            );
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
  console.log("Verbindung getrennt.");
};
