// ===== background.js (lokal, sofortiges Senden - Bereinigt) =====

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
        "position:fixed;left:50%;bottom:18px;transform:translateX(-50%);" +
        "background:#111;color:#fff;padding:10px 14px;border-radius:10px;" +
        "font:14px/1.2 system-ui,sans-serif;z-index:2147483647;opacity:.95";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    setTimeout(() => {
      t.remove();
    }, 1800);
  }

  function findComposer() {
    let el = document.querySelector(
      'footer div[contenteditable="true"][role="textbox"]'
    );
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
          key: "Enter",
          code: "Enter",
          keyCode: 13,
          which: 13,
          bubbles: true,
          shiftKey: true,
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
              key: "Enter",
              code: "Enter",
              keyCode: 13,
              which: 13,
              bubbles: true,
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
    if (!el) {
      toast("⚠️ Kein Chat/Eingabefeld gefunden.");
      return { ok: false };
    }
    insertWithLinebreaks(el, text);
    await new Promise((r) => setTimeout(r, 80));
    const sent = await clickSendRobust();
    toast(sent ? "✅ Nachricht gesendet" : "ℹ️ Eingefügt – bitte senden");
    return { ok: sent };
  }

  insertAndSend(text);
}

// Code, der in der Seite ausgeführt wird (Öffnen des Foto/Video-Dialogs)
function pageOpenPhotoVideo() {
  function toast(msg) {
    let t = document.getElementById("waqs-toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "waqs-toast";
      t.style.cssText =
        "position:fixed;left:50%;bottom:20px;transform:translateX(-50%);" +
        "background:#111;color:#fff;padding:10px 14px;border-radius:10px;" +
        "font:14px/1.2 system-ui,sans-serif;z-index:999999999;opacity:.95";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    setTimeout(() => t.remove(), 2000);
  }

  (async () => {
    // === 1️⃣ Plus-Button öffnen ===
    let plusBtn = document.querySelector(
      'div[role="button"] span[data-icon="plus-rounded"]'
    );
    if (plusBtn)
      plusBtn = plusBtn.closest('div[role="button"],button,[tabindex]');
    if (!plusBtn) {
      toast("⚠️ Plus-Button nicht gefunden");
      return;
    }

    plusBtn.click();
    toast("➕ Menü geöffnet …");
    await new Promise((r) => setTimeout(r, 400));

    // === 2️⃣ Dropdown finden ===
    // Warte kurz, bis das Menü gerendert ist
    let tries = 0;
    let menuRoot;
    while (tries < 10 && !menuRoot) {
      menuRoot = document.querySelector('div[role="application"] ul');
      if (!menuRoot) {
        await new Promise((r) => setTimeout(r, 150));
        tries++;
      }
    }
    if (!menuRoot) {
      toast("⚠️ Menü nicht gefunden");
      return;
    }

    // === 3️⃣ Option „Fotos & Videos“ finden ===
    let photoOption =
      menuRoot.querySelector('li span[data-icon="media-filled-refreshed"]') ||
      [...menuRoot.querySelectorAll("li")].find((li) =>
        /foto|video/i.test(li.innerText || "")
      );

    if (!photoOption) {
      toast("⚠️ 'Fotos & Videos' nicht gefunden");
      return;
    }

    // === 4️⃣ Klicken ===
    const clickable = photoOption.closest(
      'li[role="button"],div[role="button"],button,[tabindex]'
    );
    if (clickable) {
      clickable.click();
      toast("📂 'Fotos & Videos' geöffnet");
    } else {
      toast("⚠️ Klickbares Element nicht gefunden");
    }
  })();
}


// Nachricht in geöffnete WA-Tabs schicken
async function sendToWhatsApp(text) {
  const tabs = await findWATabs();
  if (!tabs.length) {
    console.warn("[WS] Kein WhatsApp-Tab offen – Textnachricht verworfen.");
    return;
  }
  await Promise.all(
    tabs.map((tab) =>
      chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: "MAIN",
        func: pageInject,
        args: [text],
      })
    )
  );
}

// WebSocket aufbauen + Events
function connectWS() {
  if (
    connecting ||
    (sock &&
      (sock.readyState === WebSocket.OPEN ||
        sock.readyState === WebSocket.CONNECTING))
  )
    return;
  connecting = true;
  clearTimeout(reconnectTimer);
  try {
    sock?.close();
  } catch {}

  sock = new WebSocket(WS_URL);

  sock.addEventListener("open", () => {
    console.log("[WS] verbunden:", WS_URL);
    connecting = false;
    clearInterval(heartbeatTimer);
    // Heartbeat beibehalten, um die Verbindung offen zu halten
    heartbeatTimer = setInterval(() => {
      try {
        sock?.send(JSON.stringify({ type: "ping", t: Date.now() }));
      } catch {}
    }, HEARTBEAT_MS);
  });

sock.addEventListener("message", async (ev) => {
  try {
    const data = typeof ev.data === "string" ? JSON.parse(ev.data) : ev.data;

    // Nur "send" (Text) und "openMediaPicker" werden unterstützt
    if (data && data.type === "send" && data.text) {
      console.log("[WS] Text-Nachricht empfangen, sende an WhatsApp …");
      await sendToWhatsApp(data.text);
    } else if (data && data.type === "openMediaPicker") {
      console.log("[WS] Öffne Plus → Fotos & Videos …");
      
      const tabs = await findWATabs();
      if (!tabs.length) {
        console.warn("[WS] Kein WhatsApp-Tab offen – openMediaPicker verworfen.");
        return;
      }
      await Promise.all(
        tabs.map((tab) =>
          chrome.scripting.executeScript({
            target: { tabId: tab.id },
            world: "MAIN",
            func: pageOpenPhotoVideo, // wird im DOM ausgeführt
          })
        )
      );
    } else {
      console.log("[WS] Unbekannte Nachricht verworfen:", data);
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
    try {
      sock?.close();
    } catch {}
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