/**
 * Background-Scheduler: scannt den aktiven Tab nach Artikel-Deals
 * und führt alle 10s einen Klick auf den nächsten Deal-Button (im aktiven Tab) aus.
 *
 * - Scannen: alle 3 Sekunden.
 * - Klick: alle 10 Sekunden, direkt auf den Button im aktiven Tab.
 * - PERSISTENZ: Verarbeitete Deal-IDs werden in chrome.storage.local gespeichert.
 * - AUTO-RELOAD: Aktiver mydealz-Tab wird alle 30 Sekunden neu geladen.
 */

let autoOpen = true;
let openInterval = null; // 10s-Intervall
let scanInterval = null; // 3s-Intervall
const OPEN_EVERY_MS = 10_000;
const SCAN_EVERY_MS = 3_000;
const RELOAD_EVERY_MS = 30_000; // 30 Sekunden
const STORAGE_KEY = "processed_deal_ids"; // Schlüssel für chrome.storage

let lastReloadTime = 0;
const processed = new Set(); // Artikel-IDs, die bereits "bedient" wurden (wird aus Storage geladen)
const queue = []; // { id, selectorInfo: { id, index } }

// --- Storage Funktionen ---

// Lädt verarbeitete IDs beim Start aus dem lokalen Speicher
async function loadProcessedIds() {
  const result = await chrome.storage.local.get([STORAGE_KEY]);
  const ids = result[STORAGE_KEY] || [];
  ids.forEach((id) => processed.add(id));
  updateBadge();
  console.log(`[Deal-AutoClick] ${processed.size} verarbeitete Deals aus dem Speicher geladen.`);
}

// Speichert die aktuelle Liste der verarbeiteten IDs im lokalen Speicher
function saveProcessedIds() {
  chrome.storage.local.set({ [STORAGE_KEY]: Array.from(processed) });
}

// --- POPUP Messages optional
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  switch (msg?.type) {
    case "GET_STATE":
      return sendResponse({
        autoOpen,
        queueLength: queue.length,
        processed: processed.size,
      });
    case "TOGGLE_AUTO_OPEN":
      autoOpen = !!msg.enabled;
      autoOpen ? startOpenTimer() : stopOpenTimer();
      updateBadge();
      return;
    default:
      return;
  }
});

function start() {
  loadProcessedIds(); // *** NEU: IDs VOR dem Start laden ***
  startScanTimer();
  startOpenTimer();
  updateBadge();
  console.log("[Deal-AutoClick] Background gestartet - Button-Klick im 10s Intervall");
}

function startScanTimer() {
  stopScanTimer();
  // Scannt nun zusätzlich alle 3 Sekunden auf neue Deals und prüft, ob ein Reload nötig ist.
  scanInterval = setInterval(() => {
    scanActiveTabOnce();
    checkForReload();
  }, SCAN_EVERY_MS);
}

function stopScanTimer() {
  if (scanInterval) clearInterval(scanInterval);
  scanInterval = null;
}

function startOpenTimer() {
  stopOpenTimer();
  if (!autoOpen) return;
  openInterval = setInterval(openNextInQueue, OPEN_EVERY_MS);
}

function stopOpenTimer() {
  if (openInterval) clearInterval(openInterval);
  openInterval = null;
}

// *** Funktion: Prüft, ob der aktive Tab mydealz ist und neu geladen werden soll ***
async function checkForReload() {
  if (Date.now() - lastReloadTime < RELOAD_EVERY_MS) {
    return; // Noch nicht Zeit für den nächsten Reload
  }

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

    // Prüfen, ob die URL mydealz enthält und es keine spezielle Chrome-Seite ist
    if (tab?.id && tab.url && (tab.url.includes("mydealz.de") || tab.url.includes("mydealz.com"))) {
      console.log("[Deal-AutoClick] Lade mydealz-Seite neu...");
      chrome.tabs.reload(tab.id);
      lastReloadTime = Date.now(); // Aktualisiere den Zeitstempel

      // Leere die Warteschlange nach dem Neuladen, da alle Deals neu gescannt werden
      queue.length = 0;
      // 'processed' WIRD NICHT GELEERT, da es persistent ist!
    }
  } catch (e) {
    // console.error("[Deal-AutoClick] Reload-Fehler:", e);
  }
}

async function scanActiveTabOnce() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) return;

    const [{ result } = {}] = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: false },
      func: extractArticleTargetsInPage,
    });

    if (!Array.isArray(result)) return;

    for (const item of result) {
      if (!item?.id) continue;
      if (processed.has(item.id)) continue; // **Prüft persistenten Status**
      if (!item.selectorInfo) continue;

      if (!queue.find((q) => q.id === item.id)) {
        queue.push(item);
      }
    }
    updateBadge();
  } catch (e) {
    // console.debug("[Deal-AutoClick] scan error:", e);
  }
}

// *** Klickt den nächsten Button im aktiven Tab ***
async function openNextInQueue() {
  if (!queue.length) return;
  if (!autoOpen) return;

  const item = queue.shift();
  processed.add(item.id);
  saveProcessedIds(); // *** NEU: Speichere den neuen Zustand nach dem Klicken ***

  const selectorInfo = item.selectorInfo;
  if (!selectorInfo) return;

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) {
      console.warn("[Deal-AutoClick] Aktiver Tab nicht verfügbar.");
      return;
    }

    // Führe den Klick im aktiven Tab aus
    const [{ result: clicked } = {}] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: clickDealButton,
      args: [selectorInfo], // Übergabe der Button-Information
    });

    if (clicked) {
      console.log(`[Deal-AutoClick] Button für Artikel ${item.id} erfolgreich geklickt. Neues Tab öffnet sich.`);
    } else {
      console.warn(`[Deal-AutoClick] Button für Artikel ${item.id} nicht gefunden oder Klick fehlgeschlagen.`);
    }
  } catch (e) {
    console.warn("[Deal-AutoClick] Klick fehlgeschlagen:", e);
  }

  updateBadge();
}

// *** KORRIGIERTE Funktion: Führt den Klick im DOM des aktiven Tabs aus (mit robuster Triggerung) ***
function clickDealButton(selectorInfo) {
  // FINALER, ROBUSTER SELEKTOR
  const ALL_DEAL_BUTTONS = 'a[data-t="dealLink"], button[data-t="dealLink"], button.buttonWithCode-button, a.buttonWithCode-button';

  // NEUE, ROBUSTERE KLICK-FUNKTION: Versucht native .click() vor Event-Simulation
  function triggerClick(element) {
    if (!element) return false;

    // 1. Versuche, die native .click() Methode aufzurufen (Am zuverlässigsten)
    try {
      element.click();
      return true;
    } catch (e) {
      // Fallback, wenn native click() fehlschlägt
    }

    // 2. Fallback: Event-Simulation
    try {
      element.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
      element.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
      element.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
      return true;
    } catch {}

    return false;
  }

  try {
    // Artikelsuche
    const article = document.getElementById(selectorInfo.id);
    if (!article) return false;

    // Suche alle relevanten Buttons im Artikel
    const allButtons = article.querySelectorAll(ALL_DEAL_BUTTONS);
    let btn = allButtons[selectorInfo.index];

    if (!btn && allButtons.length > 0) {
      btn = allButtons[0]; // Fallback auf den ersten gefundenen Button
    }

    if (!btn) return false;

    // Klick ausführen mit ROBUSTER Triggerung
    setTimeout(() => {
      try {
        triggerClick(btn); // Aufruf der neuen, robusten Funktion
      } catch {}
    }, 50);

    return true;
  } catch {
    return false;
  }
}

function updateBadge() {
  const text = queue.length ? String(Math.min(queue.length, 99)) : "";
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color: autoOpen ? "#0B874B" : "#999999" });
}

/**
 * Läuft IM TAB und extrahiert aus der Seite alle relevanten Artikel-Ziele.
 * Rückgabe: Array<{ id, selectorInfo? }>
 */
function extractArticleTargetsInPage() {
  const out = [];
  // FINALER, ROBUSTER SELEKTOR
  const ALL_DEAL_BUTTONS = 'a[data-t="dealLink"], button[data-t="dealLink"], button.buttonWithCode-button, a.buttonWithCode-button';

  // 1) Alle Artikel-Karten finden
  const articles = document.querySelectorAll("article.thread.cept-thread-item");
  articles.forEach((article) => {
    // ID wird korrekt aus dem 'id'-Attribut des Artikels gezogen (z.B. "thread_2659628")
    const id = article.getAttribute("id") || article.dataset?.tD || null;
    if (!id) return;

    // 2) Deal-Button suchen und Index speichern
    const dealButtons = article.querySelectorAll(ALL_DEAL_BUTTONS);

    if (dealButtons.length > 0) {
      // Wir nehmen den ersten Button (Index 0)
      out.push({
        id,
        // Speichern der ID und des Index des Buttons, um ihn später gezielt zu klicken
        selectorInfo: { id, index: 0 },
      });
    }
  });

  return out;
}

// Badge initial bei Start
chrome.runtime.onInstalled.addListener(updateBadge);
chrome.runtime.onStartup.addListener(updateBadge);

// Start!
start();
