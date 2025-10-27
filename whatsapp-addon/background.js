// ===== background.js (lokal, sofortiges Senden) =====

// Lokaler WebSocket-Server (Python-Server aus server.py)
const WS_URL = "ws://127.0.0.1:8765/";
const HEARTBEAT_MS = 30000;
const RECONNECT_MS = 5000;

let sock = null;
let heartbeatTimer = null;
let reconnectTimer = null;
let connecting = false;

// WhatsApp-Tab(s) finden
async function findWATabs() {
  return chrome.tabs.query({ url: "https://web.whatsapp.com/*" });
}

// Code, der in der Seite ausgeführt wird (Einfügen + Senden von Text)
function pageInject(text) {
  // === Funktionen laufen im WhatsApp-Web-DOM ===

  function toast(msg) {
    let t = document.getElementById("waqs-toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "waqs-toast";
      t.style.cssText =
        "position:fixed;left:50%;bottom:18px;transform:translateX(-50%);"+
        "background:#111;color:#fff;padding:10px 14px;border-radius:10px;"+
        "font:14px/1.2 system-ui,sans-serif;z-index:2147483647;opacity:.95";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    setTimeout(() => { t.remove(); }, 1800);
  }

  function findComposer() {
    let el = document.querySelector('footer div[contenteditable="true"][role="textbox"]');
    if (el) return el;
    el = document.querySelector('div[contenteditable="true"][data-tab]');
    if (el) return el;
    return document.querySelector('div[contenteditable="true"]');
  }

  function insertWithLinebreaks(el, text) {
    el.focus();
    el.textContent = "";
    const lines = String(text).split(/\r?\n/);
    for (let i = 0; i < lines.length; i++) {
      if (lines[i]) document.execCommand("insertText", false, lines[i]);
      if (i < lines.length - 1) {
        const brEvent = new KeyboardEvent("keydown", {
          key: "Enter", code: "Enter", keyCode: 13, which: 13,
          bubbles: true, shiftKey: true
        });
        el.dispatchEvent(brEvent);
      }
    }
    el.dispatchEvent(new InputEvent("input", { bubbles: true }));
  }

  function clickSendRobust() {
    const deadline = Date.now() + 1000;
    return new Promise((resolve) => {
      const tryOnce = () => {
        const footer = document.querySelector("footer");
        let ok = false;
        if (footer) {
          const btn =
            footer.querySelector('[data-icon="send"]') ||
            footer.querySelector('button[aria-label*="Senden"]') ||
            footer.querySelector("button[aria-label]");
          if (btn && !btn.disabled) {
            btn.click();
            ok = true;
          }
        }
        if (ok) return resolve(true);
        if (Date.now() > deadline) {
          const active = document.activeElement;
          if (active && active.getAttribute("contenteditable") === "true") {
            const ev = new KeyboardEvent("keydown", {
              key: "Enter", code: "Enter", keyCode: 13,
              which: 13, bubbles: true
            });
            active.dispatchEvent(ev);
            return resolve(true);
          }
          return resolve(false);
        }
        setTimeout(tryOnce, 80);
      };
      tryOnce();
    });
  }

  async function insertAndSend(text) {
    const el = findComposer();
    if (!el) { toast("⚠️ Kein Chat/Eingabefeld gefunden."); return { ok:false }; }
    insertWithLinebreaks(el, text);
    await new Promise(r => setTimeout(r, 80)); 
    const sent = await clickSendRobust();
    toast(sent ? "✅ Nachricht gesendet" : "ℹ️ Eingefügt – bitte senden");
    return { ok: sent };
  }

  insertAndSend(text);
}


// NEUE Funktion für den Base64-Bild-Upload
function pageInjectImage(base64Data, caption) {
  // === Funktionen laufen im WhatsApp-Web-DOM ===

  function toast(msg) {
    let t = document.getElementById("waqs-toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "waqs-toast";
      t.style.cssText =
        "position:fixed;left:50%;bottom:18px;transform:translateX(-50%);"+
        "background:#111;color:#fff;padding:10px 14px;border-radius:10px;"+
        "font:14px/1.2 system-ui,sans-serif;z-index:2147483647;opacity:.95";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    setTimeout(() => { t.remove(); }, 1800);
  }

  // Hilfsfunktion: Base64 in Blob umwandeln
  function b64toBlob(base64, mimeType = '') {
    const parts = base64.split(';base64,');
    if (parts.length > 1) {
        mimeType = parts[0].split(':')[1];
        base64 = parts[1];
    }
    const byteCharacters = atob(base64);
    const byteArrays = [];
    for (let offset = 0; offset < byteCharacters.length; offset += 512) {
      const slice = byteCharacters.slice(offset, offset + 512);
      const byteNumbers = new Array(slice.length);
      for (let i = 0; i < slice.length; i++) {
        byteNumbers[i] = slice.charCodeAt(i);
      }
      byteArrays.push(new Uint8Array(byteNumbers));
    }
    return new Blob(byteArrays, { type: mimeType || 'image/png' });
  }
  
  // Versuch, den WhatsApp-Upload-Prozess zu simulieren
  async function uploadAndSend(base64, captionText) {
    try {
      const imageBlob = b64toBlob(base64);
      // Dateiname mit generischer Endung
      const imageFile = new File([imageBlob], "wa-uploaded-image.png", { type: imageBlob.type });

      // 1. Finde den Anhang-Button und klicke ihn
      // Sucht nach dem Büroklammer-Icon oder dem "Attach Menu Plus"-Icon
      const attachButton = document.querySelector('[data-icon="attach-menu-plus"]') || document.querySelector('[data-icon="clip"]')?.closest('button');
      if (!attachButton) {
        toast("⚠️ Anhang-Button nicht gefunden.");
        return;
      }
      attachButton.click();
      await new Promise(r => setTimeout(r, 300)); // Kurze Pause, damit das Menü erscheint

      // 2. Finde den Datei-Input (versteckt)
      // Selektor für den Media-Upload (erster input[type=file] nach dem Klick)
      const fileInput = document.querySelector('input[type="file"][accept*="image"]') || 
                        document.querySelector('input[type="file"][accept*="video"]');
      if (!fileInput) {
        toast("⚠️ Datei-Input nicht gefunden.");
        return;
      }

      // 3. Füge die Datei in den Input ein
      const dataTransfer = new DataTransfer();
      dataTransfer.items.add(imageFile);
      fileInput.files = dataTransfer.files;
      
      // Simuliere den Change-Event, um den Upload-Dialog zu öffnen
      fileInput.dispatchEvent(new Event('change', { bubbles: true }));

      // 4. Warte auf den Upload-Dialog und füge die Bildunterschrift ein
      let sendButtonInDialog = null;
      const deadline = Date.now() + 5000;
      while (!sendButtonInDialog && Date.now() < deadline) {
        // Versuche, den Sende-Button im Medien-Vorschau-Fenster zu finden
        sendButtonInDialog = document.querySelector('div[role="dialog"] [data-icon="send"]')?.closest('button[role="button"][aria-label]');
        if (sendButtonInDialog) break;
        await new Promise(r => setTimeout(r, 100));
      }
      
      if (!sendButtonInDialog) {
          toast("⚠️ Konnte Sende-Button im Upload-Dialog nicht finden.");
          return;
      }
      
      // 5. Füge die Bildunterschrift ein
      const captionField = document.querySelector('div[contenteditable="true"][role="textbox"][aria-label*="Bildunterschrift"]');
      if (captionField) {
          captionField.focus();
          // Fügt den Text mit Zeilenumbrüchen ein (wie in insertWithLinebreaks)
          const lines = String(captionText).split(/\r?\n/);
          captionField.textContent = "";
          for (let i = 0; i < lines.length; i++) {
              if (lines[i]) document.execCommand("insertText", false, lines[i]);
              if (i < lines.length - 1) {
                  const brEvent = new KeyboardEvent("keydown", {
                      key: "Enter", code: "Enter", keyCode: 13, which: 13,
                      bubbles: true, shiftKey: true // SHIFT+ENTER für Zeilenumbruch
                  });
                  captionField.dispatchEvent(brEvent);
              }
          }
          captionField.dispatchEvent(new InputEvent("input", { bubbles: true }));
      }
      
      // 6. Senden
      sendButtonInDialog.click();
      toast("✅ Bild-Upload versucht!");

    } catch (error) {
      console.error("WhatsApp Image Upload Error:", error);
      toast("❌ Fehler beim Bild-Upload: Siehe Console.");
    }
  }

  // Starte den Prozess
  uploadAndSend(base64Data, caption);
}


// Nachricht in geöffnete WA-Tabs schicken
async function sendToWhatsApp(text) {
  const tabs = await findWATabs();
  if (!tabs.length) {
    console.warn("[WS] Kein WhatsApp-Tab offen – Textnachricht verworfen.");
    return;
  }
  await Promise.all(tabs.map(tab =>
    chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      func: pageInject,
      args: [text]
    })
  ));
}

// NEUE Funktion zum Senden des Base64-Bildes
async function sendImageToWhatsApp(base64Data, caption) {
  const tabs = await findWATabs();
  if (!tabs.length) {
    console.warn("[WS] Kein WhatsApp-Tab offen – Bildnachricht verworfen.");
    return;
  }
  await Promise.all(tabs.map(tab =>
    chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      func: pageInjectImage,
      args: [base64Data, caption]
    })
  ));
}


// WebSocket aufbauen + Events
function connectWS() {
  if (connecting || (sock && (sock.readyState === WebSocket.OPEN || sock.readyState === WebSocket.CONNECTING))) return;
  connecting = true;
  clearTimeout(reconnectTimer);
  try { sock?.close(); } catch {}

  sock = new WebSocket(WS_URL);

  sock.addEventListener("open", () => {
    console.log("[WS] verbunden:", WS_URL);
    connecting = false;
    clearInterval(heartbeatTimer);
    heartbeatTimer = setInterval(() => {
      try { sock?.send(JSON.stringify({ type: "ping", t: Date.now() })); } catch {}
    }, HEARTBEAT_MS);
  });

  sock.addEventListener("message", async (ev) => {
    try {
      const data = typeof ev.data === "string" ? JSON.parse(ev.data) : ev.data;
      if (data && data.type === "send" && data.text) { 
        console.log("[WS] Text-Nachricht empfangen, sende an WhatsApp …");
        await sendToWhatsApp(data.text);
      } else if (data && data.type === "sendImage" && data.base64_data && data.caption) {
        console.log("[WS] Bild-Nachricht empfangen, versuche Upload...");
        await sendImageToWhatsApp(data.base64_data, data.caption);
      } else {
        console.log("[WS] Unbekannte Nachricht:", data);
      }
    } catch (err) {
      console.error("[WS] message error:", err);
    }
  });

  sock.addEventListener("close", () => {
    console.warn("[WS] getrennt – Reconnect in", RECONNECT_MS, "ms");
    connecting = false;
    sock = null;
    clearInterval(heartbeatTimer);
    reconnectTimer = setTimeout(connectWS, RECONNECT_MS);
  });

  sock.addEventListener("error", (e) => {
    console.error("[WS] Fehler:", e);
    connecting = false;
    try { sock?.close(); } catch {}
  });
}

// Startpunkte
chrome.runtime.onStartup.addListener(connectWS);
chrome.runtime.onInstalled.addListener(connectWS);

// Health-Check (sorgt für Reconnects)
chrome.alarms.create("ws-ensure", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name !== "ws-ensure") return;
  if (!sock || sock.readyState === WebSocket.CLOSED) connectWS();
});