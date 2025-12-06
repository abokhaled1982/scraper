// content.js
// ==================================================
// Logik für Amazon-Seiten (Produkt- oder Deals-Seiten).
// - Produktseiten: Versendet HTML, wartet auf SiteStripe-Link, und schließt den Tab bei Erfolg.
// - Deals-Seiten: Startet Auto-Scroll, versendet HTML regelmäßig, aber schließt den Tab NICHT.
// - Opener-Trigger: Erzwingt Senden, wartet auf SiteStripe-Link (falls Produktseite) und schließt den Tab.
// ==================================================

(async () => {
  // --- Konstanten und Konfiguration ---
  const TRIGGER_PARAM = "ext_trigger";
  const TRIGGER_VALUE = "send_html";
  const MIN_RUN_INTERVAL_MS = 60_000; // Max. alle 60s pro URL
  const STRIPE_BUTTON_SEL = "#amzn-ss-get-link-button, .amzn-ss-get-link-button";
  const AFFILIATE_LINK_SEL = "#amzn-ss-text-shortlink-textarea, .amzn-ss-get-shortlink-text";

  // --- Zustandsvariablen ---
  let autoScrollInterval = null;
  let lastRunAt = 0;
  let lastRunUrl = "";
  const triggerConsumedForUrl = new Set();
  const clickedOnceForUrl = new Set();
  const linkReadyForUrl = new Set();

  // --- Dienstprogramme (Utils) ---

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /** Überprüft, ob ein Element sichtbar ist (hat einen Offset-Parent oder Client-Rechtecke). */
  function isVisible(el) {
    if (!el) return false;
    const rects = el.getClientRects?.();
    return !!(el.offsetParent !== null || (rects && rects.length));
  }

  /**
   * Sucht einen Selektor im Hauptdokument und in allen same-origin iFrames.
   * @param {Document} doc - Das zu durchsuchende Dokument.
   * @param {string} selector - Der CSS-Selektor.
   * @returns {Element|null} Das gefundene Element oder null.
   */
  function findElementInDocAndIframes(doc, selector) {
    if (!doc) return null;
    let el = doc.querySelector(selector);
    if (el) return el;

    const iframes = doc.querySelectorAll("iframe");
    for (const f of iframes) {
      try {
        const idoc = f.contentDocument || f.contentWindow?.document;
        if (!idoc) continue;
        el = idoc.querySelector(selector);
        if (el) return el;
      } catch {
        // cross-origin: ignorieren
      }
    }
    return null;
  }

  // --- Amazon-Erkennung ---

  const isAmazonHost = (h = location.hostname) => /^([a-z0-9-]+\.)*amazon\.[a-z.]+$/i.test(h);
  const isAmazonProductPath = (p = location.pathname) => /(\/dp\/[A-Z0-9]{10})(\/|$)/i.test(p) || /(\/gp\/product\/[A-Z0-9]{10})(\/|$)/i.test(p);
  const isAmazonDealsPath = (p = location.pathname) => /^\/(deals|gp\/angebote)/i.test(p);
  const isAmazonTargetPage = () => isAmazonHost() && (isAmazonProductPath() || isAmazonDealsPath());

  function hasOpenerTrigger(href = location.href) {
    try {
      const u = new URL(href, location.origin);
      return u.searchParams.get(TRIGGER_PARAM) === TRIGGER_VALUE;
    } catch {
      return false;
    }
  }

  // --- Auto-Scroll (nur für Deals) ---

  function startAutoScroll() {
     const interval = 30_000 + (200 + Math.random() * 400);
    if (autoScrollInterval) return;
    autoScrollInterval = setInterval(() => {
      // Humanizer: 800px scrollen mit leicht variablem Intervall
     
      window.scrollBy({ top: 800, behavior: "smooth" });
    }, interval); // Intervall ist 8s + jitter
    console.log("[AutoScroll] started (deals)");
  }

  function stopAutoScroll() {
    if (!autoScrollInterval) return;
    clearInterval(autoScrollInterval);
    autoScrollInterval = null;
    console.log("[AutoScroll] stopped");
  }

  // --- SiteStripe-Link-Logik (für Produktseiten) ---

  /** Wartet auf den SiteStripe-Button. */
  async function waitForStripeButton(timeoutMs = 12_000, intervalMs = 400) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const btn = findElementInDocAndIframes(document, STRIPE_BUTTON_SEL);
      if (btn) return btn;
      await sleep(intervalMs);
    }
    return null;
  }

  /** Extrahiert den Linkwert aus einem Element. */
  function extractAffiliateValue(el) {
    return (el?.value || el?.textContent || "").trim();
  }

  /** Wartet auf das befüllte, sichtbare Affiliate-Link-Element. */
  async function waitForAffiliateLink(timeoutMs = 25_000, intervalMs = 250) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const el = findElementInDocAndIframes(document, AFFILIATE_LINK_SEL);
      if (el && isVisible(el)) {
        const val = extractAffiliateValue(el);
        // Validierung des Link-Inhalts
        if (/(https?:\/\/|amzn\.to|tag=)/i.test(val)) {
          return { el, val };
        }
      }
      await sleep(intervalMs);
    }
    return null;
  }

  /**
   * Klickt den Button einmalig und wartet auf den Shortlink (NUR für Produktseiten relevant).
   * @returns {boolean} True, wenn der Link bereit ist.
   */
  async function ensureStripeLinkReadyForCurrentProduct() {
    // Diese Funktion wird NUR aufgerufen, wenn isAmazonProductPath() == true
    const urlKey = location.href.split("#")[0];

    if (linkReadyForUrl.has(urlKey)) return true;

    // Button einmalig klicken
    if (!clickedOnceForUrl.has(urlKey)) {
      const btn = await waitForStripeButton();
      if (btn) {
        console.log("[Stripe] click button");
        btn.click();
        clickedOnceForUrl.add(urlKey);
      } else {
        console.warn("[Stripe] button not found (timeout)");
      }
    }

    // Warten auf befüllten, sichtbaren Link (egal ob geklickt oder bereits offen)
    const link = await waitForAffiliateLink();
    if (link) {
      console.log("[Stripe] link ready:", link.val);
      linkReadyForUrl.add(urlKey);
      return true;
    }

    console.warn("[Stripe] link not ready -> skip sending this round");
    return false; // Wichtig: NICHT senden
  }

  // --- Pipeline (Module-Ausführung) ---

  async function runPipeline() {
    try {
      // Dynamische Imports für bessere Kapselung und geringere Ladezeit
      // HINWEIS: Ersetze dies durch deine tatsächliche Importlogik
      const loadEnabledModules = async () => []; // Platzhalter
      const makeDetachedDocumentFromPage = () => document; // Platzhalter

      let modules = await loadEnabledModules();
      let doc = makeDetachedDocumentFromPage();
      const ctx = { stats: {}, output: null };

      for (const mod of modules) {
        try {
          const res = await mod.run(doc, ctx);
          doc = res.doc || doc;
        } catch (e) {
          console.warn(`[Module error] ${mod.meta?.id || "unknown"}`, e);
          ctx.stats[`error_${mod.meta?.id || "unknown"}`] = String(e);
        }
      }
      return { stats: ctx.stats, output: ctx.output };
    } catch (e) {
      console.warn("[content] pipeline failed:", e);
      // Den Fehler werfen, um die aufrufende Funktion zu benachrichtigen
      throw e;
    }
  }

  // --- Haupt-Sende-Logik ---

  /**
   * Sendet das aktuelle HTML an den Background-Skript.
   * Schließt den Tab NUR, wenn der Typ "PRODUCT_HTML" ist und die Übertragung erfolgreich war.
   */
  function sendHtml(type, href, html) {
    const payload = { url: href, html };
    chrome.runtime.sendMessage({ type, payload }, (resp) => {
      // Fehlerbehandlung für Sende-Antworten
      if (chrome.runtime.lastError) {
        console.error(`[send] ${type} failed (runtime error):`, chrome.runtime.lastError.message);
      } else {
        console.log(`[send] ${type} resp:`, resp);

        // Tab nur schließen, wenn es eine Produktseite ist UND der Sendevorgang erfolgreich war
        if (type === "PRODUCT_HTML" && resp?.ok === true && resp?.id) {
          console.log(`[send] PRODUCT_HTML successful, closing tab...`);
          // Sendet Nachricht an Background, um den aktuellen Tab zu schließen
          chrome.runtime.sendMessage({ type: "CLOSE_CURRENT_TAB" });
        }
      }
    });
  }

  /** Hauptfunktion, die die Logik für eine Amazon-Seite ausführt. */
  async function safeRun(reason = "auto") {
    const href = location.href;

    // Nur ausführen, wenn es eine relevante Amazon-Seite ist oder der Trigger gesetzt ist
    if (!(isAmazonTargetPage() || hasOpenerTrigger())) {
      stopAutoScroll();
      return;
    }

    // Auto-Scroll starten/stoppen
    if (isAmazonDealsPath()) startAutoScroll();
    else stopAutoScroll();

    const now = Date.now();

    // Deduplizierung: maximal alle 60s pro URL (außer beim ersten Mal oder bei Trigger)
    if (href === lastRunUrl && now - lastRunAt < MIN_RUN_INTERVAL_MS) return;

    // Kurze Wartezeit, bis initiale Inhalte geladen sind (verbessert die Robustheit)
    await sleep(500);
    if ("requestIdleCallback" in window) {
      await new Promise((r) => requestIdleCallback(r, { timeout: 1500 }));
    }

    console.log("[AutoRun] runPipeline ->", reason, href);

    try {
      // 1. Opener-Trigger-Shortcut (SENDEN OHNE GATE)
      if (hasOpenerTrigger(href)) {
        const key = href.split("#")[0];
        if (!triggerConsumedForUrl.has(key)) {
          // NEU: Nur auf den SiteStripe-Link warten, wenn es eine Amazon Produktseite ist.
          if (isAmazonProductPath()) {
            const ok = await ensureStripeLinkReadyForCurrentProduct();
             // Wenn der Link nicht bereit ist, breche den Sendevorgang ab.
            if (!ok) {
              console.warn("[Opener-Trigger] Link not ready, skipping send.");
              return; 
            }
          }
          
          await runPipeline();
          sendHtml("PRODUCT_HTML", href, document.documentElement.outerHTML);
          triggerConsumedForUrl.add(key);
          lastRunAt = Date.now();
          lastRunUrl = href;
        }
        return;
      }

      // 2. Produktseiten-Gate (WARTEN AUF SHORTLINK)
      // WARTE NUR auf Shortlink, wenn es eine Produktseite ist und KEIN Opener-Trigger vorliegt.
      if (isAmazonProductPath()) {
        const ok = await ensureStripeLinkReadyForCurrentProduct();
        if (!ok) {
          // Link nicht bereit -> NICHT senden – nächste Runde abwarten
          return;
        }
        // Warten erfolgreich -> mit Schritt 3/4 fortfahren und PRODUCT_HTML senden
      }
      
      // Deals-Seiten (isAmazonDealsPath()) umgehen die Wartezeit und senden direkt.

      // 3. Modul-Pipeline ausführen (z.B. für Stats/Normalisierung)
      await runPipeline();

      // 4. HTML senden (JETZT SICHER)
      // Wähle den korrekten Nachrichtentyp: Nur Produktseiten erhalten den "Schließen"-Mechanismus.
      const html = document.documentElement.outerHTML;
      const messageType = isAmazonProductPath() ? "PRODUCT_HTML" : "PARSED_HTML";
      sendHtml(messageType, href, html);

      lastRunAt = Date.now();
      lastRunUrl = href;
    } catch (e) {
      console.warn("[AutoRun] run failed:", e);
    }
  }

  // --- Start-Logik (Events) ---

  // 1) Direkt beim Laden
  if (isAmazonTargetPage() || hasOpenerTrigger()) safeRun("initial");

  // 2) Sanfter Dauerbetrieb (nur wenn Tab sichtbar)
  setInterval(() => {
    if (!document.hidden) safeRun("interval");
  }, Math.max(10_000, Math.floor(MIN_RUN_INTERVAL_MS / 2)));

  // 3) SPA/History-Änderungen (robustes Hooking)
  (function hookHistory() {
    const _ps = history.pushState,
      _rs = history.replaceState;
    history.pushState = function (...a) {
      const r = _ps.apply(this, a);
      queueMicrotask(() => safeRun("pushState"));
      return r;
    };
    history.replaceState = function (...a) {
      const r = _rs.apply(this, a);
      queueMicrotask(() => safeRun("replaceState"));
      return r;
    };
    window.addEventListener("popstate", () => safeRun("popstate"));
  })();

  // 4) DOM-Fallback (MutationObserver)
  const mo = new MutationObserver(() => {
    // Vermeidet unnötige Läufe bei kleinen DOM-Änderungen auf der gleichen URL
    if ((isAmazonTargetPage() || hasOpenerTrigger()) && location.href !== lastRunUrl) safeRun("mutation");
  });
  // Beobachten des gesamten Dokuments auf tiefgreifende Änderungen
  mo.observe(document.documentElement, { childList: true, subtree: true });

  // 5) Bei Tab-Rückkehr erneut prüfen
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) safeRun("visibility");
  });

  // --- Message Listener (Externe Steuerung) ---

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type === "START_AUTOSCROLL") {
      startAutoScroll();
      sendResponse({ ok: true });
    } else if (msg?.type === "STOP_AUTOSCROLL") {
      stopAutoScroll();
      sendResponse({ ok: true });
    } else if (msg?.type === "RUN_SANITIZER") {
      // Die Pipeline als "Sanitizer" ausführen und Ergebnis zurücksenden
      runPipeline()
        .then((result) => sendResponse({ ok: true, result }))
        .catch((err) => sendResponse({ ok: false, error: String(err) }));
      return true; // Asynchrone Antwort
    }
  });
})();