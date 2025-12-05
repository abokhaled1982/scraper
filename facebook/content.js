// content.js - Robust mit automatischem Kommentar

// --- HELPER: WARTEZEIT ---
const randomSleep = (min = 2000, max = 5000) => {
  const delay = Math.floor(Math.random() * (max - min + 1)) + min;
  console.log(`🎲 Menschliche Pause: ${(delay / 1000).toFixed(2)} Sekunden...`);
  return new Promise((resolve) => setTimeout(resolve, delay));
};

// --- HELPER: BLOCKADEN ENTFERNEN ---
function fixFocusBlockers() {
  const blocker = document.getElementById("scrollview");
  if (blocker && blocker.getAttribute("aria-hidden") === "true") {
    // console.log("🔧 Entferne 'aria-hidden' Blockade von #scrollview...");
    blocker.removeAttribute("aria-hidden");
  }

  const dialogs = document.querySelectorAll('div[role="dialog"][aria-hidden="true"]');
  dialogs.forEach((d) => {
    d.removeAttribute("aria-hidden");
  });
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.command === "remote_post") {
    console.log("🤖 Empfange Befehl...");
    // Wir übergeben jetzt auch den Kommentar an die Startfunktion
    startPostingProcess(request.text, request.image, request.comment);
  }
});

async function startPostingProcess(text, base64Image, commentToPost) {
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
    fixFocusBlockers();
    pasteImage(textBox, base64Image);

    console.log("⏳ Warte auf Upload & Verarbeitung...");
    await randomSleep(5000, 8000);
  }

  // --- SCHRITT B: TEXT ---
  if (text) {
    console.log("📝 Füge Text ein...");
    fixFocusBlockers();
    pasteText(textBox, text);

    console.log("⏳ Text lesen/prüfen...");
    await randomSleep(4000, 7000);
  }

  // --- SCHRITT C: BUTTONS ---
  console.log("🔘 Starte Button-Logik...");
  // Wir reichen den commentToPost weiter an die Button-Logik
  await handleButtonsRecursive(commentToPost);
}

// Argument 'commentToPost' hinzugefügt
async function handleButtonsRecursive(commentToPost) {
  fixFocusBlockers();

  const dialogSelector = 'div[role="dialog"]';

  const weiterBtn = document.querySelector(`${dialogSelector} div[aria-label="Weiter"]`);
  const postenBtn = document.querySelector(`${dialogSelector} div[aria-label="Posten"]`);

  const jetztNichtSpan = document.evaluate("//span[text()='Jetzt nicht']", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;

  let targetBtn = null;
  let actionType = "";

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
    return handleButtonsRecursive(commentToPost);
  }

  const isDisabled = targetBtn.getAttribute("aria-disabled") === "true";
  if (isDisabled && actionType !== "Jetzt nicht") {
    console.log(`⏳ Button '${actionType}' noch inaktiv. Warte...`);
    await randomSleep(1000, 2000);
    return handleButtonsRecursive(commentToPost);
  }

  console.log(`🚀 KLICK auf: "${actionType}"`);

  // Klick simulieren
  try {
    targetBtn.click();
  } catch (err) {
    console.error("Klick Fehler:", err);
  }

  // --- LOGIK NACH DEM KLICK ---

  if (actionType === "Jetzt nicht") {
    console.log("🚫 'Jetzt nicht' geklickt. Warte kurz...");
    await randomSleep(2000, 3000);
    return handleButtonsRecursive(commentToPost);
  }

  if (actionType === "Weiter") {
    console.log("➡️ 'Weiter' geklickt. Warte auf nächsten Screen...");
    await randomSleep(2000, 4000);
    return handleButtonsRecursive(commentToPost);
  }

  if (actionType === "Posten") {
    console.log("🎉 'Posten' geklickt! Warte auf Abschluss...");

    // Wartezeit, um sicherzugehen, dass FB den Post verarbeitet
    await randomSleep(6000, 9000);

    // Letzter Check auf Störer-Popups nach dem Posten
    const afterPostCleanup = document.evaluate("//span[text()='Jetzt nicht']", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;

    if (afterPostCleanup) {
      console.log("🧹 Aufräumen: 'Jetzt nicht' Popup nach dem Posten gefunden.");
      afterPostCleanup.click();
      await randomSleep(1000, 2000);
    }

    console.log("🏁 Post-Vorgang abgeschlossen.");

    // --- NEU: AUTOMATISCHER KOMMENTAR ---
    if (commentToPost) {
      console.log(`💬 Kommentar gefunden: "${commentToPost}"`);
      console.log("⏳ Warte 3-6 Sekunden vor dem Kommentieren...");

      // Die gewünschte zufällige Wartezeit (3000ms bis 6000ms)
      await randomSleep(3000, 6000);

      // Starte die Kommentar-Funktion
      await processAutoComment(commentToPost);
    } else {
      console.log("🏁 Kein Kommentar zu posten. Fertig.");
    }
  }
}

// --- NEU: KOMMENTAR FUNKTIONEN (Basiert auf deinem manuellen Code) ---

async function processAutoComment(text) {
  console.log("🔍 Suche nach dem neuesten Beitrag (Position 1)...");

  // Wir versuchen es ein paar Mal, falls der Feed noch lädt
  let firstPost = null;
  for (let i = 0; i < 5; i++) {
    firstPost = document.querySelector('div[aria-posinset="1"]');
    if (firstPost) break;
    await randomSleep(1000, 1500);
  }

  if (!firstPost) {
    console.error("❌ Kein Beitrag an Position 1 gefunden.");
    return;
  }

  console.log("✅ Beitrag gefunden. Suche Eingabefeld...");

  // 1. Prüfen, ob das Textfeld schon offen ist
  let inputBox = firstPost.querySelector('div[role="textbox"][contenteditable="true"]');

  if (!inputBox) {
    // Button zum Öffnen suchen
    const commentButton = findCommentButton(firstPost);
    if (commentButton) {
      console.log("🖱 Klicke 'Kommentieren' Button...");
      commentButton.click();
      await randomSleep(1000, 2000); // Warten bis Feld da ist

      // Neu suchen nach Klick
      inputBox = firstPost.querySelector('div[role="textbox"][contenteditable="true"]');
    } else {
      console.error("❌ Weder Textfeld noch Kommentieren-Button gefunden.");
      return;
    }
  }

  if (inputBox) {
    console.log("✍️ Schreibe Kommentar...");
    insertTextAndSendComment(inputBox, text, firstPost);
  }
}

function findCommentButton(container) {
  let btn = container.querySelector('div[aria-label*="Kommentar"]');
  if (btn) return btn;
  btn = container.querySelector('div[aria-label="Kommentieren"]');
  if (btn) return btn;

  const candidates = container.querySelectorAll('div[role="button"], span[role="button"]');
  for (let c of candidates) {
    if (c.innerText.includes("Kommentieren") || c.innerText.includes("Kommentar")) {
      return c;
    }
  }
  return null;
}

function insertTextAndSendComment(element, text, postContainer) {
  if (!element) return;

  try {
    element.focus();
    element.click();

    // Text einfügen
    const success = document.execCommand("insertText", false, text);

    if (!success) {
      // Fallback
      element.innerText = text;
    }

    console.log("✅ Text eingefügt. Warte kurz vor dem Senden...");

    // Kurze Pause, damit Facebook den Button aktiviert (wichtig!)
    setTimeout(() => {
      clickCommentSendButton(postContainer);
    }, 1500);
  } catch (e) {
    console.error("Fehler beim Kommentieren:", e);
  }
}

function clickCommentSendButton(postContainer) {
  console.log("🚀 Versuche Kommentar zu senden...");

  // Strategie 1: Submit Container ID (wenn Fokus aktiv ist)
  let submitContainer = document.getElementById("focused-state-composer-submit");
  if (submitContainer) {
    let sendBtn = submitContainer.querySelector('div[role="button"]');
    if (sendBtn) {
      sendBtn.click();
      console.log("✅ Kommentar gepostet (Strategie 1)!");
      return;
    }
  }

  // Strategie 2: Button innerhalb des Posts suchen
  const buttons = postContainer.querySelectorAll('div[role="button"][aria-label="Kommentieren"]');
  if (buttons.length > 0) {
    // Meist der letzte Button (Pfeil-Icon)
    buttons[buttons.length - 1].click();
    console.log("✅ Kommentar gepostet (Strategie 2)!");
    return;
  }

  console.error("❌ Konnte Senden-Button für Kommentar nicht finden.");
}

// --- BASIS HELPER FUNKTIONEN (unverändert) ---

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
