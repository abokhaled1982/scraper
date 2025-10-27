const btn = document.getElementById("send");
const msgEl = document.getElementById("msg");

btn.addEventListener("click", async () => {
  const text = (msgEl.value || "").trim();
  if (!text) return;

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return alert("Kein aktiver Tab gefunden.");

  try {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN", // <-- wichtig: im Haupt-DOM laufen lassen
      func: (text) => {
        // ===== läuft im Seitenscope von web.whatsapp.com =====

        // kleine Toast-Hilfe für sichtbares Feedback
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
          }, 2000);
        }

        function findComposer() {
          // häufigstes Target
          let el = document.querySelector(
            'footer div[contenteditable="true"][role="textbox"]'
          );
          if (el) return el;
          // Alternativen
          el = document.querySelector('div[contenteditable="true"][data-tab]');
          if (el) return el;
          // Fallback
          return document.querySelector('div[contenteditable="true"]');
        }

        function insertText(el, text) {
          el.focus();
          el.textContent = ""; // Feld leeren

          // Jede Zeile einzeln mit Shift+Enter einfügen
          const lines = text.split(/\r?\n/);
          for (let i = 0; i < lines.length; i++) {
            if (lines[i]) {
              document.execCommand("insertText", false, lines[i]);
            }
            if (i < lines.length - 1) {
              // echten Zeilenumbruch erzeugen
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

        function clickSend() {
          const footer = document.querySelector("footer");
          if (footer) {
            const btn =
              footer.querySelector('[data-icon="send"]') ||
              footer.querySelector('button[aria-label*="Senden"]') ||
              footer.querySelector("button[aria-label]");
            if (btn) {
              btn.click();
              return true;
            }
          }
          // Fallback: Enter (nur wenn in WhatsApp „Enter = Senden“ aktiv ist)
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
            return true;
          }
          return false;
        }

        const el = findComposer();
        if (!el) {
          toast("⚠️ Chat nicht geöffnet oder Eingabefeld nicht gefunden.");
          return;
        }
        insertText(el, text);
        const sent = clickSend();
        toast(
          sent ? "✅ Nachricht gesendet" : "ℹ️ Eingefügt – bitte selbst senden"
        );
      },
      args: [text],
    });
  } catch (e) {
    alert("Fehler beim Einfügen/Senden: " + (e && e.message ? e.message : e));
    console.error(e);
  }
});
