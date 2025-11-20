chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.command === "remote_post") {
    const nachricht = request.text;
    const bildDaten = request.image;

    console.log("Empfange Befehl. Bild vorhanden?", !!bildDaten);

    // Trigger-Button suchen (Was machst du gerade?)
    const buttons = document.querySelectorAll('div[role="button"]');
    let gefunden = false;

    for (let i = 0; i < buttons.length; i++) {
      if (buttons[i].innerText.includes("Was machst du gerade")) {
        buttons[i].click();
        gefunden = true;

        // Warte auf das Popup
        waitForElement('div[role="dialog"] div[role="textbox"]').then((textBox) => {
          verarbeiteInhalt(textBox, nachricht, bildDaten);
        });
        break;
      }
    }
    if (!gefunden) console.log("Start-Button nicht gefunden.");
  }
});

// Helper: Wartet auf ein Element
function waitForElement(selector) {
  return new Promise((resolve) => {
    if (document.querySelector(selector)) {
      return resolve(document.querySelector(selector));
    }
    const observer = new MutationObserver(() => {
      if (document.querySelector(selector)) {
        observer.disconnect();
        resolve(document.querySelector(selector));
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
  });
}

function verarbeiteInhalt(textBox, text, base64Image) {
  console.log("Popup-Editor gefunden!");
  textBox.focus();

  // --- SCHRITT A: Bild einfügen ---
  if (base64Image) {
    console.log("Füge Bild ein...");
    const byteCharacters = atob(base64Image);
    const byteNumbers = new Array(byteCharacters.length);
    for (let i = 0; i < byteCharacters.length; i++) {
      byteNumbers[i] = byteCharacters.charCodeAt(i);
    }
    const byteArray = new Uint8Array(byteNumbers);
    const blob = new Blob([byteArray], { type: "image/png" });
    const file = new File([blob], "upload.png", { type: "image/png" });

    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);

    const pasteEvent = new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: dataTransfer,
    });
    textBox.dispatchEvent(pasteEvent);
  }

  // --- SCHRITT B: Text einfügen ---
  // Warte kurz auf Bildverarbeitung
  setTimeout(() => {
    if (text) {
      console.log("Schreibe Text:", text);
      textBox.focus();
      document.execCommand("insertText", false, text);
    }

    // --- SCHRITT C: Klick-Zyklus starten ---
    console.log("Starte Suche nach Weiter/Posten Buttons...");
    setTimeout(checkAndClickPostButton, 2000);
  }, 1000);
}

function checkAndClickPostButton() {
  // Wir suchen nach beiden Buttons innerhalb des Dialogs
  const dialogSelector = 'div[role="dialog"]';
  const weiterBtnSelector = `${dialogSelector} div[aria-label="Weiter"]`;
  const postenBtnSelector = `${dialogSelector} div[aria-label="Posten"]`;

  const weiterBtn = document.querySelector(weiterBtnSelector);
  const postenBtn = document.querySelector(postenBtnSelector);

  // Priorisierung: Wenn "Posten" da ist, nehmen wir den. Sonst "Weiter".
  // (Da Facebook manchmal beide im DOM behält, aber einen versteckt, prüfen wir die Sichtbarkeit idealerweise,
  // aber meistens existiert nur einer aktiv im DOM-Baum an klickbarer Stelle).
  let targetBtn = postenBtn || weiterBtn;

  if (targetBtn) {
    // Prüfen ob Button disabled ist (grau hinterlegt, z.B. wenn Upload noch läuft)
    const isDisabled = targetBtn.getAttribute("aria-disabled") === "true";

    if (isDisabled) {
      console.log("Button gefunden, ist aber noch inaktiv (disabled). Warte...");
      setTimeout(checkAndClickPostButton, 1000);
      return;
    }

    const buttonType = targetBtn.getAttribute("aria-label");
    console.log(`Klicke Button: "${buttonType}"`);

    // React benötigt oft den vollen Event-Zyklus
    const mouseUp = new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window });
    const mouseDown = new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window });
    targetBtn.dispatchEvent(mouseDown);
    targetBtn.dispatchEvent(mouseUp);
    targetBtn.click();

    // LOGIK ERWEITERUNG:
    // Wenn wir "Weiter" geklickt haben, müssen wir weitersuchen, da danach "Posten" kommt.
    if (buttonType === "Weiter") {
      console.log("Weiter geklickt. Warte auf UI-Wechsel zu 'Posten'...");
      // Wir geben der UI 2 Sekunden Zeit für die Animation zum nächsten Screen
      setTimeout(checkAndClickPostButton, 2000);
    } else if (buttonType === "Posten") {
      console.log("Posten erfolgreich geklickt! Skript beendet.");
      // Hier endet der Loop.
    }
  } else {
    console.log("Kein Weiter- oder Posten-Button gefunden. Suche erneut...");
    setTimeout(checkAndClickPostButton, 1000);
  }
}
