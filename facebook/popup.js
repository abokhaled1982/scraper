document.getElementById("triggerBtn").addEventListener("click", () => {
  
  // 1. Wir suchen den aktiven Tab im Browser
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    
    // 2. Wir senden eine Nachricht an den Tab ("content.js")
    chrome.tabs.sendMessage(tabs[0].id, { command: "click_it" }, (response) => {
        // Optional: Antwort vom Content Script loggen
        console.log("Nachricht gesendet");
    });

  });
});