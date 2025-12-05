// content.js - Robust für Profil & Feed mit Fokus-Pausen

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
    // Falls wir auf dem Profil sind und direkt posten wollen, könnte hier Logik fehlen,
    // aber wir konzentrieren uns erstmal auf den Kommentar-Part unten.
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
  await handleButtonsRecursive(commentToPost);
}

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
  targetBtn.click();

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
    await randomSleep(6000, 9000);

    const afterPostCleanup = document.evaluate("//span[text()='Jetzt nicht']", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
    if (afterPostCleanup) {
      afterPostCleanup.click();
      await randomSleep(1000, 2000);
    }

    console.log("🏁 Post-Vorgang abgeschlossen.");

    // --- AUTOMATISCHER KOMMENTAR ---
    if (commentToPost) {
      console.log(`💬 Kommentar gefunden: "${commentToPost}"`);
      console.log("⏳ Warte 3-6 Sekunden vor dem Kommentieren...");
      await randomSleep(3000, 6000);

      await processAutoComment(commentToPost);
    } else {
      console.log("🏁 Kein Kommentar zu posten. Fertig.");
    }
  }
}

// --- NEU: INTELLIGENTE KOMMENTAR FUNKTION ---

async function processAutoComment(text) {
  console.log("🔍 Suche nach dem Beitrag...");

  // 1. VERSUCH: Feed-Logik (Position 1)
  let postContainer = document.querySelector('div[aria-posinset="1"]');

  // 2. VERSUCH: Profil-Logik (Erster Artikel im Feed-Container)
  if (!postContainer) {
    console.log("ℹ️ 'posinset=1' nicht gefunden (vielleicht Profil-Ansicht?). Nehme ersten Artikel...");
    // Suche nach Rollen, die Beiträge darstellen
    const articles = document.querySelectorAll('div[role="article"]');
    if (articles.length > 0) {
      postContainer = articles[0];
    }
  }

  // 3. FALLBACK: Wenn gar nichts gefunden wird, brechen wir ab
  if (!postContainer) {
    console.error("❌ Konnte keinen Beitrag finden (weder Feed noch Profil).");
    return;
  }

  console.log("✅ Beitrag-Container gefunden. Prüfe auf offenes Textfeld...");

  // A. Prüfen, ob das Textfeld schon offen ist (Typisch für Profil-Ansicht!)
  let inputBox = postContainer.querySelector('div[role="textbox"][contenteditable="true"]');

  // B. Wenn NICHT offen, Button suchen und klicken
  if (!inputBox) {
    console.log("🔒 Textfeld nicht sichtbar. Suche 'Kommentieren' Button...");
    const commentButton = findCommentButton(postContainer);

    if (commentButton) {
      commentButton.click();
      await randomSleep(1500, 2500); // Warten bis Animation fertig
      // Neu suchen
      inputBox = postContainer.querySelector('div[role="textbox"][contenteditable="true"]');
    } else {
      console.error("❌ Weder offenes Feld noch Kommentieren-Button gefunden.");
      return;
    }
  }

  if (inputBox) {
    console.log("✍️ Feld gefunden! Starte Schreibprozess...");
    await insertTextAndSendComment(inputBox, text, postContainer);
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

async function insertTextAndSendComment(element, text, postContainer) {
  if (!element) return;

  try {
    console.log("🖱 1. Setze Fokus und klicke...");
    element.focus();
    element.click();

    // WICHTIG: Wir sagen dem Browser explizit "Hier ist jetzt der Fokus"
    element.dispatchEvent(new Event("focus", { bubbles: true }));

    // Optional: Simuliere, dass eine Taste gedrückt wird (ohne Text), um den Editor zu wecken
    element.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Shift" }));

    console.log("⏳ 2. Warte 2-3 Sekunden (Fokus etablieren)...");
    // Hier ist die Pause, BEVOR irgendein Text eingefügt wird
    await randomSleep(2000, 3000);

    console.log(`📝 3. Schreibe Text: "${text}"`);

    // Versuch 1: via execCommand (simuliert Tippen am besten)
    const success = document.execCommand("insertText", false, text);

    // Versuch 2: Falls execCommand nicht geht, direkt setzen
    if (!success) {
      element.innerText = text;
    }

     await randomSleep(2000, 3000);

    // WICHTIG: Das 'input' Event feuern, damit der Button blau wird
    console.log("⚡ 4. Feuere Input-Event (Button aktivieren)...");
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));

    // Noch eine kurze Pause, damit der Button Zeit hat, blau zu werden
    await randomSleep(1000, 2500);

    // Senden
    console.log("🚀 5. Klicke Senden...");
    clickCommentSendButton(postContainer);
  } catch (e) {
    console.error("Fehler beim Kommentieren:", e);
  }
}

function clickCommentSendButton(postContainer) {
  // Strategie 1: Spezifische ID aus deinem HTML (Die sicherste Methode!)
  // Das Element: <div id="focused-state-composer-submit">
  let submitContainer = document.getElementById("focused-state-composer-submit");

  if (submitContainer) {
    // Darin den Button suchen (Pfeil)
    let sendBtn = submitContainer.querySelector('div[role="button"]');
    if (sendBtn) {
      console.log("🎯 'focused-state-composer-submit' Button gefunden. Klicke...");
      sendBtn.click();
      return;
    }
  }

  // Strategie 2: Fallback (Suche im Post-Container)
  console.log("⚠️ ID nicht gefunden, nutze Fallback-Suche im Post...");
  const buttons = postContainer.querySelectorAll('div[role="button"][aria-label="Kommentieren"]');
  if (buttons.length > 0) {
    // Meist der letzte Button ist der Absende-Pfeil
    const lastBtn = buttons[buttons.length - 1];
    lastBtn.click();
    console.log("✅ Fallback-Klick ausgeführt.");
    return;
  }

  console.error("❌ Senden-Button konnte nicht gefunden werden.");
}

// --- BASIS HELPER FUNKTIONEN ---

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
