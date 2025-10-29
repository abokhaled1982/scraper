/**
 * Background-Scheduler: scannt den aktiven Tab nach Artikel-Deals
 * und führt alle 10s einen Klick auf den nächsten Deal-Button (im aktiven Tab) aus.
 *
 * - Scannen: alle 3 Sekunden.
 * - Klick: alle 10 Sekunden.
 * - PERSISTENZ: Verarbeitete Deal-IDs werden in chrome.storage.local gespeichert.
 * - AUTO-RELOAD: Aktiver mydealz-Tab wird alle 30 Sekunden neu geladen.
 * - WEBSOCKET: Sendet die finale URL des neuen Tabs an einen Server.
 */

// --- KONFIGURATION ---
const WEBSOCKET_URL = "ws://localhost:8765"; // 🎯 PASSE DIESE URL AN DEINEN SERVER AN!

let autoOpen = true;
let openInterval = null;
let scanInterval = null;
const OPEN_EVERY_MS = 10_000;
const SCAN_EVERY_MS = 3_000;
const RELOAD_EVERY_MS = 30_000;
const STORAGE_KEY = "processed_deal_ids";

let lastReloadTime = 0;
let isDealClicking = false; // Flag, dass ein neuer Tab erwartet wird
let expectedTabId = null; // ID des erwarteten neuen Tabs
let websocket = null; // Das WebSocket-Objekt

const processed = new Set();
const queue = [];

// -----------------------------------------------------
// ### 1. WEBSOCKET LOGIK
// -----------------------------------------------------

function connectWebSocket() {
  if (websocket && websocket.readyState === WebSocket.OPEN) {
    return;
  }

  console.log("[WS] Versuche Verbindung zu starten...");
  websocket = new WebSocket(WEBSOCKET_URL);

  websocket.onopen = () => {
    console.log("[WS] Verbindung erfolgreich hergestellt.");
  };

  websocket.onclose = () => {
    console.warn("[WS] Verbindung geschlossen. Versuche in 5s erneut zu verbinden.");
    websocket = null;
    setTimeout(connectWebSocket, 5000); // Automatischer Reconnect
  };

  websocket.onerror = (error) => {
    console.error("[WS] WebSocket Fehler:", error);
    // onclose wird normalerweise danach aufgerufen
  };
}

function sendUrlViaWebSocket(url) {
  if (websocket && websocket.readyState === WebSocket.OPEN) {
    const message = JSON.stringify({
      type: "product_url",
      url: url,
      timestamp: new Date().toISOString(),
    });
    websocket.send(message);
    console.log(`[WS] URL gesendet: ${url}`);
  } else {
    console.warn("[WS] Verbindung nicht offen. URL konnte nicht gesendet werden.");
    // Man könnte hier die URL cachen und beim Reconnect senden
  }
}

// -----------------------------------------------------
// ### 2. TAB TRACKING LOGIK
// -----------------------------------------------------

function setupTabListener() {
  chrome.tabs.onCreated.addListener(handleNewTab);
}

function handleNewTab(tab) {
  if (!isDealClicking) {
    return;
  }

  // Flag zurücksetzen, da ein Tab geöffnet wurde
  isDealClicking = false;

  // Speichere die ID, um das Laden zu verfolgen
  expectedTabId = tab.id;

  // Wir warten auf das 'complete' Status-Update
  chrome.tabs.onUpdated.addListener(logFinalUrlOnce);
}

function logFinalUrlOnce(tabId, changeInfo, tab) {
  if (tabId === expectedTabId && changeInfo.status === "complete") {
    const finalUrl = tab.url;

    // URL über WebSocket senden
    sendUrlViaWebSocket(finalUrl);

    // Den neuen Tab schließen, nachdem die URL gesendet wurde
    setTimeout(() => {
      chrome.tabs.remove(tabId, () => {
        console.log(`[Deal-AutoClick] Tab ${tabId} (${finalUrl}) geschlossen.`);
      });
    }, 1000); // 1 Sekunde warten, um sicherzustellen, dass alles geladen ist

    // Listener entfernen und Reset
    chrome.tabs.onUpdated.removeListener(logFinalUrlOnce);
    expectedTabId = null;
  }
}


// -----------------------------------------------------
// ### 3. HAUPT-SCHEDULER LOGIK
// -----------------------------------------------------

function start() {
  loadProcessedIds();
  setupTabListener(); // Tab-Tracking einrichten
  connectWebSocket(); // WebSocket-Verbindung starten
  startScanTimer();
  startOpenTimer();
  updateBadge();
  console.log("[Deal-AutoClick] Background gestartet.");
}

// *** openNextInQueue MODIFIZIERT, um isDealClicking zu setzen ***
async function openNextInQueue() {
  if (!queue.length) return;
  if (!autoOpen) return;

  const item = queue.shift();
  processed.add(item.id);
  saveProcessedIds();

  const selectorInfo = item.selectorInfo;
  if (!selectorInfo) return;

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) {
      console.warn("[Deal-AutoClick] Aktiver Tab nicht verfügbar.");
      return;
    }

    // *** Setze die Flagge VOR dem Klick ***
    isDealClicking = true;

    // Führe den Klick im aktiven Tab aus
    const [{ result: clicked } = {}] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: clickDealButton,
      args: [selectorInfo],
    });

    if (clicked) {
      console.log(`[Deal-AutoClick] Klick auf Artikel ${item.id} ausgelöst. Warte auf neuen Tab...`);
    } else {
      console.warn(`[Deal-AutoClick] Klick fehlgeschlagen.`);
      isDealClicking = false;
    }
  } catch (e) {
    console.warn("[Deal-AutoClick] Klick fehlgeschlagen:", e);
    isDealClicking = false;
  }

  updateBadge();
}

// --- RESTLICHER VORHANDENER CODE (unverändert) ---

// --- Storage Funktionen ---

async function loadProcessedIds() {
  const result = await chrome.storage.local.get([STORAGE_KEY]);
  const ids = result[STORAGE_KEY] || [];
  ids.forEach((id) => processed.add(id));
  updateBadge();
  console.log(`[Deal-AutoClick] ${processed.size} verarbeitete Deals aus dem Speicher geladen.`);
}

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

function startScanTimer() {
  stopScanTimer();
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

async function checkForReload() {
  if (Date.now() - lastReloadTime < RELOAD_EVERY_MS) {
    return;
  }

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

    if (tab?.id && tab.url && (tab.url.includes("mydealz.de") || tab.url.includes("mydealz.com"))) {
      console.log("[Deal-AutoClick] Lade mydealz-Seite neu...");
      chrome.tabs.reload(tab.id);
      lastReloadTime = Date.now();

      queue.length = 0;
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
      if (processed.has(item.id)) continue;
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

// *** KORRIGIERTE Funktion: Führt den Klick im DOM des aktiven Tabs aus (mit robuster Triggerung) ***
function clickDealButton(selectorInfo) {
  // FINALER, ROBUSTER SELEKTOR
  const ALL_DEAL_BUTTONS = 'a[data-t="dealLink"], button[data-t="dealLink"], button.buttonWithCode-button, a.buttonWithCode-button';

  // NEUE, ROBUSTERE KLICK-FUNKTION: Versucht native .click() vor Event-Simulation
  function triggerClick(element) {
    if (!element) return false;
    try {
      element.click();
      return true;
    } catch (e) {}
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
    const id = article.getAttribute("id") || article.dataset?.tD || null;
    if (!id) return;

    // 2) Deal-Button suchen und Index speichern
    const dealButtons = article.querySelectorAll(ALL_DEAL_BUTTONS);

    if (dealButtons.length > 0) {
      out.push({
        id,
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
