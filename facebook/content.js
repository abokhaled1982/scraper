// content.js - Robust gegen "aria-hidden" Fehler & mit "Jetzt nicht" Logik

// --- HELPER: WARTEZEIT ---
const randomSleep = (min = 2000, max = 5000) => {
  const delay = Math.floor(Math.random() * (max - min + 1)) + min;
  console.log(`🎲 Menschliche Pause: ${(delay / 1000).toFixed(2)} Sekunden...`);
  return new Promise((resolve) => setTimeout(resolve, delay));
};

// --- HELPER: BLOCKADEN ENTFERNEN ---
// Diese Funktion löst das "Blocked aria-hidden" Problem
function fixFocusBlockers() {
  const blocker = document.getElementById('scrollview');
  if (blocker && blocker.getAttribute('aria-hidden') === 'true') {
    console.log("🔧 Entferne 'aria-hidden' Blockade von #scrollview...");
    blocker.removeAttribute('aria-hidden');
  }
  
  // Sicherheitshalber auch von Dialogen entfernen, falls dort gesetzt
  const dialogs = document.querySelectorAll('div[role="dialog"][aria-hidden="true"]');
  dialogs.forEach(d => {
      console.log("🔧 Entferne 'aria-hidden' von Dialog...");
      d.removeAttribute('aria-hidden');
  });
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.command === "remote_post") {
    console.log("🤖 Empfange Befehl...");
    startPostingProcess(request.text, request.image);
  }
});

async function startPostingProcess(text, base64Image) {
  // 0. Vorbereitung
  fixFocusBlockers();

  // 1. Trigger-Button suchen
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
  
  // WICHTIG: Vor dem Fokus Blockaden lösen
  fixFocusBlockers(); 
  
  try {
      textBox.focus();
  } catch (e) {
      console.warn("⚠️ Fokus-Warnung ignoriert, mache weiter...", e);
  }

  await randomSleep(1000, 2000);

  // --- SCHRITT A: BILD ---
  if (base64Image) {
    console.log("📸 Füge Bild ein...");
    fixFocusBlockers(); // Sicherheitshalber vor Paste
    pasteImage(textBox, base64Image);
    
    console.log("⏳ Warte auf Upload & Verarbeitung...");
    await randomSleep(2500, 5500);
  }

  // --- SCHRITT B: TEXT ---
  if (text) {
    console.log("📝 Füge Text ein...");
    fixFocusBlockers();
    pasteText(textBox, text);
    
    console.log("⏳ Text lesen/prüfen...");
    await randomSleep(2000, 4500);
  }

  // --- SCHRITT C: BUTTONS ---
  console.log("🔘 Starte Button-Logik...");
  await handleButtonsRecursive();
}

async function handleButtonsRecursive() {
  // Immer mal wieder aufräumen
  fixFocusBlockers();

  const dialogSelector = 'div[role="dialog"]';
  
  // 1. Suche alle Kandidaten
  const weiterBtn = document.querySelector(`${dialogSelector} div[aria-label="Weiter"]`);
  const postenBtn = document.querySelector(`${dialogSelector} div[aria-label="Posten"]`);
  
  // XPath für "Jetzt nicht"
  const jetztNichtSpan = document.evaluate(
      "//span[text()='Jetzt nicht']", 
      document, 
      null, 
      XPathResult.FIRST_ORDERED_NODE_TYPE, 
      null
  ).singleNodeValue;

  let targetBtn = null;
  let actionType = "";

  // PRIORISIERUNG:
  if (postenBtn) {
      targetBtn = postenBtn;
      actionType = "Posten";
  } else if (weiterBtn) {
      targetBtn = weiterBtn;
      actionType = "Weiter";
  } else if (jetztNichtSpan) {
      targetBtn = jetztNichtSpan; 
      actionType = "Jetzt nicht";
  }

  // Wenn nichts gefunden -> Warten und nochmal suchen
  if (!targetBtn) {
    console.log("🔍 Noch keine relevanten Buttons gefunden. Suche gleich nochmal...");
    await randomSleep(1000, 2000);
    return handleButtonsRecursive();
  }

  // Disabled Check (nur für Weiter/Posten relevant)
  const isDisabled = targetBtn.getAttribute("aria-disabled") === "true";
  if (isDisabled && actionType !== "Jetzt nicht") {
    console.log(`⏳ Button '${actionType}' gefunden, aber noch inaktiv. Warte...`);
    await randomSleep(1000, 2000);
    return handleButtonsRecursive();
  }

  console.log(`🚀 KLICK auf: "${actionType}"`);

  // Klick simulieren
  const mouseUp = new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window });
  const mouseDown = new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window });
  
  try {
      targetBtn.dispatchEvent(mouseDown);
      targetBtn.dispatchEvent(mouseUp);
      targetBtn.click();
  } catch (err) {
      console.error("Klick Fehler:", err);
  }

  // --- LOGIK NACH DEM KLICK ---

  if (actionType === "Jetzt nicht") {
    console.log("🚫 'Jetzt nicht' geklickt. Warte kurz...");
    await randomSleep(2000, 3000); 
    // Nach "Jetzt nicht" könnte noch der "Posten" Button kommen oder wir sind fertig.
    // Wir rufen die Funktion nochmal auf, um sicherzugehen.
    return handleButtonsRecursive(); 
  }

  if (actionType === "Weiter") {
    console.log("➡️ 'Weiter' geklickt. Warte auf nächsten Screen...");
    await randomSleep(2000, 4000);
    return handleButtonsRecursive();
  }

  if (actionType === "Posten") {
    console.log("🎉 'Posten' geklickt! Warte auf Bestätigung oder Störer-Popups...");
    
    // WICHTIG: Wir hören hier NICHT sofort auf.
    // Wir warten kurz, ob Facebook uns noch ein "Jetzt nicht" oder "Gruppe beitreten" Popup zeigt.
    await randomSleep(3000, 5000);

    // Ein letzter Check: Ist JETZT vielleicht ein "Jetzt nicht" Button da?
    const afterPostCleanup = document.evaluate(
      "//span[text()='Jetzt nicht']", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
    ).singleNodeValue;

    if (afterPostCleanup) {
        console.log("🧹 Aufräumen: 'Jetzt nicht' Popup nach dem Posten gefunden. Klicke es weg...");
        afterPostCleanup.click();
        await randomSleep(1000, 2000);
    } else {
        console.log("✅ Kein weiteres Popup gefunden.");
    }

    console.log("🏁 Vorgang endgültig abgeschlossen.");
  }
}
// --- HELPER FUNKTIONEN ---

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