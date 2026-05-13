// popup.js â€” immer frischen DOM holen & senden
const $ = id => document.getElementById(id);
let loopAbort = false;

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}
function isInjectable(url) {
  const blocked = [/^chrome:\/\//i, /^edge:\/\//i, /^about:/i, /^view-source:/i, /^chrome-extension:\/\//i, /^https:\/\/chrome\.google\.com\/webstore/i];
  return url && !blocked.some(rx => rx.test(url));
}
async function ensureContent(tabId) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] }).catch(() => {});
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function uniqueFilename(base = "cleaned", ext = "html") {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const stamp = `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}_${pad(d.getHours())}-${pad(d.getMinutes())}-${pad(d.getSeconds())}`;
  const rnd = Math.random().toString(16).slice(2,6).toUpperCase();
  return `${base}-${stamp}_${rnd}.${ext}`;
}

async function downloadHtml(content, hintName = "cleaned") {
  const filename = uniqueFilename(hintName, "html");
  await chrome.runtime.sendMessage({
    type: "DOWNLOAD_FILE",
    filename,
    content,
    mime: "text/html;charset=utf-8"
  });
  return filename;
}

async function oneShotScrollAndWait(tabId) {
  await chrome.tabs.sendMessage(tabId, { type: "START_AUTOSCROLL" });
}

async function runRandomActions(tabId) {
  return await chrome.tabs.sendMessage(tabId, { type: "RUN_RANDOM_ACTIONS" });
}

// === Zyklus: (Action -> Scroll -> Parse) â†’ send+download ===
async function runSequenceLoop(tabId) {
  const status = $("status");
  const preview = $("preview");
  let cycle = 0;

  while (!loopAbort) {
    cycle++;
    status.textContent = `Runningâ€¦ (Cycle ${cycle})`;

    // 1) Interagieren
    await runRandomActions(tabId);
    if (loopAbort) break;

    // 2) Einmal scrollen & warten (damit DOM sich Ã¤ndert)
    await oneShotScrollAndWait(tabId);
    if (loopAbort) break;

    // 3) ***FRISCH PARSEN***
    const res = await chrome.tabs.sendMessage(tabId, { type: "RUN_SANITIZER" });
    if (!res?.ok) throw new Error(res?.error || "Sanitizer failed");
    const freshHtml = res.result?.output?.html || "";

    // Vorschau anzeigen
    preview.textContent = freshHtml.slice(0, 500);

    // 4) Senden + Downloaden (immer mit frischem HTML)
    const tabInfo = await chrome.tabs.get(tabId);
    await chrome.runtime.sendMessage({ type: "PARSED_HTML", payload: { url: tabInfo.url, html: freshHtml } });
    //await downloadHtml(freshHtml, "cleaned");

    // kleine Pause
    await sleep(30000);
  }

  status.textContent = loopAbort ? "Stopped." : "Done.";
  setTimeout(() => (status.textContent = ""), 1500);
}

// === Buttons ===
$("run").onclick = async () => {
  const status = $("status");
  const preview = $("preview");
  status.textContent = "Startingâ€¦";
  preview.textContent = "";
  loopAbort = false;

  try {
    const tab = await getActiveTab();
    if (!tab?.id || !isInjectable(tab.url)) {
      throw new Error("Ã–ffne eine http/https-Seite (nicht chrome:// oder Web Store).");
    }
    await ensureContent(tab.id);

    // direkt Loop starten (kein lastHtml mehr)
    await runSequenceLoop(tab.id);

  } catch (e) {
    console.warn(e);
    status.textContent = "Error: " + e.message;
  }
};

$("download").onclick = async () => {
  // On-demand: frisch parsen und dann downloaden
  const tab = await getActiveTab();
  if (!tab?.id || !isInjectable(tab.url)) return;
  await ensureContent(tab.id);
  const res = await chrome.tabs.sendMessage(tab.id, { type: "RUN_SANITIZER" });
  if (!res?.ok) {
    $("status").textContent = "Sanitizer failed.";
    return;
  }
  const freshHtml = res.result?.output?.html || "";
  await downloadHtml(freshHtml, "cleaned");
};

$("scroll").onclick = async () => {
  const tab = await getActiveTab();
  if (!tab?.id || !isInjectable(tab.url)) return;
  await ensureContent(tab.id);
  await chrome.tabs.sendMessage(tab.id, { type: "START_AUTOSCROLL" });
};

$("stopscroll").onclick = async () => {
  const tab = await getActiveTab();
  if (!tab?.id || !isInjectable(tab.url)) return;
  await ensureContent(tab.id);
  await chrome.tabs.sendMessage(tab.id, { type: "STOP_AUTOSCROLL" });
};

window.addEventListener("unload", () => { loopAbort = true; });