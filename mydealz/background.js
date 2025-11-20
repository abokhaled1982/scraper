/**
 * Background-Scheduler (Zentral):
 * - Verwaltet die Timing-Intervalle (Scan, Klick, Reload).
 * - Verwaltet den globalen Status (processed IDs, queue, autoOpen).
 * - Sendet Nachrichten an die Content-Skripte im aktiven Tab.
 * - Führt WebSocket- und Tab-Tracking-Logik aus.
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

// Liste der URLs, die neu geladen werden sollen (falls sie der aktive Tab sind)
const RELOAD_PATTERNS = [
  "mydealz.de", "mydealz.com",
  "dealdoktor.de","www.mein-deal.com"
];

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
  setupTabListener();
  connectWebSocket();
  startScanTimer();
  startOpenTimer();
  updateBadge();
  console.log("[Deal-AutoClick] Background gestartet.");
}

async function openNextInQueue() {
  if (!queue.length) return;
  if (!autoOpen) return;

  const item = queue.shift();
  processed.add(item.id);
  saveProcessedIds();

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) {
      console.warn("[Deal-AutoClick] Aktiver Tab nicht verfügbar.");
      return;
    }

    // *** Setze die Flagge VOR dem Senden des Klickbefehls ***
    isDealClicking = true;

    // Sende eine Nachricht an das Content-Skript, um den Klick auszuführen
    const response = await chrome.tabs.sendMessage(tab.id, {
      type: "CLICK_DEAL",
      dealId: item.id
    });

    if (response?.clicked) {
      console.log(`[Deal-AutoClick] Klick auf Artikel ${item.id} ausgelöst. Warte auf neuen Tab...`);
    } else {
      console.warn(`[Deal-AutoClick] Klick fehlgeschlagen oder Seite nicht aktiv.`);
      isDealClicking = false;
    }
  } catch (e) {
    console.warn("[Deal-AutoClick] Klick oder Nachricht fehlgeschlagen:", e.message);
    isDealClicking = false;
  }

  updateBadge();
}

async function scanActiveTabOnce() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || !tab.url) return;

    // Sende eine Nachricht an das Content-Skript, um Deals zu extrahieren
    const response = await chrome.tabs.sendMessage(tab.id, {
      type: "SCAN_DEALS"
    });

    if (!Array.isArray(response?.deals)) return;

    for (const dealId of response.deals) {
      if (!dealId) continue;
      if (processed.has(dealId)) continue;

      // Füge den Deal in die Queue ein (jetzt nur ID)
      if (!queue.find((q) => q.id === dealId)) {
        queue.push({ id: dealId });
      }
    }
    updateBadge();
  } catch (e) {
    // Wenn kein Content-Skript zuhört (andere Website), ist das normal.
    // console.debug("[Deal-AutoClick] Scan-Fehler:", e.message);
  }
}

// --- RESTLICHER CODE (Unverändert, aber auf die neue Logik bezogen) ---

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

// --- POPUP Messages
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

    if (!tab?.id || !tab.url) return;

    // Prüfe, ob die URL neu geladen werden soll
    const shouldReload = RELOAD_PATTERNS.some(pattern => tab.url.includes(pattern));

    if (shouldReload) {
      console.log(`[Deal-AutoClick] Seite neu laden: ${tab.url}...`);
      chrome.tabs.reload(tab.id);
      lastReloadTime = Date.now();
      queue.length = 0; // Leere Queue nach Reload
    }
  } catch (e) {
    // console.error("[Deal-AutoClick] Reload-Fehler:", e);
  }
}

function updateBadge() {
  const text = queue.length ? String(Math.min(queue.length, 99)) : "";
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color: autoOpen ? "#0B874B" : "#999999" });
}

// Badge initial bei Start
chrome.runtime.onInstalled.addListener(updateBadge);
chrome.runtime.onStartup.addListener(updateBadge);

// Start!
start();