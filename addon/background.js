// background.js — PRO/STABLE: streaming with begin_ack, saved-ACK, dedupe-after-save, optional retry
const WS_URL = "ws://127.0.0.1:8765";

let ws = null;
let alive = false;
let outbox = [];
let reconnectWaitMs = 1000; // exp backoff: 1s .. 30s (Resets on successful open)

// ---- Heartbeat + reconnect ----
chrome.runtime.onInstalled.addListener(() => chrome.alarms?.create("hb", { periodInMinutes: 1 }));
chrome.runtime.onStartup.addListener(() => chrome.alarms?.create("hb", { periodInMinutes: 1 }));
chrome.alarms?.onAlarm.addListener((a) => {
  if (a.name === "hb") ensureWS();
});

// === Ergänzung: bei Installation/Startup Content Script in bereits offene Amazon-Tabs injizieren
chrome.runtime.onInstalled.addListener(async () => {
  try {
    const tabs = await chrome.tabs.query({ url: "*://*.amazon.*/*" });
    for (const tab of tabs) {
      if (!tab.id) continue;
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["content.js"],
      });
    }
  } catch (e) {
    console.warn("[bg] inject on install failed:", e);
  }
});

chrome.runtime.onStartup.addListener(async () => {
  try {
    const tabs = await chrome.tabs.query({ url: "*://*.amazon.*/*" });
    for (const tab of tabs) {
      if (!tab.id) continue;
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["content.js"],
      });
    }
  } catch (e) {
    console.warn("[bg] inject on startup failed:", e);
  }
});

function ensureWS() {
  if (alive && ws?.readyState === WebSocket.OPEN) return;
  try {
    ws?.close();
  } catch {}
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    alive = true;
    reconnectWaitMs = 1000; // reset backoff
    flush();
    console.log("[WS] connected");
  };

  ws.onclose = () => {
    alive = false;
    console.log("[WS] closed");
    setTimeout(() => ensureWS(), Math.min((reconnectWaitMs *= 1.8), 30_000));
  };

  ws.onerror = (e) => {
    alive = false;
    console.warn("[WS] error", e);
    try {
      ws.close();
    } catch {}
  };

  // PRO: parse structured responses; react to begin_ack/ack/saved/errors
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(String(ev.data || ""));
      if (msg?.type === "begin_ack" && msg?.id) {
        console.log("[WS] begin_ack", msg.id);
        return;
      }
      if (msg?.type === "ack" && msg?.id) {
        console.log("[WS] ack seq", msg.seq, "id", msg.id);
        return;
      }
      if (msg?.ok && msg?.saved && msg?.id) {
        console.log("[WS] saved", msg.id, msg.saved);
        markSent(msg.id);
        return;
      }
      if (msg?.ok === false && msg?.error) {
        console.warn("[WS] server error:", msg.error, msg);
        if (msg.error === "missing_chunks" && msg.id && lastPayloadById.has(msg.id)) {
          const { url, html, meta } = lastPayloadById.get(msg.id);
          console.warn("[WS] retry due to missing_chunks; resending with new id");
          inFlight.delete(msg.id);
          recentlySent.delete(msg.id);
          sendHTMLAsStream(url, html, {
            ...meta,
            retry: true,
            salt: String(Date.now()),
          });
        }
        return;
      }
      console.log("[WS] other:", msg);
    } catch {
      console.log("[WS] echo:", String(ev.data).slice(0, 200));
    }
  };
}

function sendRaw(str) {
  if (alive && ws?.readyState === WebSocket.OPEN) ws.send(str);
  else outbox.push(str);
}
function flush() {
  while (outbox.length && ws?.readyState === WebSocket.OPEN) ws.send(outbox.shift());
}

// ---- chunking (UTF-8 → base64) ----
function chunkUtf8Base64(str, bytesPerChunk = 60_000) {
  const enc = new TextEncoder();
  const buf = enc.encode(str);
  const chunks = [];
  for (let i = 0; i < buf.length; i += bytesPerChunk) {
    const slice = buf.slice(i, i + bytesPerChunk);
    let bin = "";
    for (let j = 0; j < slice.length; j++) bin += String.fromCharCode(slice[j]);
    chunks.push(btoa(bin));
  }
  return { totalBytes: buf.length, chunks };
}

// ---- stabile, content-basierte ID (url + docType + salt) ----
async function makeStableId(url, html, salt = "", docType = "") {
  const enc = new TextEncoder();
  // Stabil und steuerbar: nur URL + docType + salt
  const data = enc.encode((url || "") + "|" + (docType || "") + "|" + (salt || ""));
  const buf = await crypto.subtle.digest("SHA-1", data);
  const hex = [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
  return hex.slice(0, 16);
}

// ---- send stream (await begin_ack) ----
async function sendLargeAsChunksWithIdAwait(id, payload, meta = {}) {
  const { html, url } = payload || {};
  if (typeof html !== "string") return;

  const { totalBytes, chunks } = chunkUtf8Base64(html);

  sendRaw(
    JSON.stringify({
      type: "begin",
      id,
      total: chunks.length,
      totalBytes,
      url,
      ...meta,
    })
  );

  await waitForBeginAck(id, 5000);

  chunks.forEach((b64, idx) => {
    sendRaw(
      JSON.stringify({
        type: "chunk",
        id,
        seq: idx,
        total: chunks.length,
        encoding: "base64",
        url,
        data: b64,
      })
    );
  });

  sendRaw(JSON.stringify({ type: "end", id, url, ...meta }));
}

function waitForBeginAck(id, timeoutMs = 5000) {
  return new Promise((resolve) => {
    let done = false;
    const onMsg = (ev) => {
      try {
        const msg = JSON.parse(String(ev.data || ""));
        if (msg?.type === "begin_ack" && msg?.id === id) {
          done = true;
          ws?.removeEventListener("message", onMsg);
          resolve();
        }
      } catch {}
    };
    ws?.addEventListener("message", onMsg);
    setTimeout(() => {
      if (!done) {
        ws?.removeEventListener("message", onMsg);
        console.warn("[WS] begin_ack timeout; continuing anyway", id);
        resolve();
      }
    }, timeoutMs);
  });
}

// ---- dedupe: only mark after server 'saved' ----
const inFlight = new Set();
const recentlySent = new Map();
const RECENT_WINDOW_MS = 120_000; // 2 Minuten: schützt vor schnellen Doppelevents

function markSent(id) {
  inFlight.delete(id);
  recentlySent.set(id, Date.now());
  for (const [k, ts] of [...recentlySent]) {
    if (Date.now() - ts > RECENT_WINDOW_MS) recentlySent.delete(k);
  }
}

// keep last payload for retry
const lastPayloadById = new Map();

// send html helper (mit docType + optional salt)
async function sendHTMLAsStream(url, html, meta = {}) {
  ensureWS();
  if (typeof html !== "string" || !html.length) return { ok: false, error: "empty_html" };
  const id = await makeStableId(url, html, meta.salt || "", meta.docType || "");

  if (inFlight.has(id)) return { ok: true, deduped: "in_flight", id };
  const last = recentlySent.get(id);
  if (last && Date.now() - last < RECENT_WINDOW_MS) return { ok: true, deduped: "recent", id };

  inFlight.add(id);
  lastPayloadById.set(id, { url, html, meta });

  await sendLargeAsChunksWithIdAwait(id, { url, html }, meta);
  return { ok: true, id };
}

// ---- extension message entrypoint ----
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg?.type === "PARSED_HTML") {
      const { url, html } = msg.payload || {};
      const res = await sendHTMLAsStream(url, html, {
        source: "ext",
        docType: "generic",
      });
      sendResponse(res);
      return;
    }

    if (msg?.type === "PRODUCT_HTML") {
      const { url, html } = msg.payload || {};
      const res = await sendHTMLAsStream(url, html, {
        source: "ext",
        docType: "product",
      });
      sendResponse(res);
      return;
    }

    if (msg?.type === "CLOSE_CURRENT_TAB") {
      const tabId = _sender.tab?.id;
      if (tabId) {
        // Schließe den Tab, der die Nachricht gesendet hat
        await chrome.tabs.remove(tabId).catch(e => console.error("Error closing tab:", e));
        console.log(`[bg] Closed tab with ID: ${tabId}`);
        sendResponse({ ok: true, closed: tabId });
        return;
      }
      sendResponse({ ok: false, error: "no_tab_id" });
      return;
    }

    if (msg?.type === "DOWNLOAD_FILE") {
      const { filename, content, mime = "text/html;charset=utf-8" } = msg;
      try {
        const blob = new Blob([content], { type: mime });
        const u = globalThis.URL || self.URL;
        if (u && typeof u.createObjectURL === "function") {
          const blobUrl = u.createObjectURL(blob);
          chrome.downloads.download({ url: blobUrl, filename }, () => u.revokeObjectURL(blobUrl));
        } else {
          const fr = new FileReader();
          fr.onload = () => chrome.downloads.download({ url: fr.result, filename });
          fr.readAsDataURL(blob);
        }
        sendResponse({ ok: true });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
      return;
    }
  })();
  return true;
});

ensureWS();
