// content.js - "Humanized" Version mit WhatsApp/Skip Handling

// --- HELPER FUNKTION ---
const randomSleep = (min = 5000, max = 10000) => {
  const delay = Math.floor(Math.random() * (max - min + 1)) + min;
  console.log(`🎲 Menschliche Pause: ${(delay / 1000).toFixed(2)} Sekunden...`);
  return new Promise((resolve) => setTimeout(resolve, delay));
};

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.command === "remote_post") {
    console.log("🤖 Empfange Befehl...");
    startPostingProcess(request.text, request.image);
  }
});

async function startPostingProcess(text, base64Image) {
  // 1. Trigger-Button suchen ("Was machst du gerade?")
  const buttons = document.querySelectorAll('div[role="button"]');
  let triggerFound = false;

  for (const btn of buttons) {
    if (btn.innerText.includes("Was machst du gerade")) {
      console.log("1. Öffne Post-Dialog...");
      btn.click();
      triggerFound = true;
      break;
    }
  }

  if (!triggerFound) {
    console.error("❌ Start-Button nicht gefunden.");
    return;
  }

  await randomSleep(1500, 3500);

  // 2. Warte auf das Textfeld im Popup
  const textBox = await waitForElement('div[role="dialog"] div[role="textbox"]');
  console.log("2. Editor gefunden! Fokus setzen...");
  textBox.focus();

  await randomSleep(1000, 2000);

  // --- SCHRITT A: BILD ---
  if (base64Image) {
    console.log("📸 Füge Bild ein...");
    pasteImage(textBox, base64Image);
    console.log("⏳ Warte auf Upload & Verarbeitung...");
    await randomSleep(2500, 5500);
  }

  // --- SCHRITT B: TEXT ---
  if (text) {
    console.log("📝 Füge Text ein...");
    pasteText(textBox, text);
    console.log("⏳ Text lesen/prüfen...");
    await randomSleep(2000, 4500);
  }

  // --- SCHRITT C: BUTTONS (Weiter / Posten / Skip) ---
  console.log("🔘 Starte Button-Logik...");
  await handleButtonsRecursive();
}

// Rekursive Button-Suche mit erweiterter Logik
async function handleButtonsRecursive() {
  const dialogSelector = 'div[role="dialog"]';
  
  // Standard Buttons
  const weiterBtn = document.querySelector(`${dialogSelector} div[aria-label="Weiter"]`);
  const postenBtn = document.querySelector(`${dialogSelector} div[aria-label="Posten"]`);
  
  // Neue Buttons (WhatsApp Dialog / Upsell)
  // Prüft auf "Button hinzufügen", "Überspringen" und "Jetzt nicht"
  const addBtn = document.querySelector(`${dialogSelector} div[aria-label="Button hinzufügen"]`);
  const skipBtn = document.querySelector(`${dialogSelector} div[aria-label="Überspringen"]`) || 
                  document.querySelector(`${dialogSelector} div[aria-label="Jetzt nicht"]`);

  // Priorisierung: Posten > Weiter > Hinzufügen > Überspringen
  let targetBtn = postenBtn || weiterBtn || addBtn || skipBtn;

  if (!targetBtn) {
    console.log("🔍 Noch keine Buttons gefunden. Suche gleich nochmal...");
    // Wir versuchen es weiter, falls das Internet langsam ist oder der Dialog wechselt
    await randomSleep(800, 1500);
    return handleButtonsRecursive();
  }

  // Prüfen ob ausgegraut (disabled)
  const isDisabled = targetBtn.getAttribute("aria-disabled") === "true";
  if (isDisabled) {
    console.log("⏳ Button gefunden, aber noch inaktiv. Warte...");
    await randomSleep(1000, 2000);
    return handleButtonsRecursive();
  }

  const buttonType = targetBtn.getAttribute("aria-label");
  console.log(`🚀 KLICK auf: "${buttonType}"`);

  // Klick simulieren
  const mouseUp = new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window });
  const mouseDown = new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window });
  targetBtn.dispatchEvent(mouseDown);
  targetBtn.dispatchEvent(mouseUp);
  targetBtn.click();

  // --- ENTSCHEIDUNG NACH KLICK ---

  if (buttonType === "Weiter") {
    console.log("➡️ 'Weiter' geklickt. Warte auf nächsten Screen...");
    await randomSleep(2000, 5000);
    return handleButtonsRecursive();
  }

  if (buttonType === "Posten") {
    console.log("🎉 'Posten' geklickt! Aber warte... kommt noch ein Popup?");
    // WICHTIG: Nicht aufhören! Oft kommt jetzt der WhatsApp-Dialog.
    // Wir warten etwas länger, bis der Post durch ist und das neue Fenster kommt.
    await randomSleep(3000, 6000); 
    return handleButtonsRecursive();
  }

  if (buttonType === "Button hinzufügen" || buttonType === "Überspringen" || buttonType === "Jetzt nicht") {
    console.log("✅ Abschluss-Dialog (WhatsApp/Skip) behandelt. Vorgang endgültig beendet.");
    return; // HIER ist jetzt wirklich Schluss
  }
  
  // Fallback: Falls wir hier landen, einfach weiter suchen
  await randomSleep(1000, 2000);
  return handleButtonsRecursive();
}

// --- HELPER FUNKTIONEN (Unverändert) ---

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

function pasteImage(target, base64Image) {
  try {
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
    target.dispatchEvent(pasteEvent);
  } catch (e) {
    console.error("Fehler beim Bild-Paste:", e);
  }
}

function pasteText(target, text) {
  const dataTransfer = new DataTransfer();
  dataTransfer.setData("text/plain", text);
  const pasteEvent = new ClipboardEvent("paste", {
    bubbles: true,
    cancelable: true,
    clipboardData: dataTransfer,
  });
  target.dispatchEvent(pasteEvent);
}