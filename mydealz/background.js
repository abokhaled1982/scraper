/**
 * Background-Scheduler: scannt den aktiven Tab nach Artikel-Deals
 * und führt alle 10s einen Klick auf den nächsten Deal-Button (im aktiven Tab) aus.
 *
 * - Scannen: alle 3 Sekunden.
 * - Klick: alle 10 Sekunden, direkt auf den Button im aktiven Tab.
 * - KORREKTUR: Vereinfachte Artikelsuche (getElementById) und Klick-Simulation für Robustheit.
 */

let autoOpen = true;
let openInterval = null; // 10s-Intervall
let scanInterval = null; // 3s-Intervall
const OPEN_EVERY_MS = 10_000;
const SCAN_EVERY_MS = 3_000;

const processed = new Set(); // Artikel-IDs, die bereits "bedient" wurden
const queue = []; // { id, selectorInfo: { id, index } }

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
  startScanTimer();
  startOpenTimer();
  updateBadge();
  console.log("[Deal-AutoClick] Background gestartet - Button-Klick im 10s Intervall");
}

function startScanTimer() {
  stopScanTimer();
  scanInterval = setInterval(scanActiveTabOnce, SCAN_EVERY_MS);
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

// *** Klickt den nächsten Button im aktiven Tab ***
async function openNextInQueue() {
  if (!queue.length) return;
  if (!autoOpen) return;

  const item = queue.shift();
  processed.add(item.id);

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

// *** KORRIGIERTE Funktion: Führt den Klick im DOM des aktiven Tabs aus (mit Simulation) ***
function clickDealButton(selectorInfo) {
  // FINALER, ROBUSTER SELEKTOR
  const ALL_DEAL_BUTTONS = 'a[data-t="dealLink"], button[data-t="dealLink"], button.buttonWithCode-button, a.buttonWithCode-button';

  // Hilfsfunktion zur Simulation eines echten Mausklicks
  function simulateRealClick(element) {
    if (!element) return false;

    // Senden der Events, um JS-Listener auszulösen
    element.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
    element.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
    element.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));

    return true;
  }

  try {
    // KORRIGIERTE SUCHE: Verlässt sich nur auf document.getElementById, da der Fallback fehlerhaft war.
    const article = document.getElementById(selectorInfo.id);
    if (!article) return false;

    // Suche alle relevanten Buttons im Artikel
    const allButtons = article.querySelectorAll(ALL_DEAL_BUTTONS);
    let btn = allButtons[selectorInfo.index];

    if (!btn && allButtons.length > 0) {
      btn = allButtons[0]; // Fallback auf den ersten gefundenen Button
    }

    if (!btn) return false;

    // Klick ausführen mit SIMULATION
    setTimeout(() => {
      try {
        simulateRealClick(btn);
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
