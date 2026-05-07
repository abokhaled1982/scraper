// content.js - Robust mit Scroll-Fix für unsichtbare Felder

// --- HELPER: WARTEZEIT ---
const randomSleep = (min = 2000, max = 5000) => {
  const delay = Math.floor(Math.random() * (max - min + 1)) + min;
  console.log(`🎲 Menschliche Pause: ${(delay / 1000).toFixed(2)} Sekunden...`);
  return new Promise((resolve) => setTimeout(resolve, delay));
};

console.log("✅ Facebook Content Script geladen.");

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
    console.log("🤖 Empfange Befehl...", {
      text: request.text ? request.text.slice(0, 80) : null,
      hasImage: !!request.image,
      hasVideo: !!request.video,
      comment: request.comment ? request.comment.slice(0, 40) : null,
    });
    startPostingProcess(request.text, request.image, request.video, request.comment)
      .then(() => sendResponse({ received: true }))
      .catch((error) => {
        console.error('Fehler in startPostingProcess:', error);
        sendResponse({ received: false, error: String(error) });
      });
    return true;
  }
});

async function startPostingProcess(text, base64Image, base64Video, commentToPost) {
  // 0. Vorbereitung
  fixFocusBlockers();

  if (base64Video) {
    // --- REEL MODE ---
    console.log("🎥 Poste als Reel...");
    await postAsReel(text, base64Video, commentToPost);
    return;
  }

  // --- NORMAL POST MODE ---

  // 1. Trigger-Button suchen (alle Facebook-Sprachvarianten)
  const TRIGGER_KEYWORDS = [
    "Was machst du gerade",
    "Was denkst du gerade",
    "Was liegt dir auf dem Herzen",
    "What's on your mind",
    "What's happening",
    "Whats on your mind",
  ];

  const buttons = document.querySelectorAll('div[role="button"], [aria-placeholder]');
  let triggerFound = false;

  for (const btn of buttons) {
    const txt = (btn.innerText || btn.getAttribute("aria-placeholder") || "");
    if (TRIGGER_KEYWORDS.some(kw => txt.includes(kw))) {
      console.log("1. Öffne Post-Dialog...", txt.trim().slice(0, 40));
      btn.click();
      triggerFound = true;
      break;
    }
  }

  // Fallback: placeholder-Suche im gesamten DOM
  if (!triggerFound) {
    const placeholders = document.querySelectorAll('[placeholder]');
    for (const el of placeholders) {
      const ph = el.getAttribute("placeholder") || "";
      if (TRIGGER_KEYWORDS.some(kw => ph.includes(kw))) {
        console.log("1. (Fallback) Öffne Post-Dialog via placeholder...");
        el.click();
        triggerFound = true;
        break;
      }
    }
  }

  if (!triggerFound) {
    console.error("❌ Start-Button nicht gefunden. Verfügbare Buttons:");
    document.querySelectorAll('div[role="button"]').forEach(b => {
      if (b.innerText.trim()) console.log("  →", b.innerText.trim().slice(0, 60));
    });
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
      // Längere Pause, damit Seite sich beruhigen kann
      console.log("⏳ Warte 5-8 Sekunden vor dem Kommentieren...");
      await randomSleep(5000, 8000);

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
    const articles = document.querySelectorAll('div[role="article"]');
    if (articles.length > 0) {
      postContainer = articles[0];
    }
  }

  if (!postContainer) {
    console.error("❌ Konnte keinen Beitrag finden (weder Feed noch Profil).");
    return;
  }

  // --- FIX: SCROLLEN ZUM BEITRAG ---
  console.log("📜 Scrolle Beitrag in Sichtbereich...");
  postContainer.scrollIntoView({ behavior: "smooth", block: "center" });
  await randomSleep(1500, 2000);

  console.log("✅ Beitrag-Container gefunden. Prüfe auf offenes Textfeld...");

  // A. Prüfen, ob das Textfeld schon offen ist
  let inputBox = postContainer.querySelector('div[role="textbox"][contenteditable="true"]');

  // B. Wenn NICHT offen, Button suchen und klicken
  if (!inputBox) {
    console.log("🔒 Textfeld nicht sichtbar. Suche 'Kommentieren' Button...");
    const commentButton = findCommentButton(postContainer);

    if (commentButton) {
      // Auch den Button sicherheitshalber ins Bild holen
      commentButton.scrollIntoView({ behavior: "smooth", block: "center" });
      await randomSleep(500, 1000);

      commentButton.click();
      await randomSleep(1500, 2500);
      
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
    // --- FIX: SCROLLEN ZUM TEXTFELD ---
    // Bevor wir irgendwas machen, sicherstellen, dass das Feld in der Mitte ist
    console.log("📜 Scrolle Textfeld exakt in die Mitte...");
    element.scrollIntoView({ behavior: "smooth", block: "center" });
    
    // Warte kurz auf das Scrollen
    await randomSleep(1000, 1500);

    console.log("🖱 1. Setze Fokus und klicke...");
    element.focus({ preventScroll: true }); // preventScroll, weil wir es schon manuell gemacht haben
    element.click();

    // WICHTIG: Browser "wecken"
    element.dispatchEvent(new Event("focus", { bubbles: true }));
    element.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Shift" }));

    console.log("⏳ 2. Warte 2-3 Sekunden (Fokus etablieren)...");
    await randomSleep(2000, 3000);

    console.log(`📝 3. Schreibe Text: "${text}"`);

    const success = document.execCommand("insertText", false, text);
    if (!success) {
      element.innerText = text;
    }
    
    // Längere Pause nach dem Schreiben
    await randomSleep(1500, 2500);

    console.log("⚡ 4. Feuere Input-Event (Button aktivieren)...");
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));

    await randomSleep(1500, 2500);

    console.log("🚀 5. Klicke Senden...");
    clickCommentSendButton(postContainer);
  } catch (e) {
    console.error("Fehler beim Kommentieren:", e);
  }
}

function clickCommentSendButton(postContainer) {
  // Strategie 1: Spezifische ID
  let submitContainer = document.getElementById("focused-state-composer-submit");

  if (submitContainer) {
    let sendBtn = submitContainer.querySelector('div[role="button"]');
    if (sendBtn) {
      console.log("🎯 'focused-state-composer-submit' Button gefunden. Klicke...");
      sendBtn.click();
      return;
    }
  }

  // Strategie 2: Fallback
  console.log("⚠️ ID nicht gefunden, nutze Fallback-Suche im Post...");
  const buttons = postContainer.querySelectorAll('div[role="button"][aria-label="Kommentieren"]');
  if (buttons.length > 0) {
    const lastBtn = buttons[buttons.length - 1];
    
    // Sicherstellen, dass auch der Button sichtbar ist, bevor wir klicken
    lastBtn.scrollIntoView({ behavior: "smooth", block: "center" });
    
    setTimeout(() => {
        lastBtn.click();
        console.log("✅ Fallback-Klick ausgeführt.");
    }, 500);
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

async function postAsReel(text, base64Video, commentToPost) {
  // 1. Suche den Reel-Button im aktuellen Facebook-Dialog
  const reelBtn = findReelButton();
  if (!reelBtn) {
    console.error("❌ Reel-Button nicht gefunden.");
    return;
  }

  console.log("🎥 Klicke Reel-Button...");
  reelBtn.scrollIntoView({ behavior: "smooth", block: "center" });
  await randomSleep(300, 700);
  reelBtn.click();
  await randomSleep(2500, 4500);

  // 2. Warte bis der Reel-Upload-Container sichtbar ist, DANN erst nach dem File-Input suchen
  console.log("⏳ Warte auf Reel-Upload-Dialog...");
  const reelContainerAppeared = await new Promise((resolve) => {
    const start = Date.now();
    const check = () => {
      const container = findReelUploadContainer();
      if (container) return resolve(container);
      if (Date.now() - start > 12000) return resolve(null);
      setTimeout(check, 300);
    };
    check();
  });

  if (!reelContainerAppeared) {
    console.warn("⚠️ Reel-Upload-Container nicht gefunden – versuche es trotzdem...");
  } else {
    console.log("✅ Reel-Upload-Container erschienen.");
    await randomSleep(500, 1000); // kurze Pause, damit DOM sich stabilisiert
  }

  const fileInput = await waitForVideoUploadInput();
  if (!fileInput) {
    console.error("❌ Upload-Input nicht gefunden.");
    return;
  }
  console.log("📁 Upload-Input gefunden. Lade Video...");

  // Base64 zu Blob konvertieren
  console.log(`📊 Base64-Größe: ${base64Video.length} bytes`);
  const byteCharacters = atob(base64Video);
  console.log(`📊 Dekodierte Größe: ${byteCharacters.length / 1024 / 1024} MB`);
  const byteNumbers = new Array(byteCharacters.length);
  for (let i = 0; i < byteCharacters.length; i++) {
    byteNumbers[i] = byteCharacters.charCodeAt(i);
  }
  const byteArray = new Uint8Array(byteNumbers);
  const blob = new Blob([byteArray], { type: "video/mp4" });
  console.log(`✅ Blob erstellt: ${blob.size / 1024 / 1024} MB, type: ${blob.type}`);
  const file = new File([blob], "reel.mp4", { type: "video/mp4" });
  console.log(`✅ File-Objekt: ${file.name}, size: ${file.size / 1024 / 1024} MB`);

  const dataTransfer = new DataTransfer();
  dataTransfer.items.add(file);

  let uploadSuccess = false;
  try {
    fileInput.files = dataTransfer.files;
    console.log("✅ Datei zum File-Input hinzugefügt.");
    uploadSuccess = true;
  } catch (e) {
    console.warn("⚠️ File-Input Zuweisung fehlgeschlagen, versuche Drag/Drop-Simulation...", e);
  }

  if (!uploadSuccess) {
    const dropZone = findReelDropZone();
    if (dropZone) {
      console.log("🧲 Drop-Zone gefunden. Simuliere Drag & Drop...");
      simulateDropOnElement(dropZone, dataTransfer);
      uploadSuccess = true;
    } else {
      console.warn("⚠️ Keine Drop-Zone gefunden. Versuche trotzdem Change-Event...");
    }
  }

  const inputToTrigger = fileInput || findVideoUploadInput();
  if (inputToTrigger) {
    console.log("🔄 Triggere Change-Event...");
    inputToTrigger.dispatchEvent(new Event("change", { bubbles: true }));
    inputToTrigger.dispatchEvent(new Event("input", { bubbles: true }));
  }
  await randomSleep(3500, 6000);

  // 3. Falls nötig: Weiter klicken, um zur Beschreibung zu gelangen
  const continueBtn = findButtonByText(["Weiter", "Continue"]);
  if (continueBtn && !isButtonDisabled(continueBtn)) {
    console.log("➡️ Weiter-Button nach Upload gefunden. Klicke weiter...");
    continueBtn.click();
    await randomSleep(2500, 4500);
  }

  // 4. Reel-Beschreibung einfügen
  if (text) {
    const filled = await fillReelDescription(text);
    if (!filled) {
      console.warn("⚠️ Reel-Beschreibung konnte nicht automatisch eingefügt werden.");
    }
  }

  // 5. Klick den Reel-Flow weiter bis zum finalen Posten
  const posted = await clickReelActionButtons();
  if (posted) {
    console.log("✅ Reel gepostet!");
  } else {
    console.error("❌ Reel-Post-Flow konnte nicht abgeschlossen werden.");
  }
}

async function clickReelActionButtons() {
  for (let attempt = 0; attempt < 8; attempt++) {
    const postBtn = findButtonByText(["Teilen", "Post", "Share", "Veröffentlichen"]);
    if (postBtn && !isButtonDisabled(postBtn)) {
      postBtn.click();
      await randomSleep(4000, 7000);
      return true;
    }

    const continueBtn = findButtonByText(["Weiter", "Continue"]);
    if (continueBtn && !isButtonDisabled(continueBtn)) {
      continueBtn.click();
      console.log("➡️ Klicke Weiter im Reel-Flow...");
      await randomSleep(2500, 4500);
      continue;
    }

    await randomSleep(1000, 2000);
  }
  return false;
}

function isButtonDisabled(btn) {
  if (!btn) return true;
  if (btn.getAttribute('aria-disabled') === 'true') return true;
  if (btn.disabled) return true;
  return false;
}

function findReelButton() {
  const selectorCandidates = [
    'button[aria-label*="Reel"]',
    'div[role="button"][aria-label*="Reel"]',
    'span[role="button"][aria-label*="Reel"]',
    'div[aria-label*="Reel"]',
    'button',
    'div[role="button"]',
    'span[role="button"]',
  ];

  for (const selector of selectorCandidates) {
    const els = document.querySelectorAll(selector);
    for (const el of els) {
      const aria = (el.getAttribute("aria-label") || "").toLowerCase();
      const text = (el.innerText || "").toLowerCase();
      if (aria.includes("reel") || text.includes("reel") || text.includes("reel erstellen") || text.includes("create reel")) {
        return el;
      }
    }
  }

  const xpath = `//div[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'reel')] | //button[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'reel')] | //span[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'reel')] | //div[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'reel')] | //span[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'reel')]`;
  const node = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
  if (node) {
    return node;
  }

  // Debug: Liste möglicher Kandidaten
  const debugButtons = Array.from(document.querySelectorAll('div[role="button"], button, span[role="button"], div[aria-label], span[aria-label]'))
    .filter(el => (el.innerText || el.getAttribute("aria-label") || "").toLowerCase().includes("reel"));
  if (debugButtons.length > 0) {
    console.log("ℹ️ Mögliche Reel-Kandidaten:", debugButtons.map(el => ({ text: el.innerText?.trim(), aria: el.getAttribute("aria-label") })));
    return debugButtons[0];
  }

  return null;
}

function findReelDropZone() {
  const textSelectors = [
    'Video hinzufügen',
    'oder hierher ziehen und ablegen',
    'Video für Reel hochladen',
    'Reel erstellen',
  ];

  const allCandidates = Array.from(document.querySelectorAll('div[role="button"], div, span, section'));
  for (const el of allCandidates) {
    const text = (el.innerText || el.getAttribute('aria-label') || '').toLowerCase();
    if (textSelectors.some((keyword) => text.includes(keyword.toLowerCase()))) {
      return el;
    }
  }

  const inputCandidates = Array.from(document.querySelectorAll('input[type="file"]'));
  for (const input of inputCandidates) {
    if (input.accept && input.accept.includes('video')) {
      return input.closest('div') || input;
    }
  }

  const labelCandidates = Array.from(document.querySelectorAll('[aria-label]'));
  for (const el of labelCandidates) {
    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
    if (aria.includes('video für reel hochladen') || aria.includes('reels') || aria.includes('upload')) {
      return el;
    }
  }

  return null;
}

function simulateDropOnElement(element, dataTransfer) {
  ['dragenter', 'dragover', 'drop'].forEach((type) => {
    const event = new DragEvent(type, {
      bubbles: true,
      cancelable: true,
      dataTransfer,
    });
    element.dispatchEvent(event);
  });
}

/**
 * Finds the Reel-specific upload drop-zone container.
 * We identify it by the presence of the video-camera SVG icon (path starting with "M8.805")
 * OR by the "Video hinzufügen" / "oder hierher ziehen" label text.
 * Returns the outermost wrapper div that contains the file input.
 */
function findReelUploadContainer() {
  // Strategy 1: Find by the unique SVG path used in the Reel drop zone
  const svgs = Array.from(document.querySelectorAll('svg'));
  for (const svg of svgs) {
    const path = svg.querySelector('path');
    if (path && (path.getAttribute('d') || '').startsWith('M8.805')) {
      // Walk up to a reasonable container that would hold an <input>
      let el = svg.parentElement;
      for (let i = 0; i < 8; i++) {
        if (!el) break;
        if (el.querySelector('input[type="file"]')) return el;
        el = el.parentElement;
      }
    }
  }

  // Strategy 2: Find by "Video hinzufügen" label text
  const REEL_UPLOAD_TEXTS = [
    'video hinzufügen',
    'oder hierher ziehen und ablegen',
    'video für reel hochladen',
    'add video',
    'drag and drop',
  ];
  const allDivs = Array.from(document.querySelectorAll('div, section'));
  for (const div of allDivs) {
    const text = (div.innerText || '').toLowerCase();
    if (REEL_UPLOAD_TEXTS.some(t => text.includes(t))) {
      // Make sure this container or a nearby ancestor has a file input
      let el = div;
      for (let i = 0; i < 8; i++) {
        if (!el) break;
        if (el.querySelector('input[type="file"]')) return el;
        el = el.parentElement;
      }
    }
  }

  return null;
}

function findVideoUploadInput() {
  // First: try to scope the search to the Reel upload container
  const reelContainer = findReelUploadContainer();
  if (reelContainer) {
    const scopedInputs = Array.from(reelContainer.querySelectorAll('input[type="file"]'));
    const videoInput = scopedInputs.find(input => !input.accept || input.accept.includes('video') || input.accept.includes('*'));
    if (videoInput) {
      console.log('✅ Reel-File-Input im Reel-Container gefunden (scoped).');
      return videoInput;
    }
  }

  // Fallback: page-wide search, but prefer inputs that are NOT inside a normal post dialog textbox area
  const fileInputs = Array.from(document.querySelectorAll('input[type="file"]'));

  // Filter to video-accepting inputs that are NOT children of the normal post composer textbox
  const reelInputs = fileInputs.filter((input) => {
    if (input.accept && !input.accept.includes('video') && !input.accept.includes('*') && !input.accept.includes('mp4')) return false;
    // Exclude inputs that sit inside a div[role="textbox"] (normal post composer)
    if (input.closest('div[role="textbox"]')) return false;
    // Prefer inputs whose ancestor contains Reel-related text
    let el = input.parentElement;
    for (let i = 0; i < 10; i++) {
      if (!el) break;
      const t = (el.innerText || '').toLowerCase();
      if (t.includes('reel') || t.includes('video hinzufügen') || t.includes('add video')) return true;
      el = el.parentElement;
    }
    return false;
  });

  if (reelInputs.length) {
    console.log('✅ Reel-File-Input via Fallback-Filterung gefunden.');
    return reelInputs[0];
  }

  // Last resort: any video file input
  console.warn('⚠️ Kein Reel-spezifischer Input gefunden. Nehme ersten Video-Input (kann falsch sein!)');
  return fileInputs.find((input) => !input.accept || input.accept.includes('video') || input.accept.includes('*')) || null;
}

function waitForVideoUploadInput(timeout = 15000) {
  const start = Date.now();
  return new Promise((resolve) => {
    const check = () => {
      const input = findVideoUploadInput();
      if (input) return resolve(input);
      if (Date.now() - start > timeout) return resolve(null);
      setTimeout(check, 250);
    };
    check();
  });
}

function findButtonByText(words) {
  const buttons = Array.from(document.querySelectorAll('button, div[role="button"], a[role="button"], span[role="button"], [role="button"]'));
  for (const btn of buttons) {
    const rawText = btn.innerText || btn.textContent || btn.getAttribute('aria-label') || '';
    const text = rawText.trim().toLowerCase();
    if (!text) continue;

    for (const word of words) {
      if (text.includes(word.toLowerCase())) {
        return btn;
      }
    }
  }
  return null;
}

function findReelDescriptionEditor() {
  const editors = Array.from(document.querySelectorAll('div[role="dialog"] div[role="textbox"][contenteditable="true"], div[role="dialog"] div[contenteditable="true"][role="textbox"]'));
  for (const editor of editors) {
    const placeholder = (editor.getAttribute('aria-placeholder') || editor.getAttribute('placeholder') || '').toLowerCase();
    if (placeholder.includes('beschreibe dein reel')) {
      return editor;
    }
  }

  // Fallback: leeres contenteditable im aktuellen Dialog
  const emptyEditors = editors.filter((editor) => editor.innerText.trim().length === 0);
  return emptyEditors.length === 1 ? emptyEditors[0] : null;
}

function waitForReelDescriptionEditor(timeout = 10000) {
  const start = Date.now();
  return new Promise((resolve) => {
    const check = () => {
      const editor = findReelDescriptionEditor();
      if (editor) return resolve(editor);
      if (Date.now() - start > timeout) return resolve(null);
      setTimeout(check, 200);
    };
    check();
  });
}

async function fillReelDescription(text) {
  const editor = await waitForReelDescriptionEditor();
  if (!editor) return false;

  try {
    console.log('✍️ Reel-Beschreibung erkannt. Setze Text...');
    editor.focus();
    editor.click();

    // Leere vorhandene Inhalte
    editor.innerText = '';
    await randomSleep(200, 400);

    const success = document.execCommand('insertText', false, text);
    if (!success) {
      editor.innerText = text;
    }

    editor.dispatchEvent(new Event('input', { bubbles: true }));
    editor.dispatchEvent(new Event('change', { bubbles: true }));
    await randomSleep(800, 1200);
    return true;
  } catch (e) {
    console.error('Fehler beim Ausfüllen der Reel-Beschreibung:', e);
    return false;
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